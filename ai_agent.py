"""
AI Desktop Agent  -  یک ایجنت دسکتاپ با کنترل سیستم (شبیه Claude Desktop / Cursor)

نصب:
    pip install PyQt6 requests pyautogui pillow pyperclip

اجرا:
    python ai_agent.py

ابزارهای ایجنت: اجرای دستور شل، خواندن/نوشتن/ویرایش فایل، لیست پوشه،
اسکرین‌شات، کلیک/تایپ/کلید/اسکرول موس و کیبورد، دریافت صفحه وب.
"""
import sys, os, json, base64, io, re, subprocess, threading, time, platform, html
from datetime import datetime

import requests
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
                             QLineEdit, QPushButton, QComboBox, QLabel, QMessageBox)

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".ai_agent_config.json")
IS_WIN = platform.system() == "Windows"
MAX_STEPS = 50
MAX_OUT = 20000

MODE_ASK = "هر عملیات تأیید بخواد"
MODE_SMART = "خودکار (فقط دستورات خطرناک تأیید بخوان)"
MODE_FULL = "کاملاً خودکار (بدون تأیید!)"

DANGEROUS = re.compile(
    r"(rm\s+-[a-z]*r|del\s+/[sqf]|rmdir\s+/s|remove-item.*-recurse|format\s+[a-z]:|diskpart|shutdown|"
    r"restart-computer|reg\s+delete|bcdedit|mkfs|dd\s+if=|cipher\s+/w|net\s+user|takeown|icacls|"
    r"set-executionpolicy|invoke-expression|\biex\b|curl.*\|\s*(sh|bash)|irm.*\|\s*iex|taskkill|stop-process)",
    re.I)

TOOLS = [
    {"name": "run_command",
     "description": "Run a shell command (PowerShell on Windows, bash otherwise) and return stdout/stderr.",
     "schema": {"type": "object", "properties": {
         "command": {"type": "string"}, "cwd": {"type": "string", "description": "optional working dir"},
         "timeout": {"type": "integer", "description": "seconds, default 60"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read a text file.",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Create/overwrite a text file (parent dirs are created).",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace one exact unique occurrence of old with new in a file.",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"},
                                                  "new": {"type": "string"}}, "required": ["path", "old", "new"]}},
    {"name": "list_dir", "description": "List files and folders in a directory.",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "fetch_url", "description": "HTTP GET a URL and return the text (truncated).",
     "schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "screenshot", "description": "Take a screenshot of the screen. Click coordinates refer to this image.",
     "schema": {"type": "object", "properties": {}}},
    {"name": "mouse_click",
     "description": "Click at x,y (in screenshot coordinates).",
     "schema": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "double": {"type": "boolean"}}, "required": ["x", "y"]}},
    {"name": "type_text", "description": "Type text into the focused control (supports Unicode/Persian via clipboard paste).",
     "schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "press_key", "description": "Press a key or combo, e.g. 'enter', 'ctrl+c', 'win+r', 'alt+tab'.",
     "schema": {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}},
    {"name": "scroll", "description": "Scroll the mouse wheel; positive = up, negative = down.",
     "schema": {"type": "object", "properties": {"amount": {"type": "integer"}}, "required": ["amount"]}},
]
SAFE_TOOLS = {"read_file", "list_dir", "fetch_url", "screenshot"}


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def trunc(s, n=MAX_OUT):
    return s if len(s) <= n else s[:n // 2] + "\n...[truncated]...\n" + s[-n // 2:]


class Agent:
    """Provider-agnostic tool loop. Runs in a worker thread."""

    def __init__(self, cfg, log, ask, should_stop):
        self.cfg, self.log, self.ask, self.should_stop = cfg, log, ask, should_stop
        self.history = []
        self.scale = 1.0

    # ---------- system prompt ----------
    def system_prompt(self):
        return (
            "You are an autonomous desktop agent with full access to the user's computer through tools. "
            f"OS: {platform.platform()}. Home: {os.path.expanduser('~')}. Now: {datetime.now():%Y-%m-%d %H:%M}. "
            f"Shell: {'PowerShell' if IS_WIN else 'bash'}.\n"
            "Work step by step, use tools to actually do the task, verify results, and give a short final summary. "
            "Prefer shell/file tools over GUI control; use screenshot before any mouse action and re-check after. "
            "Never do destructive things (deleting data, formatting, killing system processes) unless the user "
            "clearly asked for them. Reply in the user's language."
        )

    # ---------- tool execution ----------
    def needs_approval(self, name, args):
        mode = self.cfg["mode"]
        if mode == MODE_FULL or name in SAFE_TOOLS:
            return False
        if mode == MODE_ASK:
            return True
        return name == "run_command" and bool(DANGEROUS.search(args.get("command", "")))

    def execute(self, name, args):
        desc = f"{name}: {json.dumps(args, ensure_ascii=False)[:600]}"
        self.log("tool", desc)
        if self.needs_approval(name, args) and not self.ask(desc):
            return "User DENIED this action.", None
        try:
            return getattr(self, "t_" + name)(**args)
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}", None

    def t_run_command(self, command, cwd=None, timeout=60):
        cmd = ["powershell", "-NoProfile", "-Command", command] if IS_WIN else ["bash", "-lc", command]
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or None, timeout=timeout,
                           encoding="utf-8", errors="replace")
        out = f"exit code: {p.returncode}\n--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}"
        return trunc(out), None

    def t_read_file(self, path):
        with open(os.path.expanduser(path), "r", encoding="utf-8", errors="replace") as f:
            return trunc(f.read()), None

    def t_write_file(self, path, content):
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return f"Wrote {len(content)} chars to {path}", None

    def t_edit_file(self, path, old, new):
        path = os.path.expanduser(path)
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        if data.count(old) != 1:
            return f"Error: 'old' must occur exactly once (found {data.count(old)}).", None
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(data.replace(old, new))
        return "Edited.", None

    def t_list_dir(self, path):
        path = os.path.expanduser(path)
        items = []
        for n in sorted(os.listdir(path))[:500]:
            items.append(n + ("/" if os.path.isdir(os.path.join(path, n)) else ""))
        return "\n".join(items) or "(empty)", None

    def t_fetch_url(self, url):
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        return trunc(f"HTTP {r.status_code}\n{r.text}"), None

    def t_screenshot(self):
        import pyautogui
        img = pyautogui.screenshot()
        w, h = img.size
        nw = min(w, 1280)
        nh = int(h * nw / w)
        self.scale = w / nw
        img = img.resize((nw, nh))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return f"Screenshot {nw}x{nh}.", base64.b64encode(buf.getvalue()).decode()

    def t_mouse_click(self, x, y, button="left", double=False):
        import pyautogui
        X, Y = int(x * self.scale), int(y * self.scale)
        pyautogui.click(X, Y, clicks=2 if double else 1, button=button)
        return f"Clicked ({x},{y}) [{button}{', double' if double else ''}]", None

    def t_type_text(self, text):
        import pyautogui, pyperclip
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        return f"Typed {len(text)} chars.", None

    def t_press_key(self, keys):
        import pyautogui
        pyautogui.hotkey(*[k.strip().lower() for k in keys.split("+")])
        return f"Pressed {keys}", None

    def t_scroll(self, amount):
        import pyautogui
        pyautogui.scroll(amount)
        return "Scrolled.", None

    # ---------- providers ----------
    def step_anthropic(self):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.cfg["api_key"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.cfg["model"], "max_tokens": 4096, "system": self.system_prompt(),
                  "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
                            for t in TOOLS],
                  "messages": self.history}, timeout=180)
        if r.status_code != 200:
            raise RuntimeError(f"API {r.status_code}: {r.text}")
        content = r.json()["content"]
        self.history.append({"role": "assistant", "content": content})
        for b in content:
            if b["type"] == "text" and b["text"].strip():
                self.log("assistant", b["text"])
        calls = [b for b in content if b["type"] == "tool_use"]
        if not calls:
            return False
        results = []
        for c in calls:
            text, img = self.execute(c["name"], c["input"])
            blocks = [{"type": "text", "text": text}]
            if img:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}})
            results.append({"type": "tool_result", "tool_use_id": c["id"], "content": blocks})
        self.history.append({"role": "user", "content": results})
        return True

    def step_openai(self):
        base = self.cfg["base_url"].rstrip("/")
        msgs = [{"role": "system", "content": self.system_prompt()}] + self.history
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {self.cfg['api_key']}", "Content-Type": "application/json"},
            json={"model": self.cfg["model"], "messages": msgs,
                  "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                               "parameters": t["schema"]}} for t in TOOLS]},
            timeout=180)
        if r.status_code != 200:
            raise RuntimeError(f"API {r.status_code}: {r.text}")
        msg = r.json()["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        entry = {"role": "assistant", "content": msg.get("content") or ""}
        if calls:
            entry["tool_calls"] = calls
        self.history.append(entry)
        if (msg.get("content") or "").strip():
            self.log("assistant", msg["content"])
        if not calls:
            return False
        images = []
        for c in calls:
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except Exception:
                args = {}
            text, img = self.execute(c["function"]["name"], args)
            self.history.append({"role": "tool", "tool_call_id": c["id"], "content": text})
            if img:
                images.append(img)
        for img in images:
            self.history.append({"role": "user", "content": [
                {"type": "text", "text": "Screenshot result:"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img}}]})
        return True

    def run(self, user_text):
        self.history.append({"role": "user", "content": user_text})
        step = self.step_anthropic if self.cfg["provider"] == "Anthropic" else self.step_openai
        for _ in range(MAX_STEPS):
            if self.should_stop():
                self.log("system", "متوقف شد.")
                return
            if not step():
                return
        self.log("system", f"به سقف {MAX_STEPS} مرحله رسید؛ متوقف شد.")


class Worker(QThread):
    log = pyqtSignal(str, str)
    approve = pyqtSignal(str)
    finished_ok = pyqtSignal()

    def __init__(self, agent, text):
        super().__init__()
        self.agent, self.text = agent, text
        self.stop_flag = False
        self._evt = threading.Event()
        self._answer = False
        agent.log = lambda k, t: self.log.emit(k, t)
        agent.ask = self._ask
        agent.should_stop = lambda: self.stop_flag

    def _ask(self, desc):
        self._evt.clear()
        self.approve.emit(desc)
        self._evt.wait()
        return self._answer

    def answer(self, ok):
        self._answer = ok
        self._evt.set()

    def run(self):
        try:
            self.agent.run(self.text)
        except Exception as e:
            self.log.emit("error", f"{type(e).__name__}: {e}")
        self.finished_ok.emit()


class Main(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Desktop Agent")
        self.resize(900, 700)
        cfg = load_config()
        self.agent = None
        self.worker = None

        self.provider = QComboBox()
        self.provider.addItems(["Anthropic", "OpenAI-compatible"])
        self.provider.setCurrentText(cfg.get("provider", "Anthropic"))
        self.base_url = QLineEdit(cfg.get("base_url", "https://api.openai.com/v1"))
        self.api_key = QLineEdit(cfg.get("api_key", ""))
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("API Key")
        self.model = QLineEdit(cfg.get("model", "claude-sonnet-5-5"))
        self.mode = QComboBox()
        self.mode.addItems([MODE_ASK, MODE_SMART, MODE_FULL])
        self.mode.setCurrentText(cfg.get("mode", MODE_ASK))

        top = QHBoxLayout()
        for lbl, w in (("Provider", self.provider), ("Base URL", self.base_url), ("Key", self.api_key),
                       ("Model", self.model)):
            top.addWidget(QLabel(lbl))
            top.addWidget(w)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("حالت اجرا:"))
        mode_row.addWidget(self.mode, 1)

        self.chat = QTextEdit()
        self.chat.setReadOnly(True)
        self.input = QTextEdit()
        self.input.setFixedHeight(90)
        self.input.setPlaceholderText("کارت رو بنویس... (Ctrl+Enter برای ارسال)")
        self.send_btn = QPushButton("ارسال")
        self.stop_btn = QPushButton("توقف")
        self.new_btn = QPushButton("چت جدید")
        self.stop_btn.setEnabled(False)
        btns = QVBoxLayout()
        for b in (self.send_btn, self.stop_btn, self.new_btn):
            btns.addWidget(b)
        bottom = QHBoxLayout()
        bottom.addWidget(self.input, 1)
        bottom.addLayout(btns)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(mode_row)
        lay.addWidget(self.chat, 1)
        lay.addLayout(bottom)

        self.send_btn.clicked.connect(self.send)
        self.stop_btn.clicked.connect(self.stop)
        self.new_btn.clicked.connect(self.new_chat)
        self.provider.currentTextChanged.connect(self.sync_ui)
        self.sync_ui()

    def sync_ui(self):
        self.base_url.setEnabled(self.provider.currentText() != "Anthropic")

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.send()
        else:
            super().keyPressEvent(e)

    def cfg(self):
        return {"provider": self.provider.currentText(), "base_url": self.base_url.text().strip(),
                "api_key": self.api_key.text().strip(), "model": self.model.text().strip(),
                "mode": self.mode.currentText()}

    def add(self, kind, text):
        colors = {"user": "#4da3ff", "assistant": "#e6e6e6", "tool": "#e0b040", "error": "#ff6060",
                  "system": "#888888"}
        prefix = {"user": "شما", "assistant": "ایجنت", "tool": "🔧", "error": "خطا", "system": "•"}[kind]
        body = html.escape(text).replace("\n", "<br>")
        self.chat.append(f'<div dir="auto" style="color:{colors[kind]}; margin:6px 0;"><b>{prefix}:</b> {body}</div>')

    def send(self):
        text = self.input.toPlainText().strip()
        if not text or (self.worker and self.worker.isRunning()):
            return
        cfg = self.cfg()
        if not cfg["api_key"]:
            QMessageBox.warning(self, "API Key", "API Key رو وارد کن.")
            return
        save_config(cfg)
        # keep history across turns; rebuild agent only if provider changed or new chat
        if self.agent is None or self.agent.cfg["provider"] != cfg["provider"]:
            self.agent = Agent(cfg, None, None, None)
        else:
            self.agent.cfg = cfg
        self.input.clear()
        self.add("user", text)
        self.worker = Worker(self.agent, text)
        self.worker.log.connect(self.add)
        self.worker.approve.connect(self.on_approve)
        self.worker.finished_ok.connect(self.on_done)
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.worker.start()

    def on_approve(self, desc):
        r = QMessageBox.question(self, "تأیید عملیات", f"ایجنت می‌خواد این کار رو انجام بده:\n\n{desc}\n\nاجازه می‌دی؟")
        self.worker.answer(r == QMessageBox.StandardButton.Yes)

    def on_done(self):
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def stop(self):
        if self.worker:
            self.worker.stop_flag = True
            self.worker.answer(False)

    def new_chat(self):
        if self.worker and self.worker.isRunning():
            return
        self.agent = None
        self.chat.clear()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = Main()
    w.show()
    sys.exit(app.exec())
