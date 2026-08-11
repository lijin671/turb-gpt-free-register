# -*- coding: utf-8 -*-
"""
hCaptcha 音频挑战本地求解器（faster-whisper CPU 推理）。

背景（2026-08-06 实跑）：Stripe 绑卡触发的 hCaptcha 复选框点击被
机器判定拒绝后，会升级为图片/拖拽挑战；hCaptcha 挑战框始终保留
「音频挑战」无障碍入口。本模块把音频下载后用 faster-whisper 识别，
返回归一化答案，供 Playwright 填写提交。

用法：
    from core.hcaptcha_audio import transcribe_hcaptcha_audio
    text = transcribe_hcaptcha_audio(audio_bytes_or_path)
    answer = normalize_hcaptcha_answer(text)
"""
from __future__ import annotations

import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

_MODEL = None
_MODEL_LOCK = threading.Lock()
_MODEL_NAME = os.environ.get("PLUS_HCAPTCHA_WHISPER_MODEL", "tiny")

# 英文数字词 -> 数字
_NUM_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "oh": "0", "o": "0",
}
_NUM_WORD_RE = re.compile(r"\b(" + "|".join(_NUM_WORDS) + r")\b", re.IGNORECASE)
# 单词字母（hCaptcha 音频可能读字母，如 "a b c"）
_LETTER_RE = re.compile(r"\b([a-z])\b", re.IGNORECASE)


def get_whisper_model():
    """lazy 加载 faster-whisper 模型（进程内单例，CPU int8）。"""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        try:
            from faster_whisper import WhisperModel
        except Exception as e:  # pragma: no cover
            logger.warning("[hCaptchaAudio] faster-whisper 未安装: %s", e)
            return None
        try:
            _MODEL = WhisperModel(_MODEL_NAME, device="cpu", compute_type="int8")
            logger.info("[hCaptchaAudio] faster-whisper 模型加载完成: %s (cpu/int8)", _MODEL_NAME)
        except Exception as e:  # pragma: no cover
            logger.warning("[hCaptchaAudio] 模型加载失败: %s", e)
            _MODEL = None
        return _MODEL


def normalize_hcaptcha_answer(text: str) -> str:
    """把 whisper 转写文本归一化为 hCaptcha 音频答案。

    hCaptcha 音频挑战一般读数字/字母序列（如 "three five eight" -> "358"）。
    策略：
      1. 数字词转数字（three five eight -> 358）
      2. 单个字母保留（a b c -> abc）
      3. 去标点/空白，统一小写
    若没有数字/字母则返回原文清理后的文本。
    """
    if not text:
        return ""
    t = text.strip()
    # 数字词优先（hCaptcha 音频最常见是数字串）
    t = _NUM_WORD_RE.sub(lambda m: _NUM_WORDS[m.group(1).lower()], t)
    t = re.sub(r"[^0-9a-zA-Z]", "", t)
    if t:
        return t.lower()
    return re.sub(r"[^0-9a-zA-Z]", "", text.strip()).lower()


def transcribe_hcaptcha_audio(audio_path: str, timeout_s: float = 120.0) -> str:
    """转写 hCaptcha 音频文件，返回纯文本（尽力识别）。"""
    model = get_whisper_model()
    if model is None:
        logger.warning("[hCaptchaAudio] 无 whisper 模型，跳过转写")
        return ""
    try:
        # initial_prompt 提供数字语境，提升数字串识别率
        segments, _info = model.transcribe(
            audio_path,
            language="en",
            beam_size=1,
            initial_prompt="one two three four five six seven eight nine zero",
            vad_filter=True,
        )
        parts = []
        for seg in segments:
            parts.append(seg.text or "")
        return " ".join(parts).strip()
    except Exception as e:  # pragma: no cover
        logger.warning("[hCaptchaAudio] 转写失败: %s", e)
        return ""


def download_audio(url: str, dest: str, timeout_s: float = 30.0) -> bool:
    """下载音频 URL 到本地文件。优先 httpx/requests，失败时用 urllib。"""
    try:
        import httpx
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
            return True
    except Exception as e1:
        logger.debug("[hCaptchaAudio] httpx 下载失败: %s", e1)
        try:
            import requests
            r = requests.get(url, timeout=timeout_s)
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
            return True
        except Exception as e2:
            logger.warning("[hCaptchaAudio] requests 下载失败: %s", e2)
            return False
