"""Dashboard API backend for the next-prompt plugin.

Mounted by the backend at ``/api/plugins/next-prompt/``. The desktop half
reaches it through ``ctx.rest`` (profile-routed, authenticated).

The hook side (``__init__.py``) may be imported more than once in one
process — once per served profile home (``hermes_plugins.next_prompt`` and
``hermes_plugins.next_prompt__home_<hash>``). Each instance keeps its own
``_suggestions`` dict, so the endpoints look across all of them.
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, Query

router = APIRouter()


def _instances():
    for name, mod in list(sys.modules.items()):
        if mod is None or ("next_prompt" not in name and "next-prompt" not in name):
            continue
        if hasattr(mod, "_suggestions") and hasattr(mod, "_suggestions_lock"):
            yield mod


@router.get("/suggestion")
async def get_suggestion(session_id: str = Query("")):
    """The session's pending suggestion, or — when the last attempt failed and
    nothing newer exists — the failure kind so the UI can say why."""
    if not session_id:
        return {"suggestion": None, "error": None}
    best = None
    failure = None
    for mod in _instances():
        with mod._suggestions_lock:
            found = mod._suggestions.get(session_id)
            failed = getattr(mod, "_errors", {}).get(session_id)
        if found and (best is None or found.get("timestamp", 0) > best.get("timestamp", 0)):
            best = dict(found)
        if failed and (failure is None or failed.get("timestamp", 0) > failure.get("timestamp", 0)):
            failure = dict(failed)
    if best and failure and best.get("timestamp", 0) >= failure.get("timestamp", 0):
        failure = None
    return {"suggestion": best, "error": failure}


@router.post("/dismiss")
async def dismiss_suggestion(session_id: str = Query("")):
    if session_id:
        for mod in _instances():
            clear = getattr(mod, "clear_suggestion", None)
            if callable(clear):
                clear(session_id)
            else:
                with mod._suggestions_lock:
                    mod._suggestions.pop(session_id, None)
                    getattr(mod, "_errors", {}).pop(session_id, None)
    return {"ok": True}
