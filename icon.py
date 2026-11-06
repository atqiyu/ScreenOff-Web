# -*- coding: utf-8 -*-
"""icon.py —— 托盘图标与程序图标生成（不依赖外部图片资源）。"""

from __future__ import annotations

from PIL import Image, ImageDraw

GREEN = (63, 185, 80, 255)
GREY = (72, 79, 88, 255)
FRAME = (210, 218, 226, 255)
DARK = (22, 27, 34, 255)
RED = (248, 81, 73, 255)


def make_image(state: str = "on", size: int = 64) -> Image.Image:
    """画一个显示器图标：亮屏为绿色，灭屏为深灰。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0
    # 显示器外框
    outer = [int(8 * s), int(11 * s), int(56 * s), int(45 * s)]
    d.rounded_rectangle(outer, radius=int(5 * s), fill=DARK, outline=FRAME,
                        width=max(1, int(2 * s)))
    # 屏幕
    inner = [int(12 * s), int(15 * s), int(52 * s), int(41 * s)]
    screen = GREEN if state == "on" else GREY
    d.rounded_rectangle(inner, radius=int(2 * s), fill=screen)
    # 支架与底座
    d.rectangle([int(29 * s), int(45 * s), int(35 * s), int(52 * s)], fill=FRAME)
    d.rectangle([int(21 * s), int(52 * s), int(43 * s), int(56 * s)], fill=FRAME)
    # 灭屏时画一条小横线，模拟息屏
    if state != "on":
        d.rectangle([int(26 * s), int(27 * s), int(38 * s), int(29 * s)], fill=FRAME)
    return img


def save_ico(path: str) -> None:
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    make_image("on", 256).save(path, format="ICO", sizes=sizes)