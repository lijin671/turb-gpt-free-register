# -*- coding: utf-8 -*-
"""hCaptcha 图片挑战网格检测：从挑战区域截图定位 3x3/2x2 格子中心。

新版 hCaptcha（2026-08，v1/9175be...）图片挑战的网格图不在 <img> 元素里
（canvas/背景渲染），因此求解器改为纯视觉：截图 → 检测网格线 → 切格 → CLIP。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("hcaptcha-grid")


def _cluster(values, gap: int = 10) -> list:
    if not values:
        return []
    values = sorted(values)
    groups = [[values[0]]]
    for v in values[1:]:
        if v - groups[-1][-1] <= gap:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [int(sum(g) / len(g)) for g in groups]


def _detect_lines(edges, min_len):
    import cv2
    import numpy as np
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80,
                            minLineLength=int(min_len), maxLineGap=6)
    h_lines, v_lines = [], []
    if lines is not None:
        for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
            dx, dy = abs(x2 - x1), abs(y2 - y1)
            ln = max(dx, dy)
            if ln < min_len:
                continue
            if dy <= dx * 0.25:
                h_lines.append((y1 + y2) / 2)
            elif dx <= dy * 0.25:
                v_lines.append((x1 + x2) / 2)
    return h_lines, v_lines


def detect_grid_cells(image, *, min_len_ratio: float = 0.4):
    """检测 hCaptcha 图片网格，返回 {rows, cols, cells: [(cx, cy, w, h)...]}。

    cells 坐标为像素（相对输入图像）。
    策略：HoughLinesP 长直线（网格线贯穿；图片内容边缘是短线段）→ 横竖线聚类 →
    中位间距推导格宽 → 网格范围推导格数（3x3/2x2）→ 正方形校正。
    """
    import cv2
    import numpy as np
    arr = np.array(image)
    if arr.ndim == 3:
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    else:
        gray = arr
    h, w = gray.shape
    edges = cv2.Canny(gray, 50, 150)

    h_lines, v_lines = _detect_lines(edges, max(w, h) * min_len_ratio)
    # 横线排除标题区（hCaptcha 挑战标题在网格上方，通常 < 12% 高度）
    h_lines = [y for y in h_lines if y > h * 0.12]
    rows = _cluster([float(y) for y in h_lines], gap=max(6, h // 40))
    cols = _cluster([float(x) for x in v_lines], gap=max(6, w // 40))

    # 中位间距（格宽）。竖线至少 2 条才能算；否则用横线间距交叉。
    def _median_step(lines):
        if len(lines) >= 2:
            diffs = [lines[i + 1] - lines[i] for i in range(len(lines) - 1)]
            return float(np.median(diffs))
        return None

    d = _median_step(cols)
    if d is None:
        d = _median_step(rows)
    if d is None:
        d = w / 3

    # 网格 x 范围：所有竖线最小/最大；若最外侧线离图像边缘较远则外扩一格
    if cols:
        x0, x1 = min(cols), max(cols)
        if x0 > d * 0.6:
            x0 -= d
        if x1 < w - d * 0.6:
            x1 += d
        n_cols = max(2, int(round((x1 - x0) / d)))
        if n_cols > 3:
            # 可能把边缘背景线也当成了线：收缩到最接近 2/3 格的解释
            n_cols = 3
            x0 = cols[0] if len(cols) >= 2 else x0
            x1 = cols[-1] if len(cols) >= 2 else x1
        step_x = (x1 - x0) / n_cols
        # y 范围：横线同样处理；无横线时按正方形（step_x）
        if rows:
            y0, y1 = min(rows), max(rows)
            if y0 > step_x * 0.6:
                y0 -= step_x
            if y1 < h - step_x * 0.6:
                y1 += step_x
            n_rows = max(2, int(round((y1 - y0) / step_x)))
            if n_rows > 3:
                n_rows = 3
                y0 = rows[0] if len(rows) >= 2 else y0
                y1 = rows[-1] if len(rows) >= 2 else y1
        else:
            n_rows = n_cols
            # 网格应在挑战区域下半部（标题在上方）：取图像下部 90%
            y0 = h * 0.05
            y1 = y0 + n_rows * step_x
        step_y = (y1 - y0) / n_rows
        # 夹取边界
        x0 = max(0.0, min(x0, w - 1))
        x1 = max(x0 + step_x, min(x1, w))
        y0 = max(0.0, min(y0, h - 1))
        y1 = max(y0 + step_y, min(y1, h))
        cells = []
        for r in range(n_rows):
            for c in range(n_cols):
                cells.append((x0 + (c + 0.5) * step_x, y0 + (r + 0.5) * step_y,
                              step_x, step_y))
        return {"rows": n_rows, "cols": n_cols, "cells": cells}

    logger.info("网格线检测退化 rows=%s cols=%s，按 3x3 均分", rows, cols)
    step_y, step_x = h / 3, w / 3
    cells = []
    for r in range(3):
        for c in range(3):
            cells.append(((c + 0.5) * step_x, (r + 0.5) * step_y, step_x, step_y))
    return {"rows": 3, "cols": 3, "cells": cells}
