"""
تست دودی رابط گرافیکی بدون نمایشگر (QT_QPA_PLATFORM=offscreen).  اگر PyQt6 نصب نباشه رد می‌شه.
"""
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from PyQt6.QtWidgets import QApplication
    import ai_agent
    HAVE_QT = True
except Exception:       # pragma: no cover
    HAVE_QT = False

import agent_core as core  # noqa: E402
from test_core import Mock  # noqa: E402


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestGui(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.app.setStyleSheet(ai_agent.STYLE)
        self.mock = Mock()
        self.tmp = tempfile.mkdtemp()
        core.CHAT_DIR = os.path.join(self.tmp, "chats")
        ai_agent.CONFIG_PATH = os.path.join(self.tmp, "cfg.json")

    def tearDown(self):
        self.mock.close()

    def wait(self, cond, secs=15):
        end = time.time() + secs
        while time.time() < end and not cond():
            self.app.processEvents()
            time.sleep(0.02)
        self.app.processEvents()
        self.assertTrue(cond(), "timed out")

    def test_send_gemini_roundtrip_and_chat_saved(self):
        self.mock.script = [
            lambda b, h, p: (200, {"candidates": [{"content": {"parts": [
                {"text": "یه نگاه به پوشه می‌ندازم"},
                {"functionCall": {"name": "list_dir", "args": {"path": "."}}, "thoughtSignature": "S"}]}}]}),
            lambda b, h, p: (200, {"candidates": [{"content": {"parts": [
                {"text": "## نتیجه\n- پوشه **خالی** است\n\n```python\nprint(1)\n```"}]}}]}),
        ]
        w = ai_agent.Main()
        w.cfg["providers"]["Gemini"] = {"api_key": "K", "base_url": self.mock.url + "/v1beta",
                                        "model": "gemini-3.8-flash"}
        w.cfg["cwd"] = self.tmp
        w.mode.setCurrentText(core.MODE_FULL)
        w.input.setPlainText("سلام، پوشه رو ببین")
        w.send()
        self.wait(lambda: not w.busy())
        text = w.chat.toPlainText()
        self.assertIn("سلام، پوشه رو ببین", text)
        self.assertIn("list_dir", text)
        self.assertIn("پوشه خالی است", text)
        self.assertNotIn("خطا", text)
        self.assertEqual(len(core.list_chats()), 1)
        self.assertEqual(w.chats.count(), 1)
        # reopen the stored chat in a fresh window
        w2 = ai_agent.Main()
        w2.cfg["providers"]["Gemini"] = w.cfg["providers"]["Gemini"]
        w2.open_chat_item(w2.chats.item(0))
        self.assertIn("پوشه خالی است", w2.chat.toPlainText())
        out = os.environ.get("GUI_SHOT")
        if out:
            w.resize(1100, 760)
            w.grab().save(out)

    def test_error_is_shown_with_hint(self):
        self.mock.script = [lambda b, h, p: (400, {"error": {"message": "API key not valid. Please pass a valid API key."}})]
        w = ai_agent.Main()
        w.cfg["providers"]["Gemini"] = {"api_key": "bad", "base_url": self.mock.url + "/v1beta"}
        w.input.setPlainText("hi")
        w.send()
        self.wait(lambda: not w.busy())
        self.assertIn("API key not valid", w.chat.toPlainText())
        self.assertIn("کلید API نامعتبره", w.chat.toPlainText())

    def test_attach_image_and_provider_switch(self):
        from PyQt6.QtGui import QImage, QColor
        im = QImage(40, 30, QImage.Format.Format_RGB32)
        im.fill(QColor("red"))
        w = ai_agent.Main()
        w.add_qimages([im])
        self.assertEqual(len(w.pending), 1)
        self.assertEqual(w.pending[0]["mime"], "image/jpeg")
        w.open_settings = lambda: None
        w.provider.setCurrentText("OpenAI-compatible")
        self.assertEqual(w._cur_provider, "OpenAI-compatible")


if __name__ == "__main__":
    unittest.main()
