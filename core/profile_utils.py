# -*- coding: utf-8 -*-
"""注册资料生成工具。"""

from __future__ import annotations

import random
import secrets
import string
from datetime import date, timedelta


def _shift_year_safe(day: date, years: int) -> date:
    """按年偏移日期；遇到 2 月 29 日且目标年非闰年时回退到 2 月 28 日。"""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, month=2, day=28)


def generate_random_birthday(min_age: int = 18, max_age: int = 65) -> str:
    """
    生成年龄在 [min_age, max_age] 闭区间内的随机生日，格式 YYYY-MM-DD。

    例如默认会在“今天满 65 岁”到“今天满 18 岁”之间随机取一天。
    """
    if min_age < 0 or max_age < min_age:
        raise ValueError(f"年龄范围无效: min_age={min_age}, max_age={max_age}")

    today = date.today()
    oldest = _shift_year_safe(today, -max_age)
    youngest = _shift_year_safe(today, -min_age)
    span_days = (youngest - oldest).days
    birthday = oldest + timedelta(days=random.randint(0, span_days))
    return birthday.isoformat()


def generate_random_password(length: int = 14) -> str:
    """生成满足 OpenAI 密码要求的随机密码（大小写+数字+符号，长度可调）。"""
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    symbols = "!@#$%^&*"
    chars = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    pool = upper + lower + digits + symbols
    chars.extend(secrets.choice(pool) for _ in range(max(0, length - len(chars))))
    random.shuffle(chars)
    return "".join(chars)


def registration_password() -> str:
    """注册用密码：优先取 config.register.REGISTER_PASSWORD，否则随机生成。"""
    try:
        from config import register as _register_cfg
        configured = str(getattr(_register_cfg, "REGISTER_PASSWORD", "") or "").strip()
        if configured:
            return configured
    except Exception:
        pass
    return generate_random_password()
