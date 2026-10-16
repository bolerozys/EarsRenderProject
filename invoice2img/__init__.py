# -*- coding: utf-8 -*-
"""PDF 发票转图片工具（Windows / macOS 通用）。

包结构::

    invoice2img.core   转换引擎（无 GUI 依赖，可被 GUI / CLI / 测试复用）
    invoice2img.cli    命令行界面
    invoice2img.gui    图形界面（Tkinter + 拖拽）

版本号在 :data:`invoice2img.__version__` 中维护。
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
