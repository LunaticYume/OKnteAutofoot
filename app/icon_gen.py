"""应用图标生成（纯标准库 BMP-in-ICO）：构建脚本与 dist 自测共用。"""
import struct
from pathlib import Path


def generate_icon(path):
    """生成 BMP-in-ICO 应用图标（32×32 + 16×16）。

    图案：深蓝底 + 青色圆环（"环"意：异环规划器）；像素级绘制，无第三方依赖。
    """
    def render(size):
        rows = []
        cx = cy = (size - 1) / 2
        r_outer, r_inner = size * 0.42, size * 0.24
        for y in range(size - 1, -1, -1):  # BMP 自下而上
            row = bytearray()
            for x in range(size):
                dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                if dist <= r_outer:
                    if r_inner <= dist <= r_outer * 0.86:  # 青色圆环主体
                        bgra = (0xF0, 0xA3, 0x4D, 0xFF)
                    elif dist <= r_inner:  # 环内深色
                        bgra = (0x3A, 0x22, 0x14, 0xFF)
                    elif dist <= r_outer - 1:  # 环外沿
                        bgra = (0xF0, 0xA3, 0x4D, 0xFF)
                    else:
                        bgra = (0, 0, 0, 0)
                else:
                    bgra = (0, 0, 0, 0)
                row += bytes(bgra)
            rows.append(bytes(row))
        pixels = b"".join(rows)
        and_mask = b"\x00" * (((size + 31) // 32) * 4 * size)  # 透明由 alpha 表达
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                             len(pixels) + len(and_mask), 0, 0, 0, 0)
        return header + pixels + and_mask

    images = {size: render(size) for size in (32, 16)}
    out = struct.pack("<HHH", 0, 1, 2)
    offset = 6 + 16 * 2
    for size in (32, 16):  # 目录约定大尺寸在前
        data = images[size]
        out += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    Path(path).write_bytes(out + images[32] + images[16])
