# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Windows / macOS 共用）。

产物
----
* ``PDF发票转图片``  —— 图形界面版（Windows 为 .exe，macOS 为 .app）
* ``pdf2img``        —— 命令行版（Windows 为 .exe，macOS 为可执行文件）

用法::

    python -m PyInstaller --noconfirm --clean packaging/pdf2image.spec
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入
IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

# tkinterdnd2 自带 tkdnd 动态库，必须一起打包，否则拖拽功能会失效
datas = collect_data_files("tkinterdnd2")
datas += [(str(ROOT / "assets"), "assets")]

hiddenimports = collect_submodules("tkinterdnd2")

# 只排除明确用不到的重型依赖，避免影响 PyMuPDF / Pillow 的间接导入
excludes = [
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "IPython",
    "pytest",
    "notebook",
]

# macOS 上可以用 PDF2IMG_TARGET_ARCH=universal2 构建通用二进制（Intel + Apple Silicon）
TARGET_ARCH = os.environ.get("PDF2IMG_TARGET_ARCH", "").strip()
arch_kwargs = {"target_arch": TARGET_ARCH} if (IS_MACOS and TARGET_ARCH) else {}

# 图标：Windows 用 .ico，macOS 用 .icns（.icns 由 packaging/build_macos.sh 生成）
icon_file = None
icon_candidates = ["icon.ico"] if IS_WINDOWS else ["icon.icns", "icon.ico"]
for candidate in icon_candidates:
    path = ROOT / "assets" / candidate
    if path.exists():
        icon_file = str(path)
        break
if IS_MACOS and (icon_file is None or not icon_file.endswith(".icns")):
    # BUNDLE 只认 .icns，传别的会让打包失败；这里降级为“无自定义图标”
    icon_file = None
    print("[spec] 未找到 assets/icon.icns，将使用 Python 默认图标。"
          "可先运行 tools/make_icon.py + sips/iconutil 生成。")

a = Analysis(  # noqa: F821
    [str(ROOT / "pdf2image_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

GUI_NAME = "PDF发票转图片"
CLI_NAME = "pdf2img"

gui_exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=GUI_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 图形界面，不要黑色控制台窗口
    disable_windowed_traceback=False,
    icon=icon_file,
    **arch_kwargs,
)

cli_exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=CLI_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # 命令行版保留标准输出，方便脚本调用
    icon=icon_file,
    **arch_kwargs,
)

if IS_MACOS:
    app = BUNDLE(  # noqa: F821
        gui_exe,
        name=f"{GUI_NAME}.app",
        icon=icon_file,
        bundle_identifier="com.local.pdf2image",
        info_plist={
            "CFBundleName": GUI_NAME,
            "CFBundleDisplayName": GUI_NAME,
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.15",
            "LSApplicationCategoryType": "public.app-category.productivity",
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "PDF 文档",
                    "CFBundleTypeRole": "Viewer",
                    "LSItemContentTypes": ["com.adobe.pdf"],
                }
            ],
        },
    )
