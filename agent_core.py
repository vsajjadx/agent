"""
هستهٔ ایجنت (بدون GUI): ابزارها، پروویدرها (Anthropic / Gemini / OpenAI-compatible) و حلقهٔ tool-use.

تاریخچهٔ گفتگو به‌صورت «خنثی» (provider-neutral) نگه داشته می‌شود تا بشه وسط چت مدل/پروویدر
رو عوض کرد و چت‌ها رو ذخیره/بازیابی کرد:

  {"role": "user",      "content": [{"type": "text", "text": ...}, {"type": "image", "mime": ..., "data": b64}]}
  {"role": "assistant", "content": [{"type": "text", ...}, {"type": "tool_use", "id", "name", "input"}]}
  {"role": "user",      "content": [{"type": "tool_result", "id", "name", "text", "images": [{"mime","data"}]}]}
"""
import copy
import fnmatch
import glob
import html as _html
import json
import os
import platform
import re
import signal
import subprocess
import time
import uuid
from datetime import datetime

import requests

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
CREATE_NO_WINDOW = 0x08000000
MAX_STEPS = 60
MAX_OUT = 20000
KEEP_SCREENSHOTS = 3

MODE_ASK = "هر عملیات تأیید بخواد"
MODE_SMART = "خودکار (فقط دستورات خطرناک تأیید بخوان)"
MODE_FULL = "کاملاً خودکار (بدون تأیید!)"

PROVIDERS = {
    "Anthropic": {
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-5-5",
        "models": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"],
        "env": ["ANTHROPIC_API_KEY"],
    },
    "Gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-3.8-flash",
        "models": ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.1-pro-preview",
                   "gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"],
        "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    },
    "OpenAI-compatible": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5",
        "models": ["gpt-5", "gpt-4.1"],
        "env": ["OPENAI_API_KEY"],
    },
}

DANGEROUS = re.compile(
    r"(\brm\b|\bdel\b|\berase\b|\brmdir\b|\brd\s|remove-item|format\s+[a-z]:|diskpart|shutdown|"
    r"restart-computer|reg\s+delete|bcdedit|mkfs|dd\s+if=|cipher\s+/w|net\s+user|takeown|icacls|"
    r"set-executionpolicy|invoke-expression|\biex\b|curl.*\|\s*(sh|bash)|irm.*\|\s*iex|taskkill|stop-process|"
    r"git\s+reset\s+--hard|git\s+clean|git\s+push\s+.*--force)",
    re.I)

TOOLS = [
    {"name": "run_command",
     "description": "Run a shell command (PowerShell on Windows, bash otherwise) and return stdout/stderr.",
     "schema": {"type": "object", "properties": {
         "command": {"type": "string"},
         "cwd": {"type": "string", "description": "optional working dir (default: the chat's working folder)"},
         "timeout": {"type": "integer", "description": "seconds, default 60, max 600"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read a text file (optionally only a line range).",
     "schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
         "required": ["path"]}},
    {"name": "write_file", "description": "Create/overwrite a text file (parent dirs are created).",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"]}},
    {"name": "edit_file",
     "description": "Replace an exact occurrence of old with new in a file. old must be unique unless replace_all is true.",
     "schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
         "replace_all": {"type": "boolean"}}, "required": ["path", "old", "new"]}},
    {"name": "list_dir", "description": "List files and folders in a directory.",
     "schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "find_files",
     "description": "Find files by glob pattern, e.g. '**/*.py' (relative to path or the working folder).",
     "schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                "required": ["pattern"]}},
    {"name": "search_files",
     "description": "Search text files for a regex (like grep -rn). Returns path:line:text.",
     "schema": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "path": {"type": "string"},
         "file_glob": {"type": "string", "description": "e.g. '*.py' (default all)"},
         "ignore_case": {"type": "boolean"}}, "required": ["pattern"]}},
    {"name": "fetch_url", "description": "HTTP GET a URL and return the page as text (truncated).",
     "schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "screenshot",
     "description": "Take a screenshot of the screen. Mouse coordinates refer to this image.",
     "schema": {"type": "object", "properties": {}}},
    {"name": "mouse_move", "description": "Move the mouse to x,y (screenshot coordinates).",
     "schema": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"]}},
    {"name": "mouse_click", "description": "Click at x,y (in screenshot coordinates).",
     "schema": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "double": {"type": "boolean"}}, "required": ["x", "y"]}},
    {"name": "mouse_drag", "description": "Drag with the left button from (x1,y1) to (x2,y2).",
     "schema": {"type": "object", "properties": {
         "x1": {"type": "integer"}, "y1": {"type": "integer"},
         "x2": {"type": "integer"}, "y2": {"type": "integer"}}, "required": ["x1", "y1", "x2", "y2"]}},
    {"name": "type_text",
     "description": "Type text into the focused control (supports Unicode/Persian via clipboard paste).",
     "schema": {"type": "object", "properties": {"text": {"type": "string"},
                                                  "enter": {"type": "boolean", "description": "press Enter after"}},
                "required": ["text"]}},
    {"name": "press_key", "description": "Press a key or combo, e.g. 'enter', 'ctrl+c', 'win+r', 'alt+tab'.",
     "schema": {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}},
    {"name": "scroll", "description": "Scroll the mouse wheel; positive = up, negative = down.",
     "schema": {"type": "object", "properties": {"amount": {"type": "integer"}}, "required": ["amount"]}},
    {"name": "wait", "description": "Wait N seconds (max 10), e.g. for an app to open.",
     "schema": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
SAFE_TOOLS = {"read_file", "list_dir", "find_files", "search_files", "fetch_url", "screenshot", "mouse_move", "wait"}

KEY_ALIASES = {"return": "enter", "esc": "esc", "escape": "esc", "windows": "win", "super": "win", "cmd": "win",
               "control": "ctrl", "del": "delete", "pgup": "pageup", "pgdn": "pagedown"}


class Stopped(Exception):
    pass


# ======================================================================================
# helpers
# ======================================================================================
def trunc(s, n=MAX_OUT):
    return s if len(s) <= n else s[:n // 2] + "\n...[truncated]...\n" + s[-n // 2:]


def html_to_text(s):
    s = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", s)
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|section|article)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def _kill(p):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _err_text(r):
    try:
        j = r.json()
        if isinstance(j, list) and j:
            j = j[0]
        e = j.get("error", j)
        if isinstance(e, dict):
            return e.get("message") or json.dumps(e, ensure_ascii=False)
        return str(e)
    except Exception:
        return r.text[:1000]


def resolve_cfg(cfg):
    """Flat per-provider config (as stored by the GUI) -> dict the Agent understands."""
    prov = cfg.get("provider", "Gemini")
    d = PROVIDERS.get(prov, PROVIDERS["OpenAI-compatible"])
    p = (cfg.get("providers") or {}).get(prov, {})
    key = (p.get("api_key") or "").strip().strip("\"'")
    if not key:
        for env in d["env"]:
            if os.environ.get(env):
                key = os.environ[env].strip()
                break
    return {"provider": prov, "api_key": key,
            "base_url": (p.get("base_url") or d["base_url"]).strip().rstrip("/"),
            "model": (p.get("model") or d["model"]).strip(),
            "mode": cfg.get("mode", MODE_ASK), "cwd": cfg.get("cwd") or os.path.expanduser("~"),
            "instructions": cfg.get("instructions", "")}


# ======================================================================================
# chat persistence
# ======================================================================================
CHAT_DIR = os.path.join(os.path.expanduser("~"), ".ai_agent", "chats")


def strip_for_save(history):
    h = copy.deepcopy(history)
    for m in h:
        for b in m["content"]:
            if b["type"] == "tool_result" and b.get("images"):
                b["images"] = []
                b["text"] = (b.get("text") or "") + "\n[screenshot not stored]"
    return h


def save_chat(chat_id, title, history, extra=None):
    os.makedirs(CHAT_DIR, exist_ok=True)
    data = {"id": chat_id, "title": title, "updated": time.time(), "history": strip_for_save(history)}
    data.update(extra or {})
    tmp = os.path.join(CHAT_DIR, chat_id + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(CHAT_DIR, chat_id + ".json"))


def list_chats():
    out = []
    for fn in glob.glob(os.path.join(CHAT_DIR, "*.json")):
        try:
            with open(fn, "r", encoding="utf-8") as f:
                d = json.load(f)
            out.append({"id": d["id"], "title": d.get("title") or "چت", "updated": d.get("updated", 0)})
        except Exception:
            continue
    return sorted(out, key=lambda x: -x["updated"])


def load_chat(chat_id):
    with open(os.path.join(CHAT_DIR, chat_id + ".json"), "r", encoding="utf-8") as f:
        return json.load(f)


def delete_chat(chat_id):
    try:
        os.remove(os.path.join(CHAT_DIR, chat_id + ".json"))
    except OSError:
        pass


# ======================================================================================
# model discovery / connection test
# ======================================================================================
def list_models(cfg):
    prov, base, key = cfg["provider"], cfg["base_url"], cfg["api_key"]
    if prov == "Anthropic":
        r = requests.get(f"{base}/v1/models?limit=100", headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                         timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"API {r.status_code}: {_err_text(r)}")
        return [m["id"] for m in r.json().get("data", [])]
    if prov == "Gemini":
        names, token = [], None
        for _ in range(5):
            r = requests.get(f"{base}/models", params={"pageSize": 200, **({"pageToken": token} if token else {})},
                             headers={"x-goog-api-key": key}, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"API {r.status_code}: {_err_text(r)}")
            j = r.json()
            for m in j.get("models", []):
                if "generateContent" in (m.get("supportedGenerationMethods") or []):
                    names.append(m["name"].replace("models/", "", 1))
            token = j.get("nextPageToken")
            if not token:
                break
        return names
    r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"API {r.status_code}: {_err_text(r)}")
    return [m["id"] for m in r.json().get("data", [])]


def ping(cfg):
    """Small request that proves key + model + endpoint work. Returns the model's reply text."""
    a = Agent(dict(cfg, mode=MODE_FULL))
    a.history = [{"role": "user", "content": [{"type": "text", "text": "Reply with the single word: OK"}]}]
    msg = a.call_model(use_tools=False)
    return "".join(b["text"] for b in msg["content"] if b["type"] == "text").strip() or "(empty reply)"


# ======================================================================================
# the agent
# ======================================================================================
class Agent:
    """Provider-agnostic tool loop. Meant to run in a worker thread."""

    def __init__(self, cfg, log=None, ask=None, should_stop=None):
        self.cfg = cfg
        self.log = log or (lambda kind, text: None)
        self.ask = ask or (lambda desc: "yes")          # returns "yes" | "all" | "no"
        self.should_stop = should_stop or (lambda: False)
        self.history = []
        self.scale = 1.0
        self.auto_all = False
        self.gemini_img_mode = "same_turn"

    # ---------- prompt ----------
    def cwd(self):
        c = os.path.expanduser(self.cfg.get("cwd") or "~")
        return c if os.path.isdir(c) else os.path.expanduser("~")

    def system_prompt(self):
        s = (
            "You are an autonomous desktop agent (like Claude Desktop) with access to the user's computer through tools. "
            f"OS: {platform.platform()}. Home: {os.path.expanduser('~')}. Working folder: {self.cwd()} "
            f"(relative paths resolve there). Now: {datetime.now():%Y-%m-%d %H:%M}. "
            f"Shell: {'PowerShell' if IS_WIN else 'bash'}.\n"
            "Work step by step, use the tools to actually do the task, verify the results, and finish with a short "
            "summary. Prefer shell/file tools over GUI control; take a screenshot before any mouse action and re-check "
            "after it. Never do destructive things (deleting data, formatting, killing system processes) unless the "
            "user clearly asked for them. Reply in the user's language; format answers with Markdown."
        )
        if self.cfg.get("instructions", "").strip():
            s += "\n\nUser's custom instructions:\n" + self.cfg["instructions"].strip()
        return s

    # ---------- tool execution ----------
    def needs_approval(self, name, args):
        mode = self.cfg.get("mode", MODE_ASK)
        if mode == MODE_FULL or self.auto_all or name in SAFE_TOOLS:
            return False
        if mode == MODE_ASK:
            return True
        return name == "run_command" and bool(DANGEROUS.search(args.get("command", "")))

    def execute(self, name, args):
        desc = f"{name}: {json.dumps(args, ensure_ascii=False)[:600]}"
        self.log("tool", desc)
        if name not in TOOL_NAMES:
            return f"Error: unknown tool '{name}'.", None
        if self.needs_approval(name, args):
            ans = self.ask(desc)
            if ans == "all":
                self.auto_all = True
            elif ans != "yes":
                return "User DENIED this action.", None
        self.log("status", f"در حال اجرا: {name}")
        try:
            text, img = getattr(self, "t_" + name)(**args)
        except Exception as e:
            text, img = f"Error: {type(e).__name__}: {e}", None
        self.log("result", text if len(text) <= 600 else text[:600] + " …")
        return text, img

    def P(self, path):
        p = os.path.expanduser(path or ".")
        return p if os.path.isabs(p) else os.path.join(self.cwd(), p)

    def t_run_command(self, command, cwd=None, timeout=60):
        timeout = max(1, min(int(timeout or 60), 600))
        wd = self.P(cwd) if cwd else self.cwd()
        if not os.path.isdir(wd):
            return f"Error: working directory does not exist: {wd}", None
        if IS_WIN:
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                   "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command]
            kw = {"creationflags": CREATE_NO_WINDOW}
        else:
            cmd = ["bash", "-lc", command]
            kw = {"start_new_session": True}
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             cwd=wd, text=True, encoding="utf-8", errors="replace", **kw)
        start, note = time.time(), ""
        while True:
            try:
                out, err = p.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if self.should_stop():
                    note = "Stopped by user."
                elif time.time() - start > timeout:
                    note = f"TIMEOUT after {timeout}s - process killed."
                if note:
                    _kill(p)
                    try:
                        out, err = p.communicate(timeout=5)
                    except Exception:
                        out, err = "", ""
                    break
        res = f"exit code: {p.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
        return trunc((note + "\n" if note else "") + res), None

    def t_read_file(self, path, start_line=None, end_line=None):
        with open(self.P(path), "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if start_line or end_line:
            lines = text.splitlines()
            a, b = max(1, int(start_line or 1)), int(end_line or len(lines))
            text = "\n".join(f"{i}: {l}" for i, l in enumerate(lines[a - 1:b], start=a))
        return trunc(text), None

    def t_write_file(self, path, content):
        path = self.P(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return f"Wrote {len(content)} chars to {path}", None

    def t_edit_file(self, path, old, new, replace_all=False):
        path = self.P(path)
        with open(path, "r", encoding="utf-8", newline="") as f:
            data = f.read()
        n = data.count(old)
        if n == 0:
            return "Error: 'old' text not found.", None
        if n > 1 and not replace_all:
            return f"Error: 'old' occurs {n} times; make it unique or pass replace_all=true.", None
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(data.replace(old, new))
        return f"Edited ({n} replacement{'s' if n > 1 else ''}).", None

    def t_list_dir(self, path):
        path = self.P(path)
        items = []
        for n in sorted(os.listdir(path))[:500]:
            items.append(n + ("/" if os.path.isdir(os.path.join(path, n)) else ""))
        return "\n".join(items) or "(empty)", None

    def t_find_files(self, pattern, path=None):
        base = self.P(path) if path else self.cwd()
        res = glob.glob(os.path.join(base, pattern), recursive=True)
        res = [os.path.relpath(r, base) + ("/" if os.path.isdir(r) else "") for r in sorted(res)]
        more = f"\n...({len(res) - 300} more)" if len(res) > 300 else ""
        return ("\n".join(res[:300]) + more) or "(no matches)", None

    def t_search_files(self, pattern, path=None, file_glob="*", ignore_case=False):
        base = self.P(path) if path else self.cwd()
        rx = re.compile(pattern, re.I if ignore_case else 0)
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
        hits = []
        files = [base] if os.path.isfile(base) else None
        if files is None:
            files = []
            for root, dirs, fns in os.walk(base):
                dirs[:] = [d for d in dirs if d not in skip]
                files += [os.path.join(root, fn) for fn in fns if fnmatch.fnmatch(fn, file_glob or "*")]
        for fp in files:
            try:
                if os.path.getsize(fp) > 2_000_000:
                    continue
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append(f"{os.path.relpath(fp, base)}:{i}:{line.strip()[:200]}")
                            if len(hits) >= 200:
                                return "\n".join(hits) + "\n...(stopped at 200 matches)", None
            except Exception:
                continue
        return "\n".join(hits) or "(no matches)", None

    def t_fetch_url(self, url):
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        body = r.text
        if "html" in r.headers.get("content-type", "").lower():
            body = html_to_text(body)
        return trunc(f"HTTP {r.status_code}\n{body}"), None

    def t_screenshot(self):
        import io
        import base64
        import pyautogui
        img = pyautogui.screenshot().convert("RGB")
        w, h = img.size
        nw = min(w, 1280)
        nh = int(h * nw / w)
        self.scale = w / nw
        img = img.resize((nw, nh))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        return f"Screenshot {nw}x{nh}.", {"mime": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}

    def _xy(self, x, y):
        return int(x * self.scale), int(y * self.scale)

    def t_mouse_move(self, x, y):
        import pyautogui
        pyautogui.moveTo(*self._xy(x, y))
        return f"Moved to ({x},{y})", None

    def t_mouse_click(self, x, y, button="left", double=False):
        import pyautogui
        X, Y = self._xy(x, y)
        pyautogui.click(X, Y, clicks=2 if double else 1, button=button)
        time.sleep(0.3)
        return f"Clicked ({x},{y}) [{button}{', double' if double else ''}]", None

    def t_mouse_drag(self, x1, y1, x2, y2):
        import pyautogui
        pyautogui.moveTo(*self._xy(x1, y1))
        pyautogui.dragTo(*self._xy(x2, y2), duration=0.5, button="left")
        return f"Dragged ({x1},{y1}) -> ({x2},{y2})", None

    def t_type_text(self, text, enter=False):
        import pyautogui
        import pyperclip
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("command" if IS_MAC else "ctrl", "v")
        if enter:
            time.sleep(0.1)
            pyautogui.press("enter")
        return f"Typed {len(text)} chars.", None

    def t_press_key(self, keys):
        import pyautogui
        ks = [k.strip().lower() for k in keys.split("+") if k.strip()]
        ks = [KEY_ALIASES.get(k, k) for k in ks]
        pyautogui.hotkey(*ks)
        return f"Pressed {keys}", None

    def t_scroll(self, amount):
        import pyautogui
        pyautogui.scroll(int(amount))
        return "Scrolled.", None

    def t_wait(self, seconds):
        end = time.time() + max(0, min(float(seconds), 10))
        while time.time() < end and not self.should_stop():
            time.sleep(0.1)
        return "Waited.", None

    # ---------- HTTP with retry ----------
    def _post(self, url, headers, payload, timeout=180):
        r = None
        for attempt in range(4):
            if self.should_stop():
                raise Stopped()
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                try:
                    wait = min(float(r.headers.get("Retry-After", "")), 30)
                except ValueError:
                    wait = 3 * (attempt + 1)
                self.log("system", f"خطای موقت {r.status_code}؛ تلاش مجدد تا {int(wait)} ثانیه دیگه…")
                end = time.time() + wait
                while time.time() < end:
                    if self.should_stop():
                        raise Stopped()
                    time.sleep(0.2)
                continue
            break
        return r

    # ---------- providers ----------
    def call_model(self, use_tools=True):
        prov = self.cfg["provider"]
        if not self.cfg.get("api_key"):
            raise RuntimeError("API Key وارد نشده.")
        if prov == "Anthropic":
            return self._anthropic(use_tools)
        if prov == "Gemini":
            return self._gemini(use_tools)
        return self._openai(use_tools)

    # ----- Anthropic
    @staticmethod
    def _txt(s):
        return s if s and s.strip() else "(empty)"

    def _to_anthropic(self):
        msgs = []
        for m in self.history:
            out = []
            for b in m["content"]:
                t = b["type"]
                if t == "text":
                    if b["text"].strip():
                        out.append({"type": "text", "text": b["text"]})
                elif t == "image":
                    out.append({"type": "image", "source": {"type": "base64", "media_type": b["mime"],
                                                            "data": b["data"]}})
                elif t == "tool_use":
                    out.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b["input"]})
                elif t == "tool_result":
                    inner = [{"type": "text", "text": self._txt(b.get("text"))}]
                    for im in b.get("images", []):
                        inner.append({"type": "image", "source": {"type": "base64", "media_type": im["mime"],
                                                                  "data": im["data"]}})
                    out.append({"type": "tool_result", "tool_use_id": b["id"], "content": inner})
            if out:
                msgs.append({"role": m["role"], "content": out})
        return msgs

    def _anthropic(self, use_tools):
        payload = {"model": self.cfg["model"], "max_tokens": 8192, "system": self.system_prompt(),
                   "messages": self._to_anthropic()}
        if use_tools:
            payload["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
                                for t in TOOLS]
        r = self._post(f"{self.cfg['base_url']}/v1/messages",
                       {"x-api-key": self.cfg["api_key"], "anthropic-version": "2023-06-01",
                        "content-type": "application/json"}, payload)
        if r.status_code != 200:
            raise RuntimeError(f"API {r.status_code}: {_err_text(r)}")
        blocks = []
        for b in r.json().get("content", []):
            if b["type"] == "text":
                blocks.append({"type": "text", "text": b["text"]})
            elif b["type"] == "tool_use":
                blocks.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b.get("input") or {}})
        return {"role": "assistant", "content": blocks}

    # ----- OpenAI-compatible
    def _to_openai(self):
        msgs = [{"role": "system", "content": self.system_prompt()}]
        for m in self.history:
            if m["role"] == "assistant":
                text = "".join(b["text"] for b in m["content"] if b["type"] == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)}}
                         for b in m["content"] if b["type"] == "tool_use"]
                e = {"role": "assistant", "content": text}
                if calls:
                    e["tool_calls"] = calls
                if text or calls:
                    msgs.append(e)
                continue
            parts, imgs = [], []
            for b in m["content"]:
                if b["type"] == "text" and b["text"].strip():
                    parts.append({"type": "text", "text": b["text"]})
                elif b["type"] == "image":
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{b['mime']};base64,{b['data']}"}})
                elif b["type"] == "tool_result":
                    msgs.append({"role": "tool", "tool_call_id": b["id"], "content": self._txt(b.get("text"))})
                    imgs += b.get("images", [])
            if imgs:
                parts.append({"type": "text", "text": "Screenshot result:"})
                parts += [{"type": "image_url", "image_url": {"url": f"data:{i['mime']};base64,{i['data']}"}}
                          for i in imgs]
            if parts:
                msgs.append({"role": "user", "content": parts})
        return msgs

    def _openai(self, use_tools):
        payload = {"model": self.cfg["model"], "messages": self._to_openai()}
        if use_tools:
            payload["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                  "parameters": t["schema"]}} for t in TOOLS]
        r = self._post(f"{self.cfg['base_url']}/chat/completions",
                       {"Authorization": f"Bearer {self.cfg['api_key']}", "Content-Type": "application/json"}, payload)
        if r.status_code != 200:
            raise RuntimeError(f"API {r.status_code}: {_err_text(r)}")
        msg = r.json()["choices"][0]["message"]
        blocks = []
        if isinstance(msg.get("content"), str) and msg["content"].strip():
            blocks.append({"type": "text", "text": msg["content"]})
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args})
        return {"role": "assistant", "content": blocks}

    # ----- Gemini
    @staticmethod
    def _gem_schema(s):
        """JSON-schema subset -> Gemini OpenAPI subset (upper-case types)."""
        o = {"type": str(s.get("type", "string")).upper()}
        if "description" in s:
            o["description"] = s["description"]
        if "enum" in s:
            o["enum"] = s["enum"]
        if "properties" in s:
            o["properties"] = {k: Agent._gem_schema(v) for k, v in s["properties"].items()}
        if s.get("required"):
            o["required"] = s["required"]
        if "items" in s:
            o["items"] = Agent._gem_schema(s["items"])
        return o

    def _gem_tools(self):
        decls = []
        for t in TOOLS:
            d = {"name": t["name"], "description": t["description"]}
            if t["schema"].get("properties"):      # Gemini rejects an OBJECT with empty properties
                d["parameters"] = self._gem_schema(t["schema"])
            decls.append(d)
        return [{"functionDeclarations": decls}]

    def _to_gemini(self):
        mode = self.gemini_img_mode
        need_dummy = bool(re.search(r"gemini-[34]", self.cfg["model"]))
        contents = []
        for m in self.history:
            if m["role"] == "assistant":
                if m.get("gemini_parts"):
                    parts = copy.deepcopy(m["gemini_parts"])          # exact replay keeps thought signatures
                else:
                    parts, first_call = [], True
                    for b in m["content"]:
                        if b["type"] == "text" and b["text"].strip():
                            parts.append({"text": b["text"]})
                        elif b["type"] == "tool_use":
                            fc = {"name": b["name"], "args": b["input"]}
                            if b.get("gid"):
                                fc["id"] = b["id"]
                            p = {"functionCall": fc}
                            sig = b.get("sig") or ("skip_thought_signature_validator" if need_dummy and first_call
                                                   else None)
                            if sig:
                                p["thoughtSignature"] = sig
                            first_call = False
                            parts.append(p)
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue
            parts, imgs = [], []
            for b in m["content"]:
                if b["type"] == "text" and b["text"].strip():
                    parts.append({"text": b["text"]})
                elif b["type"] == "image":
                    parts.append({"inlineData": {"mimeType": b["mime"], "data": b["data"]}})
                elif b["type"] == "tool_result":
                    fr = {"name": b["name"], "response": {"result": self._txt(b.get("text"))}}
                    if b.get("gid"):
                        fr["id"] = b["id"]
                    ims = b.get("images", [])
                    if ims and mode == "nested":
                        fr["parts"] = [{"inlineData": {"mimeType": i["mime"], "data": i["data"]}} for i in ims]
                    else:
                        imgs += ims
                    parts.append({"functionResponse": fr})
            extra = [{"inlineData": {"mimeType": i["mime"], "data": i["data"]}} for i in imgs]
            if extra and mode == "separate_turn":
                contents.append({"role": "user", "parts": parts})
                contents.append({"role": "user", "parts": [{"text": "Screenshot result:"}] + extra})
                continue
            if parts or extra:
                contents.append({"role": "user", "parts": parts + extra})
        return contents

    def _gemini(self, use_tools):
        model = self.cfg["model"].replace("models/", "", 1)
        url = f"{self.cfg['base_url']}/models/{model}:generateContent"
        headers = {"x-goog-api-key": self.cfg["api_key"], "Content-Type": "application/json"}
        modes = ["same_turn", "nested", "separate_turn"]
        malformed = 0
        while True:
            payload = {"systemInstruction": {"parts": [{"text": self.system_prompt()}]},
                       "contents": self._to_gemini()}
            if use_tools:
                payload["tools"] = self._gem_tools()
            r = self._post(url, headers, payload)
            if r.status_code == 400 and self._has_images() and self.gemini_img_mode != modes[-1]:
                # some models want screenshots in a different place; fall back to the next layout and retry
                self.gemini_img_mode = modes[modes.index(self.gemini_img_mode) + 1]
                continue
            if r.status_code != 200:
                raise RuntimeError(f"API {r.status_code}: {_err_text(r)}")
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                raise RuntimeError(f"Gemini returned no answer: {data.get('promptFeedback') or data}")
            cand = cands[0]
            parts = (cand.get("content") or {}).get("parts") or []
            if not parts and cand.get("finishReason") == "MALFORMED_FUNCTION_CALL" and malformed < 2:
                malformed += 1
                continue
            if not parts and cand.get("finishReason") not in (None, "STOP"):
                raise RuntimeError(f"Gemini stopped: {cand.get('finishReason')} {cand.get('finishMessage', '')}")
            break
        blocks = []
        for p in parts:
            if "functionCall" in p:
                fc = p["functionCall"]
                blocks.append({"type": "tool_use", "id": fc.get("id") or "call_" + uuid.uuid4().hex[:16],
                               "name": fc["name"], "input": fc.get("args") or {}, "gid": bool(fc.get("id")),
                               "sig": p.get("thoughtSignature") or p.get("thought_signature")})
            elif p.get("text") and not p.get("thought"):
                blocks.append({"type": "text", "text": p["text"]})
        msg = {"role": "assistant", "content": blocks}
        if any(b["type"] == "tool_use" for b in blocks):
            msg["gemini_parts"] = parts
        return msg

    def _has_images(self):
        return any(b["type"] == "tool_result" and b.get("images") for m in self.history for b in m["content"])

    # ---------- history management ----------
    def prune_images(self):
        seen = 0
        for m in reversed(self.history):
            for b in m["content"]:
                if b["type"] == "tool_result" and b.get("images"):
                    seen += 1
                    if seen > KEEP_SCREENSHOTS:
                        b["images"] = []
                        b["text"] = (b.get("text") or "") + "\n[older screenshot removed]"

    def repair_history(self):
        """If a previous run died between a tool_use and its result, close the gap so the API accepts the history."""
        if not self.history:
            return
        last = self.history[-1]
        if last["role"] == "assistant":
            calls = [b for b in last["content"] if b["type"] == "tool_use"]
            if calls:
                self.history.append({"role": "user", "content": [
                    {"type": "tool_result", "id": c["id"], "name": c["name"], "gid": c.get("gid", False),
                     "text": "Interrupted.", "images": []} for c in calls]})

    # ---------- main loop ----------
    def run(self, user_text, images=()):
        self.repair_history()
        content = [{"type": "image", "mime": i["mime"], "data": i["data"]} for i in images]
        if user_text.strip():
            content.append({"type": "text", "text": user_text})
        self.history.append({"role": "user", "content": content})
        try:
            for _ in range(MAX_STEPS):
                if self.should_stop():
                    self.log("system", "متوقف شد.")
                    return
                self.log("status", "در حال فکر کردن…")
                msg = self.call_model()
                self.history.append(msg)
                for b in msg["content"]:
                    if b["type"] == "text" and b["text"].strip():
                        self.log("assistant", b["text"])
                calls = [b for b in msg["content"] if b["type"] == "tool_use"]
                if not calls:
                    return
                results = []
                for c in calls:
                    if self.should_stop():
                        text, img = "Stopped by user.", None
                    else:
                        text, img = self.execute(c["name"], c["input"])
                    results.append({"type": "tool_result", "id": c["id"], "name": c["name"],
                                    "gid": c.get("gid", False), "text": text, "images": [img] if img else []})
                self.history.append({"role": "user", "content": results})
                self.prune_images()
            self.log("system", f"به سقف {MAX_STEPS} مرحله رسید؛ متوقف شد.")
        except Stopped:
            self.log("system", "متوقف شد.")
