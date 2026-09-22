"""Next Prompt — AI-generated follow-up suggestions for Hermes.

After each completed turn (no tool calls, no errors), this plugin uses the
host LLM to generate a concise follow-up prompt suggestion. The suggestion
is published to the desktop app via a REST endpoint that the desktop half
of this plugin polls.
"""

import json
import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── state ────────────────────────────────────────────────────────────────

# Per-session latest suggestion, keyed by session_id.
# Written by the background generator, read by the REST endpoint.
_suggestions: Dict[str, Dict[str, Any]] = {}
_suggestions_lock = threading.Lock()

# Timestamps of last suggestion per session to enforce cooldown.
_last_suggestion_time: Dict[str, float] = {}

# The ctx reference, set during register().
_ctx = None


def _get_settings() -> Dict[str, Any]:
    """Read plugin settings from config.yaml, with defaults."""
    defaults = {
        "enabled": True,
        "max_context_messages": 4,
        "cooldown_seconds": 3,
    }
    if _ctx is None:
        return defaults
    try:
        raw = _ctx.settings or {}
    except Exception:
        raw = {}
    return {k: raw.get(k, v) for k, v in defaults.items()}


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
    """Build a compact context string from the last N messages."""
    recent = messages[-max_messages:] if len(messages) > max_messages else messages
    parts = []
    for msg in recent:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multimodal: extract text parts only
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if not content or not content.strip():
            continue
        # Truncate long messages
        if len(content) > 500:
            content = content[:500] + "…"
        label = "User" if role == "user" else "Agent" if role == "assistant" else role
        parts.append(f"[{label}]: {content.strip()}")
    return "\n\n".join(parts)


def _has_tool_calls(response: Any) -> bool:
    """Check if the LLM response contains tool calls (turn not finished)."""
    if isinstance(response, dict):
        choices = response.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            if msg.get("tool_calls"):
                return True
    return False


def _generate_suggestion(session_id: str, messages: list, settings: dict) -> None:
    """Background task: call ctx.llm to generate a suggestion."""
    if _ctx is None or not _ctx.llm:
        return

    try:
        context = _build_context(messages, settings["max_context_messages"])
        if not context.strip():
            return

        prompt = f"Recent conversation:\n\n{context}"

        result = _ctx.llm.complete(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=100,
            temperature=0.7,
        )

        text = (result.text or "").strip()

        if not text or text.upper() == "NULL" or len(text) < 3:
            # No suggestion — clear any stale one
            with _suggestions_lock:
                _suggestions.pop(session_id, None)
            return

        # Remove surrounding quotes if the model added them
        if (text.startswith('"') and text.endswith('"')) or \
           (text.startswith("'") and text.endswith("'")):
            text = text[1:-1].strip()

        suggestion = {
            "text": text,
            "session_id": session_id,
            "timestamp": time.time(),
        }

        with _suggestions_lock:
            _suggestions[session_id] = suggestion
            _last_suggestion_time[session_id] = time.time()

        logger.debug("next-prompt: suggestion for %s: %s", session_id, text)

    except Exception:
        logger.debug("next-prompt: failed to generate suggestion", exc_info=True)


# ── hooks ────────────────────────────────────────────────────────────────

def _on_post_llm_call(*, session_id: str = "", **kwargs) -> None:
    """Fired after each LLM call. If the turn is finished (no tool calls),
    generate a suggestion in a background thread."""
    settings = _get_settings()
    if not settings["enabled"]:
        return

    if not session_id:
        return

    # Cooldown check
    cooldown = settings["cooldown_seconds"]
    last = _last_suggestion_time.get(session_id, 0)
    if time.time() - last < cooldown:
        return

    # We need the conversation history to build context.
    # The hook payload includes 'messages' when available.
    messages = kwargs.get("messages") or kwargs.get("conversation_history")
    if not messages or len(messages) < 2:
        return

    # Check the last assistant message — if it has tool calls, the turn
    # isn't finished yet, skip.
    last_msg = messages[-1] if messages else {}
    if last_msg.get("role") != "assistant":
        return
    if last_msg.get("tool_calls"):
        return

    # Fire and forget in a background thread
    t = threading.Thread(
        target=_generate_suggestion,
        args=(session_id, list(messages), settings),
        daemon=True,
    )
    t.start()


# ── REST API (for the desktop half) ──────────────────────────────────────

def _setup_api(app):
    """Mount REST endpoints on the dashboard FastAPI app."""
    from starlette.responses import JSONResponse

    @app.get("/api/plugins/next-prompt/suggestion")
    async def get_suggestion(session_id: str = ""):
        """Return the current suggestion for a session, if any."""
        if not session_id:
            return JSONResponse({"suggestion": None})
        with _suggestions_lock:
            suggestion = _suggestions.get(session_id)
        return JSONResponse({"suggestion": suggestion})

    @app.post("/api/plugins/next-prompt/dismiss")
    async def dismiss_suggestion(session_id: str = ""):
        """Clear the suggestion for a session (user dismissed it)."""
        if session_id:
            with _suggestions_lock:
                _suggestions.pop(session_id, None)
        return JSONResponse({"ok": True})


# ── registration ─────────────────────────────────────────────────────────

def register(ctx):
    """Plugin entry point — register hooks and expose the REST API."""
    global _ctx
    _ctx = ctx

    ctx.register_hook("post_llm_call", _on_post_llm_call)
    logger.info("next-prompt: registered post_llm_call hook")
