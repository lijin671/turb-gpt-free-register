# -*- coding: utf-8 -*-
"""
常驻 CLIP 图片挑战求解服务（hCaptcha 网格选图）。

系统 Python3（有 torch/transformers/CLIP 模型缓存），HTTP JSON 接口：
    POST /solve
    {"task": "choose all you can safely put on the object in the image",
     "images": ["<url|data-uri|local-path>", ...], "top": 3}
    -> {"ok": true, "indices": [2, 5], "scores": [0.91, 0.87], "elapsed": 1.2}

CLIP 策略：任务文本抽关键短语 → positive/negative 双提示 softmax，
选 positive 概率 > 0.5 的格子（top 截断）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hcaptcha-clip")

MODEL = None
PROC = None
MODEL_NAME = os.environ.get("HCAPTCHA_CLIP_MODEL", "openai/clip-vit-base-patch32")

# 任务文本 → CLIP 提示模板
# 上下文型任务（"near a sink" 等）：CLIP 对句子级上下文关联不敏感，
# 直接给出该场景下的常见目标物类别 + 通用干扰类别，效果远好于整句软提示。
# 实测 2026-08-06: "click on all items you might find near a sink" 9 格
# （4 火柴 / 2 水瓶 / 2 蜥蜴 / 1 松鼠），用示例列表提示可干净分离 {水瓶}。
_CONTEXT_EXAMPLES = [
    (re.compile(r"(?:near|around|beside|next to) a sink", re.I),
     ["a plastic water bottle", "a bar of soap", "a toothbrush", "a cup", "a sponge",
      "a tube of toothpaste", "a hand towel", "a dish rack", "a dish drying rack",
      "a kitchen towel", "a bottle of dish soap"],
     ["matches", "matchstick", "wild animals", "a lizard", "a bearded dragon", "a frog",
      "a squirrel", "a human figure", "insects", "toys", "food", "plants", "tools",
      "electronics"]),
    (re.compile(r"in a kitchen|in the kitchen|kitchen", re.I),
     ["a frying pan", "a spatula", "a knife", "a plate", "a bowl", "a cutting board",
      "a pot", "a kitchen utensil", "a whisk"],
     ["wild animals", "insects", "toys", "clothes", "books", "electronics", "matches"]),
    (re.compile(r"can safely put on|can be safely placed on|safely put on", re.I),
     ["a book", "a plate", "a bowl", "a cup", "a sheet of paper", "a tray", "a laptop",
      "a phone", "a mug"],
     ["liquids", "animals", "food", "sharp objects", "clothes", "a candle"]),
    (re.compile(r"near a bed|in a bedroom|bedroom", re.I),
     ["a pillow", "a blanket", "a lamp", "an alarm clock", "a book", "a phone charger",
      "a cushion"],
     ["food", "wild animals", "kitchen tools", "matches", "a frying pan"]),
    (re.compile(r"in a bathroom|bathroom", re.I),
     ["a toothbrush", "a bar of soap", "a bottle of shampoo", "a towel", "a tube of toothpaste",
      "a comb", "a razor", "a bottle of face wash"],
     ["matches", "food", "wild animals", "toys", "tools", "a frying pan"]),
    (re.compile(r"in a living room|living room", re.I),
     ["a sofa", "a coffee table", "a lamp", "a television", "a cushion", "a book",
      "a remote control"],
     ["food", "wild animals", "bathroom items", "matches", "a toothbrush"]),
    (re.compile(r"in a car|in a vehicle|inside a car|car interior", re.I),
     ["a steering wheel", "a car seat", "a dashboard", "a rearview mirror", "a seat belt",
      "a car door", "a headrest"],
     ["wild animals", "food", "furniture", "kitchen tools", "a frying pan"]),
    (re.compile(r"in an office|office|on a desk|desk", re.I),
     ["a computer", "a keyboard", "a mouse", "a printer", "a desk", "a chair",
      "a document", "a pen", "a notebook"],
     ["food", "wild animals", "bathroom items", "matches", "a toothbrush"]),
    (re.compile(r"outdoors|outside|in nature|forest|park|hiking", re.I),
     ["a tree", "a rock", "a leaf", "a flower", "a bird", "grass", "a path", "a mushroom"],
     ["kitchen tools", "electronic devices", "indoor furniture", "a frying pan"]),
    (re.compile(r"at the beach|beach|seaside", re.I),
     ["a seashell", "a starfish", "a beach ball", "a sandcastle", "a flip flop", "a sun hat",
      "a beach towel", "a bottle of sunscreen"],
     ["kitchen tools", "office items", "electronic devices", "matches", "a frying pan"]),
]


# 直接目标类任务（containing X / with X / pictures of X）的通用负例类别
_GENERIC_NEGATIVES = [
    "wild animals", "insects", "food", "toys", "plants", "tools", "vehicles",
    "clothing", "furniture", "electronic devices", "random objects",
]

# 尺寸类挑战（Pick all objects smaller/larger than the one shown）：
# 像素占比法不可行（run25 实测 5驴+3墨镜+1牛，物体都只占格子 ~1-15%，无区分度）。
# 改用 CLIP 零样本分类到候选类别表 + 典型尺寸等级（1=极小 ~ 8=极大），
# 选尺寸等级严格小于/大于参考的格子。
_SIZE_CATS = [
    # animals
    ("ant", 1), ("bee", 1), ("butterfly", 1), ("mouse", 1), ("fish", 1), ("frog", 1),
    ("lizard", 1), ("bird", 1), ("squirrel", 1), ("rabbit", 1), ("turtle", 1), ("duck", 1),
    ("chicken", 1), ("snake", 1), ("octopus", 1), ("cat", 2), ("dog", 2), ("fox", 2),
    ("monkey", 2), ("sheep", 3), ("goat", 3), ("pig", 3), ("deer", 3), ("donkey", 4),
    ("cow", 4), ("horse", 4), ("bear", 4), ("lion", 4), ("tiger", 4), ("zebra", 4),
    ("giraffe", 5), ("camel", 4), ("rhino", 5), ("hippo", 6), ("elephant", 6), ("whale", 7),
    ("shark", 5),
    # vehicles
    ("skateboard", 1), ("scooter", 2), ("bicycle", 4), ("motorcycle", 4), ("wheelchair", 3),
    ("car", 5), ("truck", 6), ("bus", 7), ("train", 7), ("airplane", 7), ("boat", 5),
    ("helicopter", 5), ("tractor", 5),
    # furniture
    ("stool", 2), ("chair", 3), ("table", 4), ("desk", 4), ("bookshelf", 5), ("sofa", 5),
    ("bed", 6), ("lamp", 2), ("cabinet", 5),
    # household
    ("cup", 1), ("mug", 1), ("glass", 1), ("bottle", 1), ("plate", 1), ("bowl", 1),
    ("spoon", 1), ("fork", 1), ("knife", 1), ("toothbrush", 1), ("soap", 1), ("sponge", 1),
    ("towel", 2), ("bucket", 2), ("broom", 3), ("mop", 3), ("umbrella", 3), ("clock", 2),
    ("book", 2), ("pen", 1), ("pencil", 1), ("notebook", 2), ("laptop", 2), ("phone", 1),
    ("camera", 2), ("headphones", 1), ("television", 4), ("computer", 3), ("keyboard", 2),
    ("mouse", 1), ("sunglasses", 1), ("glasses", 1), ("hat", 1), ("cap", 1), ("shoe", 1),
    ("boot", 2), ("sandal", 1), ("backpack", 2), ("handbag", 1), ("wallet", 1), ("key", 1),
    ("watch", 1), ("ring", 1), ("ball", 1), ("balloon", 1), ("kite", 1), ("doll", 1),
    ("robot", 2), ("candle", 1), ("vase", 2), ("mirror", 3), ("fridge", 6), ("microwave", 4),
    ("toaster", 2), ("kettle", 2), ("pan", 2), ("pot", 2), ("blender", 2), ("basket", 2),
    ("box", 2), ("pillow", 2), ("blanket", 3), ("rug", 3), ("curtain", 3),
    # food
    ("apple", 1), ("banana", 1), ("orange", 1), ("watermelon", 3), ("bread", 1), ("cake", 1),
    ("pizza", 1), ("burger", 1), ("egg", 1), ("cheese", 1), ("carrot", 1), ("tomato", 1),
    ("lemon", 1), ("strawberry", 1), ("grape", 1), ("cookie", 1), ("donut", 1), ("ice cream", 1),
    ("corn", 1), ("pumpkin", 2),
    # nature
    ("tree", 6), ("flower", 1), ("mushroom", 1), ("rock", 3), ("mountain", 8), ("leaf", 1),
    ("shell", 1), ("starfish", 1), ("coral", 1), ("pine cone", 1),
    # tools
    ("hammer", 2), ("screwdriver", 1), ("wrench", 1), ("drill", 2), ("saw", 3), ("axe", 3),
    ("nail", 1), ("rope", 1), ("ladder", 6), ("paintbrush", 1), ("scissors", 1),
    # instruments
    ("guitar", 3), ("piano", 5), ("drum", 3), ("trumpet", 2), ("violin", 2), ("flute", 1),
    # sports
    ("soccer ball", 1), ("basketball", 1), ("tennis ball", 1), ("baseball", 1), ("hockey stick", 3),
    ("surfboard", 5), ("skis", 4), ("snowboard", 4), ("tennis racket", 2), ("dumbbell", 1),
    ("barbell", 3), ("bowling ball", 1), ("football", 1),
]
_SIZE_LABELS = [c for c, _ in _SIZE_CATS]
_SIZE_OF = dict(_SIZE_CATS)
_SIZE_TEXTS = ["a photo of a " + l for l in _SIZE_LABELS]


def _classify_top3(im):
    """返回该图 top-3 类别 [(label, prob)]。"""
    import torch
    import torch.nn.functional as F
    inputs = PROC(text=_SIZE_TEXTS, images=[im], return_tensors="pt", padding=True)
    with torch.no_grad():
        out = MODEL(**inputs)
    probs = F.softmax(out.logits_per_image, dim=1)[0]
    top = probs.topk(3)
    return [(_SIZE_LABELS[i], float(probs[i])) for i in top.indices]


def decide_size_hit(top3, ref_size, want_smaller):
    """纯函数：给定 top-3 类别及参考尺寸等级，判定该格是否命中。

    - top-3 全部严格小于/大于参考 → 命中（低置信度也稳，run25 cell4 墨镜 top1=0.159）
    - top-1 置信 >0.2 且严格小于/大于参考 → 命中
    """
    sizes = [_SIZE_OF.get(l) for l, _ in top3]
    if all(s is not None and (s < ref_size if want_smaller else s > ref_size) for s in sizes):
        return True
    return (top3[0][1] > 0.2 and _SIZE_OF.get(top3[0][0]) is not None
            and ((_SIZE_OF[top3[0][0]] < ref_size) if want_smaller else (_SIZE_OF[top3[0][0]] > ref_size)))


def solve_size(task: str, ref: str, images: list, top: int = 9) -> dict:
    """尺寸挑战求解：分类参考图与每格 → 典型尺寸等级对比 → 选格。

    run25 实测校准：参考=驴(size 4)，9 格 = 驴x5 + 墨镜x3 + 牛x1，
    top-3 全小于参考的格子（墨镜 size 1）被选中 → {2,4,5} 与人工标注一致。
    """
    _load()
    import torch
    import torch.nn.functional as F
    t0 = time.time()
    tl = (task or "").lower()
    want_smaller = any(k in tl for k in ("smaller", "smallest"))
    try:
        ref_im = _fetch_image(ref)
    except Exception as e:
        return {"ok": False, "error": f"ref image load failed: {e}"}
    ref_top = _classify_top3(ref_im)
    ref_size = _SIZE_OF.get(ref_top[0][0])
    if ref_top[0][1] < 0.25 or ref_size is None:
        return {"ok": False, "error": f"ref classify uncertain: {ref_top}"}
    imgs = []
    bad = []
    for i, src in enumerate(images):
        try:
            imgs.append((i, _fetch_image(src)))
        except Exception as e:
            bad.append(i)
            logger.warning("尺寸图片 %d 加载失败: %s", i, e)
    if not imgs:
        return {"ok": False, "error": "no images loadable", "bad": bad}
    picked = []
    details = []
    for i, im in imgs:
        top3 = _classify_top3(im)
        sizes = [_SIZE_OF.get(l) for l, _ in top3]
        smaller_ok = all(s is not None and s < ref_size for s in sizes)
        larger_ok = all(s is not None and s > ref_size for s in sizes)
        top1_ok = (top3[0][1] > 0.2 and _SIZE_OF.get(top3[0][0]) is not None
                   and ((_SIZE_OF[top3[0][0]] < ref_size) if want_smaller else (_SIZE_OF[top3[0][0]] > ref_size)))
        hit = (smaller_ok if want_smaller else larger_ok) or top1_ok
        details.append({"i": i, "top3": [(l, round(p, 3)) for l, p in top3],
                        "ref_size": ref_size, "want_smaller": want_smaller, "hit": hit})
        if hit:
            picked.append(i)
    picked = picked[:top]
    result = {"ok": True, "indices": picked,
              "ref": [ref_top[0][0], round(ref_top[0][1], 3), ref_size],
              "details": details, "elapsed": round(time.time() - t0, 2)}
    logger.info("尺寸求解: ref=%s picked=%s elapsed=%.2fs",
                result["ref"], picked, result["elapsed"])
    return result


_TEMPLATES = [
    (re.compile(r"choose all you can safely put on", re.I), "an object that can be safely placed on {subject}"),
    (re.compile(r"select all images containing", re.I), "an image containing {subject}"),
    (re.compile(r"select all images that contain", re.I), "an image containing {subject}"),
    (re.compile(r"click on all images containing", re.I), "an image containing {subject}"),
    (re.compile(r"click on all images that contain", re.I), "an image containing {subject}"),
    (re.compile(r"choose all images with", re.I), "an image with {subject}"),
    (re.compile(r"select all pictures of", re.I), "a picture of {subject}"),
    (re.compile(r"click on all items you might find near", re.I), "an item you might find near {subject}"),
    (re.compile(r"select all items you might find near", re.I), "an item you might find near {subject}"),
    (re.compile(r"click on all items", re.I), "an image of {subject}"),
    (re.compile(r"please click on the", re.I), "{subject}"),
    (re.compile(r"click on the", re.I), "{subject}"),
]


def _load():
    global MODEL, PROC
    if MODEL is not None:
        return
    from transformers import CLIPModel, CLIPProcessor
    t0 = time.time()
    MODEL = CLIPModel.from_pretrained(MODEL_NAME)
    PROC = CLIPProcessor.from_pretrained(MODEL_NAME)
    logger.info("CLIP %s 加载完成 %.1fs", MODEL_NAME, time.time() - t0)


def _fetch_image(src: str):
    """返回 PIL Image。支持 url / data-uri / 本地路径。"""
    from PIL import Image
    import io
    if src.startswith("data:"):
        _, _, b64 = src.partition(",")
        raw = base64.b64decode(b64)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    if src.startswith(("http://", "https://")):
        import httpx
        with httpx.Client(timeout=20, follow_redirects=True) as c:
            r = c.get(src)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    return Image.open(src).convert("RGB")


def _build_prompts(task: str):
    """从任务文本构造 (pos_examples, neg_examples) 提示列表。

    返回的是**具体类别短语列表**而非整句提示：逐示例 max-margin 策略
    （每个图片取对正例的最大相似度 - 对负例的最大相似度）在实测中远优于
    单句 softmax（"near a sink" 一类上下文任务单句提示会把所有日常物都
    选进去）。2026-08-06 两组真实 hCaptcha 网格验证：
      - "near a sink"(水瓶x2+火柴/蜥蜴/松鼠) → margin>0 精确选中 2 水瓶；
      - "near a sink"(碗碟沥水架+蜥蜴/火柴/人形) → margin>0 精确选中沥水架。
    """
    t = task.strip()
    t = re.sub(r"\bplease try again\b.*$", "", t, flags=re.I)
    t = re.sub(r"\b(verify|skip)\b.*$", "", t, flags=re.I)
    t = t.strip(" .,:;")
    # 1) 上下文型：直接返回场景常见目标/干扰类别
    for pat, pos_examples, neg_examples in _CONTEXT_EXAMPLES:
        if pat.search(t):
            return pos_examples, neg_examples
    # 2) 直接目标类（containing X / with X / pictures of X / click on the X）
    subject = ""
    for pat, tmpl in _TEMPLATES:
        m = pat.search(t)
        if m:
            subj = t[m.end():]
            subj = re.sub(r"\b(in|in the|in the image|pictured|pictured above|above|below)\b.*$", "", subj, flags=re.I)
            subj = re.sub(r"\s+", " ", subj).strip(" .,:;")
            if subj:
                subject = subj
            break
    if subject:
        pos = [subject,
               "an image containing " + subject,
               "a photo of " + subject,
               "a picture of " + subject]
        return pos, _GENERIC_NEGATIVES
    # 3) 兜底
    return [t], _GENERIC_NEGATIVES


def solve(task: str, images: list, top: int = 9) -> dict:
    _load()
    import torch
    t0 = time.time()
    # 垃圾任务防御（run24 实测 checkbox-invisible frame 的 JS blob 被当任务文本：
    # 数千字符直接送 CLIP 触发 max_position_embeddings=77 的 500）
    task = str(task or "").strip()
    if (len(task) > 300 or task.startswith("/*") or "/* {" in task
            or task.count("{") > 5 or "function" in task.lower() or "!function" in task):
        logger.warning("任务文本疑似非挑战文本，返回空选择: %.80s", task[:80])
        return {"ok": True, "indices": [], "scores": {}, "elapsed": round(time.time() - t0, 2)}
    task = task[:200]
    imgs = []
    bad = []
    for i, src in enumerate(images):
        try:
            imgs.append((i, _fetch_image(src)))
        except Exception as e:
            bad.append(i)
            logger.warning("图片 %d 加载失败: %s", i, e)
    if not imgs:
        return {"ok": False, "error": "no images loadable", "bad": bad}
    pos_examples, neg_examples = _build_prompts(task)
    logger.info("提示: POS=%r NEG=%r", pos_examples[:6], neg_examples[:6])
    texts = pos_examples + neg_examples
    inputs = PROC(text=texts, images=[im for _, im in imgs], return_tensors="pt", padding=True)
    with torch.no_grad():
        out = MODEL(**inputs)
    sim = out.logits_per_text  # (T, N)
    n_pos = len(pos_examples)
    pos_max = sim[:n_pos].max(dim=0).values   # (N,)
    neg_max = sim[n_pos:].max(dim=0).values   # (N,)
    margin = (pos_max - neg_max).tolist()
    ranked = sorted(zip([i for i, _ in imgs], margin), key=lambda x: -x[1])
    # 选择：margin > 0 的格子（目标类与干扰类显著分离），上限 top
    picked = [i for i, mg in ranked if mg > 0][:top]
    if not picked and ranked and ranked[0][1] > -1.0:
        # 弱兜底：全负但最高分尚可 → 取 1 个，交给上层重试观察
        picked = [ranked[0][0]]
    scores = {str(i): round(m, 4) for i, m in ranked}
    result = {"ok": True, "indices": picked, "scores": scores, "elapsed": round(time.time() - t0, 2)}
    logger.info("求解完成: picked=%s elapsed=%.2fs", picked, result["elapsed"])
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
            path = self.path.split("?")[0].rstrip("/")
            task = str(body.get("task", ""))
            images = list(body.get("images", []))
            top = int(body.get("top", 9))
            if path == "/solve_size":
                ref = str(body.get("ref", ""))
                if not ref or not images:
                    self._send(400, {"ok": False, "error": "ref/images empty"})
                    return
                result = solve_size(task, ref, images, top=top)
                self._send(200, result)
                return
            if not images:
                self._send(400, {"ok": False, "error": "images empty"})
                return
            result = solve(task, images, top=top)
            self._send(200, result)
        except Exception as e:
            logger.exception("solve error")
            self._send(500, {"ok": False, "error": str(e)})

    def do_GET(self):
        self._send(200, {"ok": True, "service": "hcaptcha-clip", "model": MODEL_NAME})

    def _send(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    port = int(os.environ.get("HCAPTCHA_CLIP_PORT", "8765"))
    _load()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    logger.info("hCaptcha CLIP 服务就绪: http://127.0.0.1:%d", port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
