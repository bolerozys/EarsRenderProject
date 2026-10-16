#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 发票转图片 —— 统一入口。

用法
----
* 不带参数（或双击/拖文件到图标上）：打开图形界面。::

      python pdf2image_app.py

* 带参数：走命令行模式（适合批处理脚本）。::

      python pdf2image_app.py --cli 发票.pdf -o 输出目录 -f jpg -d 300
      python pdf2image_app.py --help

在 macOS 上通过 Finder 启动时，系统可能附带 ``-psn_...`` 参数，这里会自动忽略。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 保证从任意工作目录启动都能 import 到本目录下的包
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _clean_args(argv: list[str]) -> list[str]:
    """去掉 macOS Finder 附加的 -psn_* 等无意义参数。"""
    result: list[str] = []
    for arg in argv[1:]:
        if arg.startswith("-psn_"):
            continue
        result.append(arg)
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    args = _clean_args(argv)

    # 判断模式：显式 --cli，或出现任何选项，都按命令行处理；
    # 只有一串文件/目录时才打开界面并预加载它们。
    force_cli = "--cli" in args
    args = [a for a in args if a != "--cli"]
    is_cli = force_cli or any(a.startswith("-") for a in args)

    if is_cli:
        from invoice2img.cli import main as cli_main

        return cli_main(args)

    from invoice2img.gui import main as gui_main

    files = [Path(a).expanduser() for a in args]
    return gui_main(initial_files=files or None)


if __name__ == "__main__":
    if os.name == "nt":  # 冻结成 exe 时避免弹出多余的控制台窗口
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW("PDF 发票转图片")
        except Exception:  # pragma: no cover
            pass
    raise SystemExit(main())
