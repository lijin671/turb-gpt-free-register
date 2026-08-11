# -*- coding: utf-8 -*-
"""hCaptcha 视觉网格检测测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw

from core.hcaptcha_grid import detect_grid_cells


def _make_grid(rows: int, cols: int, *, title: str = "Please click on the TWO stars",
               canvas_w: int = 302, canvas_h: int = 460, grid_top: int = 80):
    img = Image.new("RGB", (canvas_w, canvas_h), "white")
    d = ImageDraw.Draw(img)
    cell_w = (canvas_w - 2) // cols
    cell_h = cell_w
    colors = [(200, 50, 50), (50, 200, 50), (50, 50, 200), (200, 200, 50),
              (200, 50, 200), (50, 200, 200), (120, 120, 120), (220, 120, 30),
              (30, 120, 220)]
    for i in range(rows * cols):
        r, c = divmod(i, cols)
        d.rectangle([1 + c * cell_w + 12, grid_top + r * cell_h + 12,
                     1 + c * cell_w + cell_w - 12, grid_top + r * cell_h + cell_h - 12],
                    fill=colors[i % len(colors)])
    for i in range(cols + 1):
        x = 1 + i * cell_w
        d.line([(x, grid_top), (x, grid_top + rows * cell_h)], fill=(180, 180, 180), width=2)
    for i in range(rows + 1):
        y = grid_top + i * cell_h
        d.line([(1, y), (canvas_w - 1, y)], fill=(180, 180, 180), width=2)
    d.text((20, 20), title, fill=(0, 0, 0))
    d.text((20, 45), "Please try again", fill=(120, 120, 120))
    return img


COLORS = [(200, 50, 50), (50, 200, 50), (50, 50, 200), (200, 200, 50),
          (200, 50, 200), (50, 200, 200), (120, 120, 120), (220, 120, 30),
          (30, 120, 220)]


def _correct_count(res, rows, cols, img):
    import numpy as np
    arr = np.array(img)
    ok = 0
    for i, (cx, cy, w, h) in enumerate(res["cells"]):
        px = arr[int(cy), int(cx)]
        exp = COLORS[i % len(COLORS)]
        if abs(int(px[0]) - exp[0]) < 40 and abs(int(px[1]) - exp[1]) < 40 and abs(int(px[2]) - exp[2]) < 40:
            ok += 1
    return ok


class TestGridDetection(unittest.TestCase):
    def test_3x3_stars(self):
        img = _make_grid(3, 3)
        res = detect_grid_cells(img)
        self.assertEqual((res["rows"], res["cols"]), (3, 3))
        self.assertEqual(_correct_count(res, 3, 3, img), 9)

    def test_2x2(self):
        img = _make_grid(2, 2)
        res = detect_grid_cells(img)
        self.assertEqual((res["rows"], res["cols"]), (2, 2))
        self.assertEqual(_correct_count(res, 2, 2, img), 4)

    def test_3x3_sink_title(self):
        img = _make_grid(3, 3, title="Click on all items you might find near a sink")
        res = detect_grid_cells(img)
        self.assertEqual((res["rows"], res["cols"]), (3, 3))
        self.assertEqual(_correct_count(res, 3, 3, img), 9)


if __name__ == "__main__":
    unittest.main()
