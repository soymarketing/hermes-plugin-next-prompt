"""Behaviour of the Python half (__init__.py, store.py, dashboard/plugin_api.py).

Run: python -m unittest discover -s tests -v
Needs the hermes-agent package importable (its venv, or `pip install -e`).
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
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
    """Each test gets its own Hermes home (the store lives under it) and its
    own ``ctx.state.data_dir`` (where 0.3.0 kept ``suggestions.json``)."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="next-prompt-home-"))
        self.data_dir = Path(tempfile.mkdtemp(prefix="next-prompt-state-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.data_dir, ignore_errors=True)
        previous = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)
        self.addCleanup(self._restore_home, previous)
        self.modules = []
        self.addCleanup(self._unload)
        module = load_plugin()
        self.modules.append(module)
        self.store = module.store  # the module object every load of this tree shares

    @staticmethod
    def _restore_home(previous):
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous

    def _unload(self):
        # Let workers finish before the temp dirs go away.
        for thread in threading.enumerate():
            if thread.name.startswith("next-prompt") and thread is not threading.current_thread():
                thread.join(timeout=5)
        self.store.set_regenerator(None)
        for module in self.modules:
            sys.modules.pop(module.__name__, None)

    def start(self, llm=None, **ctx_kwargs):
        module = load_plugin()
        self.modules.append(module)
        ctx = FakeCtx(self.data_dir, llm or FakeLLM(), **ctx_kwargs)
        module.register(ctx)
        return module, ctx

    def db_path(self):
        return self.home / "plugin-data" / "next-prompt" / "suggestions.db"

    def suggestion(self, session_id):
        return self.store.get(session_id)["suggestion"]

    def has(self, session_id):
        return self.suggestion(session_id) is not None

    def failed(self, session_id):
        return dict(self.store.get(session_id)["error"] or {})

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
        self.assertTrue(wait_for(lambda: self.has("s1")))
        self.assertEqual(self.suggestion("s1")["text"], "Haz el deploy a producción")
        self.assertTrue(self.db_path().is_file())

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
        self.assertTrue(wait_for(lambda: self.has("s1")))
        self._unload()
        restarted, _ = self.start(FakeLLM(text="otra"))
        self.assertEqual(restarted.store.get("s1")["suggestion"]["text"], "Haz el deploy a producción")

    def test_own_message_clears_it(self):
        module, ctx = self.start()
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.has("s1")))
        ctx.hooks["pre_llm_call"](session_id="s1", platform="desktop", user_message="otra cosa")
        self.assertFalse(self.has("s1"))

    def test_late_answer_for_an_old_turn_is_discarded(self):
        llm = FakeLLM(delay=0.4)
        module, ctx = self.start(llm)
        self.turn_done(ctx, "s2")
        time.sleep(0.05)
        ctx.hooks["pre_llm_call"](session_id="s2", platform="desktop")
        self.assertTrue(wait_for(lambda: llm.calls))
        time.sleep(0.6)
        self.assertFalse(self.has("s2"))

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
        self.store.put_suggestion("s3", "x")
        self.turn_done(ctx, "s3")
        self.assertTrue(wait_for(lambda: not self.has("s3")))

    def test_hooks_tolerate_unknown_kwargs(self):
        module, ctx = self.start()
        self.turn_done(ctx, "s1", some_future_field=1, model="m", turn_id="t")
        ctx.hooks["pre_llm_call"](session_id="s9", another_future_field=True)

    def test_suggestions_expire_after_a_week(self):
        self.start()
        self.store.put_suggestion("old", "viejo")
        with sqlite3.connect(self.db_path()) as conn:
            conn.execute("UPDATE pending SET timestamp = ? WHERE session_id = 'old'", (time.time() - 8 * 24 * 3600,))
        self.assertFalse(self.has("old"))

    def test_non_interactive_turns_do_not_clear_or_generate(self):
        llm = FakeLLM(text="Should not run")
        module, ctx = self.start(llm)
        self.store.put_suggestion("s-ui", "Keep me")
        ctx.hooks["pre_llm_call"](session_id="s-ui", platform="telegram", user_message="hi")
        ctx.hooks["pre_llm_call"](
            session_id="s-ui",
            platform="desktop",
            user_message="[IMPORTANT: Background process watcher finished.]",
        )
        ctx.hooks["pre_llm_call"](
            session_id="s-ui",
            platform="desktop",
            user_message="[SYSTEM: Background process matched]",
        )
        ctx.hooks["pre_llm_call"](
            session_id="s-ui",
            platform="desktop",
            user_message="[Background process proc_1 heartbeat #1 — still running]",
        )
        self.turn_done(
            ctx,
            "s-ui",
            platform="desktop",
            user_message="[IMPORTANT: Background process watcher finished.]",
        )
        time.sleep(0.2)
        self.assertEqual(self.suggestion("s-ui")["text"], "Keep me")
        self.assertEqual(llm.calls, [])

    def test_turn_cache_is_bounded_lru(self):
        module, ctx = self.start(FakeLLM(text="ok"))
        limit = module._MAX_CACHED_TURNS
        for i in range(limit + 5):
            module._cache_turn(f"s-{i}", f"[User]: msg {i}", f"msg {i}")
        self.assertEqual(len(module._last_turns), limit)
        self.assertNotIn("s-0", module._last_turns)
        self.assertIn(f"s-{limit + 4}", module._last_turns)


class LegacyStoreTests(PluginTestCase):
    """0.3.0 kept pending suggestions in ``ctx.state.data_dir/suggestions.json``."""

    def write_legacy(self, entries):
        (self.data_dir / "suggestions.json").write_text(json.dumps(entries), encoding="utf-8")

    def test_pending_suggestions_move_to_the_store_once(self):
        now = time.time()
        self.write_legacy({
            "s1": {"text": "Sigue con el deploy", "session_id": "s1", "timestamp": now},
            "old": {"text": "viejo", "session_id": "old", "timestamp": now - 8 * 24 * 3600},
        })
        self.start()
        self.assertEqual(self.suggestion("s1")["text"], "Sigue con el deploy")
        self.assertFalse(self.has("old"))
        self.assertFalse((self.data_dir / "suggestions.json").exists())
        self.assertTrue((self.data_dir / "suggestions.json.migrated").exists())

    def test_a_dismissed_suggestion_does_not_come_back_after_a_restart(self):
        self.write_legacy({"s1": {"text": "x", "session_id": "s1", "timestamp": time.time()}})
        self.start()
        self.store.clear("s1")
        self._unload()
        self.start()
        self.assertFalse(self.has("s1"))

    def test_unreadable_legacy_file_is_set_aside(self):
        (self.data_dir / "suggestions.json").write_text("{not json", encoding="utf-8")
        self.start()
        self.assertTrue((self.data_dir / "suggestions.json.migrated").exists())


class ContextAndCleanTests(unittest.TestCase):
    """The prompt input and the output filter, without any LLM."""

    @classmethod
    def setUpClass(cls):
        cls.m = load_plugin()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(cls.m.__name__, None)

    def test_tool_heavy_turn_keeps_the_request_and_the_final_reply(self):
        history = [
            {"role": "user", "content": "Arregla el bug del login"},
            {"role": "assistant", "content": "Voy a revisar los logs.", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "x" * 5000, "tool_call_id": "1"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "2"}]},
            {"role": "tool", "content": "y" * 5000, "tool_call_id": "2"},
            {"role": "assistant", "content": "Listo: el token expiraba antes de tiempo. Lo corregí en auth.py."},
        ]
        context = self.m._build_context(history, 4)
        self.assertIn("[User]: Arregla el bug del login", context)
        self.assertIn("[Agent]: Listo: el token expiraba", context)
        self.assertNotIn("Voy a revisar los logs", context, "narration between tool calls is dropped")
        self.assertNotIn("xxxx", context, "tool output never reaches the prompt")

    def test_keeps_the_last_exchanges_only(self):
        history = []
        for i in range(5):
            history += [{"role": "user", "content": f"pregunta {i}"}, {"role": "assistant", "content": f"respuesta {i}"}]
        context = self.m._build_context(history, 4)
        self.assertNotIn("pregunta 2", context)
        self.assertIn("pregunta 3", context)
        self.assertTrue(context.endswith("[Agent]: respuesta 4"))

    def test_long_reply_keeps_its_ending(self):
        reply = "inicio " + "relleno " * 400 + "¿Quieres que lo despliegue?"
        context = self.m._build_context([{"role": "user", "content": "hazlo"}, {"role": "assistant", "content": reply}], 4)
        self.assertIn("¿Quieres que lo despliegue?", context)
        self.assertIn("inicio", context)
        self.assertLess(len(context), 1400)

    def test_multimodal_content_is_reduced_to_text(self):
        history = [
            {"role": "user", "content": [{"type": "text", "text": "mira esta imagen"}, {"type": "image_url", "image_url": {"url": "data:x"}}]},
            {"role": "assistant", "content": "Es un gato."},
        ]
        self.assertIn("[User]: mira esta imagen", self.m._build_context(history, 4))

    def test_language_reminder_quotes_the_users_last_message(self):
        history = [
            {"role": "user", "content": "Arregla el login"},
            {"role": "assistant", "content": "Listo."},
            {"role": "user", "content": "Now compare  Stripe\nand Mercado Pago fees in Mexico " + "x" * 400},
            {"role": "assistant", "content": "Stripe charges 3.6%."},
        ]
        last = self.m._last_user_text(history)
        self.assertTrue(last.startswith("Now compare"))
        system, user = self.m._prompt_messages(self.m._build_context(history, 4), last)
        self.assertEqual(system["role"], "system")
        tail = user["content"].rsplit("\n\n", 1)[1]
        self.assertIn('"Now compare Stripe and Mercado Pago fees in Mexico', tail, "whitespace collapsed, quoted")
        sample = tail.split('"', 1)[1].rsplit('"', 1)[0]
        self.assertLessEqual(len(sample), self.m._LANGUAGE_SAMPLE_CHARS + 1, "the sample is capped")

    def test_language_reminder_without_a_user_message(self):
        self.assertEqual(self.m._last_user_text([{"role": "assistant", "content": "hola"}]), "")
        _, user = self.m._prompt_messages("[Agent]: hola", "")
        self.assertTrue(user["content"].endswith("Write in the user's language."))

    def test_clean(self):
        cases = {
            "Corre los tests": "Corre los tests",
            '"Corre los tests"': "Corre los tests",
            "“Corre los tests”": "Corre los tests",
            "«Corre los tests»": "Corre los tests",
            "Suggestion: Run the tests": "Run the tests",
            "**Run the tests**": "Run the tests",
            "Run the tests\nBecause the fix touched auth.py": "Run the tests",
            "NULL": "",
            "null.": "",
            "NULL — nothing to act on": "",
            "None": "",
            "": "",
            "ok": "",
            "x" * 240: "",
            "x" * 200: "x" * 200,
            # A reply to the agent's question survives untouched...
            "Sí, despliégalo a producción": "Sí, despliégalo a producción",
            "Reinicié y el botón ya funciona": "Reinicié y el botón ya funciona",
            # Inline code brackets/generics are technical syntax, not fill-ins.
            "Fix `arr[0]` and `List<str>` in parser.py": "Fix `arr[0]` and `List<str>` in parser.py",
            # A whole suggestion wrapped in backticks still cannot sneak placeholders past _FILL_IN.
            "`Send to <email>`": "",
            # ...but anything the user must edit before sending is dropped.
            "Envíala a [correo del cliente]": "",
            "Probé la pill: [funcionó / no salió / salió pero con error]": "",
            "Send it to <email address>": "",
            "My email is ___": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw[:40]):
                self.assertEqual(self.m._clean(raw), expected)

    def test_clean_with_reason_reports_drop_causes(self):
        self.assertEqual(self.m._clean_with_reason(""), ("", "empty"))
        self.assertEqual(self.m._clean_with_reason("NULL"), ("", "null_answer"))
        self.assertEqual(self.m._clean_with_reason("Send to <email>"), ("", "fill_in_placeholder"))
        self.assertEqual(self.m._clean_with_reason("ok"), ("", "too_short"))
        self.assertEqual(self.m._clean_with_reason("x" * 240), ("", "too_long"))
        self.assertEqual(self.m._clean_with_reason("Run pytest"), ("Run pytest", ""))


class AuthError(Exception):
    """Shaped like anthropic/openai AuthenticationError."""

    status_code = 401


class RateLimitError(Exception):
    status_code = 429


class ReadTimeout(Exception):
    pass


class FailureTests(PluginTestCase):
    def test_failure_kinds(self):
        cases = [
            (AuthError("revoked"), "auth"),
            (RateLimitError("slow down"), "rate_limit"),
            (ReadTimeout("late"), "timeout"),
            (RuntimeError("boom"), "other"),
        ]
        for error, kind in cases:
            with self.subTest(kind=kind):
                session_id = f"s-{kind}"
                module, ctx = self.start(FakeLLM(error=error))
                self.turn_done(ctx, session_id)
                self.assertTrue(wait_for(lambda: self.failed(session_id)))
                self.assertEqual(self.failed(session_id)["kind"], kind)

    def test_failure_detail_never_leaks(self):
        module, ctx = self.start(FakeLLM(error=AuthError("sk-secret-value")))
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.failed("s1")))
        self.assertNotIn("sk-secret", json.dumps(self.failed("s1")))
        self.assertNotIn(b"sk-secret", self.db_path().read_bytes())

    def test_failure_note_expires_sooner_than_a_suggestion(self):
        self.start()
        self.store.put_failure("s1", "auth")
        with sqlite3.connect(self.db_path()) as conn:
            conn.execute("UPDATE pending SET timestamp = ? WHERE session_id = 's1'", (time.time() - 7 * 3600,))
        self.assertEqual(self.failed("s1"), {})

    def test_success_clears_the_failure(self):
        llm = FakeLLM(error=AuthError("x"))
        module, ctx = self.start(llm)
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.failed("s1")))
        llm.error = None
        module._last_suggestion_time.clear()
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.has("s1")))
        self.assertEqual(self.failed("s1"), {})

    def test_own_message_clears_the_failure(self):
        module, ctx = self.start(FakeLLM(error=AuthError("x")))
        self.turn_done(ctx, "s1")
        self.assertTrue(wait_for(lambda: self.failed("s1")))
        ctx.hooks["pre_llm_call"](session_id="s1")
        self.assertEqual(self.failed("s1"), {})

    def test_failure_for_an_old_turn_is_discarded(self):
        llm = FakeLLM(error=AuthError("x"), delay=0.4)
        module, ctx = self.start(llm)
        self.turn_done(ctx, "s2")
        time.sleep(0.05)
        ctx.hooks["pre_llm_call"](session_id="s2")
        self.assertTrue(wait_for(lambda: llm.calls))
        time.sleep(0.6)
        self.assertEqual(self.failed("s2"), {})


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
        self.assertTrue(wait_for(lambda: self.has("s4")))
        self.assertEqual(self.get("s4")["suggestion"]["text"], "Revisa los logs")
        self.client.post("/api/plugins/next-prompt/dismiss", params={"session_id": "s4"})
        self.assertEqual(self.get("s4"), {"suggestion": None, "error": None})
        self.assertFalse(self.has("s4"))

    def test_failure_is_reported_and_dismissable(self):
        module, ctx = self.start(FakeLLM(error=AuthError("x")))
        self.turn_done(ctx, "s5")
        self.assertTrue(wait_for(lambda: self.get("s5")["error"]))
        body = self.get("s5")
        self.assertIsNone(body["suggestion"])
        self.assertEqual(body["error"]["kind"], "auth")
        self.client.post("/api/plugins/next-prompt/dismiss", params={"session_id": "s5"})
        self.assertEqual(self.get("s5"), {"suggestion": None, "error": None})

    def test_newer_suggestion_replaces_an_older_failure(self):
        self.start()
        self.store.put_failure("s6", "auth")
        self.store.put_suggestion("s6", "ok")
        body = self.get("s6")
        self.assertEqual(body["suggestion"]["text"], "ok")
        self.assertIsNone(body["error"])

    def test_missing_or_blank_session(self):
        self.assertEqual(self.get("missing"), {"suggestion": None, "error": None})
        self.assertEqual(self.get(""), {"suggestion": None, "error": None})

    def test_reads_a_suggestion_written_by_another_process(self):
        """The bug behind 0.3.1: on a gateway serving several profiles, a
        secondary profile's plugin API runs in its plugin host while its hooks
        run in the gateway. Write from a separate interpreter, read here."""
        script = textwrap.dedent(f"""
            import importlib.util, sys
            spec = importlib.util.spec_from_file_location("np_store_child", {str(ROOT / "store.py")!r})
            store = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(store)
            assert store.put_suggestion("s-cross", "Desde otro proceso") is not None
        """)
        # Same import path as this interpreter (hermes-agent, its dependencies).
        env = dict(os.environ, HERMES_HOME=str(self.home), PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        subprocess.run([sys.executable, "-c", script], env=env, check=True, timeout=60)
        self.assertEqual(self.get("s-cross")["suggestion"]["text"], "Desde otro proceso")
        self.client.post("/api/plugins/next-prompt/dismiss", params={"session_id": "s-cross"})
        self.assertFalse(self.has("s-cross"))

    def test_generate_endpoint_regenerates_from_cached_turn(self):
        llm = FakeLLM(text="First suggestion")
        module, ctx = self.start(llm)
        self.turn_done(ctx, "s-gen")
        self.assertTrue(wait_for(lambda: (self.suggestion("s-gen") or {}).get("text") == "First suggestion"))
        self.client.post("/api/plugins/next-prompt/dismiss", params={"session_id": "s-gen"})
        self.assertFalse(self.has("s-gen"))

        llm.text = "Regenerated suggestion"
        res = self.client.post("/api/plugins/next-prompt/generate", params={"session_id": "s-gen"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["started"])
        self.assertTrue(wait_for(lambda: (self.suggestion("s-gen") or {}).get("text") == "Regenerated suggestion"))

    def test_generate_endpoint_reports_unavailable_without_in_process_hook(self):
        self.store.set_regenerator(None)
        res = self.client.post("/api/plugins/next-prompt/generate", params={"session_id": "s-missing"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": False, "started": False, "reason": "unavailable"})


if __name__ == "__main__":
    unittest.main()
