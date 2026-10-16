# -*- coding: utf-8 -*-
"""图形界面：Tkinter + 拖拽（tkinterdnd2）。

功能对照需求：
* 支持把 PDF（或整个文件夹）**批量拖进窗口**；
* 也支持按钮添加、剪贴板粘贴（拖拽库不可用时的兜底）；
* **批量导出**图片，实时进度、可取消；
* 导出目录可选：默认「当前目录」，也可选「与源文件同一目录」或指定目录。

界面只在主线程操作 Tk 控件；转换在后台线程里跑，通过 ``queue`` 回传事件。
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from tkinter import font as tkfont
from tkinter import ttk

from .core import (
    DEFAULT_DPI,
    DEFAULT_FORMAT,
    DEFAULT_QUALITY,
    DEFAULT_TEMPLATE,
    IMAGE_FORMATS,
    ConversionError,
    ConversionReport,
    ConvertOptions,
    Progress,
    check_dependencies,
    convert_files,
    default_output_dir,
    describe_pdf,
    open_path,
    parse_page_spec,
)

try:  # 拖拽支持（可选依赖）
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_AVAILABLE = True
except Exception:  # pragma: no cover - 缺库时自动降级
    DND_FILES = None  # type: ignore[assignment]
    TkinterDnD = None  # type: ignore[assignment]
    DND_AVAILABLE = False

APP_TITLE = "PDF 发票转图片"
DPI_CHOICES = ("72", "96", "150", "200", "300", "400", "600")
ACCENT = "#2f6fd0"
DROP_BG = "#eef4fd"
DROP_BG_ACTIVE = "#d8e7fb"

_PLACEHOLDER = "全部页面"


@dataclass
class Entry:
    """列表里的一行（一个 PDF 文件）。"""

    path: Path
    iid: str
    pages: int = 0
    status: str = "读取中…"
    error: str | None = None
    password: str | None = None


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def pick_ui_font(root: tk.Misc) -> str:
    """挑一个能正常显示中文的界面字体。"""
    families = set(tkfont.families(root))
    for name in (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "PingFang SC",
        "Hiragino Sans GB",
        "Heiti SC",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "WenQuanYi Micro Hei",
        "Segoe UI",
    ):
        if name in families:
            return name
    return "TkDefaultFont"


def apply_ui_font(root: tk.Misc) -> None:
    family = pick_ui_font(root)
    base = 13 if sys.platform == "darwin" else 10
    for name, size in (
        ("TkDefaultFont", base),
        ("TkTextFont", base),
        ("TkMenuFont", base),
        ("TkHeadingFont", base),
        ("TkTooltipFont", base - 1),
    ):
        try:
            tkfont.nametofont(name).configure(family=family, size=size)
        except tk.TclError:  # pragma: no cover - 个别主题缺字体
            pass
    try:
        tkfont.nametofont("TkFixedFont").configure(size=base - 1)
    except tk.TclError:  # pragma: no cover
        pass


def asset_path(name: str) -> Path:
    """定位随程序分发的资源文件（源码运行 / PyInstaller 打包后都可用）。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / name


def _apply_window_icon(root: tk.Tk) -> None:
    for candidate in ("icon-256.png", "icon.png"):
        path = asset_path(candidate)
        if not path.exists():
            continue
        try:
            image = tk.PhotoImage(file=str(path))
            root.iconphoto(True, image)
            root._dsh_icon = image  # 保持引用，避免被垃圾回收
            return
        except Exception:  # pragma: no cover - 个别平台不支持
            continue


def create_root() -> tk.Tk:
    """创建主窗口；优先使用带拖拽能力的 Tk。"""
    root: tk.Tk | None = None
    if DND_AVAILABLE:
        try:
            root = TkinterDnD.Tk()  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - tkdnd 动态库加载失败时降级
            root = None
    if root is None:
        root = tk.Tk()
    _apply_window_icon(root)
    return root


# --------------------------------------------------------------------------- #
# 主界面
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root: tk.Tk, initial_files: list[Path] | None = None) -> None:
        self.root = root
        self.entries: list[Entry] = []
        self.by_iid: dict[str, Entry] = {}
        self.by_path: dict[str, Entry] = {}
        self._counter = 0
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.scanner: threading.Thread | None = None
        self._scan_jobs: "queue.Queue[Path]" = queue.Queue()
        self._running = False
        self._last_dir = str(Path.home())
        self._retry_attempted = False

        apply_ui_font(root)
        root.title(APP_TITLE)
        root.minsize(940, 700)
        self._center(1040, 780)

        self.var_format = tk.StringVar(value=DEFAULT_FORMAT)
        self.var_dpi = tk.StringVar(value=str(DEFAULT_DPI))
        self.var_quality = tk.StringVar(value=str(DEFAULT_QUALITY))
        self.var_pages = tk.StringVar(value="")
        self.var_template = tk.StringVar(value=DEFAULT_TEMPLATE)
        self.var_grayscale = tk.BooleanVar(value=False)
        self.var_overwrite = tk.BooleanVar(value=False)
        self.var_subfolder = tk.BooleanVar(value=False)
        self.var_open_dir = tk.BooleanVar(value=False)
        self.var_recursive = tk.BooleanVar(value=True)
        self.var_target = tk.StringVar(value="cwd")
        self.var_custom_dir = tk.StringVar(value=str(Path.home()))

        self._build_ui()
        self._setup_dnd()
        self._bind_keys()

        if initial_files:
            self.add_paths([Path(p) for p in initial_files])

        self._closed = False
        self._after_id: str | None = self.root.after(100, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ 外观
    def _center(self, width: int, height: int) -> None:
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)  # 文件列表占据多余高度
        pad = {"padx": 12, "pady": 6}

        try:  # 列表行高矮一点，同样高度能多显示几行
            ttk.Style(root).configure("Treeview", rowheight=24)
        except tk.TclError:  # pragma: no cover
            pass

        # ---- 标题 ----
        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=APP_TITLE, font=("TkDefaultFont", 15, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        hint = "拖入 PDF 发票 → 批量导出图片" + ("" if DND_AVAILABLE else "（拖拽不可用，见下方提示）")
        ttk.Label(header, text=hint, foreground="#666666").grid(row=0, column=1, sticky="w", padx=10)

        # ---- 拖拽区 ----
        self.drop_frame = tk.Frame(
            root, bg=DROP_BG, highlightbackground="#a9c7ef", highlightthickness=2, height=76
        )
        self.drop_frame.grid(row=1, column=0, sticky="ew", **pad)
        self.drop_frame.grid_propagate(False)
        self.drop_frame.columnconfigure(0, weight=1)
        self.drop_frame.rowconfigure(0, weight=1)
        self.drop_label = tk.Label(
            self.drop_frame,
            text="把 PDF 文件或整个文件夹拖到这里\n（支持一次拖入多个文件）",
            bg=DROP_BG,
            fg="#33547f",
            justify="center",
        )
        self.drop_label.grid(row=0, column=0, sticky="nsew")
        self.drop_label.bind("<Button-1>", lambda _e: self.pick_files())

        # ---- 列表工具栏 ----
        bar = ttk.Frame(root)
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 0))
        for text, cmd in (
            ("添加文件", self.pick_files),
            ("添加文件夹", self.pick_folder),
            ("粘贴路径", self.paste_paths),
            ("移除选中", self.remove_selected),
            ("清空列表", self.clear_all),
        ):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))
        self.lbl_count = ttk.Label(bar, text="共 0 个文件 / 0 页", foreground="#555555")
        self.lbl_count.pack(side="right")

        # ---- 文件列表 ----
        table = ttk.Frame(root)
        table.grid(row=3, column=0, sticky="nsew", padx=12, pady=(6, 0))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = ("name", "folder", "pages", "status")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="extended", height=8)
        for col, title, width, anchor in (
            ("name", "文件名", 260, "w"),
            ("folder", "所在目录", 300, "w"),
            ("pages", "页数", 60, "center"),
            ("status", "状态", 200, "w"),
        ):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "folder"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.tag_configure("ok", foreground="#1a7f37")
        self.tree.tag_configure("failed", foreground="#c0392b")
        self.tree.tag_configure("running", foreground=ACCENT)

        # ---- 设置区（整行两块，横排不裁切文字） ----
        settings = ttk.Frame(root)
        settings.grid(row=4, column=0, sticky="ew", padx=12, pady=(8, 0))
        settings.columnconfigure(0, weight=1)

        image_box = ttk.LabelFrame(settings, text=" 图片设置 ")
        image_box.grid(row=0, column=0, sticky="ew")

        row1 = ttk.Frame(image_box)
        row1.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        ttk.Label(row1, text="格式").pack(side="left")
        self.fmt_box = ttk.Combobox(
            row1,
            textvariable=self.var_format,
            values=[f"{k} — {v}" for k, v in IMAGE_FORMATS.items()],
            state="readonly",
            width=28,
        )
        self.fmt_box.pack(side="left", padx=(6, 18))
        self.fmt_box.set(f"{DEFAULT_FORMAT} — {IMAGE_FORMATS[DEFAULT_FORMAT]}")
        self.fmt_box.bind("<<ComboboxSelected>>", self._on_format_change)

        ttk.Label(row1, text="DPI").pack(side="left")
        ttk.Combobox(row1, textvariable=self.var_dpi, values=DPI_CHOICES, width=6).pack(
            side="left", padx=(6, 4)
        )
        ttk.Label(row1, text="（默认 300）", foreground="#777777").pack(side="left", padx=(0, 18))

        ttk.Label(row1, text="质量").pack(side="left")
        self.quality_spin = ttk.Spinbox(
            row1, from_=1, to=100, textvariable=self.var_quality, width=5
        )
        self.quality_spin.pack(side="left", padx=(6, 4))
        ttk.Label(row1, text="（默认 100）", foreground="#777777").pack(side="left", padx=(0, 18))
        ttk.Checkbutton(row1, text="灰度图", variable=self.var_grayscale).pack(side="left")

        row2 = ttk.Frame(image_box)
        row2.grid(row=1, column=0, sticky="w", padx=8, pady=(2, 8))
        ttk.Label(row2, text="页码范围").pack(side="left")
        self.pages_entry = ttk.Entry(row2, textvariable=self.var_pages, width=16)
        self.pages_entry.pack(side="left", padx=(6, 4))
        self.pages_entry.insert(0, _PLACEHOLDER)
        self.pages_entry.configure(foreground="#9a9a9a")
        self.pages_entry.bind("<FocusIn>", self._pages_focus_in)
        self.pages_entry.bind("<FocusOut>", self._pages_focus_out)
        ttk.Label(row2, text="留空=全部页面，例：1-3,5,8-", foreground="#777777").pack(
            side="left", padx=(0, 22)
        )

        ttk.Label(row2, text="文件名").pack(side="left")
        ttk.Entry(row2, textvariable=self.var_template, width=26).pack(side="left", padx=(6, 4))
        ttk.Label(
            row2, text="可用占位符：{name} {page} {page0} {total} {ext}", foreground="#777777"
        ).pack(side="left")

        # 默认值就是最高画质，这里明确写出来，避免用户以为要手动调
        ttk.Label(
            image_box,
            text="默认最高画质：无损 PNG · 300 DPI · 质量 100"
                 "（JPG ≥95 关闭色度抽样，WebP 质量 100 自动无损）",
            foreground="#2f6fd0",
        ).grid(row=2, column=0, sticky="w", padx=8, pady=(0, 8))

        out_box = ttk.LabelFrame(settings, text=" 导出位置 ")
        out_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        out_box.columnconfigure(0, weight=1)

        out_row1 = ttk.Frame(out_box)
        out_row1.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        ttk.Radiobutton(
            out_row1,
            text=f"当前目录（{default_output_dir()}）",
            value="cwd",
            variable=self.var_target,
            command=self._sync_custom_dir_state,
        ).pack(side="left")
        ttk.Radiobutton(
            out_row1,
            text="与每个 PDF 相同的目录",
            value="source",
            variable=self.var_target,
            command=self._sync_custom_dir_state,
        ).pack(side="left", padx=(16, 0))
        ttk.Radiobutton(
            out_row1,
            text="指定目录",
            value="custom",
            variable=self.var_target,
            command=self._sync_custom_dir_state,
        ).pack(side="left", padx=(16, 6))
        self.dir_entry = ttk.Entry(out_row1, textvariable=self.var_custom_dir)
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.dir_button = ttk.Button(out_row1, text="浏览…", command=self.pick_output_dir, width=8)
        self.dir_button.pack(side="left")

        out_row2 = ttk.Frame(out_box)
        out_row2.grid(row=1, column=0, sticky="w", padx=8, pady=(2, 8))
        ttk.Checkbutton(out_row2, text="覆盖同名文件", variable=self.var_overwrite).pack(side="left")
        ttk.Checkbutton(out_row2, text="每个 PDF 建子文件夹", variable=self.var_subfolder).pack(
            side="left", padx=(16, 0)
        )
        ttk.Checkbutton(out_row2, text="文件夹递归子目录", variable=self.var_recursive).pack(
            side="left", padx=(16, 0)
        )
        ttk.Checkbutton(out_row2, text="完成后打开目录", variable=self.var_open_dir).pack(
            side="left", padx=(16, 0)
        )
        self._sync_custom_dir_state()
        self._on_format_change()

        # ---- 进度与日志 ----
        bottom = ttk.Frame(root)
        bottom.grid(row=5, column=0, sticky="ew", padx=12, pady=(8, 12))
        bottom.columnconfigure(0, weight=1)

        actions = ttk.Frame(bottom)
        actions.grid(row=0, column=0, sticky="ew")
        actions.columnconfigure(2, weight=1)
        self.btn_start = ttk.Button(actions, text="开始转换", command=self.start_conversion)
        self.btn_start.grid(row=0, column=0, padx=(0, 6))
        self.btn_cancel = ttk.Button(actions, text="取消", command=self.cancel_conversion, state="disabled")
        self.btn_cancel.grid(row=0, column=1)
        self.btn_open = ttk.Button(actions, text="打开导出目录", command=self.open_output_dir)
        self.btn_open.grid(row=0, column=3, sticky="e")

        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(8, 2))
        self.lbl_status = ttk.Label(bottom, text="就绪", foreground="#333333")
        self.lbl_status.grid(row=2, column=0, sticky="w")

        log_frame = ttk.Frame(bottom)
        log_frame.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=5, wrap="none", state="disabled", background="#fbfbfb")
        self.log.grid(row=0, column=0, sticky="ew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

        tip = (
            "提示：可直接把文件从资源管理器/Finder 拖进窗口；也支持 Ctrl+V 粘贴路径。"
            if DND_AVAILABLE
            else "提示：当前缺少拖拽库 tkinterdnd2，拖拽不可用。安装命令：pip install tkinterdnd2"
        )
        ttk.Label(root, text=tip, foreground="#777777").grid(row=6, column=0, sticky="w", padx=12, pady=(0, 8))

    # ------------------------------------------------------------------ 交互
    def _bind_keys(self) -> None:
        self.root.bind("<Control-o>", lambda _e: self.pick_files())
        self.root.bind("<Control-O>", lambda _e: self.pick_files())
        self.root.bind("<Control-v>", self._on_paste)
        self.root.bind("<Control-V>", self._on_paste)
        self.root.bind("<Delete>", lambda _e: self.remove_selected())

    def _setup_dnd(self) -> None:
        if not DND_AVAILABLE:
            return
        for widget in (self.root, self.drop_frame, self.drop_label, self.tree):
            try:
                widget.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
                widget.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
                widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)  # type: ignore[attr-defined]
                widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - 个别控件不支持
                continue

    def _on_drop_enter(self, _event) -> None:
        self.drop_frame.configure(bg=DROP_BG_ACTIVE)
        self.drop_label.configure(bg=DROP_BG_ACTIVE)

    def _on_drop_leave(self, _event) -> None:
        self.drop_frame.configure(bg=DROP_BG)
        self.drop_label.configure(bg=DROP_BG)

    def _on_drop(self, event) -> None:
        self._on_drop_leave(event)
        try:
            raw = self.root.tk.splitlist(event.data)
        except Exception:  # pragma: no cover - 非文件类型的拖拽
            raw = str(event.data).split()
        self.add_paths([Path(item) for item in raw if str(item).strip()])

    def _on_paste(self, _event=None) -> None:
        self.paste_paths()
        return "break"

    def _pages_focus_in(self, _event=None) -> None:
        if self.var_pages.get().strip() == _PLACEHOLDER:
            self.var_pages.set("")
            self.pages_entry.configure(foreground="#000000")

    def _pages_focus_out(self, _event=None) -> None:
        if not self.var_pages.get().strip():
            self.pages_entry.delete(0, "end")
            self.pages_entry.insert(0, _PLACEHOLDER)
            self.pages_entry.configure(foreground="#9a9a9a")

    def _page_range_value(self) -> str:
        """取真实页码范围；占位提示文字按“留空 = 全部页面”处理。"""
        value = self.var_pages.get().strip()
        return "" if value == _PLACEHOLDER or value.startswith(_PLACEHOLDER) else value

    def _on_format_change(self, _event=None) -> None:
        fmt = self._selected_format()
        state = "normal" if fmt in ("jpg", "webp") else "disabled"
        self.quality_spin.configure(state=state)

    def _selected_format(self) -> str:
        raw = self.var_format.get().split("—")[0].strip().lower()
        return raw if raw in IMAGE_FORMATS else DEFAULT_FORMAT

    def _sync_custom_dir_state(self) -> None:
        custom = self.var_target.get() == "custom"
        self.dir_entry.configure(state="normal" if custom else "disabled")
        self.dir_button.configure(state="normal" if custom else "disabled")

    # ------------------------------------------------------------- 列表维护
    def _next_iid(self) -> str:
        self._counter += 1
        return f"row{self._counter}"

    def add_paths(self, paths: list[Path]) -> None:
        """把用户拖入/选择的路径加进列表（目录会被展开成 PDF）。"""
        if not paths:
            return
        to_scan: list[Path] = []
        ignored = 0
        added = 0
        recursive = self.var_recursive.get()

        for path in paths:
            if path.is_dir():
                walker = path.rglob("*") if recursive else path.glob("*")
                children = sorted((c for c in walker if c.is_file()), key=lambda p: str(p).lower())
                if not children:
                    ignored += 1
                for child in children:
                    if child.suffix.lower() != ".pdf":
                        ignored += 1
                        continue
                    if self._register(child, to_scan):
                        added += 1
            elif path.is_file():
                if path.suffix.lower() != ".pdf":
                    ignored += 1
                    continue
                if self._register(path, to_scan):
                    added += 1
            else:
                ignored += 1

        if added:
            # 先入队再启动线程，避免扫描线程看到空队列提前退出
            for item in to_scan:
                self._scan_jobs.put(item)
            self._start_scanner()
        parts = [f"已添加 {added} 个 PDF"]
        if ignored:
            parts.append(f"忽略 {ignored} 个非 PDF / 无效路径")
        self.set_status("，".join(parts))
        self._log("，".join(parts))
        self._refresh_count()

    def _register(self, path: Path, to_scan: list[Path]) -> bool:
        try:
            key = str(path.resolve()).casefold()
        except OSError:  # pragma: no cover
            key = str(path).casefold()
        if key in self.by_path:
            return False
        iid = self._next_iid()
        entry = Entry(path=path, iid=iid)
        self.entries.append(entry)
        self.by_iid[iid] = entry
        self.by_path[key] = entry
        self.tree.insert(
            "", "end", iid=iid,
            values=(path.name, str(path.parent), "…", "读取中…"),
            tags=("running",),
        )
        to_scan.append(path)
        return True

    def remove_selected(self) -> None:
        if self._running:
            return
        for iid in list(self.tree.selection()):
            entry = self.by_iid.pop(iid, None)
            if entry is None:
                continue
            self.entries.remove(entry)
            try:
                self.by_path.pop(str(entry.path.resolve()).casefold(), None)
            except OSError:  # pragma: no cover
                pass
            self.tree.delete(iid)
        self._refresh_count()

    def clear_all(self) -> None:
        if self._running:
            return
        self.tree.delete(*self.tree.get_children())
        self.entries.clear()
        self.by_iid.clear()
        self.by_path.clear()
        self.progress["value"] = 0
        self.set_status("已清空列表")
        self._refresh_count()

    def pick_files(self) -> None:
        files = filedialog.askopenfilenames(
            title="选择 PDF 发票（可多选）",
            initialdir=self._last_dir,
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if files:
            self._last_dir = str(Path(files[0]).parent)
            self.add_paths([Path(f) for f in files])

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择包含 PDF 的文件夹", initialdir=self._last_dir)
        if folder:
            self._last_dir = folder
            self.add_paths([Path(folder)])

    def pick_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="选择导出目录", initialdir=self.var_custom_dir.get() or self._last_dir)
        if folder:
            self.var_custom_dir.set(folder)
            self.var_target.set("custom")
            self._sync_custom_dir_state()

    def paste_paths(self) -> None:
        """从剪贴板读取路径（拖拽不可用或路径已在剪贴板时的兜底）。"""
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            self.set_status("剪贴板里没有文本")
            return
        candidates: list[Path] = []
        for line in str(text).splitlines():
            cleaned = line.strip().strip('"').strip("'")
            if not cleaned:
                continue
            path = Path(cleaned)
            if path.exists():
                candidates.append(path)
        if not candidates:
            self.set_status("剪贴板里没有找到有效路径")
            return
        self.add_paths(candidates)

    # --------------------------------------------------------------- 扫描线程
    def _start_scanner(self) -> None:
        if self.scanner and self.scanner.is_alive():
            return
        self.scanner = threading.Thread(target=self._scan_loop, name="pdf-scan", daemon=True)
        self.scanner.start()

    def _scan_loop(self) -> None:
        """后台线程：读取每个 PDF 的页数/是否加密，结果回传主线程。

        连续几轮取不到任务才退出，避免“刚入队线程就退出”的竞争。
        """
        idle_rounds = 0
        while idle_rounds < 5:
            try:
                path = self._scan_jobs.get(timeout=0.2)
            except queue.Empty:
                idle_rounds += 1
                continue
            idle_rounds = 0
            info = describe_pdf(path)
            self.queue.put(("scan", path, info))

    # --------------------------------------------------------------- 转换流程
    def collect_options(self) -> ConvertOptions:
        target = self.var_target.get()
        if target == "custom":
            raw = self.var_custom_dir.get().strip()
            if not raw:
                raise ConversionError("请选择导出目录，或改选「当前目录」")
            output_dir = Path(raw).expanduser()
        else:
            output_dir = None  # 由 convert_files 决定（当前目录 / 源文件目录）
        try:
            quality = int(float(self.var_quality.get()))
        except ValueError:
            raise ConversionError("图片质量需要是 1-100 的数字")
        return ConvertOptions(
            output_dir=output_dir,
            image_format=self._selected_format(),
            dpi=self.var_dpi.get().strip() or DEFAULT_DPI,
            quality=quality,
            page_range=self._page_range_value(),
            name_template=self.var_template.get(),
            overwrite=self.var_overwrite.get(),
            grayscale=self.var_grayscale.get(),
            subfolder_per_pdf=self.var_subfolder.get(),
        )

    def start_conversion(self) -> None:
        if self._running:
            return
        if not self.entries:
            messagebox.showinfo(APP_TITLE, "请先拖入或添加 PDF 文件。")
            return
        try:
            options = self.collect_options()
        except ConversionError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        pending = [e for e in self.entries if e.status == "读取中…"]
        if pending:
            for entry in pending:
                info = describe_pdf(entry.path)
                self._apply_scan_result(entry, info)

        entries = [e for e in self.entries if e.error is None]
        if not entries:
            messagebox.showwarning(APP_TITLE, "列表里没有可以转换的 PDF（请检查失败原因）。")
            return

        self._retry_attempted = False
        self._log("-" * 40)
        self._log(f"开始转换：{len(entries)} 个文件 → {self._describe_target(options)}")
        for entry in entries:
            entry.status = "等待中"
            entry.error = None
            self.tree.item(entry.iid, values=(entry.path.name, str(entry.path.parent), entry.pages or "…", "等待中"), tags=("running",))
        self._set_running(True)
        self.progress["value"] = 0
        self.cancel_event.clear()
        # 注意：Tk 变量只能在主线程读取，这里先取好再交给后台线程
        to_source = self.var_target.get() == "source"
        self.worker = threading.Thread(
            target=self._convert_worker,
            args=(list(entries), options, to_source),
            name="convert",
            daemon=True,
        )
        self.worker.start()

    def _describe_target(self, options: ConvertOptions) -> str:
        target = self.var_target.get()
        if target == "source":
            return "每个 PDF 所在目录"
        return str(options.resolved_output_dir())

    def _convert_worker(self, entries: list[Entry], options: ConvertOptions, to_source: bool) -> None:
        """后台线程：按密码分组调用核心引擎，事件通过 queue 回传。

        这里绝对不能读写任何 Tk 控件或 Tk 变量（Tkinter 不是线程安全的）。
        """
        groups: dict[str, list[Entry]] = {}
        for entry in entries:
            groups.setdefault(entry.password or "", []).append(entry)

        counts = {id(entry): self._entry_page_count(entry, options) for entry in entries}
        grand_total = max(sum(counts.values()), 1)

        merged = ConversionReport(output_dir=options.resolved_output_dir())
        offset = 0
        source_dir: Path | None = None
        try:
            for index, (password, group) in enumerate(groups.items()):
                if self.cancel_event.is_set():
                    break
                if len(groups) > 1:
                    self.queue.put(("status", f"第 {index + 1}/{len(groups)} 组（{len(group)} 个文件）"))
                group_options = replace(options, password=password or None)
                bridge = self._progress_bridge(offset, grand_total)
                if to_source:
                    # “与源文件同目录”：逐个文件调用，各自的导出目录=源文件所在目录
                    for entry in group:
                        if self.cancel_event.is_set():
                            break
                        report = convert_files(
                            [entry.path],
                            replace(group_options, output_dir=entry.path.parent),
                            on_progress=bridge,
                            cancel_event=self.cancel_event,
                        )
                        merged.files.extend(report.files)
                        merged.ignored.extend(report.ignored)
                        if source_dir is None:
                            source_dir = report.output_dir
                        offset += counts[id(entry)]
                else:
                    report = convert_files(
                        [e.path for e in group],
                        group_options,
                        on_progress=bridge,
                        cancel_event=self.cancel_event,
                    )
                    merged.files.extend(report.files)
                    merged.ignored.extend(report.ignored)
                    merged.output_dir = report.output_dir
                    offset += sum(counts[id(e)] for e in group)
        except BaseException as exc:  # noqa: BLE001 - 传回界面显示
            self.queue.put(("error", f"{exc}"))
            return

        merged.cancelled = self.cancel_event.is_set()
        if source_dir is not None:
            merged.output_dir = source_dir
        self.queue.put(("done", merged))

    def _entry_page_count(self, entry: Entry, options: ConvertOptions) -> int:
        try:
            return len(parse_page_spec(options.page_range, entry.pages))
        except ConversionError:
            return entry.pages or 1

    def _progress_bridge(self, offset: int, grand_total: int):
        def callback(event: Progress) -> None:
            self.queue.put(("progress", offset, grand_total, event))

        return callback

    # --------------------------------------------------------------- 事件循环
    def _drain_queue(self) -> None:
        if self._closed:
            return
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "scan":
                    _, path, info = item
                    entry = self._entry_for(path)
                    if entry is not None:
                        self._apply_scan_result(entry, info)
                elif kind == "progress":
                    _, offset, grand_total, event = item
                    self._apply_progress(offset, grand_total, event)
                elif kind == "status":
                    self.set_status(item[1])
                elif kind == "done":
                    self._on_done(item[1])
                elif kind == "error":
                    self._on_error(item[1])
        except queue.Empty:
            pass
        finally:
            if not self._closed:
                try:
                    if self.root.winfo_exists():
                        self._after_id = self.root.after(100, self._drain_queue)
                except tk.TclError:  # pragma: no cover - 窗口已销毁
                    pass

    def _entry_for(self, path: Path) -> Entry | None:
        try:
            key = str(path.resolve()).casefold()
        except OSError:  # pragma: no cover
            key = str(path).casefold()
        return self.by_path.get(key)

    def _apply_scan_result(self, entry: Entry, info) -> None:
        if info.error:
            icon = "🔒 " if "加密" in (info.error or "") else "⚠ "
            entry.pages = 0
            entry.error = info.error
            entry.status = f"{icon}{info.error}"
            self.tree.item(
                entry.iid,
                values=(entry.path.name, str(entry.path.parent), "—", entry.status),
                tags=("failed",),
            )
            self._log(f"跳过 {entry.path.name}：{info.error}")
            if "加密" in (info.error or ""):
                self._ask_password(entry)
        else:
            entry.pages = info.pages
            entry.error = None
            entry.status = "待转换"
            self.tree.item(
                entry.iid,
                values=(entry.path.name, str(entry.path.parent), info.pages, "待转换"),
            )
        self._refresh_count()

    def _ask_password(self, entry: Entry) -> None:
        for _ in range(3):
            password = simpledialog.askstring(
                APP_TITLE, f"「{entry.path.name}」已加密，请输入密码：", show="*", parent=self.root
            )
            if password is None:
                return
            info = describe_pdf(entry.path, password)
            if not info.error:
                entry.password = password
                entry.pages = info.pages
                entry.error = None
                entry.status = "待转换"
                self.tree.item(
                    entry.iid,
                    values=(entry.path.name, str(entry.path.parent), info.pages, "待转换"),
                )
                self._log(f"{entry.path.name}：密码正确")
                return
            if "密码" not in info.error:
                messagebox.showerror(APP_TITLE, info.error)
                return
        messagebox.showwarning(APP_TITLE, f"「{entry.path.name}」密码不正确，已跳过。")

    def _apply_progress(self, offset: int, grand_total: int, event: Progress) -> None:
        done = offset + event.done
        self.progress["value"] = min(100.0, done * 100.0 / max(grand_total, 1))
        self.set_status(f"[{done}/{grand_total}] {event.message}")
        if event.path is None:
            return
        entry = self._entry_for(event.path)
        if entry is None:
            return
        if event.event == "file_failed":
            entry.status = "失败"
            entry.error = event.message
            self.tree.item(
                entry.iid,
                values=(entry.path.name, str(entry.path.parent), entry.pages, "失败"),
                tags=("failed",),
            )
        else:
            self.tree.item(
                entry.iid,
                values=(entry.path.name, str(entry.path.parent), entry.pages, "转换中…"),
                tags=("running",),
            )
        if event.event in ("page",) and event.done % 10 == 0:
            self._log(event.message)

    def _on_done(self, report: ConversionReport) -> None:
        self._set_running(False)
        self.progress["value"] = 100 if report.outputs and not report.cancelled else self.progress["value"]

        for entry in self.entries:
            outcome = next((o for o in report.files if o.path == entry.path), None)
            if outcome is None:
                continue
            if outcome.status == "ok":
                entry.status = f"✓ 完成（{outcome.written} 张）"
                entry.error = None
                self.tree.item(
                    entry.iid,
                    values=(entry.path.name, str(entry.path.parent), entry.pages, entry.status),
                    tags=("ok",),
                )
            elif outcome.status == "failed":
                entry.status = f"✗ {outcome.error}"
                entry.error = outcome.error
                self.tree.item(
                    entry.iid,
                    values=(entry.path.name, str(entry.path.parent), entry.pages or "—", entry.status),
                    tags=("failed",),
                )
            elif outcome.error:
                entry.status = f"- {outcome.error}"
                self.tree.item(
                    entry.iid,
                    values=(entry.path.name, str(entry.path.parent), entry.pages or "—", entry.status),
                )

        self.set_status(report.summary())
        self._log(report.summary())
        for outcome in report.failures:
            self._log(f"失败：{outcome.name} → {outcome.error}")

        password_failures = [
            o for o in report.failures if o.error and ("加密" in o.error or "密码" in o.error)
        ]
        if password_failures and not self._retry_attempted:
            self._retry_attempted = True
            pending = [self._entry_for(o.path) for o in password_failures]
            pending = [e for e in pending if e is not None]
            solved = False
            for entry in pending:
                self._ask_password(entry)
                solved = solved or entry.password is not None
            if solved:
                self._log("使用新密码重试…")
                self.start_conversion()
                return

        if report.outputs:
            message = (
                f"完成：{len(report.succeeded)} 个文件 / {report.done_pages} 张图片\n"
                f"导出目录：{report.output_dir}"
            )
            if report.failures:
                message += f"\n失败 {len(report.failures)} 个文件，详见日志。"
            if report.cancelled:
                message = "已取消。\n" + message
            messagebox.showinfo(APP_TITLE, message)
            if self.var_open_dir.get():
                try:
                    open_path(report.output_dir)
                except Exception as exc:  # pragma: no cover
                    self._log(f"无法打开目录：{exc}")
        elif report.cancelled:
            messagebox.showinfo(APP_TITLE, "已取消，没有导出图片。")
        else:
            details = "\n".join(f"• {o.name}：{o.error}" for o in report.failures[:8])
            messagebox.showwarning(APP_TITLE, "没有导出任何图片。\n" + (details or "请检查文件与设置。"))

    def _on_error(self, message: str) -> None:
        self._set_running(False)
        self.set_status(f"出错：{message}")
        self._log(f"出错：{message}")
        messagebox.showerror(APP_TITLE, message)

    def cancel_conversion(self) -> None:
        if not self._running:
            return
        self.cancel_event.set()
        self.set_status("正在取消…")
        self._log("用户取消了转换")

    def open_output_dir(self) -> None:
        target = self.var_target.get()
        try:
            if target == "source" and self.entries:
                path = self.entries[0].path.parent
            elif target == "custom":
                path = Path(self.var_custom_dir.get().strip() or ".")
            else:
                path = default_output_dir()
            if not path.exists():
                messagebox.showinfo(APP_TITLE, f"目录还不存在：{path}")
                return
            open_path(path)
        except Exception as exc:  # pragma: no cover
            messagebox.showerror(APP_TITLE, f"无法打开目录：{exc}")

    # ------------------------------------------------------------------ 杂项
    def _set_running(self, running: bool) -> None:
        self._running = running
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_cancel.configure(state="normal" if running else "disabled")

    def _refresh_count(self) -> None:
        total_pages = sum(e.pages for e in self.entries)
        self.lbl_count.configure(text=f"共 {len(self.entries)} 个文件 / {total_pages} 页")

    def set_status(self, text: str) -> None:
        self.lbl_status.configure(text=text)

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        # 控制日志长度，避免长时间运行后占用过多内存
        try:
            lines = int(float(self.log.index("end-1c").split(".")[0]))
            if lines > 600:
                self.log.delete("1.0", f"{lines - 500}.0")
        except Exception:  # pragma: no cover
            pass
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno(APP_TITLE, "正在转换，确定要退出吗？"):
                return
            self.cancel_event.set()
        self._closed = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:  # pragma: no cover
                pass
            self._after_id = None
        self.root.destroy()

    def destroy(self) -> None:
        """供脚本/测试主动关闭（等价于点右上角关闭）。"""
        self._on_close()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(initial_files: list[Path] | None = None) -> int:
    problems = check_dependencies()
    root = create_root()
    if problems:
        root.withdraw()
        messagebox.showerror(
            APP_TITLE,
            "缺少运行依赖：\n" + "\n".join(problems) + "\n\n请执行：pip install -r requirements.txt",
        )
        root.destroy()
        return 2
    App(root, initial_files=initial_files)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
