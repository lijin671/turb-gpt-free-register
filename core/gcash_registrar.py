# -*- coding: utf-8 -*-
"""
GCash 注册机编排模块（半自动：HeroSMS 接码 + ADB 驱动 GCash App）。

社区实测（2026-08-07，linux.do《GPT Plus GCash最新渠道焚决》）：
  - 全程不能挂梯子；菲律宾号接码只有 HeroSMS 稳定（10 分钟 2 码/IP，需轮换出口 IP）
  - 注册信息用菲律宾信息生成器；KYC 可选（推荐，Card Type 拍照件）
  - OpenAI 结算页出 QR → GCash App 扫码完成支付

本模块职责：
  1. HeroSMS 购买菲律宾号 + 轮询取验证码（已完整实现，可独立测试）
  2. ADB 驱动 GCash App 的注册步骤（需要已连接真机/模拟器；无设备时打印步骤）
  3. 注册完成后产出账号信息，供 plus_zero.run_gcash_checkout 绑定 Plus

ADB 依赖：adb（platform-tools），设备已开启 USB 调试并安装 GCash。

用法（半自动）:
  python tools/gcash_registrar_cli.py --serial EMULATOR_SERIAL --profile profile.json

  profile.json:
  {
    "first_name": "Juan",
    "last_name": "Dela Cruz",
    "birthday": "1990-01-01",
    "email": "juan@example.com"
  }
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

from core.hero_sms import HeroSMSClient, extract_code_from_text


@dataclass
class GcashAccount:
    phone: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    status: str = "registered"  # registered | kyc_done
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "phone": self.phone,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "status": self.status,
        }


class AdbShell:
    """极简 ADB 封装（在本地跑 adb 命令操作手机/模拟器）。"""

    def __init__(self, serial: str = "", adb_bin: str = "adb") -> None:
        self.serial = serial
        self.adb_bin = adb_bin

    def _cmd(self, *args: str, timeout: float = 30) -> str:
        base = [self.adb_bin]
        if self.serial:
            base += ["-s", self.serial]
        base += list(args)
        proc = subprocess.run(base, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)} 失败: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout.strip()

    def is_connected(self) -> bool:
        out = self._cmd("get-state", timeout=10)
        return "device" in out

    def launch_app(self, package: str) -> None:
        self._cmd("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")

    def tap(self, x: int, y: int) -> None:
        self._cmd("shell", "input", "tap", str(x), str(y))

    def input_text(self, text: str) -> None:
        # 空格/特殊字符转义
        escaped = text.replace(" ", "%s")
        self._cmd("shell", "input", "text", escaped)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self._cmd("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))

    def screenshot(self, path: str) -> None:
        self._cmd("exec-out", "screencap", "-p")  # 需要管道重定向，单独实现
        # 简版：存到设备再 pull
        self._cmd("shell", "screencap", "-p", "/sdcard/_gcash_shot.png")
        self._cmd("pull", "/sdcard/_gcash_shot.png", path)

    def press_key(self, keycode: int) -> None:
        self._cmd("shell", "input", "keyevent", str(keycode))


class GcashRegistrar:
    """GCash 注册编排：HeroSMS 取号取码 + ADB 驱动 App。

    无设备（serial 为空）时：只完成 HeroSMS 取号/取码，并把 App 内步骤打印出来，
    适合先用 CLI 验证接码链路。
    """

    def __init__(
        self,
        hero: HeroSMSClient,
        profile: dict | None = None,
        serial: str = "",
        package: str = "com.globe.gcash.android",
        wait_timeout: int = 240,
        poll_interval: int = 5,
    ) -> None:
        self.hero = hero
        self.profile = profile or {}
        self.adb = AdbShell(serial=serial) if serial else None
        self.package = package
        self.wait_timeout = wait_timeout
        self.poll_interval = poll_interval

    # ── 接码链路（可独立使用）───────────────────────────────────────────
    def acquire_number(self, service: str = "gcash", country: int = 6,
                       max_price: float | None = None) -> Any:
        act = self.hero.get_number(service=service, country=country, max_price=max_price)
        logger.info("已购号: id=%s phone=%s cost=%s", act.id, act.phone, act.cost)
        return act

    def wait_otp(self, activation_id: int) -> str:
        code = self.hero.wait_for_code(
            activation_id, timeout=self.wait_timeout, poll_interval=self.poll_interval)
        logger.info("取到验证码: %s", code)
        return code

    # ── App 注册步骤（ADB；坐标为占位，需按实际分辨率校准）──────────────
    def register_app_flow(self, activation_id: int, phone: str) -> str:
        """驱动 App 完成注册并返回验证码。

        坐标均为占位符——不同分辨率/渠道包需要先人工跑一遍，
        用 `adb shell uiautomator dump` 拿真实坐标后填入。
        """
        if self.adb is None:
            print("  [无设备] 请手动完成 GCash App 注册：")
            print(f"    1. 打开 GCash，手机号选菲律宾，输入 {phone}")
            print("    2. 点 Continue，等 Processing 消失")
            print("    3. 输入菲律宾信息生成器的姓名/生日")
            print(f"    4. 收到验证码后填入（HeroSMS 已在后台等待）")
            code = self.wait_otp(activation_id)
            return code

        adb = self.adb
        adb.launch_app(self.package)
        time.sleep(6)

        # [占位] 首次启动页 → 点注册/手机号登录
        adb.tap(540, 1600)
        time.sleep(2)
        # [占位] 输入菲律宾手机号
        adb.input_text(phone)
        adb.press_key(66)  # ENTER
        time.sleep(4)

        # [占位] 填姓名/生日（来自 profile）
        if self.profile.get("first_name"):
            adb.input_text(self.profile["first_name"])
            adb.press_key(61)  # TAB
            adb.input_text(self.profile.get("last_name", ""))
            adb.press_key(66)

        # 等 HeroSMS 来码
        code = self.wait_otp(activation_id)
        # [占位] 输入验证码
        adb.input_text(code)
        adb.press_key(66)
        time.sleep(3)
        return code

    # ── 完整流程 ─────────────────────────────────────────────────────────
    def run(self, service: str = "gcash", country: int = 6,
            max_price: float | None = None) -> GcashAccount:
        act = self.acquire_number(service=service, country=country, max_price=max_price)
        try:
            code = self.register_app_flow(act.id, act.phone)
            self.hero.complete(act.id)
            logger.info("激活完成 id=%s", act.id)
        except Exception:
            try:
                self.hero.cancel(act.id)
            except Exception:
                pass
            raise
        return GcashAccount(
            phone=act.phone,
            first_name=self.profile.get("first_name", ""),
            last_name=self.profile.get("last_name", ""),
            email=self.profile.get("email", ""),
            status="registered",
        )


def load_profile(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════
# 注册流程集成（config.plus 驱动）
# ══════════════════════════════════════════════════════════════════════════

def build_hero_client_from_config(proxy: str = "") -> HeroSMSClient:
    """从 config.plus 构建 HeroSMS 客户端；proxy 缺省时读 HERO_SMS_PROXY。"""
    from config import plus as plus_cfg
    return HeroSMSClient(
        api_key=plus_cfg.HERO_SMS_API_KEY,
        base_url=plus_cfg.HERO_SMS_BASE_URL,
        proxy=proxy or plus_cfg.HERO_SMS_PROXY,
    )


def pick_hero_proxy(exclude: str = "") -> str:
    """批量时给每个 worker 轮换一个出口代理，绕 HeroSMS 的 10分钟2码/IP 限制。"""
    from config import plus as plus_cfg
    if plus_cfg.HERO_SMS_PROXY:
        return plus_cfg.HERO_SMS_PROXY
    try:
        from config.proxy import pick_proxy
        for _ in range(5):
            candidate = pick_proxy() or ""
            if candidate and candidate != exclude:
                return candidate
    except Exception:
        logger.debug("代理池不可用，HeroSMS 走本机 IP", exc_info=True)
    return ""


def register_gcash_account(
    profile: dict | None = None,
    serial: str = "",
    proxy: str = "",
    service: str = "",
    country: int = 0,
    max_price: float | None = None,
    wait_timeout: int = 0,
    poll_interval: int = 0,
    hero: HeroSMSClient | None = None,
) -> GcashAccount:
    """跑一次 GCash 号注册（HeroSMS 买号 + 接码 + ADB 驱动 App）。

    参数缺省时全部从 config.plus 读取；传 hero 可复用已有客户端。
    失败时自动 cancel 激活号并抛异常，由调用方决定是否阻塞主流程。
    """
    from config import plus as plus_cfg
    if hero is None:
        hero = build_hero_client_from_config(proxy=proxy)
    reg = GcashRegistrar(
        hero=hero,
        profile=profile if profile is not None else plus_cfg.GCASH_REGISTER_PROFILE,
        serial=serial or plus_cfg.GCASH_ADB_SERIAL,
        package=plus_cfg.GCASH_APP_PACKAGE,
        wait_timeout=wait_timeout or plus_cfg.HERO_SMS_WAIT_TIMEOUT,
        poll_interval=poll_interval or plus_cfg.HERO_SMS_POLL_INTERVAL,
    )
    return reg.run(
        service=service or plus_cfg.HERO_SMS_SERVICE,
        country=country or plus_cfg.HERO_SMS_COUNTRY,
        max_price=max_price if max_price is not None else (plus_cfg.HERO_SMS_MAX_PRICE or None),
    )
