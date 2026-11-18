"""Next Prompt — AI-generated follow-up suggestions for Hermes.

After each completed turn the plugin asks the host LLM for one short
follow-up the user might send next and keeps it per session until the user
uses it, dismisses it, or sends their own next message (``pre_llm_call``).
Suggestions live in ``store.py``'s SQLite file under the profile's
plugin-data dir: the desktop half reads them through
``dashboard/plugin_api.py``, which may run in another process, and they
survive screen switches and backend restarts.
"""

from collections import OrderedDict
import contextvars
import hashlib
import importlib.util
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_UI_PLATFORMS = frozenset({"", "desktop", "tui"})
_AUX_TASK = "next_prompt"

# ── state ────────────────────────────────────────────────────────────────

def _load_store():
    """``store.py``, loaded by path like ``plugin_api.py`` does, so both halves
    of one install share a single module object."""
    path = Path(__file__).resolve().with_name("store.py")
    name = "next_prompt_store_" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return module


store = _load_store()

# session_id → turn counter, bumped when the user sends a message. A
# generator launched for an older turn must not publish over a newer one.
# Per process is enough: a session's pre and post hooks run in the same one.
_turn_seq: Dict[str, int] = {}
_last_suggestion_time: Dict[str, float] = {}
_MAX_CACHED_TURNS = 50
_last_turns: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
_lock = threading.Lock()
_llm_task = None
_ctx = None
_SYNTHETIC_USER_PREFIXES = (
    "[important: background process ",
    "[background process ",
    "[system: ",
    "[async delegation complete",
    "[async batch complete",
    "[your active task list was preserved across context compression]",
)


def _get_settings() -> Dict[str, Any]:
    defaults = {"enabled": True, "max_context_messages": 4, "cooldown_seconds": 3}
    try:
        raw = (_ctx.settings if _ctx is not None else None) or {}
    except Exception:
        raw = {}
    return {k: raw.get(k, v) for k, v in defaults.items()}


# ── persistence ──────────────────────────────────────────────────────────

def _import_legacy_store(ctx) -> None:
    """0.3.0 kept suggestions in ``suggestions.json`` under ``ctx.state.data_dir``,
    a path the REST half cannot derive. Move what is still pending into the
    store once, then set the file aside so a dismissed one never comes back."""
    try:
        legacy = Path(ctx.state.data_dir) / "suggestions.json"
    except Exception:
        return
    if not legacy.is_file():
        return
    try:
        entries = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("next-prompt: ignoring unreadable %s: %s", legacy, exc)
        entries = {}
    imported = store.import_legacy(entries) if isinstance(entries, dict) else 0
    try:
        legacy.replace(legacy.with_name("suggestions.json.migrated"))
    except OSError as exc:
        logger.warning("next-prompt: could not set %s aside: %s", legacy, exc)
        return
    logger.info("next-prompt: moved %d saved suggestion(s) to %s", imported, store.DB_FILE)


def clear_suggestion(session_id: str) -> None:
    """Drop a session's suggestion and failure note (used, dismissed, or superseded)."""
    store.clear(session_id)


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


# ── suggestion generation ────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You predict the exact next message a user will send to their AI agent. It is \
shown as a one-click suggestion under the chat input: clicking it only puts \
the text in the input box, where the user can edit it before sending. A \
suggestion never sends anything, so a good guess saves the user from typing \
it and a wrong one costs nothing.

If the agent's last message asks the user something (a question, a choice, \
a yes/no offer, a request to try something and report back), write the \
user's most likely reply, in their own words, committed to ONE answer: pick \
the option the conversation favours or the agent recommends, answer yes to a \
proposal that matches what the user asked for, and include the requested \
detail when the conversation contains it. When the agent asked them to try \
something and report back, confirm they did it and give the most likely \
outcome; the user corrects it if it went differently. When the agent asked \
for several things (to confirm a step and to decide something), cover each \
briefly, the confirmation first.

Otherwise suggest the natural next step: verify or test what was just done, \
apply it somewhere related, drill into a detail the agent surfaced, or move \
to the next pending item the agent mentioned.

Answer exactly NULL when:
- the exchange is closed socially (thanks, goodbye) with nothing left to act \
on; or
- the reply is mostly a detail only the user knows and the conversation does \
not contain (an email address, a password, a name): a guess would be wrong \
and a blank saves nothing.

Writing rules:
- Write in the language of the user's last message, whatever the language \
of the topic, the names in it, or these instructions.
- One short message under 80 characters, phrased the way the user writes to \
the agent: a reply to the agent's question, or a request (usually imperative).
- Ready to send as is: no brackets, blanks, placeholders or lists of \
alternatives ("X / Y", "X or Y?"). Deleting the options that don't apply is \
as much work as typing the message.
- Specific to this conversation: use its names, files and numbers. Never \
generic ("Continue", "Tell me more", "Sounds good", "Thanks, looks good"): \
when the work is done, suggest how to verify, ship or extend it.
- Never ask for something the agent already did or already answered.
- Never include a password or other secret.
- Output only the suggestion, or NULL. No quotes, labels or explanation.

Examples (the language always follows the user, never the example):
User: "Fix the parser crash" Agent: "Fixed the null check in parser.py; all 42 tests pass." -> Commit the parser.py fix
User: "Compare our two hosting plans" Agent: "Plan B is 30% cheaper but has half the bandwidth." -> How much bandwidth did we use last month?
User: "Resume las ventas de agosto" Agent: "312 pedidos; Monterrey cayó 18%." -> ¿Por qué cayeron las ventas en Monterrey?
User: "Deploy the site" Agent: "Build is ready. Staging first, or straight to production? I'd go staging." -> Deploy to staging first
User: "Email the report to the team" Agent: "Want me to attach the Q3 chart too?" -> Yes, attach the Q3 chart
User: "The export button does nothing" Agent: "Fixed. Restart the app and tell me if it works." -> Restarted it, and the export button works now
User: "Send the invoice" Agent: "I don't have the client's email. Which address?" -> NULL
User: "Perfect, thanks!" Agent: "Anytime!" -> NULL
"""

_LANGUAGE_REMINDER = (
    "Write the suggestion in the same language as the user's last message, even "
    "when the topic, places or names belong to another language: \"{sample}\""
)
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
_MAX_SUGGESTION_CHARS = 220
_NULL_ANSWERS = frozenset({"NULL", "NONE", "N/A", "NO SUGGESTION"})
_LABELS = ("suggestion:", "suggested prompt:", "next prompt:", "prompt:", "sugerencia:", "user:", "[user]:")
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "«": "»", "`": "`"}
# "[result]", "[works / fails]", "<email>", "___": the user would have to edit
# it before sending, which is as much work as typing it. Inline code spans that
# look like code (`arr[0]`, `List<str>`, `foo.bar()`) are stripped first so
# technical syntax survives while backtick-wrapped placeholders (`<email>`, `[name]`)
# are still caught.
_INLINE_CODE = re.compile(r"`[^`]+`")
_CODE_SPAN_HINT = re.compile(r"[.(]|::|->|[A-Za-z0-9](?:\[|<)")
_FILL_IN = re.compile(r"\[[^\]]*\]|<[^<>]+>|_{3,}")


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


def _strip_code_spans(text: str) -> str:
    return _INLINE_CODE.sub(lambda m: "" if _CODE_SPAN_HINT.search(m.group(0)[1:-1]) else m.group(0), text)


def _clean_with_reason(text: str) -> tuple[str, str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return "", "empty"
    text = lines[0]
    lowered = text.lower()
    for label in _LABELS:
        if lowered.startswith(label):
            text = text[len(label):].strip()
            break
    candidate = text
    if len(candidate) >= 2 and _QUOTE_PAIRS.get(candidate[0]) == candidate[-1]:
        candidate = candidate[1:-1].strip()
    if _FILL_IN.search(_strip_code_spans(candidate)):
        return "", "fill_in_placeholder"  # a blank to fill in or options to delete saves no typing
    text = text.strip("*_ ").strip()
    if len(text) >= 2 and _QUOTE_PAIRS.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    if not text:
        return "", "empty"
    if text.upper().rstrip(".!") in _NULL_ANSWERS or text.upper().startswith("NULL"):
        return "", "null_answer"
    if len(text) < 3:
        return "", "too_short"
    if len(text) > _MAX_SUGGESTION_CHARS:
        return "", "too_long"
    return text, ""


def _clean(text: str) -> str:
    cleaned, _ = _clean_with_reason(text)
    return cleaned


def _generate_from_context(session_id: str, seq: int, context: str, last_user: str) -> None:
    if not context.strip():
        return
    try:
        llm = getattr(_ctx, "llm", None)
        if llm is None:
            return
        kwargs = {"task": _llm_task} if _llm_task else {}
        result = llm.complete(
            messages=_prompt_messages(context, last_user),
            max_tokens=100,
            temperature=0.3,
            purpose="next-prompt suggestion",
            **kwargs,
        )
        text, drop_reason = _clean_with_reason(getattr(result, "text", ""))
    except Exception as exc:
        logger.warning("next-prompt: suggestion generation failed for %s", session_id, exc_info=True)
        with _lock:
            if _turn_seq.get(session_id, 0) == seq:
                store.put_failure(session_id, _classify_error(exc))
        return

    # Under the lock, so the user's next message (pre_llm_call) cannot slip
    # between the staleness check and the write.
    with _lock:
        if _turn_seq.get(session_id, 0) != seq:
            return  # the user already moved on; this suggestion is stale
        if not text:
            store.clear(session_id)
            logger.info("next-prompt: dropped suggestion for %s (reason=%s)", session_id, drop_reason)
            return
        suggestion = store.put_suggestion(session_id, text)
        if suggestion is None:
            return
        _last_suggestion_time[session_id] = suggestion["timestamp"]
    logger.info("next-prompt: stored suggestion for session %s", session_id)


def _generate_suggestion(session_id: str, seq: int, messages: list, settings: dict) -> None:
    context = _build_context(messages, settings["max_context_messages"])
    _generate_from_context(session_id, seq, context, _last_user_text(messages))


def _cache_turn(session_id: str, context: str, last_user: str) -> None:
    if not context.strip():
        return
    _last_turns[session_id] = (context, last_user)
    _last_turns.move_to_end(session_id)
    while len(_last_turns) > _MAX_CACHED_TURNS:
        _last_turns.popitem(last=False)


def regenerate(session_id: str) -> bool:
    """Manually trigger suggestion generation using the last cached turn."""
    settings = _get_settings()
    if not session_id or not settings["enabled"]:
        return False
    with _lock:
        cached = _last_turns.get(session_id)
        if cached is None:
            return False
        _last_turns.move_to_end(session_id)
        context, last_user = cached
        seq = _turn_seq.get(session_id, 0) + 1
        _turn_seq[session_id] = seq
    _spawn(_generate_from_context, session_id, seq, context, last_user)
    return True


# ── hooks ────────────────────────────────────────────────────────────────

def _is_synthetic_turn(kwargs: dict) -> bool:
    user_msg = kwargs.get("user_message")
    return isinstance(user_msg, str) and user_msg.lstrip().lower().startswith(_SYNTHETIC_USER_PREFIXES)


def _on_pre_llm_call(**kwargs) -> None:
    """The user sent their own next message: any pending suggestion is moot."""
    session_id = kwargs.get("session_id") or ""
    if not session_id or _is_synthetic_turn(kwargs):
        return
    with _lock:
        _turn_seq[session_id] = _turn_seq.get(session_id, 0) + 1
        store.clear(session_id)


def _on_post_llm_call(**kwargs) -> None:
    """Fired once per completed turn (agent/turn_finalizer.py)."""
    settings = _get_settings()
    if not settings["enabled"]:
        return
    session_id = kwargs.get("session_id") or ""
    messages = kwargs.get("conversation_history") or []
    if not session_id or len(messages) < 2:
        return
    # Only surfaces that render the pill; messaging/synthetic turns would
    # spend an LLM call on a suggestion nobody can see.
    if _is_synthetic_turn(kwargs) or (kwargs.get("platform") or "") not in _UI_PLATFORMS:
        return
    context = _build_context(messages, settings["max_context_messages"])
    last_user = _last_user_text(messages)
    with _lock:
        _cache_turn(session_id, context, last_user)
        seq = _turn_seq.get(session_id, 0)
    if time.time() - _last_suggestion_time.get(session_id, 0) < settings["cooldown_seconds"]:
        return
    # The worker must inherit this profile's secret/home scope, or ctx.llm
    # fails with UnscopedSecretError.
    _spawn(_generate_from_context, session_id, seq, context, last_user)


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
    global _ctx, _llm_task
    _ctx = ctx
    _import_legacy_store(ctx)  # register() runs in the owning profile's home scope
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
    store.set_regenerator(regenerate)
    logger.info("next-prompt: registered (%d saved suggestion(s) in %s)", store.count(), store.DB_FILE)
