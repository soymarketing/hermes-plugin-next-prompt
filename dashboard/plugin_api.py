"""Dashboard API backend for the next-prompt plugin.

Mounted by the backend at ``/api/plugins/next-prompt/``. The desktop half
reaches it through ``ctx.rest`` (profile-routed, authenticated).

This router and the hooks in ``__init__.py`` can live in different
processes: a gateway serving several profiles runs a secondary profile's
plugin API in that profile's plugin host, while the profile's turns (and
their hooks) run in the gateway. So the endpoints read and write
``store.py``'s SQLite file, never the hook module's memory. The host imports
this file by path with no package, so ``store.py`` is loaded by path too.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter()


def _store():
    path = Path(__file__).resolve().parents[1] / "store.py"
    name = "next_prompt_store_" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return module


# Plain ``def`` handlers: SQLite is blocking I/O, so FastAPI runs them in a
# worker thread instead of on the event loop.


@router.get("/suggestion")
def get_suggestion(session_id: str = Query("")):
    """The session's pending suggestion, or, when the last attempt failed,
    the failure kind so the UI can say why."""
    return _store().get(session_id)


@router.post("/dismiss")
def dismiss_suggestion(session_id: str = Query("")):
    if session_id:
        _store().clear(session_id)
    return {"ok": True}


def _plugin_modules():
    store_mod = _store()
    return [
        mod
        for mod in reversed(list(sys.modules.values()))
        if mod is not None and getattr(mod, "store", None) is store_mod and callable(getattr(mod, "regenerate", None))
    ]


@router.post("/generate")
def generate_suggestion(session_id: str = Query("")):
    if not session_id:
        return {"ok": False, "started": False}
    for mod in _plugin_modules():
        if mod.regenerate(session_id):
            return {"ok": True, "started": True}
    return {"ok": False, "started": False}
