"""
AI Desktop Agent  -  ایجنت دسکتاپ با کنترل سیستم (شبیه Claude Desktop)

پروویدرها: Google Gemini (بومی، با function calling) ، Anthropic Claude ، هر API سازگار با OpenAI

نصب و اجرا:
    pip install -r requirements.txt
    python ai_agent.py
"""
import base64
import html
import json
import os
import re
import sys
import threading
import uuid

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QImage, QTextBlockFormat, QTextCursor, QTextDocument
from PyQt6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar,
                             QPushButton, QTextBrowser, QTextEdit, QVBoxLayout, QWidget)

import agent_core as core

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".ai_agent_config.json")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

STYLE = """
QWidget { background:#262624; color:#ece9e0; font-size:10pt; }
QLabel { background:transparent; }
QTextEdit, QLineEdit, QPlainTextEdit, QComboBox {
    background:#30302e; border:1px solid #454340; border-radius:8px; padding:6px; color:#ece9e0; }
QTextBrowser { background:#262624; border:none; padding:6px; }
QComboBox QAbstractItemView { background:#30302e; selection-background-color:#4a4845; }
QListWidget { background:#1f1e1d; border:none; padding:4px; }
QListWidget::item { padding:8px; border-radius:6px; }
QListWidget::item:selected { background:#3a3835; color:#ffffff; }
QPushButton { background:#3a3835; border:1px solid #4a4845; border-radius:8px; padding:6px 12px; }
QPushButton:hover { background:#46443f; }
QPushButton:disabled { color:#77746d; background:#2f2e2c; }
QPushButton#send { background:#d97757; color:#ffffff; border:none; font-weight:bold; padding:8px 18px; }
QPushButton#send:disabled { background:#6b4a3c; color:#bbb; }
QProgressBar { border:none; background:#30302e; max-height:3px; }
QProgressBar::chunk { background:#d97757; }
QScrollBar:vertical { background:#262624; width:10px; }
QScrollBar::handle:vertical { background:#4a4845; border-radius:5px; min-height:24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
"""

# ----------------------------------------------------------------------------- config


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if "providers" not in cfg and any(k in cfg for k in ("api_key", "model", "base_url")):   # old flat format
        prov = cfg.get("provider", "Anthropic")
        cfg = {"provider": prov, "mode": cfg.get("mode", core.MODE_ASK),
               "providers": {prov: {k: cfg[k] for k in ("api_key", "base_url", "model") if cfg.get(k)}}}
    cfg.setdefault("provider", "Gemini")
    cfg.setdefault("providers", {})
    cfg.setdefault("mode", core.MODE_ASK)
    cfg.setdefault("cwd", os.path.expanduser("~"))
    cfg.setdefault("instructions", "")
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(CONFIG_PATH, 0o600)       # the file holds API keys
        except OSError:
            pass
    except Exception:
        pass


# ----------------------------------------------------------------------------- helpers
_AR = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LAT = re.compile(r"[A-Za-z]")


def is_rtl(text):
    return len(_AR.findall(text)) > 0.4 * len(_LAT.findall(text)) and bool(_AR.search(text))


def esc(text):
    return html.escape(text).replace("\n", "<br>")


def md_to_html(text):
    d = QTextDocument()
    d.setMarkdown(text)
    if is_rtl(text):
        cur = QTextCursor(d)
        cur.select(QTextCursor.SelectionType.Document)
        f = QTextBlockFormat()
        f.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        cur.mergeBlockFormat(f)
    m = re.search(r"<body[^>]*>(.*)</body>", d.toHtml(), re.S)
    return m.group(1) if m else esc(text)


def qimage_to_block(img, max_side=1568):
    if max(img.width(), img.height()) > max_side:
        img = img.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPEG", 88)
    return {"mime": "image/jpeg", "data": base64.b64encode(bytes(ba.data())).decode()}


def friendly_error(msg):
    low = msg.lower()
    hint = ""
    if "api key not valid" in low or "api_key_invalid" in low or "api 401" in low or "invalid x-api-key" in low \
            or "incorrect api key" in low:
        hint = "کلید API نامعتبره؛ از ⚙ تنظیمات دوباره واردش کن و «تست اتصال» بزن."
    elif "api 403" in low:
        hint = "دسترسی رد شد (کلید اجازهٔ این مدل/منطقه رو نداره یا فعال نشده)."
    elif "api 404" in low:
        hint = "مدل پیدا نشد؛ اسم مدل رو چک کن یا دکمهٔ 🔄 رو بزن تا لیست مدل‌های واقعی بیاد."
    elif "limit: 0" in low:
        hint = ("این مدل توی پلن رایگان اصلاً سهمیه نداره (نه اینکه پر شده باشه). یه مدل Flash مثل gemini-3.8-flash "
                "یا gemini-2.5-flash انتخاب کن، یا توی Google AI Studio پرداخت (Billing) رو فعال کن.")
    elif "api 429" in low or "quota" in low or "resource_exhausted" in low:
        hint = "سقف نرخ/سهمیه پر شده؛ کمی صبر کن یا مدل دیگه‌ای انتخاب کن."
    elif "connection" in low or "timed out" in low or "max retries" in low:
        hint = "اتصال به سرور برقرار نشد؛ اینترنت/VPN/پروکسی و Base URL رو چک کن."
    return msg + (f"\n💡 {hint}" if hint else "")


class BG(QObject):
    """Run a function in a thread and deliver (result, error) to the GUI thread."""
    done = pyqtSignal(object, str)

    def run(self, fn):
        threading.Thread(target=self._t, args=(fn,), daemon=True).start()

    def _t(self, fn):
        try:
            self.done.emit(fn(), "")
        except Exception as e:
            self.done.emit(None, f"{type(e).__name__}: {e}")


# ----------------------------------------------------------------------------- widgets
class Composer(QTextEdit):
    submit = pyqtSignal()
    images = pyqtSignal(list)          # list of QImage

    def __init__(self):
        super().__init__()
        self.setAcceptRichText(False)
        self.setFixedHeight(84)
        self.setPlaceholderText("پیام بنویس…  (Enter = ارسال ، Shift+Enter = خط جدید ، Ctrl+V برای چسباندن تصویر)")

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submit.emit()
            return
        super().keyPressEvent(e)

    def canInsertFromMimeData(self, src):
        return src.hasImage() or src.hasUrls() or super().canInsertFromMimeData(src)

    def insertFromMimeData(self, src):
        imgs = []
        if src.hasUrls():
            for u in src.urls():
                p = u.toLocalFile()
                if p.lower().endswith(IMG_EXT):
                    im = QImage(p)
                    if not im.isNull():
                        imgs.append(im)
        elif src.hasImage() and not src.hasText():
            im = src.imageData()
            if isinstance(im, QImage) and not im.isNull():
                imgs.append(im)
        if imgs:
            self.images.emit(imgs)
        elif src.hasText():
            self.insertPlainText(src.text())


class SettingsDialog(QDialog):
    test_done = pyqtSignal(bool, str)

    def __init__(self, parent, provider, pcfg, instructions):
        super().__init__(parent)
        self.setWindowTitle(f"تنظیمات — {provider}")
        self.resize(560, 420)
        self.provider = provider
        d = core.PROVIDERS[provider]
        self.key = QLineEdit(pcfg.get("api_key", ""))
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("API Key (از " + ("aistudio.google.com/apikey" if provider == "Gemini"
                                                       else "console.anthropic.com" if provider == "Anthropic"
                                                       else "ارائه‌دهنده") + ")")
        self.show_key = QPushButton("نمایش")
        self.show_key.setCheckable(True)
        self.show_key.toggled.connect(lambda on: self.key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        krow = QHBoxLayout()
        krow.addWidget(self.key, 1)
        krow.addWidget(self.show_key)
        self.base = QLineEdit(pcfg.get("base_url", d["base_url"]))
        self.instr = QPlainTextEdit(instructions)
        self.instr.setPlaceholderText("دستورالعمل سفارشی برای ایجنت (مثلاً: همیشه فارسی جواب بده، کدها رو کامل بنویس…)")
        self.test_btn = QPushButton("تست اتصال")
        self.result = QLabel("")
        self.result.setWordWrap(True)
        form = QFormLayout()
        form.addRow("API Key", krow)
        form.addRow("Base URL", self.base)
        form.addRow("دستورالعمل سفارشی", self.instr)
        ok = QPushButton("ذخیره")
        ok.setObjectName("send")
        cancel = QPushButton("انصراف")
        btns = QHBoxLayout()
        btns.addWidget(self.test_btn)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.result)
        lay.addLayout(btns)
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        self.test_btn.clicked.connect(self.run_test)
        self.test_done.connect(self.on_test)
        self.model_for_test = pcfg.get("model") or d["model"]

    def values(self):
        return {"api_key": self.key.text().strip().strip("\"'"), "base_url": self.base.text().strip().rstrip("/")}, \
            self.instr.toPlainText()

    def run_test(self):
        vals, _ = self.values()
        cfg = {"provider": self.provider, "api_key": vals["api_key"], "mode": core.MODE_FULL,
               "base_url": vals["base_url"] or core.PROVIDERS[self.provider]["base_url"], "model": self.model_for_test}
        if not cfg["api_key"]:
            self.result.setText("اول API Key رو وارد کن.")
            return
        self.test_btn.setEnabled(False)
        self.result.setText("در حال تست…")

        def job():
            try:
                self.test_done.emit(True, f"✅ اتصال موفق ({cfg['model']}) — جواب مدل: {core.ping(cfg)}")
            except Exception as e:
                self.test_done.emit(False, "❌ " + friendly_error(f"{type(e).__name__}: {e}"))
        threading.Thread(target=job, daemon=True).start()

    def on_test(self, ok, msg):
        self.test_btn.setEnabled(True)
        self.result.setText(msg)


class Worker(QThread):
    log = pyqtSignal(str, str)
    approve = pyqtSignal(str)
    finished_ok = pyqtSignal()

    def __init__(self, agent, text, images):
        super().__init__()
        self.agent, self.text, self.images = agent, text, images
        self.stop_flag = False
        self._evt = threading.Event()
        self._answer = "no"
        agent.log = lambda k, t: self.log.emit(k, t)
        agent.ask = self._ask
        agent.should_stop = lambda: self.stop_flag

    def _ask(self, desc):
        self._evt.clear()
        if self.stop_flag:
            return "no"
        self.approve.emit(desc)
        self._evt.wait()
        return self._answer

    def answer(self, ans):
        self._answer = ans
        self._evt.set()

    def run(self):
        try:
            self.agent.run(self.text, self.images)
        except Exception as e:
            self.log.emit("error", friendly_error(f"{type(e).__name__}: {e}"))
        self.finished_ok.emit()


# ----------------------------------------------------------------------------- main window
class Main(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Desktop Agent")
        self.resize(1100, 760)
        self.cfg = load_config()
        self.agent = None
        self.worker = None
        self.chat_id = None
        self.pending = []
        self._bg = []
        self._cur_provider = self.cfg["provider"] if self.cfg["provider"] in core.PROVIDERS else "Gemini"

        # ---- sidebar
        self.new_btn = QPushButton("＋  چت جدید")
        self.chats = QListWidget()
        self.del_btn = QPushButton("🗑  حذف چت")
        side = QWidget()
        side.setFixedWidth(250)
        side.setStyleSheet("QWidget{background:#1f1e1d;}")
        sl = QVBoxLayout(side)
        sl.addWidget(self.new_btn)
        sl.addWidget(self.chats, 1)
        sl.addWidget(self.del_btn)

        # ---- top bar
        self.provider = QComboBox()
        self.provider.addItems(list(core.PROVIDERS))
        self.provider.setCurrentText(self._cur_provider)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.refresh_btn = QPushButton("🔄")
        self.refresh_btn.setToolTip("دریافت لیست مدل‌ها از API")
        self.mode = QComboBox()
        self.mode.addItems([core.MODE_ASK, core.MODE_SMART, core.MODE_FULL])
        self.mode.setCurrentText(self.cfg["mode"])
        self.folder_btn = QPushButton()
        self.folder_btn.setToolTip("پوشهٔ کاری ایجنت")
        self.settings_btn = QPushButton("⚙  تنظیمات")
        row1 = QHBoxLayout()
        row1.addWidget(self.provider)
        row1.addWidget(self.model, 1)
        row1.addWidget(self.refresh_btn)
        row1.addWidget(self.mode, 1)
        row2 = QHBoxLayout()
        row2.addWidget(self.folder_btn, 1)
        row2.addWidget(self.settings_btn)

        # ---- chat
        self.chat = QTextBrowser()
        self.chat.setOpenExternalLinks(True)
        self.chat.setPlaceholderText("سلام 👋\nهر کاری روی کامپیوترت داری بنویس؛ ایجنت خودش دستور اجرا می‌کنه، فایل می‌سازه و "
                                     "با موس/کیبورد کار می‌کنه.")
        self.chat.document().setDefaultStyleSheet(
            "pre{background-color:#1a1918;} code{font-family:Consolas,monospace;} a{color:#e8956f;}")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.status = QLabel("")
        self.status.setStyleSheet("color:#9a968c;")

        # ---- input
        self.input = Composer()
        self.attach_btn = QPushButton("📎")
        self.attach_btn.setToolTip("پیوست تصویر")
        self.clear_att = QPushButton()
        self.clear_att.hide()
        self.send_btn = QPushButton("ارسال")
        self.send_btn.setObjectName("send")
        self.stop_btn = QPushButton("توقف")
        self.stop_btn.setEnabled(False)
        right = QVBoxLayout()
        right.addWidget(self.send_btn)
        right.addWidget(self.stop_btn)
        bottom = QHBoxLayout()
        bottom.addWidget(self.attach_btn, 0, Qt.AlignmentFlag.AlignTop)
        bottom.addWidget(self.input, 1)
        bottom.addLayout(right)

        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(12, 10, 12, 12)
        ml.addLayout(row1)
        ml.addLayout(row2)
        ml.addWidget(self.chat, 1)
        ml.addWidget(self.progress)
        ml.addWidget(self.status)
        ml.addWidget(self.clear_att, 0, Qt.AlignmentFlag.AlignLeft)
        ml.addLayout(bottom)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(side)
        root.addWidget(main, 1)

        # ---- wiring
        self.send_btn.clicked.connect(self.send)
        self.input.submit.connect(self.send)
        self.input.images.connect(self.add_qimages)
        self.stop_btn.clicked.connect(self.stop)
        self.new_btn.clicked.connect(self.new_chat)
        self.del_btn.clicked.connect(self.delete_current)
        self.chats.itemClicked.connect(self.open_chat_item)
        self.attach_btn.clicked.connect(self.pick_images)
        self.clear_att.clicked.connect(self.clear_attachments)
        self.settings_btn.clicked.connect(self.open_settings)
        self.folder_btn.clicked.connect(self.pick_folder)
        self.refresh_btn.clicked.connect(self.fetch_models)
        self.provider.currentTextChanged.connect(self.on_provider)
        self.mode.currentTextChanged.connect(lambda _: self.sync_cfg())
        self.model.lineEdit().editingFinished.connect(self.sync_cfg)
        self.model.currentIndexChanged.connect(lambda _: self.sync_cfg())

        self.fill_models()
        self.update_folder_label()
        self.refresh_sidebar()

    # ---------------- config <-> widgets
    def pcfg(self, prov=None):
        return self.cfg["providers"].setdefault(prov or self._cur_provider, {})

    def sync_cfg(self):
        self.cfg["provider"] = self._cur_provider
        self.cfg["mode"] = self.mode.currentText()
        m = self.model.currentText().strip()
        if m:
            self.pcfg()["model"] = m
        save_config(self.cfg)

    def fill_models(self):
        prov = self._cur_provider
        d = core.PROVIDERS[prov]
        models = list(dict.fromkeys((self.pcfg().get("models") or []) + d["models"]))
        cur = self.pcfg().get("model") or d["model"]
        if cur not in models:
            models.insert(0, cur)
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItems(models)
        self.model.setCurrentText(cur)
        self.model.blockSignals(False)

    def on_provider(self, prov):
        if prov == self._cur_provider:
            return
        self.sync_cfg()
        self._cur_provider = prov
        self.fill_models()
        self.sync_cfg()
        if not core.resolve_cfg(self.cfg)["api_key"]:
            self.open_settings()

    def update_folder_label(self):
        p = self.cfg.get("cwd") or os.path.expanduser("~")
        self.folder_btn.setText("📁  " + (p if len(p) < 70 else "…" + p[-67:]))

    def pick_folder(self):
        p = QFileDialog.getExistingDirectory(self, "پوشهٔ کاری ایجنت", self.cfg.get("cwd", ""))
        if p:
            self.cfg["cwd"] = p
            self.update_folder_label()
            self.sync_cfg()

    def open_settings(self):
        prov = self._cur_provider
        dlg = SettingsDialog(self, prov, self.pcfg(prov), self.cfg.get("instructions", ""))
        dlg.model_for_test = self.model.currentText().strip() or dlg.model_for_test
        if dlg.exec():
            vals, instr = dlg.values()
            self.pcfg(prov).update(vals)
            self.cfg["instructions"] = instr
            self.sync_cfg()

    def fetch_models(self):
        cfg = core.resolve_cfg(self.cfg)
        if not cfg["api_key"]:
            self.open_settings()
            return
        self.refresh_btn.setEnabled(False)
        prov = self._cur_provider
        bg = BG()
        self._bg.append(bg)

        def done(res, err):
            self._bg.remove(bg)
            self.refresh_btn.setEnabled(True)
            if err:
                self.add("error", friendly_error(err))
                return
            if res:
                self.pcfg(prov)["models"] = sorted(res)
                if prov == self._cur_provider:
                    self.fill_models()
                self.add("system", f"{len(res)} مدل از API دریافت شد.")
            save_config(self.cfg)
        bg.done.connect(done)
        bg.run(lambda: core.list_models(cfg))

    # ---------------- attachments
    def add_qimages(self, imgs):
        for im in imgs:
            self.pending.append(qimage_to_block(im))
        self.update_attachments()

    def pick_images(self):
        files, _ = QFileDialog.getOpenFileNames(self, "انتخاب تصویر", "", "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp)")
        self.add_qimages([QImage(f) for f in files if not QImage(f).isNull()])

    def clear_attachments(self):
        self.pending = []
        self.update_attachments()

    def update_attachments(self):
        self.clear_att.setVisible(bool(self.pending))
        self.clear_att.setText(f"📎 {len(self.pending)} تصویر پیوست شد — ✕ حذف")

    # ---------------- sidebar / chats
    def refresh_sidebar(self):
        self.chats.clear()
        for c in core.list_chats():
            it = QListWidgetItem(c["title"][:42])
            it.setData(Qt.ItemDataRole.UserRole, c["id"])
            self.chats.addItem(it)
            if c["id"] == self.chat_id:
                self.chats.setCurrentItem(it)

    def busy(self):
        return bool(self.worker and self.worker.isRunning())

    def new_chat(self):
        if self.busy():
            return
        self.agent, self.chat_id = None, None
        self.chat.clear()
        self.clear_attachments()
        self.chats.clearSelection()

    def open_chat_item(self, item):
        if self.busy():
            return
        try:
            data = core.load_chat(item.data(Qt.ItemDataRole.UserRole))
        except Exception as e:
            self.add("error", f"باز کردن چت ناموفق بود: {e}")
            return
        self.chat_id = data["id"]
        self.agent = core.Agent(core.resolve_cfg(self.cfg))
        self.agent.history = data["history"]
        self.chat.clear()
        for m in self.agent.history:
            for b in m["content"]:
                t = b["type"]
                if t == "text":
                    self.add("user" if m["role"] == "user" else "assistant", b["text"])
                elif t == "image":
                    self.add("system", "🖼 [تصویر پیوست‌شده]")
                elif t == "tool_use":
                    self.add("tool", f"{b['name']}: {json.dumps(b['input'], ensure_ascii=False)[:600]}")
                elif t == "tool_result":
                    self.add("result", (b.get("text") or "")[:600])

    def delete_current(self):
        it = self.chats.currentItem()
        if not it or self.busy():
            return
        cid = it.data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(self, "حذف چت", "این چت حذف بشه؟") == QMessageBox.StandardButton.Yes:
            core.delete_chat(cid)
            if cid == self.chat_id:
                self.new_chat()
            self.refresh_sidebar()

    def save_current(self):
        if not self.agent or not self.chat_id:
            return
        first = next((b["text"] for m in self.agent.history if m["role"] == "user"
                      for b in m["content"] if b["type"] == "text"), "چت")
        try:
            core.save_chat(self.chat_id, first.strip().replace("\n", " "), self.agent.history,
                           {"provider": self._cur_provider, "model": self.model.currentText()})
        except Exception as e:
            self.add("error", f"ذخیرهٔ چت ناموفق بود: {e}")
        self.refresh_sidebar()

    # ---------------- rendering
    def add(self, kind, text):
        if kind == "status":
            self.status.setText(text)
            return
        rtl = ' dir="rtl" align="right"' if is_rtl(text) else ""
        if kind == "user":
            h = (f'<table width="100%" cellpadding="9" style="margin:8px 0"><tr><td bgcolor="#3a3835">'
                 f'<p{rtl} style="margin:0">{esc(text)}</p></td></tr></table>')
        elif kind == "assistant":
            h = f'<div style="margin:8px 2px">{md_to_html(text)}</div>'
        elif kind == "tool":
            h = (f'<div style="margin:6px 2px;color:#d9a441;font-family:Consolas,monospace;font-size:9pt">'
                 f'🔧 {esc(text)}</div>')
        elif kind == "result":
            h = (f'<div style="margin:0 2px 6px 18px;color:#8d897f;font-family:Consolas,monospace;font-size:8.5pt">'
                 f'{esc(text)}</div>')
        elif kind == "error":
            h = f'<div style="margin:8px 2px;color:#ff7a70"{rtl}><b>خطا:</b> {esc(text)}</div>'
        else:
            h = f'<div style="margin:6px 2px;color:#8d897f"{rtl}>• {esc(text)}</div>'
        self.chat.append(h)
        sb = self.chat.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---------------- running
    def send(self):
        if self.busy():
            return
        text = self.input.toPlainText().strip()
        if not text and not self.pending:
            return
        self.sync_cfg()
        cfg = core.resolve_cfg(self.cfg)
        if not cfg["api_key"]:
            QMessageBox.warning(self, "API Key", f"API Key برای {cfg['provider']} وارد نشده.")
            self.open_settings()
            return
        if self.agent is None:
            self.agent = core.Agent(cfg)
        else:
            self.agent.cfg = cfg                   # model / provider can be changed in the middle of a chat
        if not self.chat_id:
            self.chat_id = uuid.uuid4().hex
        images, self.pending = self.pending, []
        self.update_attachments()
        self.input.clear()
        if images:
            self.add("system", f"🖼 {len(images)} تصویر پیوست شد")
        self.add("user", text or "(فقط تصویر)")
        self.worker = Worker(self.agent, text, images)
        self.worker.log.connect(self.add)
        self.worker.approve.connect(self.on_approve)
        self.worker.finished_ok.connect(self.on_done)
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.show()
        self.worker.start()

    def on_approve(self, desc):
        self.activateWindow()
        self.raise_()
        box = QMessageBox(self)
        box.setWindowTitle("تأیید عملیات")
        box.setText("ایجنت می‌خواد این کار رو انجام بده:")
        box.setInformativeText(desc)
        yes = box.addButton("اجازه بده", QMessageBox.ButtonRole.AcceptRole)
        all_ = box.addButton("اجازه برای همهٔ موارد این چت", QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton("رد کن", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        c = box.clickedButton()
        self.worker.answer("yes" if c is yes else "all" if c is all_ else "no")

    def on_done(self):
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress.hide()
        self.status.setText("")
        self.save_current()
        self.input.setFocus()

    def stop(self):
        if self.busy():
            self.worker.stop_flag = True
            self.worker.answer("no")
            self.status.setText("در حال توقف…")

    def closeEvent(self, e):
        self.sync_cfg()
        if self.busy():
            self.worker.stop_flag = True
            self.worker.answer("no")
            self.worker.wait(4000)
        self.save_current()
        super().closeEvent(e)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    if sys.platform == "win32":
        app.setFont(QFont("Segoe UI", 10))
    w = Main()
    w.show()
    sys.exit(app.exec())
