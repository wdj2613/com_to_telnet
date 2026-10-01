# -*- coding: utf-8 -*-
"""校验 packaging/app.ico 的结构与图案（不需要看图，直接检查像素）。

    python packaging/check_icon.py
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from comtel.console import setup_console  # noqa: E402

setup_console()

ICO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
FAILED = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" —— " + detail) if detail else ""))
    if not ok:
        FAILED.append(name)


def parse_ico(path):
    with open(path, "rb") as fh:
        blob = fh.read()
    reserved, kind, count = struct.unpack_from("<HHH", blob, 0)
    entries = []
    for i in range(count):
        w, h, colors, res, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", blob, 6 + 16 * i)
        entries.append(
            {"width": w or 256, "height": h or 256, "bits": bits, "size": size, "offset": offset,
             "data": blob[offset:offset + size]}
        )
    return reserved, kind, entries


def decode_bmp32(data, size):
    """把 ICO 里的 32 位 BMP 解成 RGBA（自上而下）。"""
    header = struct.unpack_from("<IiiHHIIiiII", data, 0)
    width, height2 = header[1], header[2]
    assert width == size and height2 == size * 2, "尺寸不符"
    pixels = bytearray(size * size * 4)
    base = 40
    for y in range(size):
        src_row = size - 1 - y  # BMP 自下而上
        for x in range(size):
            o = base + (src_row * size + x) * 4
            b, g, r, a = data[o:o + 4]
            d = (y * size + x) * 4
            pixels[d:d + 4] = bytes((r, g, b, a))
    return pixels


def px(pixels, size, x, y):
    o = (y * size + x) * 4
    return tuple(pixels[o:o + 4])


def main() -> int:
    check("app.ico 存在", os.path.isfile(ICO), ICO)
    if not os.path.isfile(ICO):
        return 1

    reserved, kind, entries = parse_ico(ICO)
    sizes = [e["width"] for e in entries]
    check("ICO 头合法", reserved == 0 and kind == 1, "reserved=%d type=%d" % (reserved, kind))
    check("包含 7 种尺寸", sizes == [16, 24, 32, 48, 64, 128, 256], str(sizes))
    check("全部为 32 位带 alpha", all(e["bits"] == 32 for e in entries))

    entry = [e for e in entries if e["width"] == 256][0]
    expected = 40 + 256 * 256 * 4 + ((256 + 31) // 32) * 4 * 256
    check("BMP 数据长度正确", entry["size"] == expected, "%d vs %d" % (entry["size"], expected))

    pixels = decode_bmp32(entry["data"], 256)
    corner = px(pixels, 256, 1, 1)
    check("四角透明", corner[3] == 0, str(corner))

    top = px(pixels, 256, int(256 * 0.5), int(256 * 0.355))
    check("上箭头是绿色", top[1] > 150 and top[1] > top[0] and top[1] > top[2], str(top))

    bottom = px(pixels, 256, int(256 * 0.5), int(256 * 0.645))
    check("下箭头是蓝色", bottom[2] > 150 and bottom[2] > bottom[0], str(bottom))

    edge = px(pixels, 256, 128, 0)
    inner = px(pixels, 256, 128, 12)
    check("上边缘是边框色（不透明）", edge[3] == 255 and edge != inner, "%s vs %s" % (edge, inner))
    check("边缘内部回到底色", inner == (0x16, 0x21, 0x2E, 0xFF), str(inner))

    center = px(pixels, 256, 128, 128)
    check("中间区域不透明", center[3] == 255, str(center))

    print("\n结果：%s" % ("全部通过 🎉" if not FAILED else "失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED))))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
