# -*- coding: utf-8 -*-
"""生成界面截图（README / 说明用），并顺带做一次真实拖入→转换的冒烟检查。

用法::

    python tools/gui_screenshot.py [输出图片路径] [--keep-open 秒数]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf as fitz

from invoice2img import gui


def make_invoice(path: Path, number: str, pages: int = 1) -> Path:
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 90), f"增值税电子普通发票  {number}", fontsize=15)
        page.insert_text((60, 140), f"发票代码 011002100311   第 {index + 1}/{pages} 页", fontsize=10)
        page.draw_rect(fitz.Rect(60, 170, 535, 420), color=(0.6, 0.6, 0.6), width=0.8)
        for row in range(6):
            y = 205 + row * 35
            page.insert_text((80, y), f"项目名称 {row + 1}", fontsize=10)
            page.insert_text((330, y), f"¥ {100 + row * 37}.00", fontsize=10)
            page.draw_line(fitz.Point(60, y + 12), fitz.Point(535, y + 12), color=(0.8, 0.8, 0.8), width=0.4)
        page.insert_text((60, 470), "价税合计（大写）壹仟贰佰叁拾肆圆整", fontsize=10)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return path


class QuietMessageBox:
    def _noop(self, *_args, **_kwargs):
        return True

    showinfo = showwarning = showerror = askyesno = _noop


def pump(root, predicate, timeout: float = 60.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def screen_scale() -> float:
    """Windows 上 Tk 用的是逻辑像素，抓屏用的是物理像素，需要换算（高 DPI 缩放）。"""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            return ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
        except Exception:
            return 1.0
    return 1.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", default="assets/screenshot.png")
    parser.add_argument("--keep-open", type=float, default=0.0, help="截图后保持窗口显示的秒数")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="p2i-shot-"))
    files = [
        make_invoice(tmp / "增值税发票_20260301.pdf", "No.0001", pages=2),
        make_invoice(tmp / "增值税发票_20260302.pdf", "No.0002"),
        make_invoice(tmp / "差旅电子发票_0325.pdf", "No.0003"),
    ]
    out_dir = Path("D:/pdf2image/demo-output") if sys.platform.startswith("win") else Path("./demo-output")

    gui.messagebox = QuietMessageBox()  # type: ignore[assignment]

    root = gui.create_root()
    app = gui.App(root)

    # 走真实的拖拽事件解析路径
    app._on_drop(SimpleNamespace(data=" ".join("{%s}" % f for f in files)))
    pump(root, lambda: all(e.status != "读取中…" for e in app.entries), 20)

    # 真跑一次转换，让截图里能看到“完成”状态（不改 DPI/格式，展示的就是默认最高画质）
    app.var_target.set("custom")
    app.var_custom_dir.set(str(out_dir))
    app.var_open_dir.set(False)
    app.start_conversion()
    pump(root, lambda: not app._running, 120)
    time.sleep(0.4)
    root.update()

    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import ImageGrab

        # 让窗口短暂置顶，否则会被其它窗口挡住（截图靠抓屏实现）
        root.deiconify()
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            root.update()
            time.sleep(0.05)
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        scale = screen_scale()
        bbox = (
            int(x * scale),
            int(y * scale),
            int((x + w) * scale),
            int((y + h) * scale),
        )
        image = ImageGrab.grab(bbox=bbox)
        if scale != 1.0:  # 超采样后再缩回逻辑尺寸，文字更清晰
            image = image.resize((w, h), 1)  # 1 = Image.LANCZOS
        image.save(target)
        root.attributes("-topmost", False)
        print(f"截图已保存：{target}  ({image.width}x{image.height}, 屏幕缩放 {scale:g}x)")
    except Exception as exc:
        print(f"[警告] 截图失败：{exc}", file=sys.stderr)

    print("界面状态：")
    for entry in app.entries:
        print(f"  {entry.path.name}: {entry.status}")
    print(f"导出目录内容：{sorted(p.name for p in out_dir.glob('*.png'))}")

    if args.keep_open:
        time.sleep(args.keep_open)
    app.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
