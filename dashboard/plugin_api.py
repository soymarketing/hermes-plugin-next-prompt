"""Dashboard API backend for next-prompt plugin.

Mounts REST endpoints the desktop plugin.js polls to fetch and dismiss
suggestions. Hermes loads this file because plugin.yaml declares
``api: dashboard/plugin_api.py``.
"""

from __future__ import annotations


def register_routes(app, **kwargs):
    """Called by the dashboard when the plugin is enabled."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    # Import the shared suggestion state from the plugin root.
    # Late import so the module is only loaded when the dashboard runs.
    def _state():
        from hermes_cli.plugins import get_plugin_module
        mod = get_plugin_module("next-prompt")
        if mod is None:
            # Fallback: import directly (works when installed as a directory plugin)
            try:
                import importlib
                mod = importlib.import_module("next-prompt")
            except ImportError:
                return None, None
        return getattr(mod, "_suggestions", None), getattr(mod, "_suggestions_lock", None)

    @app.get("/api/plugins/next-prompt/suggestion")
    async def get_suggestion(request: Request):
        session_id = request.query_params.get("session_id", "")
        suggestions, lock = _state()
        if not suggestions or not lock or not session_id:
            return JSONResponse({"suggestion": None})
        with lock:
            suggestion = suggestions.get(session_id)
        return JSONResponse({"suggestion": suggestion})

    @app.post("/api/plugins/next-prompt/dismiss")
    async def dismiss_suggestion(request: Request):
        session_id = request.query_params.get("session_id", "")
        suggestions, lock = _state()
        if suggestions and lock and session_id:
            with lock:
                suggestions.pop(session_id, None)
        return JSONResponse({"ok": True})
