# -*- coding: utf-8 -*-
"""生成应用图标：assets/icon.png（窗口图标）与 assets/icon.ico（Windows 可执行文件）。

macOS 的 .icns 由 packaging/build_macos.sh 用系统自带的 iconutil 从 icon.png 生成。

用法::

    python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

BLUE = (47, 111, 208, 255)
BLUE_DARK = (30, 84, 168, 255)
WHITE = (255, 255, 255, 255)
PAPER = (247, 250, 255, 255)
INK = (34, 68, 128, 255)
ARROW = (255, 196, 61, 255)
SIZE = 1024


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - 老版本 Pillow
        return ImageFont.load_default()


def draw_logo(size: int = SIZE) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = size / 1024.0

    # 圆角蓝色底板
    draw.rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(220 * unit), fill=BLUE
    )
    draw.rounded_rectangle(
        [int(24 * unit), int(24 * unit), size - int(24 * unit), size - int(24 * unit)],
        radius=int(200 * unit),
        outline=BLUE_DARK,
        width=int(18 * unit),
    )

    # 白色纸张（带折角）
    left, top, right, bottom = (190 * unit, 190 * unit, 690 * unit, 790 * unit)
    fold = 150 * unit
    draw.polygon(
        [
            (left, top + 30 * unit),
            (right - fold, top),
            (right, top + fold),
            (right, bottom - 30 * unit),
            (left, bottom),
        ],
        fill=PAPER,
    )
    draw.polygon(
        [(right - fold, top), (right, top + fold), (right - fold, top + fold)],
        fill=(214, 226, 245, 255),
    )

    # 纸张上的文字行，示意“发票内容”
    line_color = (168, 190, 222, 255)
    for index in range(5):
        y = top + (210 + index * 78) * unit
        width = (430 if index != 4 else 250) * unit
        draw.rounded_rectangle(
            [left + 60 * unit, y, left + 60 * unit + width, y + 34 * unit],
            radius=int(17 * unit),
            fill=line_color,
        )

    # 右侧“图片”相框 + 向下箭头，表示 PDF -> 图片
    frame = [640 * unit, 470 * unit, 900 * unit, 700 * unit]
    draw.rounded_rectangle(frame, radius=int(36 * unit), fill=WHITE, outline=WHITE, width=int(12 * unit))
    draw.rounded_rectangle(
        [frame[0] + 30 * unit, frame[1] + 130 * unit, frame[0] + 110 * unit, frame[3] - 40 * unit],
        radius=int(16 * unit),
        fill=(90, 150, 220, 255),
    )
    draw.rounded_rectangle(
        [frame[0] + 130 * unit, frame[1] + 70 * unit, frame[0] + 226 * unit, frame[3] - 40 * unit],
        radius=int(16 * unit),
        fill=(120, 190, 140, 255),
    )
    draw.polygon(
        [
            (765 * unit, 250 * unit),
            (855 * unit, 250 * unit),
            (855 * unit, 360 * unit),
            (905 * unit, 360 * unit),
            (810 * unit, 460 * unit),
            (715 * unit, 360 * unit),
            (765 * unit, 360 * unit),
        ],
        fill=ARROW,
    )
    return image


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    logo = draw_logo()
    png = ASSETS / "icon.png"
    logo.save(png)

    ico_sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    logo.save(ASSETS / "icon.ico", format="ICO", sizes=ico_sizes)

    # 窗口图标用 256px 就够，体积小一点
    logo.resize((256, 256), Image.LANCZOS).save(ASSETS / "icon-256.png")
    print(f"已生成：{png}")
    print(f"已生成：{ASSETS / 'icon.ico'}")
    print(f"已生成：{ASSETS / 'icon-256.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
