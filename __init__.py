"""Next Prompt — AI-generated follow-up suggestions for Hermes.

After each completed turn the plugin asks the host LLM for one short
follow-up the user might send next and keeps it per session until the user
uses it, dismisses it, or sends their own next message (``pre_llm_call``).
Suggestions are saved under the profile's plugin-data dir, so switching
screens or restarting the backend does not lose them. The desktop half reads
them through ``dashboard/plugin_api.py``.
"""

import contextvars
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_TTL_SECONDS = 7 * 24 * 3600
_MAX_STORED = 200
_UI_PLATFORMS = frozenset({"", "desktop", "tui"})
_AUX_TASK = "next_prompt"

# ── state ────────────────────────────────────────────────────────────────

# session_id → {"text", "session_id", "timestamp"}. Read by plugin_api.py.
_suggestions: Dict[str, Dict[str, Any]] = {}
_suggestions_lock = threading.Lock()
# session_id → {"kind", "session_id", "timestamp"} for the last turn whose
# suggestion could not be generated. Memory only (a restart may be the fix);
# guarded by _suggestions_lock. Read by plugin_api.py.
_errors: Dict[str, Dict[str, Any]] = {}
# session_id → turn counter, bumped when the user sends a message. A
# generator launched for an older turn must not publish over a newer one.
_turn_seq: Dict[str, int] = {}
_last_suggestion_time: Dict[str, float] = {}
_store_path: Optional[Path] = None
_llm_task: Optional[str] = None
_ctx = None


def _get_settings() -> Dict[str, Any]:
    defaults = {"enabled": True, "max_context_messages": 4, "cooldown_seconds": 3}
    try:
        raw = (_ctx.settings if _ctx is not None else None) or {}
    except Exception:
        raw = {}
    return {k: raw.get(k, v) for k, v in defaults.items()}


# ── persistence ──────────────────────────────────────────────────────────

def _resolve_store_path(ctx) -> Optional[Path]:
    """Called from register(), which runs inside the owning profile's home scope."""
    try:
        base = Path(ctx.state.data_dir)
    except Exception:  # older hosts without ctx.state
        try:
            from hermes_constants import get_hermes_home
            base = get_hermes_home() / "plugin-data" / "next-prompt"
        except Exception:
            return None
    return base / "suggestions.json"


def _live(entries: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    now = time.time()
    kept = [
        (sid, s) for sid, s in entries.items()
        if isinstance(s, dict) and s.get("text") and now - float(s.get("timestamp") or 0) < _TTL_SECONDS
    ]
    kept.sort(key=lambda kv: kv[1].get("timestamp", 0), reverse=True)
    return dict(kept[:_MAX_STORED])


def _read_store() -> Dict[str, Any]:
    if _store_path is None:
        return {}
    try:
        data = json.loads(_store_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("next-prompt: ignoring unreadable %s: %s", _store_path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _persist(session_id: str, suggestion: Optional[Dict[str, Any]]) -> None:
    """Merge one change into the file (another process serving the same
    profile may write it too). Caller holds ``_suggestions_lock``."""
    if _store_path is None:
        return
    data = _read_store()
    if suggestion is None:
        if session_id not in data:
            return
        data.pop(session_id)
    else:
        data[session_id] = suggestion
    try:
        _store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _store_path.with_name(f"{_store_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(_live(data), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _store_path)
    except OSError as exc:
        logger.warning("next-prompt: could not save %s: %s", _store_path, exc)


def clear_suggestion(session_id: str) -> None:
    """Drop a session's suggestion and failure note (used, dismissed, or superseded)."""
    with _suggestions_lock:
        _suggestions.pop(session_id, None)
        _errors.pop(session_id, None)
        _persist(session_id, None)


def _classify_error(exc: BaseException) -> str:
    """Coarse failure kind for the UI. Never exposes the error text itself."""
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__.lower()
    if status in (401, 403) or "authentication" in name or "permissiondenied" in name:
        return "auth"
    if status == 429 or "ratelimit" in name:
        return "rate_limit"
    if "timeout" in name:
        return "timeout"
    return "other"


def _record_error(session_id: str, kind: str) -> None:
    """Caller holds ``_suggestions_lock``."""
    _errors[session_id] = {"kind": kind, "session_id": session_id, "timestamp": time.time()}
    if len(_errors) > _MAX_STORED:
        oldest = min(_errors, key=lambda sid: _errors[sid]["timestamp"])
        _errors.pop(oldest, None)


# ── suggestion generation ────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You predict the next message a user will send to their AI agent. It is shown \
as a one-click suggestion under the chat input: clicking it puts the text in \
the input box, and the user sends it.

Suggest a next step when there is one. Most turns have one, even finished \
tasks: verify or test what was just done, apply it somewhere related, drill \
into a detail the agent surfaced, or move to the next pending item the agent \
mentioned.

Answer exactly NULL when:
- the agent's last message asks the user a question, offers a choice, or \
needs a decision or information only the user has. Never answer it for the \
user, even when one answer looks likely; or
- the exchange is closed socially (thanks, goodbye) with nothing to act on.

Writing rules:
- Write in the language of the user's last message, whatever the language \
of the topic, the names in it, or these instructions.
- One short request under 80 characters, phrased the way the user writes to \
the agent (usually imperative).
- The user has done nothing since the agent's last message. Never report \
actions or results ("I restarted it", "it works now"); ask the agent for \
the next step instead.
- Specific to this conversation: use its names, files and numbers. Never \
generic ("Continue", "Tell me more").
- Never ask for something the agent already did or already answered.
- Output only the suggestion, or NULL. No quotes, labels or explanation.

Examples (the language always follows the user, never the example):
User: "Fix the parser crash" Agent: "Fixed the null check in parser.py; all 42 tests pass." -> Commit the parser.py fix
User: "Resume las ventas de agosto" Agent: "312 pedidos; Monterrey cayó 18%." -> ¿Por qué cayeron las ventas en Monterrey?
User: "Fix the login bug" Agent: "Done. Restart the app to try it." -> Add a test that covers the login bug
Agent: "Should I deploy to staging or straight to production?" -> NULL
Agent: "Want me to email it to all 40 clients?" -> NULL
User: "Perfect, thanks!" Agent: "Anytime!" -> NULL
"""

_LANGUAGE_REMINDER = "Write the suggestion in the same language as the user's last message: \"{sample}\""
_LANGUAGE_SAMPLE_CHARS = 200


def _last_user_text(messages: list) -> str:
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = _text_of(msg)
            if text:
                return text
    return ""


def _prompt_messages(context: str, last_user: str = "") -> list:
    """System prompt plus the conversation. The language rule is repeated
    last, quoting the user's own words: models otherwise drift to the topic's
    language (an English question about Mexico got Spanish suggestions)."""
    sample = " ".join(last_user.split())[:_LANGUAGE_SAMPLE_CHARS]
    tail = _LANGUAGE_REMINDER.format(sample=sample) if sample else "Write in the user's language."
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Recent conversation:\n\n{context}\n\n{tail}"},
    ]

# The newest messages are the ones that matter; a long reply's conclusion and
# closing question sit at its end, so keep more of the tail than the head.
_HEAD_CHARS = 300
_TAIL_CHARS = 900
_MAX_SUGGESTION_CHARS = 160
_NULL_ANSWERS = frozenset({"NULL", "NONE", "N/A", "NO SUGGESTION"})
_LABELS = ("suggestion:", "suggested prompt:", "next prompt:", "prompt:", "sugerencia:", "user:", "[user]:")
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "«": "»", "`": "`"}


def _text_of(msg: Dict[str, Any]) -> str:
    content = msg.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return content.strip() if isinstance(content, str) else ""


def _clip(text: str) -> str:
    if len(text) <= _HEAD_CHARS + _TAIL_CHARS:
        return text
    return f"{text[:_HEAD_CHARS].rstrip()} … {text[-_TAIL_CHARS:].lstrip()}"


def _build_context(messages: list, max_messages: int) -> str:
    """The last few user requests and the agent's FINAL reply to each.

    ``conversation_history`` is dominated by tool traffic — tool results and
    the agent's narration between tool calls. Those are skipped: a turn is the
    user's message plus the last assistant text before the next user message.
    Without this the user's request often fell outside the window entirely.
    """
    exchanges: list = []  # [user_text, final_assistant_text]
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        text = _text_of(msg)
        if role == "user" and text:
            exchanges.append([text, ""])
        elif role == "assistant" and text and exchanges:
            exchanges[-1][1] = text  # later assistant text supersedes narration
    keep = max(1, int(max_messages) // 2)
    parts = []
    for user_text, reply in exchanges[-keep:]:
        parts.append(f"[User]: {_clip(user_text)}")
        if reply:
            parts.append(f"[Agent]: {_clip(reply)}")
    return "\n\n".join(parts)


def _clean(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    text = lines[0]
    lowered = text.lower()
    for label in _LABELS:
        if lowered.startswith(label):
            text = text[len(label):].strip()
            break
    text = text.strip("*_ ").strip()
    if len(text) >= 2 and _QUOTE_PAIRS.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    if text.upper().rstrip(".!") in _NULL_ANSWERS or text.upper().startswith("NULL"):
        return ""
    if len(text) < 3 or len(text) > _MAX_SUGGESTION_CHARS:
        return ""
    return text


def _generate_suggestion(session_id: str, seq: int, messages: list, settings: dict) -> None:
    context = _build_context(messages, settings["max_context_messages"])
    if not context.strip():
        return
    try:
        llm = getattr(_ctx, "llm", None)
        if llm is None:
            return
        kwargs = {"task": _llm_task} if _llm_task else {}
        result = llm.complete(
            messages=_prompt_messages(context, _last_user_text(messages)),
            max_tokens=100,
            temperature=0.3,
            purpose="next-prompt suggestion",
            **kwargs,
        )
        text = _clean(getattr(result, "text", ""))
    except Exception as exc:
        logger.warning("next-prompt: suggestion generation failed for %s", session_id, exc_info=True)
        with _suggestions_lock:
            if _turn_seq.get(session_id, 0) == seq:
                _record_error(session_id, _classify_error(exc))
        return

    with _suggestions_lock:
        if _turn_seq.get(session_id, 0) != seq:
            return  # the user already moved on; this suggestion is stale
        _errors.pop(session_id, None)
        if not text:
            _suggestions.pop(session_id, None)
            _persist(session_id, None)
            return
        suggestion = {"text": text, "session_id": session_id, "timestamp": time.time()}
        _suggestions[session_id] = suggestion
        _last_suggestion_time[session_id] = suggestion["timestamp"]
        _persist(session_id, suggestion)
    logger.info("next-prompt: stored suggestion for session %s", session_id)


# ── hooks ────────────────────────────────────────────────────────────────

def _on_pre_llm_call(**kwargs) -> None:
    """The user sent their own next message: any pending suggestion is moot."""
    session_id = kwargs.get("session_id") or ""
    if not session_id:
        return
    with _suggestions_lock:
        _turn_seq[session_id] = _turn_seq.get(session_id, 0) + 1
    clear_suggestion(session_id)


def _on_post_llm_call(**kwargs) -> None:
    """Fired once per completed turn (agent/turn_finalizer.py)."""
    settings = _get_settings()
    if not settings["enabled"]:
        return
    session_id = kwargs.get("session_id") or ""
    messages = kwargs.get("conversation_history") or []
    if not session_id or len(messages) < 2:
        return
    # Only surfaces that render the pill; messaging turns (Telegram, …) would
    # spend an LLM call on a suggestion nobody can see.
    if (kwargs.get("platform") or "") not in _UI_PLATFORMS:
        return
    if time.time() - _last_suggestion_time.get(session_id, 0) < settings["cooldown_seconds"]:
        return
    with _suggestions_lock:
        seq = _turn_seq.get(session_id, 0)
    # The worker must inherit this profile's secret/home scope, or ctx.llm
    # fails with UnscopedSecretError.
    _spawn(_generate_suggestion, session_id, seq, list(messages), settings)


def _spawn(target, *args) -> None:
    try:
        from agent.memory_provider import spawn_context_thread
    except ImportError:  # older hosts: same thing by hand
        ctx_copy = contextvars.copy_context()
        threading.Thread(target=ctx_copy.run, args=(target, *args), daemon=True).start()
        return
    spawn_context_thread(target, name="next-prompt", args=args).start()


# ── registration ─────────────────────────────────────────────────────────

def register(ctx):
    global _ctx, _store_path, _llm_task
    _ctx = ctx
    _store_path = _resolve_store_path(ctx)
    with _suggestions_lock:
        _suggestions.update(_live(_read_store()))
    # Own auxiliary slot: runs on the main model by default, but the user can
    # point `auxiliary.next_prompt` at a cheaper/faster model.
    try:
        ctx.register_auxiliary_task(
            _AUX_TASK,
            display_name="Next Prompt",
            description="Suggests the follow-up prompt shown under the composer.",
        )
        _llm_task = _AUX_TASK
    except (AttributeError, ValueError):
        _llm_task = None  # older host: plain main-model call
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    logger.info("next-prompt: registered (%d saved suggestion(s))", len(_suggestions))
