# -*- coding: utf-8 -*-
"""生成程序图标 packaging/app.ico（纯标准库，无 Pillow 依赖）。

图案：深色圆角方块 + 两条反向箭头（绿色 / 蓝色），表示串口与网络双向互通。
用 3 倍超采样做抗锯齿，输出 16/24/32/48/64/128/256 共 7 种尺寸的 ICO。

    python packaging/make_icon.py                  # 生成 app.ico
    python packaging/make_icon.py --preview p.png  # 顺便导出 256 尺寸 PNG 便于查看
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib

BG = (0x16, 0x21, 0x2E, 0xFF)  # 深蓝灰
BORDER = (0x3A, 0x5A, 0x7A, 0xFF)
GREEN = (0x5F, 0xD7, 0x5F, 0xFF)
BLUE = (0x5F, 0xB8, 0xFF, 0xFF)
SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 3  # 超采样倍数


def inside_rounded(x: float, y: float, size: float, radius: float, inset: float = 0.0) -> bool:
    """圆角矩形内部判定（左上角为原点）；``inset`` 表示向内收缩的边距。"""
    x -= inset
    y -= inset
    size -= 2 * inset
    radius = max(radius - inset, 0.0)
    if x < 0 or y < 0 or x > size - 1 or y > size - 1:
        return False
    r = radius
    right, bottom = size - 1 - r, size - 1 - r
    if x < r and y < r:
        cx, cy = r, r
    elif x > right and y < r:
        cx, cy = right, r
    elif x < r and y > bottom:
        cx, cy = r, bottom
    elif x > right and y > bottom:
        cx, cy = right, bottom
    else:
        return True
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def dist_to_segment(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = ((px - x1) * dx + (py - y1) * dy) / length_sq
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    qx, qy = x1 + t * dx, y1 + t * dy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5


def point_in_triangle(px, py, tri) -> bool:
    (x1, y1), (x2, y2), (x3, y3) = tri
    d1 = (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2)
    d2 = (px - x3) * (y2 - y3) - (x2 - x3) * (py - y3)
    d3 = (px - x1) * (y3 - y1) - (x3 - x1) * (py - y1)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def render(size: int) -> bytearray:
    """渲染为 size x size 的 RGBA（行优先、自上而下）。"""
    big = size * SS
    radius = big * 0.20
    border_w = max(1.0, big * 0.022)
    inner_radius = max(radius - border_w, radius * 0.5)
    thickness = big * 0.072
    head = big * 0.115
    y_top, y_bottom = big * 0.355, big * 0.645
    x_left, x_right = big * 0.235, big * 0.765

    top_line = (x_left, y_top, x_right - head * 0.65, y_top)
    top_head = ((x_right, y_top), (x_right - head, y_top - head * 0.85), (x_right - head, y_top + head * 0.85))
    bottom_line = (x_right, y_bottom, x_left + head * 0.65, y_bottom)
    bottom_head = ((x_left, y_bottom), (x_left + head, y_bottom - head * 0.85), (x_left + head, y_bottom + head * 0.85))

    hi = bytearray(big * big * 4)

    # 第 1 遍：背景 + 边框
    for y in range(big):
        for x in range(big):
            if not inside_rounded(x, y, big, radius):
                continue
            color = BG if inside_rounded(x, y, big, inner_radius, border_w) else BORDER
            o = (y * big + x) * 4
            hi[o:o + 4] = bytes(color)

    # 第 2 遍：只在箭头包围盒内做几何判定（省时间）
    y0 = int(max(0, y_top - head * 1.1))
    y1 = int(min(big, y_bottom + head * 1.1)) + 1
    x0 = int(max(0, x_left - head * 0.4))
    x1 = int(min(big, x_right + head * 0.4)) + 1
    for y in range(y0, y1):
        for x in range(x0, x1):
            o = (y * big + x) * 4
            if hi[o + 3] == 0:
                continue
            if dist_to_segment(x, y, *top_line) <= thickness or point_in_triangle(x, y, top_head):
                hi[o:o + 4] = bytes(GREEN)
            elif dist_to_segment(x, y, *bottom_line) <= thickness or point_in_triangle(x, y, bottom_head):
                hi[o:o + 4] = bytes(BLUE)

    # 下采样（盒式滤波，按 alpha 加权）
    out = bytearray(size * size * 4)
    area = SS * SS
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(SS):
                base = ((y * SS + dy) * big + x * SS) * 4
                for dx in range(SS):
                    o = base + dx * 4
                    alpha = hi[o + 3]
                    if alpha:
                        a += alpha
                        r += hi[o] * alpha
                        g += hi[o + 1] * alpha
                        b += hi[o + 2] * alpha
            o = (y * size + x) * 4
            if a:
                out[o] = r // a
                out[o + 1] = g // a
                out[o + 2] = b // a
                out[o + 3] = a // area
    return out


def ico_bytes(images) -> bytes:
    """把 [(size, rgba)] 打包成 ICO（每尺寸用 32 位 BMP 存储）。"""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    entries = b""
    payload = b""
    offset = 6 + 16 * count
    for size, rgba in images:
        # BITMAPINFOHEADER：高度写两倍（XOR 图 + AND 掩码，掩码全 0 表示不透明由 alpha 决定）
        info = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
        xor = bytearray()
        for y in range(size - 1, -1, -1):  # BMP 自下而上
            row = rgba[y * size * 4:(y + 1) * size * 4]
            for x in range(size):
                r, g, b, a = row[x * 4:x * 4 + 4]
                xor += bytes((b, g, r, a))
        mask_row = ((size + 31) // 32) * 4
        data = info + bytes(xor) + b"\x00" * (mask_row * size)
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        payload += data
        offset += len(data)
    return header + entries + payload


def png_bytes(rgba: bytes, size: int) -> bytes:
    """把 RGBA 写成 PNG（仅用于人工查看效果）。"""
    raw = b"".join(b"\x00" + bytes(rgba[y * size * 4:(y + 1) * size * 4]) for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 app.ico")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico"))
    parser.add_argument("--preview", default="", help="额外导出 256x256 PNG 路径")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from comtel.console import setup_console

        setup_console()
    except Exception:
        pass

    images = []
    for size in SIZES:
        images.append((size, render(size)))
        print("渲染 %dx%d 完成" % (size, size))

    with open(args.out, "wb") as fh:
        fh.write(ico_bytes(images))
    print("已写入 %s（%d 字节，%d 种尺寸）" % (args.out, os.path.getsize(args.out), len(images)))

    if args.preview:
        with open(args.preview, "wb") as fh:
            fh.write(png_bytes(dict(images)[256], 256))
        print("预览图：%s" % args.preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
