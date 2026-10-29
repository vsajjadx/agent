"""
تست‌های هسته با سرور جعلی محلی (بدون نیاز به API Key یا اینترنت).
اجرا:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent_core as core  # noqa: E402

FAKE_IMG = {"mime": "image/jpeg", "data": "QUJD"}


class Mock:
    """Tiny scripted HTTP server: each POST pops the next handler(body, headers, path) -> (status, json)."""

    def __init__(self):
        self.script, self.requests, self.get_routes = [], [], {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj, extra=None):
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status, obj = outer.script.pop(0)(body, dict(self.headers), self.path)
                self._send(status, obj, {"Retry-After": "0"} if status == 503 else None)

            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": None})
                self._send(*outer.get_routes[self.path.split("?")[0]])

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def make_agent(provider, mock, tmp, model="m"):
    base = {"Anthropic": mock.url, "Gemini": mock.url + "/v1beta", "OpenAI-compatible": mock.url + "/v1"}[provider]
    cfg = {"provider": provider, "api_key": "KEY", "base_url": base, "model": model, "mode": core.MODE_FULL,
           "cwd": tmp, "instructions": "be nice"}
    logs = []
    a = core.Agent(cfg, log=lambda k, t: logs.append((k, t)))
    a.logs = logs
    # fake screenshot so no display is needed
    a.t_screenshot = lambda: ("Screenshot 10x10.", dict(FAKE_IMG))
    return a


class Base(unittest.TestCase):
    def setUp(self):
        self.mock = Mock()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        self.mock.close()


class TestGemini(Base):
    def test_tool_loop_signature_images(self):
        sig = "SIG_A"

        def first(body, headers, path):
            self.assertEqual(path, "/v1beta/models/gemini-3.8-flash:generateContent")
            self.assertEqual(headers["x-goog-api-key"], "KEY")
            self.assertIn("be nice", body["systemInstruction"]["parts"][0]["text"])
            decl = {d["name"]: d for d in body["tools"][0]["functionDeclarations"]}
            self.assertNotIn("parameters", decl["screenshot"])           # empty-object tools: no parameters
            self.assertEqual(decl["write_file"]["parameters"]["type"], "OBJECT")
            self.assertEqual(decl["mouse_click"]["parameters"]["properties"]["button"]["enum"],
                             ["left", "right", "middle"])
            self.assertEqual(body["contents"][0]["role"], "user")
            return 200, {"candidates": [{"content": {"role": "model", "parts": [
                {"text": "ok, writing"},
                {"functionCall": {"name": "write_file", "args": {"path": "a.txt", "content": "salam سلام"}},
                 "thoughtSignature": sig},
                {"functionCall": {"name": "screenshot", "args": {}}}]}}]}

        def second(body, headers, path):
            c = body["contents"]
            self.assertEqual([x["role"] for x in c], ["user", "model", "user"])
            model_parts = c[1]["parts"]
            self.assertEqual(model_parts[1]["thoughtSignature"], sig)     # signature echoed back verbatim
            self.assertEqual(model_parts[1]["functionCall"]["name"], "write_file")
            user_parts = c[2]["parts"]
            frs = [p["functionResponse"] for p in user_parts if "functionResponse" in p]
            self.assertEqual([f["name"] for f in frs], ["write_file", "screenshot"])
            self.assertIn("Wrote", frs[0]["response"]["result"])
            self.assertTrue(any("inlineData" in p for p in user_parts))  # screenshot rides in the same turn
            return 200, {"candidates": [{"content": {"parts": [{"text": "تمام شد"}]}, "finishReason": "STOP"}]}

        self.mock.script = [first, second]
        a = make_agent("Gemini", self.mock, self.tmp, "gemini-3.8-flash")
        a.run("hello")
        with open(os.path.join(self.tmp, "a.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "salam سلام")
        self.assertEqual([k for k, t in a.logs if k == "assistant"], ["assistant", "assistant"])
        self.assertEqual(len(self.mock.script), 0)

    def test_image_layout_fallback_on_400(self):
        reply_fc = lambda b, h, p: (200, {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "screenshot", "args": {}}}]}}]})

        def strict(body, headers, path):
            # model rejects inlineData next to functionResponse in the same turn
            last = body["contents"][-1]["parts"]
            if any("functionResponse" in p for p in last) and any("inlineData" in p for p in last):
                return 400, {"error": {"message": "bad layout"}}
            return 200, {"candidates": [{"content": {"parts": [{"text": "seen it"}]}}]}

        def nested(body, headers, path):
            return strict(body, headers, path)

        # first call -> screenshot; 2nd (same_turn) -> 400; 3rd (nested) -> 200
        self.mock.script = [reply_fc, strict, lambda b, h, p: (200, {"candidates": [{"content": {"parts": [
            {"text": "seen it"}]}}]})]
        a = make_agent("Gemini", self.mock, self.tmp)
        a.run("shot please")
        self.assertEqual(a.gemini_img_mode, "nested")
        nested_req = self.mock.requests[-1]["body"]["contents"][-1]["parts"][0]["functionResponse"]
        self.assertIn("parts", nested_req)

    def test_dummy_signature_for_foreign_history(self):
        a = make_agent("Gemini", self.mock, self.tmp, "gemini-3.8-flash")
        a.history = [
            {"role": "user", "content": [{"type": "text", "text": "x"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "list_dir",
                                               "input": {"path": "."}}]},
            {"role": "user", "content": [{"type": "tool_result", "id": "t1", "name": "list_dir", "text": "f",
                                          "images": []}]}]
        c = a._to_gemini()
        self.assertEqual(c[1]["parts"][0]["thoughtSignature"], "skip_thought_signature_validator")
        self.assertNotIn("id", c[1]["parts"][0]["functionCall"])        # made-up ids are never sent

    def test_blocked_prompt_raises(self):
        self.mock.script = [lambda b, h, p: (200, {"promptFeedback": {"blockReason": "SAFETY"}})]
        a = make_agent("Gemini", self.mock, self.tmp)
        with self.assertRaises(RuntimeError) as cm:
            a.run("x")
        self.assertIn("SAFETY", str(cm.exception))

    def test_retry_then_ok_and_error_message(self):
        ok = lambda b, h, p: (200, {"candidates": [{"content": {"parts": [{"text": "fine"}]}}]})
        self.mock.script = [lambda b, h, p: (503, {"error": {"message": "overloaded"}}), ok]
        a = make_agent("Gemini", self.mock, self.tmp)
        a.run("x")
        self.assertIn(("assistant", "fine"), a.logs)
        self.mock.script = [lambda b, h, p: (400, {"error": {"message": "API key not valid"}})]
        with self.assertRaises(RuntimeError) as cm:
            make_agent("Gemini", self.mock, self.tmp).run("x")
        self.assertIn("API key not valid", str(cm.exception))

    def test_no_retry_when_free_tier_limit_is_zero(self):
        msg = "Quota exceeded ... limit: 0, model: gemini-3.1-pro ... Please retry in 22h6m17.29s."
        self.mock.script = [lambda b, h, p: (429, {"error": {"message": msg}})]
        with self.assertRaises(RuntimeError) as cm:
            make_agent("Gemini", self.mock, self.tmp).run("x")
        self.assertIn("limit: 0", str(cm.exception))
        self.assertEqual(len(self.mock.requests), 1)             # failed fast, no pointless retries
        self.assertEqual(len(self.mock.script), 0)

    def test_ping_and_models(self):
        self.mock.script = [lambda b, h, p: (200, {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})]
        cfg = make_agent("Gemini", self.mock, self.tmp).cfg
        self.assertEqual(core.ping(cfg), "OK")
        self.assertNotIn("tools", self.mock.requests[-1]["body"])
        self.mock.get_routes["/v1beta/models"] = (200, {"models": [
            {"name": "models/gemini-a", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]}]})
        self.assertEqual(core.list_models(cfg), ["gemini-a"])


class TestAnthropicOpenAI(Base):
    def test_anthropic_loop(self):
        def first(body, headers, path):
            self.assertEqual(path, "/v1/messages")
            self.assertEqual(headers["x-api-key"], "KEY")
            self.assertEqual(body["tools"][0]["name"], "run_command")
            return 200, {"content": [{"type": "text", "text": "listing"},
                                     {"type": "tool_use", "id": "toolu_1", "name": "screenshot", "input": {}}]}

        def second(body, headers, path):
            tr = body["messages"][-1]["content"][0]
            self.assertEqual(tr["type"], "tool_result")
            self.assertEqual(tr["tool_use_id"], "toolu_1")
            self.assertEqual(tr["content"][1]["type"], "image")
            return 200, {"content": [{"type": "text", "text": "done"}]}

        self.mock.script = [first, second]
        a = make_agent("Anthropic", self.mock, self.tmp)
        a.run("go", images=[FAKE_IMG])
        first_user = self.mock.requests[0]["body"]["messages"][0]["content"]
        self.assertEqual(first_user[0]["type"], "image")      # attached image is sent
        self.assertIn(("assistant", "done"), a.logs)

    def test_openai_loop(self):
        def first(body, headers, path):
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(headers["Authorization"], "Bearer KEY")
            return 200, {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}}]}}]}

        def second(body, headers, path):
            roles = [m["role"] for m in body["messages"]]
            self.assertEqual(roles, ["system", "user", "assistant", "tool"])
            self.assertEqual(body["messages"][3]["tool_call_id"], "call_1")
            return 200, {"choices": [{"message": {"content": "bye"}}]}

        self.mock.script = [first, second]
        a = make_agent("OpenAI-compatible", self.mock, self.tmp)
        a.run("hi")
        self.assertIn(("assistant", "bye"), a.logs)

    def test_switch_provider_mid_chat(self):
        """History is neutral: a chat started on Gemini continues on Anthropic."""
        self.mock.script = [lambda b, h, p: (200, {"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "list_dir", "args": {"path": "."}}}]}}]}),
            lambda b, h, p: (200, {"candidates": [{"content": {"parts": [{"text": "one"}]}}]}),
            lambda b, h, p: (200, {"content": [{"type": "text", "text": "two"}]})]
        a = make_agent("Gemini", self.mock, self.tmp)
        a.run("first")
        a.cfg = make_agent("Anthropic", self.mock, self.tmp).cfg
        a.run("second")
        msgs = self.mock.requests[-1]["body"]["messages"]
        self.assertEqual(msgs[1]["content"][0]["type"], "tool_use")
        self.assertIn(("assistant", "two"), a.logs)


class TestTools(Base):
    def setUp(self):
        super().setUp()
        self.a = make_agent("Gemini", self.mock, self.tmp)

    def test_files_and_search(self):
        a = self.a
        a.t_write_file("sub/x.py", "alpha\nbeta needle\ngamma\n")
        self.assertIn("beta needle", a.t_read_file("sub/x.py")[0])
        self.assertIn("2: beta needle", a.t_read_file("sub/x.py", 2, 2)[0])
        self.assertIn("sub/x.py", a.t_find_files("**/*.py")[0].replace("\\", "/"))
        self.assertIn("x.py:2:beta needle", a.t_search_files("NEEDLE", ignore_case=True)[0].replace("\\", "/"))
        self.assertIn("not found", a.t_edit_file("sub/x.py", "zzz", "q")[0])
        a.t_write_file("d.txt", "a a")
        self.assertIn("occurs 2", a.t_edit_file("d.txt", "a", "b")[0])
        self.assertIn("2 replacements", a.t_edit_file("d.txt", "a", "b", replace_all=True)[0])

    def test_run_command_cwd_timeout_stop(self):
        a = self.a
        out = a.t_run_command("echo hello-from-shell")[0]
        self.assertIn("hello-from-shell", out)
        self.assertIn("exit code: 0", out)
        if not core.IS_WIN:
            self.assertIn(os.path.realpath(self.tmp), a.t_run_command("pwd")[0])
            self.assertIn("TIMEOUT", a.t_run_command("sleep 5", timeout=1)[0])
            a.should_stop = lambda: True
            self.assertIn("Stopped", a.t_run_command("sleep 5")[0])

    def test_approval_modes(self):
        a = self.a
        a.cfg["mode"] = core.MODE_SMART
        self.assertFalse(a.needs_approval("run_command", {"command": "ls -la"}))
        self.assertTrue(a.needs_approval("run_command", {"command": "rm -rf /tmp/x"}))
        a.cfg["mode"] = core.MODE_ASK
        self.assertTrue(a.needs_approval("write_file", {}))
        self.assertFalse(a.needs_approval("read_file", {}))
        a.ask = lambda d: "no"
        self.assertIn("DENIED", a.execute("write_file", {"path": "q", "content": "1"})[0])
        a.ask = lambda d: "all"
        a.execute("write_file", {"path": "q", "content": "1"})
        self.assertFalse(a.needs_approval("write_file", {}))        # "allow all" sticks for the chat

    def test_screenshot_pruning_and_save(self):
        a = self.a
        for i in range(5):
            a.history.append({"role": "user", "content": [{"type": "tool_result", "id": str(i), "name": "screenshot",
                                                          "text": "s", "images": [dict(FAKE_IMG)]}]})
        a.prune_images()
        kept = [b for m in a.history for b in m["content"] if b["images"]]
        self.assertEqual(len(kept), core.KEEP_SCREENSHOTS)
        self.assertEqual(kept[-1]["id"], "4")
        core.CHAT_DIR = os.path.join(self.tmp, "chats")
        core.save_chat("c1", "title", a.history, {"provider": "Gemini"})
        d = core.load_chat("c1")
        self.assertTrue(all(not b.get("images") for m in d["history"] for b in m["content"]))
        self.assertEqual(core.list_chats()[0]["id"], "c1")
        core.delete_chat("c1")
        self.assertEqual(core.list_chats(), [])

    def test_repair_history(self):
        a = self.a
        a.history = [{"role": "user", "content": [{"type": "text", "text": "x"}]},
                     {"role": "assistant", "content": [{"type": "tool_use", "id": "z", "name": "wait", "input": {}}]}]
        a.repair_history()
        self.assertEqual(a.history[-1]["content"][0]["id"], "z")

    def test_unknown_tool_and_bad_args(self):
        self.assertIn("unknown tool", self.a.execute("rm_everything", {})[0])
        self.assertIn("Error", self.a.execute("read_file", {"nope": 1})[0])


if __name__ == "__main__":
    unittest.main()
