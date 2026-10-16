# -*- coding: utf-8 -*-
"""命令行界面：``python pdf2image_app.py --cli 发票.pdf -o 输出目录``。

图形界面里能用到的能力命令行都有，方便批量脚本化调用（两个平台语法一致）。
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

from . import __version__
from .core import (
    DEFAULT_DPI,
    DEFAULT_FORMAT,
    DEFAULT_QUALITY,
    DEFAULT_TEMPLATE,
    IMAGE_FORMATS,
    ConversionError,
    ConvertOptions,
    Progress,
    check_dependencies,
    convert_files,
    natural_key,
    open_path,
)

PROG = "pdf2img"


def _streams() -> tuple[object, object]:
    """窗口模式（PyInstaller --windowed）下 sys.stdout/stderr 可能为 None。"""
    out = sys.stdout if sys.stdout is not None else open(os.devnull, "w", encoding="utf-8")
    err = sys.stderr if sys.stderr is not None else out
    return out, err


def _say(message: str = "", *, error: bool = False) -> None:
    out, err = _streams()
    try:
        print(message, file=err if error else out, flush=True)
    except Exception:  # pragma: no cover - 极端环境下不可打印
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="把 PDF 发票批量转成图片（PNG / JPG / TIFF / WebP）。",
        epilog="示例：pdf2img 发票/*.pdf -o 输出 -f jpg -d 300 --pages 1-2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", help="PDF 文件、目录或通配符；可以一次给多个")
    parser.add_argument("-o", "--out", default=None,
                        help="导出目录；不填则导出到当前目录（默认：当前工作目录）")
    parser.add_argument("-f", "--format", default=DEFAULT_FORMAT, choices=sorted(IMAGE_FORMATS),
                        help="图片格式（默认：%(default)s，无损、画质最好）")
    parser.add_argument("-d", "--dpi", type=int, default=DEFAULT_DPI,
                        help="渲染分辨率（默认：%(default)s，越高越清晰、文件越大）")
    parser.add_argument("-q", "--quality", type=int, default=DEFAULT_QUALITY,
                        help="JPG / WebP 质量 1-100（默认：%(default)s，最高画质；100 时 WebP 为无损）")
    parser.add_argument("-p", "--pages", default="", metavar="SPEC",
                        help='页码范围，如 "1-3,5,8-"；不填表示全部页面')
    parser.add_argument("-t", "--template", default=DEFAULT_TEMPLATE,
                        help="文件名模板（默认：%(default)s），可用 "
                             "{name} {page} {page0} {total} {ext} {dpi}")
    parser.add_argument("--overwrite", action="store_true", help="覆盖同名文件（默认自动改名）")
    parser.add_argument("--gray", "--grayscale", dest="grayscale", action="store_true",
                        help="输出灰度图")
    parser.add_argument("--subfolder", action="store_true", help="每个 PDF 建一个同名子文件夹")
    parser.add_argument("--no-recursive", action="store_true", help="输入目录时不递归子目录")
    parser.add_argument("--password", default=None, help="加密 PDF 的密码")
    parser.add_argument("--workers", type=int, default=None, help="并行线程数（默认自动）")
    parser.add_argument("--open", dest="open_when_done", action="store_true",
                        help="转换完成后打开导出目录")
    parser.add_argument("--quiet", action="store_true", help="不输出进度，只输出结果摘要")
    parser.add_argument("--selftest", action="store_true",
                        help="自检：检查依赖、拖拽库、渲染与编码是否正常（打包后验证用）")
    parser.add_argument("--selftest-report", default="pdf2img-selftest.txt", metavar="PATH",
                        help="自检报告写入的文件（默认：%(default)s）")
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    return parser


def _selftest(report_path: str | None) -> int:
    """不弹界面的自检：验证依赖、资源与一次完整的“PDF -> 图片”。"""
    import tempfile

    lines: list[str] = []
    ok = True

    def check(name: str, func) -> None:
        nonlocal ok
        try:
            detail = func()
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"[失败] {name}：{exc.__class__.__name__}: {exc}")
        else:
            lines.append(f"[通过] {name}：{detail}")

    lines.append(f"pdf2img {__version__}")
    lines.append(f"Python {sys.version.split()[0]}  ({sys.platform})")
    lines.append(f"打包运行：{'是' if getattr(sys, 'frozen', False) else '否'}")
    if getattr(sys, "frozen", False):
        lines.append(f"程序目录：{Path(sys.executable).parent}")
    lines.append("")

    check("PyMuPDF", lambda: __import__("pymupdf").__version__)
    check("Pillow", lambda: __import__("PIL").__version__)
    check("tkinterdnd2（拖拽）", lambda: __import__("tkinterdnd2").__name__)

    def check_tkdnd() -> str:
        from tkinterdnd2 import TkinterDnD

        root = TkinterDnD.Tk()
        try:
            root.withdraw()
            return f"tkdnd {root.tk.call('package', 'require', 'tkdnd')}"
        finally:
            root.destroy()

    check("tkdnd 动态库", check_tkdnd)

    def check_gui() -> str:
        from . import gui

        root = gui.create_root()
        root.withdraw()  # 自检时不弹窗
        try:
            app = gui.App(root)
            widgets = (app.tree, app.btn_start, app.btn_cancel, app.drop_frame, app.log, app.dir_entry)
            missing = [w for w in widgets if not w.winfo_exists()]
            if missing:
                raise RuntimeError(f"界面控件缺失：{len(missing)} 个")
            app.destroy()
            return f"界面构建正常（拖拽{'可用' if gui.DND_AVAILABLE else '不可用'}）"
        except BaseException:
            try:
                root.destroy()
            except Exception:
                pass
            raise

    check("图形界面", check_gui)

    def check_assets() -> str:
        from .gui import asset_path

        path = asset_path("icon-256.png")
        if not path.exists():
            raise FileNotFoundError(f"缺少图标资源 {path}")
        return str(path)

    check("图标资源", check_assets)

    def check_conversion() -> str:
        import pymupdf as fitz

        with tempfile.TemporaryDirectory(prefix="pdf2img-selftest-") as tmp:
            pdf = Path(tmp) / "selftest.pdf"
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((72, 120), "SELFTEST INVOICE", fontsize=20)
            doc.save(str(pdf))
            doc.close()
            report = convert_files([pdf], ConvertOptions(output_dir=tmp, dpi=100))
            if not report.outputs:
                raise RuntimeError(report.files[0].error or "没有生成图片")
            from PIL import Image

            with Image.open(report.outputs[0]) as image:
                if image.width < 700:
                    raise RuntimeError(f"图片尺寸异常：{image.size}")
                return f"{report.outputs[0].name} {image.size} {image.mode}"

    check("PDF 转图片", check_conversion)

    lines.append("")
    lines.append("自检结果：" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
    text = "\n".join(lines)
    _say(text)
    if report_path:
        try:
            target = Path(report_path).expanduser()
            target.write_text(text + "\n", encoding="utf-8")
            _say(f"报告已写入：{target}")
        except OSError as exc:
            _say(f"[提示] 无法写入报告文件：{exc}", error=True)
    return 0 if ok else 1


def _expand_globs(inputs: list[str]) -> list[str]:
    """在 shell 没有展开通配符时（Windows cmd / PowerShell）自己展开通配符。"""
    expanded: list[str] = []
    for item in inputs:
        if any(ch in item for ch in "*?["):
            matches = sorted(glob.glob(item, recursive=True), key=natural_key)
            if matches:
                expanded.extend(matches)
                continue
        expanded.append(item)
    return expanded


def main(argv: list[str] | None = None) -> int:
    # 窗口模式（PyInstaller --windowed）下标准流可能为 None，先兜底再做其它事
    if sys.stdout is None:  # pragma: no cover - 仅冻结的窗口程序会走到
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:  # pragma: no cover
        sys.stderr = sys.stdout

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest(args.selftest_report)

    problems = check_dependencies()
    if problems:
        _say("[错误] " + "；".join(problems), error=True)
        _say("安装依赖：pip install -r requirements.txt", error=True)
        return 2

    inputs = _expand_globs(args.inputs)
    if not inputs:
        parser.print_help()
        return 1

    options = ConvertOptions(
        output_dir=args.out,
        image_format=args.format,
        dpi=args.dpi,
        quality=args.quality,
        page_range=args.pages,
        name_template=args.template,
        overwrite=args.overwrite,
        grayscale=args.grayscale,
        subfolder_per_pdf=args.subfolder,
        password=args.password,
        workers=args.workers,
    )

    state = {"last": ""}

    def on_progress(event: Progress) -> None:
        if args.quiet:
            return
        if event.event == "page":
            line = f"[{event.done}/{event.total}] {event.message}"
        else:
            line = event.message
        if line != state["last"]:
            state["last"] = line
            _say(line)

    try:
        report = convert_files(
            inputs, options, on_progress=on_progress, recursive=not args.no_recursive
        )
    except ConversionError as exc:
        _say(f"[错误] {exc}", error=True)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - 手动中断
        _say("\n[中断] 已停止", error=True)
        return 130

    if not report.files:
        _say("[提示] 没有找到任何 PDF 文件。", error=True)
        for item in report.ignored[:10]:
            _say(f"        忽略：{item}", error=True)
        return 1

    _say("-" * 60)
    _say(f"导出目录：{report.output_dir}")
    for outcome in report.files:
        if outcome.status == "ok":
            _say(f"  ✓ {outcome.name}：{outcome.written} 张图片")
        elif outcome.status == "failed":
            _say(f"  ✗ {outcome.name}：{outcome.error}", error=True)
        else:
            _say(f"  - {outcome.name}：跳过（{outcome.error or '无可用页面'}）")
    _say(report.summary())

    if args.open_when_done:
        try:
            open_path(report.output_dir)
        except Exception as exc:  # pragma: no cover
            _say(f"[提示] 无法自动打开目录：{exc}")

    return 0 if report.outputs and not report.failures else (1 if report.failures else 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
