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
    """Drop a session's suggestion (used, dismissed, or superseded)."""
    with _suggestions_lock:
        _suggestions.pop(session_id, None)
        _persist(session_id, None)


# ── suggestion generation ────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a prompt-suggestion assistant. Given the last few messages of a \
conversation between a user and an AI agent, decide whether there is a \
natural, useful follow-up the user might want to send next.

Rules:
- If there IS a good follow-up, respond with ONLY the suggested prompt \
text (one sentence, imperative, concise — what the user would type).
- If the task is clearly finished, the agent asked a question the user \
must answer themselves, or there is no useful continuation, respond with \
exactly: NULL
- Match the language the user has been writing in.
- Never explain your reasoning. Output only the suggestion or NULL.
- Keep it under 80 characters.
"""


def _build_context(messages: list, max_messages: int) -> str:
    parts = []
    for msg in messages[-max_messages:]:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue
        if len(content) > 500:
            content = content[:500] + "…"
        label = "User" if role == "user" else "Agent" if role == "assistant" else role
        parts.append(f"[{label}]: {content.strip()}")
    return "\n\n".join(parts)


def _clean(text: str) -> str:
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return "" if text.upper() == "NULL" or len(text) < 3 else text


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
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Recent conversation:\n\n{context}"},
            ],
            max_tokens=100,
            temperature=0.7,
            purpose="next-prompt suggestion",
            **kwargs,
        )
        text = _clean(getattr(result, "text", ""))
    except Exception:
        logger.warning("next-prompt: suggestion generation failed for %s", session_id, exc_info=True)
        return

    with _suggestions_lock:
        if _turn_seq.get(session_id, 0) != seq:
            return  # the user already moved on; this suggestion is stale
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
