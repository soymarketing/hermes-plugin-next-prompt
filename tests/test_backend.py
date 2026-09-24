"""Behaviour of the Python half (__init__.py + dashboard/plugin_api.py).

Run: python -m unittest discover -s tests -v
Needs the hermes-agent package importable (its venv, or `pip install -e`).
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY = [
    {"role": "user", "content": "hola"},
    {"role": "assistant", "content": "listo, hice X"},
]
_names = itertools.count()


def load_plugin():
    name = f"hermes_plugins.next_prompt__test{next(_names)}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_api():
    spec = importlib.util.spec_from_file_location("next_prompt_api_under_test", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeLLM:
    def __init__(self, text="Haz el deploy a producción", delay=0.0, error=None):
        self.text, self.delay, self.error = text, delay, error
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(text=self.text)


class FakeCtx:
    """The slice of PluginContext the plugin touches."""

    def __init__(self, data_dir, llm, settings=None, aux_tasks=True):
        self.hooks: dict = {}
        self.llm = llm
        self.settings = settings or {}
        self.state = types.SimpleNamespace(data_dir=data_dir)
        self.aux_tasks: list = []
        if aux_tasks:
            self.register_auxiliary_task = lambda key, **kw: self.aux_tasks.append((key, kw))

    def register_hook(self, name, fn):
        self.hooks[name] = fn


def wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class PluginTestCase(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="next-prompt-test-"))
        self.addCleanup(shutil.rmtree, self.data_dir, ignore_errors=True)
        self.modules = []
        self.addCleanup(self._unload)

    def _unload(self):
        # Let workers finish before the temp dir goes away.
        for thread in threading.enumerate():
            if thread.name.startswith("next-prompt") and thread is not threading.current_thread():
                thread.join(timeout=5)
        for module in self.modules:
            sys.modules.pop(module.__name__, None)

    def start(self, llm=None, **ctx_kwargs):
        module = load_plugin()
        self.modules.append(module)
        ctx = FakeCtx(self.data_dir, llm or FakeLLM(), **ctx_kwargs)
        module.register(ctx)
        return module, ctx

    def stored(self):
        path = self.data_dir / "suggestions.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    @staticmethod
    def has(module, session_id):
        with module._suggestions_lock:
            return session_id in module._suggestions

    @staticmethod
    def turn_done(ctx, session_id, platform="desktop", history=HISTORY, **extra):
        ctx.hooks["post_llm_call"](session_id=session_id, platform=platform, conversation_history=history, **extra)


class GenerationTests(PluginTestCase):
    def test_registers_both_hooks_and_its_aux_task(self):
        module, ctx = self.start()
        self.assertEqual(set(ctx.hooks), {"pre_llm_call", "post_llm_call"})
        self.assertEqual([key for key, _ in ctx.aux_tasks], ["next_prompt"])

    def test_generates_and_persists(self):
        module, ctx = self.start()
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.has(module, "s1")))
        self.assertEqual(self.stored()["s1"]["text"], "Haz el deploy a producción")

    def test_calls_the_llm_on_its_own_aux_task(self):
        llm = FakeLLM()
        module, ctx = self.start(llm)
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: llm.calls))
        call = llm.calls[0]
        self.assertEqual(call["task"], "next_prompt")
        self.assertTrue(call["purpose"])
        self.assertEqual(call["messages"][0]["role"], "system")

    def test_older_host_without_aux_tasks_uses_a_plain_call(self):
        llm = FakeLLM()
        module, ctx = self.start(llm, aux_tasks=False)
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: llm.calls))
        self.assertNotIn("task", llm.calls[0])

    def test_survives_a_backend_restart(self):
        module, ctx = self.start()
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.has(module, "s1")))
        restarted, _ = self.start(FakeLLM(text="otra"))
        self.assertEqual(restarted._suggestions["s1"]["text"], "Haz el deploy a producción")

    def test_own_message_clears_memory_and_disk(self):
        module, ctx = self.start()
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.has(module, "s1")))
        ctx.hooks["pre_llm_call"](session_id="s1", platform="desktop", user_message="otra cosa")
        self.assertFalse(self.has(module, "s1"))
        self.assertNotIn("s1", self.stored())

    def test_late_answer_for_an_old_turn_is_discarded(self):
        llm = FakeLLM(delay=0.4)
        module, ctx = self.start(llm)
        self.turn_done(ctx, "s2")
        time.sleep(0.05)
        ctx.hooks["pre_llm_call"](session_id="s2", platform="desktop")
        self.assertTrue(wait_for(lambda: llm.calls))
        time.sleep(0.6)
        self.assertFalse(self.has(module, "s2"))

    def test_messaging_platforms_never_spend_a_call(self):
        llm = FakeLLM()
        module, ctx = self.start(llm)
        for platform in ("telegram", "discord", "cron"):
            self.turn_done(ctx, f"s-{platform}", platform=platform)
        time.sleep(0.3)
        self.assertEqual(llm.calls, [])

    def test_disabled_setting_never_spends_a_call(self):
        llm = FakeLLM()
        module, ctx = self.start(llm, settings={"enabled": False})
        self.turn_done(ctx, "s1")
        time.sleep(0.3)
        self.assertEqual(llm.calls, [])

    def test_null_answer_shows_nothing_and_clears_the_old_one(self):
        module, ctx = self.start(FakeLLM(text="NULL"))
        with module._suggestions_lock:
            module._suggestions["s3"] = {"text": "x", "session_id": "s3", "timestamp": time.time()}
        self.turn_done(ctx, "s3")
        self.assertTrue(wait_for(lambda: not self.has(module, "s3")))

    def test_hooks_tolerate_unknown_kwargs(self):
        module, ctx = self.start()
        self.turn_done(ctx, "s1", some_future_field=1, model="m", turn_id="t")
        ctx.hooks["pre_llm_call"](session_id="s9", another_future_field=True)

    def test_suggestions_expire_after_a_week(self):
        module, _ = self.start()
        old = {"text": "viejo", "session_id": "old", "timestamp": time.time() - 8 * 24 * 3600}
        self.assertNotIn("old", module._live({"old": old}))


class AuthError(Exception):
    """Shaped like anthropic/openai AuthenticationError."""

    status_code = 401


class RateLimitError(Exception):
    status_code = 429


class ReadTimeout(Exception):
    pass


class FailureTests(PluginTestCase):
    def failed(self, module, session_id):
        with module._suggestions_lock:
            return dict(module._errors.get(session_id) or {})

    def test_failure_kinds(self):
        cases = [
            (AuthError("revoked"), "auth"),
            (RateLimitError("slow down"), "rate_limit"),
            (ReadTimeout("late"), "timeout"),
            (RuntimeError("boom"), "other"),
        ]
        for error, kind in cases:
            with self.subTest(kind=kind):
                module, ctx = self.start(FakeLLM(error=error))
                self.turn_done(ctx, "s1")
                self.assertTrue(wait_for(lambda: self.failed(module, "s1")))
                self.assertEqual(self.failed(module, "s1")["kind"], kind)

    def test_failure_detail_never_leaks(self):
        module, ctx = self.start(FakeLLM(error=AuthError("sk-secret-token-in-message")))
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.failed(module, "s1")))
        self.assertNotIn("sk-secret", json.dumps(self.failed(module, "s1")))

    def test_failure_is_not_written_to_disk(self):
        module, ctx = self.start(FakeLLM(error=AuthError("x")))
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.failed(module, "s1")))
        self.assertNotIn("s1", self.stored())

    def test_success_clears_the_failure(self):
        llm = FakeLLM(error=AuthError("x"))
        module, ctx = self.start(llm)
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.failed(module, "s1")))
        llm.error = None
        module._last_suggestion_time.clear()
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.has(module, "s1")))
        self.assertEqual(self.failed(module, "s1"), {})

    def test_own_message_clears_the_failure(self):
        module, ctx = self.start(FakeLLM(error=AuthError("x")))
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.failed(module, "s1")))
        ctx.hooks["pre_llm_call"](session_id="s1")
        self.assertEqual(self.failed(module, "s1"), {})

    def test_failure_for_an_old_turn_is_discarded(self):
        llm = FakeLLM(error=AuthError("x"), delay=0.4)
        module, ctx = self.start(llm)
        self.turn_done(ctx, "s2")
        time.sleep(0.05)
        ctx.hooks["pre_llm_call"](session_id="s2")
        self.assertTrue(wait_for(lambda: llm.calls))
        time.sleep(0.6)
        self.assertEqual(self.failed(module, "s2"), {})


class RestApiTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(load_api().router, prefix="/api/plugins/next-prompt")
        self.client = TestClient(app)

    def get(self, session_id):
        return self.client.get("/api/plugins/next-prompt/suggestion", params={"session_id": session_id}).json()

    def test_get_and_dismiss(self):
        module, ctx = self.start(FakeLLM(text="Revisa los logs"))
        self.turn_done(ctx, "s4")
        self.assertTrue(wait_for(lambda: self.has(module, "s4")))
        self.assertEqual(self.get("s4")["suggestion"]["text"], "Revisa los logs")
        self.client.post("/api/plugins/next-prompt/dismiss", params={"session_id": "s4"})
        self.assertEqual(self.get("s4"), {"suggestion": None, "error": None})
        self.assertNotIn("s4", self.stored())

    def test_failure_is_reported_and_dismissable(self):
        module, ctx = self.start(FakeLLM(error=AuthError("x")))
        self.turn_done(ctx, "s5")
        self.assertTrue(wait_for(lambda: self.get("s5")["error"]))
        body = self.get("s5")
        self.assertIsNone(body["suggestion"])
        self.assertEqual(body["error"]["kind"], "auth")
        self.client.post("/api/plugins/next-prompt/dismiss", params={"session_id": "s5"})
        self.assertEqual(self.get("s5"), {"suggestion": None, "error": None})

    def test_newer_suggestion_hides_an_older_failure(self):
        module, _ = self.start()
        with module._suggestions_lock:
            module._errors["s6"] = {"kind": "auth", "session_id": "s6", "timestamp": 1.0}
            module._suggestions["s6"] = {"text": "ok", "session_id": "s6", "timestamp": 2.0}
        body = self.get("s6")
        self.assertEqual(body["suggestion"]["text"], "ok")
        self.assertIsNone(body["error"])

    def test_newest_wins_across_profile_instances(self):
        first, _ = self.start()
        second, _ = self.start()
        with first._suggestions_lock:
            first._suggestions["s1"] = {"text": "old", "session_id": "s1", "timestamp": 1.0}
        with second._suggestions_lock:
            second._suggestions["s1"] = {"text": "new", "session_id": "s1", "timestamp": 2.0}
        self.assertEqual(self.get("s1")["suggestion"]["text"], "new")
        self.assertEqual(self.get("missing"), {"suggestion": None, "error": None})
        self.assertEqual(self.get(""), {"suggestion": None, "error": None})


if __name__ == "__main__":
    unittest.main()
