# -*- coding: utf-8 -*-
"""
hCaptcha「click on the crosses」挑战求解：OpenCV 检测图中的 X/十字标记。

用法（CLI 或作为模块）：
    python3 tools/hcaptcha_cross_detect.py <image> [--num N] [--port 8767]
服务模式：POST /detect {"image": "<url|path|data-uri>", "num": 2}
    -> {"ok": true, "points": [{"x": 0.52, "y": 0.31}, ...]}  # 归一化坐标
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hcaptcha-cross")


def _load_image(src: str):
    from PIL import Image
    if src.startswith("data:"):
        _, _, b64 = src.partition(",")
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    if src.startswith(("http://", "https://")):
        import httpx
        with httpx.Client(timeout=20, follow_redirects=True) as c:
            r = c.get(src)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    return Image.open(src).convert("RGB")


def detect_crosses(img, num: int = 2, method: str = "morph"):
    """在图中找 X 形/十字标记，返回归一化坐标 [(x, y), ...]（中心点）。"""
    import cv2
    import numpy as np
    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    points = []
    if method == "morph":
        # X 形标记通常比背景暗或亮，用形态学开运算保留小十字结构
        # 尝试明暗两种方向
        for invert in (False, True):
            g = 255 - gray if invert else gray
            # 二值化：取最暗部分（标记常为深色）
            _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            # X 形：用十字核开运算
            k = cv2.getStructuringElement(cv2.MORPH_CROSS, (7, 7))
            opened = cv2.morphologyEx(th, cv2.MORPH_OPEN, k)
            # 找连通域
            n, labels, stats, _ = cv2.connectedComponentsWithStats(opened, connectivity=8)
            cands = []
            for i in range(1, n):
                x, y, w, h, area = stats[i]
                if area < 30 or w < 5 or h < 5:
                    continue
                # X 形特征：宽高接近、填充率低（十字是稀疏结构）
                fill = area / (w * h) if w * h else 0
                if 0.05 < fill < 0.5 and 0.4 < w / h < 2.5:
                    cands.append((x + w / 2, y + h / 2, area))
            if cands:
                cands.sort(key=lambda c: -c[2])
                for cx, cy, _ in cands[:num]:
                    points.append((cx / arr.shape[1], cy / arr.shape[0]))
                if len(points) >= num:
                    break
        # 形态学未找到足够，退化为模板匹配：生成 X 模板
        if len(points) < num:
            try:
                tmpl = _make_x_template(arr.shape[1] // 20)
                g2 = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
                res = cv2.matchTemplate(g2, tmpl, cv2.TM_CCOEFF_NORMED)
                _, maxv, _, maxloc = cv2.minMaxLoc(res)
                if maxv > 0.4:
                    hh, ww = tmpl.shape
                    cx = maxloc[0] + ww / 2
                    cy = maxloc[1] + hh / 2
                    points.append((cx / arr.shape[1], cy / arr.shape[0]))
            except Exception as e:
                logger.warning("模板匹配失败: %s", e)
    elif method == "template":
        g2 = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        for _ in range(num):
            tmpl = _make_x_template(arr.shape[1] // 16)
            res = cv2.matchTemplate(g2, tmpl, cv2.TM_CCOEFF_NORMED)
            _, maxv, _, maxloc = cv2.minMaxLoc(res)
            if maxv < 0.3:
                break
            hh, ww = tmpl.shape
            cx, cy = maxloc[0] + ww / 2, maxloc[1] + hh / 2
            points.append((cx / arr.shape[1], cy / arr.shape[0]))
            # 挖掉已找到区域
            g2[cy - hh:cy + hh, cx - ww:cx + ww] = 128
    # 去重（距离过近）
    uniq = []
    for p in points:
        if all(((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5 > 0.08 for q in uniq):
            uniq.append(p)
    return uniq[:num]


def _make_x_template(size: int):
    import cv2
    import numpy as np
    t = np.full((size, size), 200, dtype=np.uint8)
    cv2.line(t, (size // 10, size // 10), (size - size // 10, size - size // 10), 30, max(2, size // 12))
    cv2.line(t, (size - size // 10, size // 10), (size // 10, size - size // 10), 30, max(2, size // 12))
    return t


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
            img_src = str(body.get("image", ""))
            num = int(body.get("num", 2))
            if not img_src:
                self._send(400, {"ok": False, "error": "image empty"})
                return
            img = _load_image(img_src)
            pts = detect_crosses(img, num=num)
            self._send(200, {"ok": True, "points": [{"x": x, "y": y} for x, y in pts], "num": num})
        except Exception as e:
            logger.exception("detect error")
            self._send(500, {"ok": False, "error": str(e)})

    def do_GET(self):
        self._send(200, {"ok": True, "service": "hcaptcha-cross"})

    def _send(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    port = int(os.environ.get("HCAPTCHA_CROSS_PORT", "8767"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    logger.info("hCaptcha cross 检测服务就绪: http://127.0.0.1:%d", port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
