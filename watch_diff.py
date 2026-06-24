# -*- coding: utf-8 -*-
"""
文件变更监控 GUI — 选中文件夹，实时打印 diff，可溯源修改进程。
"""

import os
import sys
import time
import threading
import difflib
import queue
import collections
import json
import hashlib
import gzip
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_FOLDER_FILE = os.path.join(SCRIPT_DIR, ".watch_diff_last.json")
HISTORY_FILE = os.path.join(SCRIPT_DIR, ".watch_diff_history.json")
WINDOWS_FILE = os.path.join(SCRIPT_DIR, ".watch_diff_windows.json")


def _state_path(folder: str) -> str:
    """每个文件夹独立的状态文件，支持多开互不覆盖。"""
    h = hashlib.md5(os.path.abspath(folder).encode()).hexdigest()[:8]
    return os.path.join(SCRIPT_DIR, f".watch_diff_state_{h}.json")


# ---- 窗口注册（用于"跳到前台"） ----
def _load_windows() -> dict:
    try:
        with open(WINDOWS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_windows(data: dict):
    try:
        with open(WINDOWS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _register_window(hwnd: int, folder: str):
    """注册本窗口（打开时调用）。"""
    data = _load_windows()
    norm = os.path.normpath(folder)
    data[norm] = {"hwnd": hwnd, "time": time.time()}
    _save_windows(data)


def _unregister_window(folder: str):
    """注销本窗口（关闭时调用）。"""
    data = _load_windows()
    norm = os.path.normpath(folder)
    data.pop(norm, None)
    _save_windows(data)


def _find_window_hwnd(folder: str) -> int | None:
    """查找已存在的窗口 HWND，不存在返回 None。"""
    data = _load_windows()
    norm = os.path.normpath(folder)
    info = data.get(norm)
    return info["hwnd"] if info else None


def _bring_to_front(hwnd: int) -> bool:
    """将指定 HWND 窗口激活并跳到前台。成功返回 True。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)

        def errcheck(result, func, args):
            if result == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            return result

        SW_MINIMIZE = 6
        SW_RESTORE = 9
        SetForegroundWindow = user32.SetForegroundWindow
        SetForegroundWindow.errcheck = errcheck
        ShowWindow = user32.ShowWindow
        ShowWindow.errcheck = errcheck

        ShowWindow(hwnd, SW_RESTORE)
        SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".superpowers"}
DEFAULT_EXTS = (".py", ".html", ".css", ".js", ".json", ".yaml", ".yml", ".md")
ALL_EXTS = DEFAULT_EXTS + (
    ".txt",
    ".xml",
    ".cfg",
    ".ini",
    ".toml",
    ".ts",
    ".tsx",
    ".jsx",
    ".vue",
    ".svelte",
    ".scss",
    ".less",
    ".sql",
    ".sh",
    ".bat",
    ".ps1",
)

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

_EDITOR_KEYWORDS = (
    "code", "cursor", "notepad", "sublime", "vim", "nvim", "emacs",
    "pycharm", "idea", "webstorm", "clion", "rider", "phpstorm", "goland",
    "devenv", "msbuild", "explorer", "totalcmd", "doublecmd",
    "typora", "obsidian", "logseq", "marvis", "python", "node",
)

_EDITOR_NAMES = [
    "Code.exe", "Cursor.exe", "Notepad++.exe", "Notepad.exe",
    "Sublime Text.exe", "Sublime Merge.exe",
    "PyCharm64.exe", "PyCharm.exe", "Idea64.exe", "Idea.exe",
    "WebStorm64.exe", "WebStorm.exe", "CLion64.exe", "CLion.exe",
    "Rider64.exe", "Rider.exe", "GoLand64.exe", "GoLand.exe",
    "devenv.exe",
    "Typora.exe", "Obsidian.exe", "Logseq.exe",
    "Marvis.exe", "MarvisAgent.exe", "MarvisAssistant.exe",
    "MarvisDlSvr.exe", "MarvisHost.exe", "MarvisMCP.exe",
    "MarvisNode.exe", "MarvisSvr.exe", "MarvisKnowledgebase.exe",
    "python.exe", "node.exe",
]

_EDITOR_PRIORITY = (
    "code.exe", "cursor.exe", "notepad++.exe", "notepad.exe",
    "sublime_text.exe", "pycharm64.exe", "pycharm.exe",
    "idea64.exe", "idea.exe", "webstorm64.exe", "webstorm.exe",
    "typora.exe", "obsidian.exe", "logseq.exe",
    "devenv.exe", "sublime_merge.exe",
    "python.exe", "node.exe",
    "marvis.exe", "Marvis.exe",
    "MarvisAgent.exe", "MarvisAssistant.exe",
    "MarvisDlSvr.exe", "MarvisHost.exe", "MarvisMCP.exe",
    "MarvisNode.exe", "MarvisSvr.exe", "MarvisKnowledgebase.exe",
)

_SERVICE_KEYWORDS = ("svc", "host", "agent", "dl", "mcp", "knowledgebase")
_active_editors = []


def _update_active_editors():
    if not HAS_PSUTIL:
        return
    active = []
    try:
        for p in psutil.process_iter(["pid", "name"]):
            name = (p.info["name"] or "").lower()
            name_orig = p.info["name"] or ""
            if not any(kw in name for kw in _EDITOR_KEYWORDS):
                continue
            if name_orig.lower() == "explorer.exe":
                continue
            is_service = any(kw in name for kw in _SERVICE_KEYWORDS)
            if not is_service:
                active.append(p.info["name"])
    except Exception:
        pass
    global _active_editors
    _active_editors = list(set(active))


def _sort_editors(editors, allowed_editors=None):
    if not editors:
        return []
    lower_map = {e.lower(): e for e in editors}
    sorted_list = []
    for prio in _EDITOR_PRIORITY:
        if prio in lower_map:
            candidate = lower_map[prio]
            if allowed_editors is None or candidate in allowed_editors:
                sorted_list.append(candidate)
    for e in editors:
        if e not in sorted_list:
            if allowed_editors is None or e in allowed_editors:
                sorted_list.append(e)
    return sorted_list[:2]


def _find_writers(filepath: str, allowed_editors=None) -> list[str]:
    if not HAS_PSUTIL:
        return []
    abs_path = os.path.abspath(filepath)
    procs = []
    try:
        for p in psutil.process_iter(["pid", "name"]):
            name = (p.info["name"] or "").lower()
            name_orig = p.info["name"] or ""
            if not any(kw in name for kw in _EDITOR_KEYWORDS):
                continue
            try:
                for f in p.open_files():
                    if f.path == abs_path:
                        procs.append(name_orig)
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    result = list(set(procs))
    if result:
        return _sort_editors(result, allowed_editors)
    return _sort_editors(_active_editors, allowed_editors)


ChangeEvent = tuple


def _load_state(folder: str) -> dict:
    try:
        with open(_state_path(folder), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(folder: str, state: dict):
    try:
        with open(_state_path(folder), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_history() -> list[dict]:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save_history(entries: list[dict]):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _record_history(folder: str, extensions: list[str], cool: int, poll: int):
    entries = _load_history()
    entries = [e for e in entries if os.path.normpath(e.get("path", "")) != os.path.normpath(folder)]
    entries.insert(0, {
        "path": folder,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "extensions": extensions,
        "cool": cool,
        "poll": poll,
    })
    _save_history(entries[:100])


class DedupChangeQueue:
    """字典 + OrderedDict 实现去重队列：同一文件的多次变更只保留最新的一次，按首次入队顺序消费。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._order = collections.OrderedDict()

    def put(self, filepath: str, old_content: str, new_content: str, writers: list[str]):
        with self._lock:
            if filepath in self._order:
                _, _, prev_writers = self._order[filepath]
                writers = writers or prev_writers
            self._order[filepath] = (old_content, new_content, writers)
            self._order.move_to_end(filepath)

    def get(self) -> tuple | None:
        with self._lock:
            if not self._order:
                return None
            fp, (old, new, writers) = self._order.popitem(last=False)
            return (fp, old, new, writers)

    def size(self) -> int:
        with self._lock:
            return len(self._order)

    def empty(self) -> bool:
        with self._lock:
            return len(self._order) == 0

    def contains(self, filepath: str) -> bool:
        with self._lock:
            return filepath in self._order


_NEON_BLUE = "#00D9FF"
_NEON_GREEN = "#00FF88"
_NEON_PURPLE = "#B967FF"
_NEON_ORANGE = "#FF6B35"
_DARK_BG = "#0a0e17"
_PANEL_BG = "#131b2d"
_PANEL_BORDER = "#1e3a5f"
_TEXT_LIGHT = "#e0e6ed"
_TEXT_DIM = "#6B7C93"


class WatchDiffApp:
    def __init__(self, start_folder: str = ""):
        self.watch_dir = ""
        self.running = False
        self.thread = None
        self._file_signatures: dict = {}
        self._file_contents: dict = {}
        self._debounce_until: float = 0.0
        self._change_queue = DedupChangeQueue()
        self._poll_interval = 1
        self._change_history = []
        self._archive_buffer: list[str] = []
        self._archive_buffer_size: int = 0
        self._archive_seq: int = 0
        self._filter_vars: dict[str, tk.BooleanVar] = {}
        self._filter_chips: dict[str, tk.Label] = {}
        self._filter_canvas = None
        self._filter_inner = None
        self._file_tag_map: dict[str, str] = {}

        self._build_ui()
        self._restore_state(start_folder)

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Diff Watcher — 文件变更监控")
        self.root.geometry("1400x1050")
        self.root.configure(bg=_DARK_BG)

        ctrl = tk.Frame(self.root, bg=_DARK_BG, height=60)
        ctrl.pack(fill=tk.X, padx=16, pady=(12, 0))
        ctrl.pack_propagate(False)

        left_ctrl = tk.Frame(ctrl, bg=_DARK_BG)
        left_ctrl.pack(side=tk.LEFT, fill=tk.Y)

        self.dir_label = tk.Label(left_ctrl, text="未选择文件夹", foreground=_TEXT_DIM, bg=_DARK_BG, font=("Consolas", 11))
        self.dir_label.pack(side=tk.LEFT, anchor="w")

        self.status_indicator = tk.Label(left_ctrl, text="●", foreground=_TEXT_DIM, bg=_DARK_BG, font=("Consolas", 12))
        self.status_indicator.pack(side=tk.LEFT, padx=(12, 0))

        btn_frame = tk.Frame(ctrl, bg=_DARK_BG)
        btn_frame.pack(side=tk.LEFT, padx=40)

        self._create_cyber_button(btn_frame, "选择文件夹", self._choose_dir).pack(side=tk.LEFT, padx=4)
        self.btn_start = self._create_cyber_button(btn_frame, "开始监控", self._toggle, width=10)
        self.btn_start.pack(side=tk.LEFT, padx=4)
        self._create_cyber_button(btn_frame, "清空", self._clear_console, width=6).pack(side=tk.LEFT, padx=4)
        self._create_cyber_button(btn_frame, "新窗口", self._new_window, width=7).pack(side=tk.LEFT, padx=4)
        self._create_cyber_button(btn_frame, "历史", self._show_history, width=6).pack(side=tk.LEFT, padx=4)

        self._settings_visible = True
        self.btn_toggle_settings = self._create_cyber_button(btn_frame, "设置 ▾", self._toggle_settings, width=8)
        self.btn_toggle_settings.pack(side=tk.LEFT, padx=4)

        right_ctrl = tk.Frame(ctrl, bg=_DARK_BG)
        right_ctrl.pack(side=tk.RIGHT, fill=tk.Y)

        self.file_count_label = tk.Label(right_ctrl, text="", foreground=_TEXT_DIM, bg=_DARK_BG, font=("Consolas", 10))
        self.file_count_label.pack(anchor="e")

        self.queue_label = tk.Label(right_ctrl, text="", foreground=_TEXT_DIM, bg=_DARK_BG, font=("Consolas", 10))
        self.queue_label.pack(anchor="e", pady=(4, 0))

        self._cfg_separator = tk.Frame(self.root, bg=_PANEL_BORDER, height=1)
        self._cfg_separator.pack(fill=tk.X, padx=16, pady=(8, 0))

        self.cfg_frame = tk.Frame(self.root, bg=_DARK_BG)
        self.cfg_frame.pack(fill=tk.X, padx=16, pady=(8, 0))

        ext_panel = self._create_panel(self.cfg_frame, " 监控类型 ")
        ext_panel.pack(side=tk.LEFT, fill=tk.BOTH, padx=(0, 12))

        self.ext_vars = {}
        row1 = tk.Frame(ext_panel, bg=_PANEL_BG)
        row1.pack(fill=tk.X)
        for ext in DEFAULT_EXTS:
            var = tk.BooleanVar(value=True)
            self.ext_vars[ext] = var
            self._create_cyber_checkbox(row1, ext, var).pack(side=tk.LEFT, padx=(0, 6))

        extra = [e for e in ALL_EXTS if e not in DEFAULT_EXTS]
        row2 = tk.Frame(ext_panel, bg=_PANEL_BG)
        row2.pack(fill=tk.X, pady=(4, 0))
        for ext in extra:
            var = tk.BooleanVar(value=False)
            self.ext_vars[ext] = var
            self._create_cyber_checkbox(row2, ext, var).pack(side=tk.LEFT, padx=(0, 6))

        editor_panel = self._create_panel(self.cfg_frame, " 显示的编辑器 ")
        editor_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 12))

        self.editor_vars = {}
        dev_editors = [e for e in _EDITOR_NAMES if e not in ("MarvisAgent.exe", "MarvisAssistant.exe", "MarvisDlSvr.exe", "MarvisHost.exe", "MarvisMCP.exe", "MarvisNode.exe", "MarvisSvr.exe", "MarvisKnowledgebase.exe")]

        dev_row1 = tk.Frame(editor_panel, bg=_PANEL_BG)
        dev_row1.pack(fill=tk.X)
        for editor in dev_editors[:7]:
            var = tk.BooleanVar(value=True)
            self.editor_vars[editor] = var
            self._create_cyber_checkbox(dev_row1, editor, var).pack(side=tk.LEFT, padx=(0, 6))

        dev_row2 = tk.Frame(editor_panel, bg=_PANEL_BG)
        dev_row2.pack(fill=tk.X, pady=(4, 0))
        for editor in dev_editors[7:14]:
            var = tk.BooleanVar(value=True)
            self.editor_vars[editor] = var
            self._create_cyber_checkbox(dev_row2, editor, var).pack(side=tk.LEFT, padx=(0, 6))

        dev_row3 = tk.Frame(editor_panel, bg=_PANEL_BG)
        dev_row3.pack(fill=tk.X, pady=(4, 0))
        for editor in dev_editors[14:]:
            var = tk.BooleanVar(value=True)
            self.editor_vars[editor] = var
            self._create_cyber_checkbox(dev_row3, editor, var).pack(side=tk.LEFT, padx=(0, 6))

        marvis_label_row = tk.Frame(editor_panel, bg=_PANEL_BG)
        marvis_label_row.pack(fill=tk.X, pady=(8, 0))
        tk.Label(marvis_label_row, text="MARVIS 系列", foreground=_NEON_ORANGE, bg=_PANEL_BG, font=("Consolas", 9, "bold")).pack(side=tk.LEFT)

        marvis_editors = [e for e in _EDITOR_NAMES if "Marvis" in e]
        marvis_row1 = tk.Frame(editor_panel, bg=_PANEL_BG)
        marvis_row1.pack(fill=tk.X)
        for editor in marvis_editors[:5]:
            var = tk.BooleanVar(value=True)
            self.editor_vars[editor] = var
            self._create_cyber_checkbox(marvis_row1, editor, var).pack(side=tk.LEFT, padx=(0, 6))

        marvis_row2 = tk.Frame(editor_panel, bg=_PANEL_BG)
        marvis_row2.pack(fill=tk.X, pady=(4, 0))
        for editor in marvis_editors[5:]:
            var = tk.BooleanVar(value=True)
            self.editor_vars[editor] = var
            self._create_cyber_checkbox(marvis_row2, editor, var).pack(side=tk.LEFT, padx=(0, 6))

        param_panel = self._create_panel(self.cfg_frame, " 参数设置 ")
        param_panel.pack(side=tk.RIGHT, fill=tk.Y)

        cool_frame = tk.Frame(param_panel, bg=_PANEL_BG)
        cool_frame.pack(fill=tk.X, pady=(0, 8))
        tk.Label(cool_frame, text="冷却", foreground=_TEXT_LIGHT, bg=_PANEL_BG, font=("Consolas", 10)).pack(side=tk.LEFT)
        self.cool_var = tk.StringVar(value="30")
        tk.Spinbox(cool_frame, from_=5, to=300, width=4, textvariable=self.cool_var, bg=_PANEL_BORDER, fg=_NEON_GREEN, buttonbackground=_PANEL_BG, font=("Consolas", 10), insertbackground=_NEON_GREEN, relief=tk.FLAT, highlightthickness=1, highlightcolor=_NEON_BLUE, highlightbackground=_PANEL_BORDER).pack(side=tk.LEFT, padx=8)
        tk.Label(cool_frame, text="秒", foreground=_TEXT_DIM, bg=_PANEL_BG, font=("Consolas", 9)).pack(side=tk.LEFT)

        poll_frame = tk.Frame(param_panel, bg=_PANEL_BG)
        poll_frame.pack(fill=tk.X, pady=(0, 8))
        tk.Label(poll_frame, text="轮询", foreground=_TEXT_LIGHT, bg=_PANEL_BG, font=("Consolas", 10)).pack(side=tk.LEFT)
        self.poll_var = tk.StringVar(value="1")
        tk.Spinbox(poll_frame, from_=1, to=10, width=4, textvariable=self.poll_var, bg=_PANEL_BORDER, fg=_NEON_GREEN, buttonbackground=_PANEL_BG, font=("Consolas", 10), insertbackground=_NEON_GREEN, relief=tk.FLAT, highlightthickness=1, highlightcolor=_NEON_BLUE, highlightbackground=_PANEL_BORDER).pack(side=tk.LEFT, padx=8)
        tk.Label(poll_frame, text="秒", foreground=_TEXT_DIM, bg=_PANEL_BG, font=("Consolas", 9)).pack(side=tk.LEFT)

        psutil_frame = tk.Frame(param_panel, bg=_PANEL_BG)
        psutil_frame.pack(fill=tk.X)
        self.psutil_info = tk.Label(psutil_frame, text="", bg=_PANEL_BG, font=("Consolas", 10))
        self.psutil_info.pack(anchor="w")
        if HAS_PSUTIL:
            self.psutil_info.config(text="◉ 进程溯源", foreground=_NEON_GREEN)
        else:
            self.psutil_info.config(text="○ 进程溯源", foreground=_TEXT_DIM)

        self._log_separator = tk.Frame(self.root, bg=_PANEL_BORDER, height=1)
        self._log_separator.pack(fill=tk.X, padx=16, pady=(8, 0))

        console_panel = self._create_panel(self.root, " 变更日志 ", pady=(8, 12))
        console_panel.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        self._filter_outer_height = 100
        filter_outer = tk.Frame(console_panel, bg=_PANEL_BG, height=self._filter_outer_height, highlightthickness=1, highlightbackground=_PANEL_BORDER)
        filter_outer.pack(fill=tk.X, pady=(0, 4))
        filter_outer.pack_propagate(False)

        canvas_height = self._filter_outer_height - 16
        self._filter_canvas = tk.Canvas(filter_outer, bg=_PANEL_BG, height=canvas_height, highlightthickness=0, bd=0)
        filter_scrollbar = tk.Scrollbar(filter_outer, orient=tk.HORIZONTAL, command=self._filter_canvas.xview)
        self._filter_canvas.configure(xscrollcommand=filter_scrollbar.set)

        self._filter_inner = tk.Frame(self._filter_canvas, bg=_PANEL_BG, height=canvas_height)
        self._filter_placeholder = tk.Label(self._filter_inner, text="  变更文件过滤  (暂无)  ", bg=_PANEL_BG, fg=_TEXT_DIM, font=("Consolas", 9), padx=6, pady=2)
        self._filter_placeholder.place(x=4, y=4)

        self._filter_window_id = self._filter_canvas.create_window((0, 0), window=self._filter_inner, anchor="nw")
        filter_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)
        self._filter_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def _on_filter_canvas_configure(event):
            self._relayout_chips()
        self._filter_canvas.bind("<Configure>", _on_filter_canvas_configure)

        def _on_filter_wheel(event):
            self._filter_canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")
        self._filter_canvas.bind("<Enter>", lambda e: self._filter_canvas.bind_all("<MouseWheel>", _on_filter_wheel))
        self._filter_canvas.bind("<Leave>", lambda e: self._filter_canvas.unbind_all("<MouseWheel>"))

        self.console = scrolledtext.ScrolledText(console_panel, wrap=tk.WORD, bg="#0d1117", fg="#c9d1d9", insertbackground=_NEON_BLUE, font=("JetBrains Mono", 10, "normal"), undo=False, maxundo=0, borderwidth=0, highlightthickness=0, padx=8, pady=8)
        self.console.pack(fill=tk.BOTH, expand=True)

        self.console.tag_configure("info", foreground=_NEON_BLUE)
        self.console.tag_configure("warn", foreground="#FFA657")
        self.console.tag_configure("add", foreground=_NEON_GREEN)
        self.console.tag_configure("del", foreground="#FF6B6B")
        self.console.tag_configure("header", foreground=_NEON_PURPLE)
        self.console.tag_configure("file", foreground="#79C0FF")
        self.console.tag_configure("proc", foreground=_NEON_ORANGE)
        self.console.tag_configure("dedup", foreground="#8B949E")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _create_panel(self, parent, title, pady=0):
        outer = tk.Frame(parent, bg=_PANEL_BORDER, padx=1, pady=1)
        outer.pack(fill=tk.BOTH, expand=True)
        inner = tk.Frame(outer, bg=_PANEL_BG, padx=10, pady=6)
        inner.pack(fill=tk.BOTH, expand=True)
        tk.Label(inner, text=title, foreground=_NEON_BLUE, bg=_PANEL_BG, font=("Consolas", 10, "bold")).pack(anchor="w", pady=(0, 6))
        return inner

    def _create_cyber_button(self, parent, text, command, width=8):
        btn = tk.Button(parent, text=text, command=command, width=width, bg=_PANEL_BORDER, fg=_TEXT_LIGHT, activebackground=_NEON_BLUE, activeforeground=_DARK_BG, relief=tk.FLAT, cursor="hand2", font=("Consolas", 10), pady=4, highlightthickness=1, highlightcolor=_NEON_BLUE, highlightbackground=_PANEL_BORDER)
        def on_enter(e):
            btn.configure(bg=_NEON_BLUE, fg=_DARK_BG, highlightbackground=_NEON_BLUE)
        def on_leave(e):
            btn.configure(bg=_PANEL_BORDER, fg=_TEXT_LIGHT, highlightbackground=_PANEL_BORDER)
        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        return btn

    def _create_cyber_checkbox(self, parent, text, variable):
        return tk.Checkbutton(parent, text=text, variable=variable, bg=_PANEL_BG, fg=_TEXT_LIGHT, activebackground=_PANEL_BG, activeforeground=_NEON_BLUE, selectcolor=_PANEL_BORDER, font=("Consolas", 9), padx=4, pady=2, indicatoron=0, relief=tk.FLAT)

    @staticmethod
    def _normalize_tags(tag):
        if tag is None:
            return ()
        if isinstance(tag, (tuple, list)):
            return tuple(tag)
        return (tag,)

    def _log(self, text, tag=None):
        tags = self._normalize_tags(tag)
        self.console.insert(tk.END, text + "\n", tags)
        self.console.see(tk.END)
        self._trim_console()

    def _log_batch(self, lines: list[tuple[str, object]]):
        self.console.configure(state=tk.NORMAL)
        for text, tag in lines:
            tags = self._normalize_tags(tag)
            self.console.insert(tk.END, text + "\n", tags)
        self.console.see(tk.END)
        self._trim_console()

    def _trim_console(self):
        max_lines = 5000
        trim_margin = 2000
        line_count = int(self.console.index("end-1c").split(".")[0])
        excess = line_count - (max_lines + trim_margin)
        if excess > 0:
            delete_count = excess + trim_margin
            self._archive_trimmed(delete_count)
            self.console.delete("1.0", f"{delete_count + 1}.0")

    def _open_archive(self):
        self._archive_buffer.clear()
        self._archive_buffer_size = 0
        self._log("归档缓冲区就绪 (阈值 10MB)", "info")

    def _archive_trimmed(self, line_count: int):
        try:
            end_idx = f"{line_count + 1}.0"
            text = self.console.get("1.0", end_idx)
            text_bytes = len(text.encode("utf-8"))
            self._archive_buffer.append(text)
            self._archive_buffer_size += text_bytes
        except Exception:
            return
        threshold = 10 * 1024 * 1024
        if self._archive_buffer_size >= threshold:
            self._flush_archive()

    def _flush_archive(self):
        if not self._archive_buffer:
            return
        os.makedirs(os.path.join(SCRIPT_DIR, "archive"), exist_ok=True)
        self._archive_seq += 1
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SCRIPT_DIR, "archive", f"watch_diff_{ts}_{self._archive_seq:03d}.log.gz")
        with gzip.open(path, "wt", encoding="utf-8") as f:
            for chunk in self._archive_buffer:
                f.write(chunk)
        self._log(f"归档打包: {path} ({self._archive_buffer_size / 1024 / 1024:.1f}MB)", "info")
        self._archive_buffer.clear()
        self._archive_buffer_size = 0

    def _clear_console(self):
        self.console.delete(1.0, tk.END)
        for chip in self._filter_chips.values():
            chip.destroy()
        self._filter_chips.clear()
        self._filter_vars.clear()
        self._file_tag_map.clear()
        if self._filter_placeholder is None:
            self._filter_placeholder = tk.Label(self._filter_inner, text="  变更文件过滤  (暂无)  ", bg=_PANEL_BG, fg=_TEXT_DIM, font=("Consolas", 9), padx=6, pady=2)
            self._filter_placeholder.place(x=4, y=4)

    def _get_file_tag(self, fp: str) -> str:
        if fp not in self._file_tag_map:
            h = hashlib.md5(fp.encode()).hexdigest()[:10]
            self._file_tag_map[fp] = f"__f_{h}"
            self.console.tag_configure(self._file_tag_map[fp], elide=False)
        return self._file_tag_map[fp]

    def _add_filter_chip(self, fp: str):
        if fp in self._filter_vars:
            return
        if self._filter_placeholder is not None:
            self._filter_placeholder.destroy()
            self._filter_placeholder = None
        rel = os.path.relpath(fp, self.watch_dir)
        display = rel if len(rel) <= 40 else "..." + rel[-37:]
        var = tk.BooleanVar(value=True)
        self._filter_vars[fp] = var
        chip = tk.Label(self._filter_inner, text=display, bg=_PANEL_BORDER, fg=_NEON_GREEN, font=("Consolas", 9), padx=8, pady=2, cursor="hand2", relief=tk.FLAT, borderwidth=1, highlightthickness=0)
        def _on_click(e, f=fp, c=chip, v=var):
            new_val = not v.get()
            v.set(new_val)
            c.configure(bg=_PANEL_BORDER if new_val else "#2a1a2a", fg=_NEON_GREEN if new_val else _TEXT_DIM)
            self.console.tag_configure(self._file_tag_map[f], elide=not new_val)
        chip.bind("<Button-1>", _on_click)
        self._filter_chips[fp] = chip
        self._relayout_chips()

    def _relayout_chips(self):
        if not self._filter_chips:
            return
        canvas_w = self._filter_canvas.winfo_width()
        if canvas_w < 50:
            canvas_w = 800
        chip_h = 30
        max_rows = 3
        gap_x, gap_y = 3, 2
        pad_x, pad_y = 4, 2
        for chip in self._filter_chips.values():
            chip.update_idletasks()
        x, y = pad_x, pad_y
        row = 0
        max_x = canvas_w
        for fp, chip in self._filter_chips.items():
            cw = chip.winfo_reqwidth()
            if x + cw + gap_x > canvas_w - pad_x:
                row += 1
                x = pad_x
                y = row * chip_h + pad_y
            if row >= max_rows:
                y = (max_rows - 1) * chip_h + pad_y
            chip.place(x=x, y=y)
            x += cw + gap_x
            if x > max_x:
                max_x = x
        total_rows = min(row + 1, max_rows)
        total_h = total_rows * chip_h + pad_y * 2
        self._filter_inner.configure(width=max_x + pad_x, height=total_h)
        self._filter_canvas.itemconfigure(self._filter_window_id, width=max_x + pad_x, height=total_h)
        self._filter_canvas.configure(scrollregion=(0, 0, max_x + pad_x, total_h))

    def _restore_state(self, start_folder: str = ""):
        if start_folder and os.path.isdir(start_folder):
            self.watch_dir = os.path.normpath(start_folder)
        else:
            try:
                with open(LAST_FOLDER_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                folder = data.get("last_folder", "")
                if folder and os.path.isdir(folder):
                    self.watch_dir = folder
            except Exception:
                pass
        if self.watch_dir:
            self.dir_label.config(text=self.watch_dir, foreground=_TEXT_LIGHT)
            self.btn_start.configure(state=tk.NORMAL)
            state = _load_state(self.watch_dir)
            self.cool_var.set(str(state.get("cool", 30)))
            self.poll_var.set(str(state.get("poll", 1)))
            saved_exts = state.get("extensions", None)
            if saved_exts:
                for ext in self.ext_vars:
                    self.ext_vars[ext].set(ext in saved_exts)
            saved_editors = state.get("editors", None)
            if saved_editors:
                for editor in self.editor_vars:
                    self.editor_vars[editor].set(editor in saved_editors)

    def _persist_state(self):
        if not self.watch_dir:
            return
        try:
            with open(LAST_FOLDER_FILE, "w", encoding="utf-8") as f:
                json.dump({"last_folder": self.watch_dir}, f, ensure_ascii=False)
        except Exception:
            pass
        active_exts = [e for e, v in self.ext_vars.items() if v.get()]
        active_editors = [e for e, v in self.editor_vars.items() if v.get()]
        cool = int(self.cool_var.get())
        poll = int(self.poll_var.get())
        _save_state(self.watch_dir, {"cool": cool, "poll": poll, "extensions": active_exts, "editors": active_editors})
        _record_history(self.watch_dir, active_exts, cool, poll)

    def _new_window(self):
        path = filedialog.askdirectory(title="选择另一个要监控的文件夹")
        if not path:
            return
        path = os.path.normpath(path)
        subprocess.Popen([sys.executable, os.path.abspath(__file__), path], creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)

    def _toggle_settings(self):
        self._settings_visible = not self._settings_visible
        if self._settings_visible:
            self._cfg_separator.pack(fill=tk.X, padx=16, pady=(8, 0))
            self.cfg_frame.pack(fill=tk.X, padx=16, pady=(8, 0))
            self.cfg_frame.pack_configure(before=self._log_separator)
            self._cfg_separator.pack_configure(before=self.cfg_frame)
            self.btn_toggle_settings.config(text="设置 ▾")
        else:
            self._cfg_separator.pack_forget()
            self.cfg_frame.pack_forget()
            self.btn_toggle_settings.config(text="设置 ▸")

    def _choose_dir(self):
        path = filedialog.askdirectory(title="选择要监控的文件夹")
        if path:
            self.watch_dir = os.path.normpath(path)
            self.dir_label.config(text=self.watch_dir, foreground=_TEXT_LIGHT)
            self.btn_start.configure(state=tk.NORMAL)
            self._persist_state()

    def _show_history(self):
        entries = _load_history()
        if not entries:
            messagebox.showinfo("最近监控", "暂无监控历史。")
            return
        win = tk.Toplevel(self.root)
        win.title("最近监控文件夹")
        win.geometry("820x480")
        win.resizable(True, True)
        win.configure(bg=_DARK_BG)
        tk.Label(win, text=f"共 {len(entries)} 条记录（最多 100），重复文件夹自动合并为最新。", foreground=_TEXT_DIM, bg=_DARK_BG, font=("Consolas", 9)).pack(anchor="w", padx=16, pady=(12, 0))
        cols = ("序号", "最近监控时间", "文件夹路径", "扩展名")
        tree = ttk.Treeview(win, columns=cols, show="headings", selectmode="browse")
        tree.heading("序号", text="#", anchor=tk.CENTER)
        tree.heading("最近监控时间", text="最近监控时间")
        tree.heading("文件夹路径", text="文件夹路径")
        tree.heading("扩展名", text="扩展名")
        tree.column("序号", width=40, anchor=tk.CENTER, stretch=False)
        tree.column("最近监控时间", width=140, anchor=tk.W, stretch=False)
        tree.column("文件夹路径", width=380, anchor=tk.W)
        tree.column("扩展名", width=200, anchor=tk.W)
        scrollbar = ttk.Scrollbar(win, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(16, 0), pady=(8, 12))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=(8, 12), padx=(0, 12))
        for i, entry in enumerate(entries, 1):
            tree.insert("", tk.END, values=(i, entry.get("time", ""), entry.get("path", ""), ", ".join(entry.get("extensions", [])) or "—"))
        def _on_activate(event=None):
            sel = tree.selection()
            if not sel:
                return
            item = tree.item(sel[0])
            path = item["values"][2]
            if not path or not os.path.isdir(path):
                messagebox.showwarning("无效路径", f"文件夹不存在:\n{path}")
                return
            norm = os.path.normpath(path)
            hwnd = _find_window_hwnd(norm)
            if hwnd and _bring_to_front(hwnd):
                win.destroy()
                return
            subprocess.Popen([sys.executable, os.path.abspath(__file__), path], creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0))
            win.destroy()
        tree.bind("<Double-1>", _on_activate)
        btn_frame = tk.Frame(win, bg=_DARK_BG)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(0, 12))
        tk.Button(btn_frame, text="打开新窗口监控", command=lambda: _on_activate(None), bg=_PANEL_BORDER, fg=_TEXT_LIGHT, activebackground=_NEON_BLUE, relief=tk.FLAT, font=("Consolas", 9)).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(btn_frame, text="清空历史", command=lambda: (_save_history([]), win.destroy()), bg=_PANEL_BORDER, fg=_TEXT_LIGHT, activebackground=_NEON_BLUE, relief=tk.FLAT, font=("Consolas", 9)).pack(side=tk.RIGHT)
        tk.Button(btn_frame, text="关闭", command=win.destroy, bg=_PANEL_BORDER, fg=_TEXT_LIGHT, activebackground=_NEON_BLUE, relief=tk.FLAT, font=("Consolas", 9)).pack(side=tk.LEFT)
        win.transient(self.root)
        win.grab_set()
        self.root.wait_window(win)

    def _toggle(self):
        if self.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        self.running = True
        self.btn_start.configure(text="停止监控")
        self.status_indicator.configure(text="◉", foreground=_NEON_GREEN)
        self._persist_state()
        if self._settings_visible:
            self._toggle_settings()
        self._open_archive()
        self._log("=" * 60, "info")
        self._log(f">>> 开始监控: {self.watch_dir}", "info")
        self._log(f">>> 进程溯源: {'可用' if HAS_PSUTIL else '不可用 (pip install psutil)'}", "info")
        self._log("=" * 60, "info")
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()
        self.root.after(200, self._drain_queue)

    def _stop(self):
        self.running = False
        self.btn_start.configure(text="开始监控")
        self.status_indicator.configure(text="○", foreground=_TEXT_DIM)
        self._log("[已停止] 监控结束", "warn")

    def _on_close(self):
        self.running = False
        if self.watch_dir:
            _unregister_window(self.watch_dir)
        self._persist_state()
        self._flush_archive()
        self.root.destroy()

    def _drain_queue(self):
        count = 0
        while True:
            item = self._change_queue.get()
            if item is None:
                break
            fp, old_c, new_c, writers = item
            self._print_diff(fp, old_c, new_c, writers)
            count += 1
        if count > 0:
            dup_hint = " (已去重)" if self._change_queue.size() == 0 else ""
            self.queue_label.configure(text=f"队列: 0{dup_hint}")
        if self.running:
            self.root.after(300, self._drain_queue)

    @staticmethod
    def _scan_source_files(directory, extensions):
        results = []
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in SKIP_DIRS:
                            continue
                        results.extend(WatchDiffApp._scan_source_files(entry.path, extensions))
                    else:
                        if os.path.splitext(entry.name)[1].lower() in extensions:
                            results.append(entry.path)
        except OSError:
            pass
        return results

    def _watch(self):
        ext_tuple = tuple(e for e, v in self.ext_vars.items() if v.get())
        if not ext_tuple:
            self.root.after(0, lambda: self._log("未选择任何文件类型", "warn"))
            self.root.after(0, self._stop)
            return
        allowed_editors = [e for e, v in self.editor_vars.items() if v.get()]
        cool = int(self.cool_var.get())
        self._poll_interval = int(self.poll_var.get())
        source_files = self._scan_source_files(self.watch_dir, ext_tuple)
        self._file_signatures.clear()
        self._file_contents.clear()
        for fp in source_files:
            try:
                st = os.stat(fp)
                self._file_signatures[fp] = (st.st_mtime, st.st_size)
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    self._file_contents[fp] = f.read()
            except Exception:
                self._file_signatures[fp] = (0, 0)
                self._file_contents[fp] = ""
        self.root.after(0, self._update_file_count, len(source_files))
        while self.running:
            changed_count = 0
            dedup_count = 0
            _update_active_editors()
            source_files = self._scan_source_files(self.watch_dir, ext_tuple)
            self.root.after(0, self._update_file_count, len(source_files))
            for fp in source_files:
                try:
                    st = os.stat(fp)
                    sig = (st.st_mtime, st.st_size)
                except Exception:
                    continue
                old_sig = self._file_signatures.get(fp)
                if old_sig is None:
                    writers = _find_writers(fp, allowed_editors)
                    self._file_signatures[fp] = sig
                    try:
                        with open(fp, "r", encoding="utf-8", errors="replace") as f:
                            nc = f.read()
                    except Exception:
                        nc = ""
                    self._file_contents[fp] = nc
                    deduped = self._change_queue.contains(fp)
                    self._change_queue.put(fp, "", nc, writers)
                    if deduped:
                        dedup_count += 1
                    changed_count += 1
                elif old_sig != sig:
                    writers = _find_writers(fp, allowed_editors)
                    try:
                        with open(fp, "r", encoding="utf-8", errors="replace") as f:
                            nc = f.read()
                    except Exception:
                        nc = ""
                    if fp in self._file_contents and self._file_contents[fp] != nc:
                        deduped = self._change_queue.contains(fp)
                        self._change_queue.put(fp, self._file_contents[fp], nc, writers)
                        if deduped:
                            dedup_count += 1
                        changed_count += 1
                    self._file_signatures[fp] = sig
                    self._file_contents[fp] = nc
            qsize = self._change_queue.size()
            if qsize > 0:
                self.root.after(0, lambda s=qsize: self.queue_label.configure(text=f"队列: {s}", foreground="#FFA657"))
            now = time.time()
            if changed_count > 0:
                self._debounce_until = now + cool
                msg = f"[检测] {changed_count} 个变更"
                if dedup_count > 0:
                    msg += f" (其中 {dedup_count} 个已去重)"
                msg += f"，冷却 {cool}s"
                self.root.after(0, self._log, msg, "info")
            if self._debounce_until > 0 and now >= self._debounce_until:
                self.root.after(0, self._log, "[就绪] 冷却结束", "info")
                self._debounce_until = 0.0
            time.sleep(self._poll_interval)

    def _update_file_count(self, count):
        self.file_count_label.configure(text=f"文件: {count}")

    def _print_diff(self, fp, old_content, new_content, writers):
        self._change_history.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "file": fp, "old": old_content, "new": new_content, "writers": writers})
        self._add_filter_chip(fp)
        ftag = self._get_file_tag(fp)
        rel = os.path.relpath(fp, self.watch_dir)
        lines_buf = [("", ftag)]
        lines_buf.append((f"{'─' * 56}", ("header", ftag)))
        if writers:
            lines_buf.append((f"[{rel}]  ←  {', '.join(writers)}", ("file", ftag)))
        else:
            lines_buf.append((f"[{rel}]", ("file", ftag)))
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"旧/{rel}", tofile=f"新/{rel}", n=3, lineterm="")
        for line in diff:
            if line.startswith("@@"):
                lines_buf.append((line, ("header", ftag)))
            elif line.startswith("+"):
                lines_buf.append((line[1:], ("add", ftag)))
            elif line.startswith("-"):
                lines_buf.append((line[1:], ("del", ftag)))
            elif line.startswith("---") or line.startswith("+++"):
                lines_buf.append((line, ("dedup", ftag)))
            else:
                lines_buf.append((line, ftag))
        self._log_batch(lines_buf)

    def run(self):
        def _do_register():
            if self.watch_dir:
                hwnd = int(self.root.winfo_id())
                _register_window(hwnd, self.watch_dir)
        self.root.after(100, _do_register)
        self.root.mainloop()


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else ""
    app = WatchDiffApp(start_folder=folder)
    app.run()
