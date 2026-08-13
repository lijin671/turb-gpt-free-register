# -*- coding: utf-8 -*-
"""
AccessToken → GrizzlySMS 自动接码 → Codex OAuth refresh_token 独立模块。

用途
----
手上只有 ChatGPT Web 会话的 accessToken（例如本项目"注册成功的token.txt"里的 AT），
账号尚未完成手机验证、无法直接用 Codex。本模块完成：

    输入:  ChatGPT web session accessToken（email 自动从 JWT 解析，可显式覆盖）
    过程:  1) 本地解析 JWT，提取 email、检查过期
           2) （可选）用 AT 调 accounts/check 确认账号有效
           3) 全新干净 BrowserSession 走 Codex OAuth（本地 PKCE）：
              bootstrap authorize → 提交邮箱 → 邮箱 OTP
           4) 【本模块核心】GrizzlySMS 全自动接码完成手机验证
              （取号 → add-phone/send → 轮询收码 → phone-otp/validate，
               失败 cancel 换号重试，成功 complete）
           5) 选 workspace → 拿 authorization code → PKCE 换 token
    输出:  dict，含 refresh_token / access_token / id_token claims /
           接码信息（手机号、activation_id、短信码、尝试次数）/ 凭证文件路径

依赖
----
- GrizzlySMS：config.codex 的 SMS_API_KEY / SMS_API_BASE / SMS_SERVICE / SMS_COUNTRY
  （本模块运行期间强制 SMS_PROVIDER=grizzly，退出时恢复原值）
- 邮箱 OTP：复用 core.email_provider.wait_for_otp（邮箱须在已配置的邮箱池内；
  USE_EMAIL_SERVICE=False 时走人工输入通道）
- 代理：config.PROXY_POOL 或显式传入

CLI
----
    python -m core.phone_verify_refresh <ACCESS_TOKEN> [--email x@y.com]
        [--proxy socks5h://...] [--no-save] [--skip-plan-check] [-v]
"""
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

import argparse
import json
import logging
import random
import time

from config import codex as _codex_cfg
from core.session import BrowserSession
from core.humanize import delay as human_delay
from core.openai_auth import network_preflight, AccountUnusableError
from core.chatgpt_plan import token_claims, check_account_plan
from core import sms_provider
from core.codex_oauth import (
    _generate_pkce,
    _generate_state,
    _bootstrap_authorize,
    _submit_email,
    _submit_email_otp,
    _select_workspace_and_get_callback,
    _extract_code,
    _post_json,
    _response_text,
    _phone_failure_reason,
    exchange_codex_token,
    _parse_id_token,
    build_codex_storage,
    save_codex_credential,
)

logger = logging.getLogger(__name__)


class PhoneVerifyRefreshError(RuntimeError):
    """本模块统一业务错误。"""


# ============================================================
# 上下文：切换接码平台（运行期间临时覆盖，退出恢复）
# ============================================================

class _ForceSmsProvider:
    """sms_provider 每次调用都动态读 config.codex.SMS_PROVIDER，
    这里临时把它压成指定 provider，退出时恢复原值。

    hero 分支额外覆盖 config.plus 的 HERO_SMS_SERVICE/COUNTRY/MAX_PRICE：
    plus.py 的默认值是 GCash 绑卡用的（service=bc, country=4），
    解码保号要的是 OpenAI 服务码（dr）+ 有实体号的国家。
    """

    def __init__(self, provider: str, hero_country: int | None = None):
        self.provider = (provider or "grizzly").strip().lower()
        self.hero_country = hero_country
        self._old_provider = None
        self._old_hero: dict = {}

    def __enter__(self):
        self._old_provider = getattr(_codex_cfg, "SMS_PROVIDER", "grizzly")
        _codex_cfg.SMS_PROVIDER = self.provider
        logger.info(f"[接码] SMS_PROVIDER 临时切换: {self._old_provider} -> {self.provider}")

        if self.provider == "hero":
            from config import plus as _plus
            service = str(getattr(_codex_cfg, "REFRESH_DECODE_HERO_SERVICE", "dr") or "dr")
            country = self.hero_country
            if country is None:
                country = int(str(getattr(_codex_cfg, "REFRESH_DECODE_HERO_COUNTRY", 187) or 187))
            max_price = float(getattr(_codex_cfg, "REFRESH_DECODE_HERO_MAX_PRICE", 0) or 0)
            for key, val in (
                ("HERO_SMS_SERVICE", service),
                ("HERO_SMS_COUNTRY", country),
                ("HERO_SMS_MAX_PRICE", max_price),
            ):
                self._old_hero[key] = getattr(_plus, key, None)
                setattr(_plus, key, val)
            logger.info(
                f"[接码] HeroSMS 参数覆盖: service={service}, country={country}, "
                f"max_price={max_price or '不限'}"
            )
        return self

    def __exit__(self, exc_type, exc, tb):
        _codex_cfg.SMS_PROVIDER = self._old_provider
        if self._old_hero:
            from config import plus as _plus
            for key, val in self._old_hero.items():
                setattr(_plus, key, val)
        return False


def _decode_sms_plan() -> list[tuple[str, int | None]]:
    """解码保号的接码平台尝试顺序：[(provider, hero_country|None), ...]。

    REFRESH_DECODE_SMS_PROVIDERS 支持 "grizzly,hero:187,hero:4" 形式，
    冒号后是该次尝试的 hero 国家码（grizzly 忽略）。
    """
    raw = str(getattr(_codex_cfg, "REFRESH_DECODE_SMS_PROVIDERS", "") or "").strip()
    if not raw:
        raw = "grizzly,hero"
    plan: list[tuple[str, int | None]] = []
    for item in raw.split(","):
        item = item.strip().lower()
        if not item:
            continue
        name, _, country = item.partition(":")
        name = name.strip()
        if not name:
            continue
        try:
            country_code = int(country.strip()) if country.strip() else None
        except ValueError:
            country_code = None
        plan.append((name, country_code))
    return plan or [("grizzly", None)]


# ============================================================
# 步骤 1：从 accessToken 解析账号信息
# ============================================================

def resolve_account_from_token(access_token: str) -> dict:
    """本地解析 JWT（不验签），提取 email / 过期时间。过期或缺 email 直接失败。"""
    token = (access_token or "").strip()
    if not token:
        raise PhoneVerifyRefreshError("accessToken 为空")
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        raise PhoneVerifyRefreshError(
            f"accessToken 已过期（exp={claims.get('token_expires_at')}），请先查活刷新"
        )
    email = (claims.get("email") or "").strip()
    if not email:
        raise PhoneVerifyRefreshError(
            "无法从 accessToken JWT 解析 email（https://api.openai.com/profile.email 缺失），"
            "请用 --email 显式指定"
        )
    logger.info(
        f"[输入] email={email}, plan={claims.get('claim_plan_type') or 'unknown'}, "
        f"exp={claims.get('token_expires_at') or 'unknown'}"
    )
    return claims


# ============================================================
# 步骤 2（可选）：用 accessToken 在线校验账号有效性
# ============================================================

def check_account_alive(access_token: str, proxy: str | None = None) -> dict:
    """借套餐检测链路（accounts/check）确认 AT 在线可用。失败抛错，避免白烧接码费用。

    出口脏（CONNECT tunnel failed / SSL EOF / 5xx）不代表 token 死了：
    resin 池里同一 sid 会粘住某个上游节点，实测可达率只有一部分。
    因此网络类错误换新 sid 重试，只有拿到明确的鉴权答复（401/403 等）
    才判定 token 不可用。
    """
    attempts = max(1, int(getattr(_codex_cfg, "REFRESH_DECODE_ALIVE_CHECK_ATTEMPTS", 4) or 4))
    from config.proxy import pick_proxy

    last_error = ""
    last_http = None
    for attempt in range(1, attempts + 1):
        # proxy 显式传入时只在重试时换 sid；未传入时每轮都抽新出口
        effective = proxy if (proxy and attempt == 1) else (pick_proxy() or proxy)
        result = check_account_plan(access_token, proxy=effective)
        if result.get("ok"):
            logger.info(
                f"[校验] accessToken 在线有效，plan={result.get('plan_type') or 'unknown'}"
                + (f"（第 {attempt} 次尝试）" if attempt > 1 else "")
            )
            return result

        last_error = str(result.get("error") or "")
        last_http = result.get("http_status")
        if last_http is not None:
            # 服务端给了明确答复 → token 状态已确定，不必再换出口
            break
        logger.warning(
            f"[校验] 第 {attempt}/{attempts} 次网络失败（出口疑似脏），换出口重试："
            f"{last_error[:120]}"
        )

    raise PhoneVerifyRefreshError(
        f"accessToken 在线校验失败: {last_error} (http={last_http})"
    )


# ============================================================
# 步骤 3：GrizzlySMS 全自动接码（本模块核心，重写版）
# ============================================================

def sms_verify_phone(session: BrowserSession, max_retries: int | None = None) -> dict:
    """
    在已建立 auth.openai.com 会话（且已过邮箱 OTP）的 BrowserSession 上，
    用当前 SMS_PROVIDER 完成 /add-phone 手机验证。

    调用方需先用 _ForceSmsProvider 上下文压定 provider。

    单个号码的完整生命周期：
        acquire_number 取号
          -> POST /api/accounts/add-phone/send 发短信
             （号码无效/无法投递/限流/WhatsApp通道 -> cancel 换号）
          -> set_status(1) 通知平台短信已发
          -> wait_for_sms_code 轮询收码（超时 -> cancel 换号）
          -> POST /api/accounts/phone-otp/validate 验码（失败 -> cancel 换号）
          -> complete(6) 完成激活

    Returns:
        {
            "phone": "16195551234",         # 不带 + 的号码
            "activation_id": "123456789",   # 接码平台激活 ID
            "sms_code": "123456",           # 收到的短信验证码
            "attempts": 2,                  # 第几次尝试成功
            "provider": "grizzly",          # 实际用的接码平台
        }
    """
    provider_name = str(getattr(_codex_cfg, "SMS_PROVIDER", "grizzly") or "grizzly")
    max_retries = max_retries or int(getattr(_codex_cfg, "SMS_MAX_RETRIES", 3) or 3)
    http = sms_provider._http()
    last_err: Exception | None = None
    try:
        for attempt in range(1, max_retries + 1):
            activation_id = None
            phone = ""
            try:
                activation_id, phone = sms_provider.acquire_number(http)
                logger.info(
                    f"[接码] 尝试 {attempt}/{max_retries}: "
                    f"activation_id={activation_id}, 号码=+{phone}"
                )

                # ---- 发短信 ----
                send_resp = _post_json(
                    session,
                    "https://auth.openai.com/api/accounts/add-phone/send",
                    {"phone_number": f"+{phone}", "channel": "sms"},
                    referer="https://auth.openai.com/add-phone",
                )
                send_text = _response_text(send_resp)
                send_reason = _phone_failure_reason(send_text, send_resp.status_code)
                if send_resp.status_code not in (200, 204) or send_reason:
                    logger.warning(
                        f"[接码] add-phone/send 失败 reason={send_reason or 'unknown'} "
                        f"status={send_resp.status_code}: {send_text[:200]}，换号"
                    )
                    sms_provider.cancel(activation_id, http)
                    _sleep_before_retry(attempt, max_retries)
                    continue

                # ---- 通知平台 + 轮询收码 ----
                sms_provider.set_status(activation_id, 1, http=http)
                try:
                    sms_code = sms_provider.wait_for_sms_code(activation_id, http)
                except sms_provider.SmsCodeTimeout:
                    logger.warning(f"[接码] +{phone} 超时未收到短信，取消换号")
                    sms_provider.cancel(activation_id, http)
                    _sleep_before_retry(attempt, max_retries)
                    continue

                # ---- 验码 ----
                val_resp = _post_json(
                    session,
                    "https://auth.openai.com/api/accounts/phone-otp/validate",
                    {"code": sms_code},
                    referer="https://auth.openai.com/add-phone",
                )
                if val_resp.status_code != 200:
                    val_text = _response_text(val_resp)
                    val_reason = _phone_failure_reason(val_text, val_resp.status_code) or "code_rejected"
                    logger.warning(
                        f"[接码] phone-otp/validate 失败 reason={val_reason} "
                        f"status={val_resp.status_code}: {val_text[:200]}，换号"
                    )
                    sms_provider.cancel(activation_id, http)
                    _sleep_before_retry(attempt, max_retries)
                    continue

                # ---- 成功 ----
                sms_provider.complete(activation_id, http)
                logger.info(f"[接码] 手机验证通过：+{phone}，短信码={sms_code}")
                return {
                    "phone": phone,
                    "activation_id": activation_id,
                    "sms_code": sms_code,
                    "attempts": attempt,
                    "provider": provider_name,
                }

            except sms_provider.SmsNoBalanceError:
                # 余额不足重试无意义，直接终止（由调用方换平台）
                raise
            except sms_provider.SmsProviderError as exc:
                last_err = exc
                logger.warning(f"[接码] 尝试 {attempt} 平台错误：{exc}")
                if activation_id:
                    sms_provider.cancel(activation_id, http)
                _sleep_before_retry(attempt, max_retries)
                continue

        raise PhoneVerifyRefreshError(
            f"{provider_name} 接码重试 {max_retries} 次仍失败"
            + (f"，最后错误：{last_err}" if last_err else "")
        )
    finally:
        http.close()


def verify_phone_with_fallback(
    session: BrowserSession,
    max_retries: int | None = None,
    plan: list[tuple[str, int | None]] | None = None,
) -> dict:
    """按 _decode_sms_plan() 顺序尝试各接码平台，NO_BALANCE / 无号自动换下一家。

    只有全部平台都失败才抛错。返回值同 sms_verify_phone。
    """
    plan = plan or _decode_sms_plan()
    errors: list[str] = []
    for provider, hero_country in plan:
        with _ForceSmsProvider(provider, hero_country=hero_country):
            try:
                return sms_verify_phone(session, max_retries=max_retries)
            except sms_provider.SmsNoBalanceError as exc:
                msg = f"{provider}: 余额不足（{exc}）"
                logger.warning(f"[接码] {msg}，尝试下一个平台")
                errors.append(msg)
                continue
            except PhoneVerifyRefreshError as exc:
                msg = f"{provider}: {exc}"
                logger.warning(f"[接码] {msg}，尝试下一个平台")
                errors.append(msg)
                continue
    raise PhoneVerifyRefreshError(
        "所有接码平台均失败：" + "；".join(errors) if errors else "接码计划为空"
    )


def _sleep_before_retry(attempt: int, max_retries: int) -> None:
    """换号前随机等待至少 3 秒，避免连续提交号码过快触发风控。"""
    if attempt >= max_retries:
        return
    seconds = random.uniform(3.0, 8.0)
    logger.info(f"[接码] 换号前随机等待 {seconds:.1f}s")
    time.sleep(seconds)


# ============================================================
# 主入口
# ============================================================

def run_phone_verify_refresh(
    access_token: str,
    email: str | None = None,
    proxy: str | None = None,
    otp_provider=None,
    save_credential: bool = True,
    skip_plan_check: bool = False,
    max_sms_retries: int | None = None,
) -> dict:
    """
    输入 ChatGPT web session accessToken，完成 GrizzlySMS 手机验证并输出 refresh_token。

    Args:
        access_token:    ChatGPT Web 会话 accessToken（注册成功的token.txt 里的 AT）
        email:           账号邮箱；不传则自动从 AT 的 JWT profile claim 解析
        proxy:           代理；不传从 PROXY_POOL 随机抽
        otp_provider:    邮箱 OTP 获取回调 fn(email, after_ts)->code；
                         默认 core.email_provider.wait_for_otp
        save_credential: True 时把 CPA 兼容凭证落盘 codex_accounts/codex-{email}.json
        skip_plan_check: True 时跳过 AT 在线有效性校验
        max_sms_retries: 覆盖 SMS_MAX_RETRIES

    Returns:
        {
            "ok": True,
            "email": ...,
            "refresh_token": ...,        # ← 目标产物
            "access_token": ...,         # Codex OAuth 的新 AT（非输入的 web AT）
            "id_token": ...,
            "expires_in": ...,
            "plan_type": ...,
            "account_id": ...,
            "phone": {...},              # 接码信息：号码/激活ID/短信码/尝试次数
            "file_path": ... | None,
        }
        失败时 {"ok": False, "email": ..., "error": ..., "phone": {...}|None}
    """
    result: dict = {"ok": False, "email": email or "", "phone": None}
    session: BrowserSession | None = None
    try:
        # ---- 1. 解析 AT ----
        claims = resolve_account_from_token(access_token)
        email = (email or claims.get("email") or "").strip()
        result["email"] = email

        # ---- 2. 可选在线校验 ----
        if not skip_plan_check:
            check_account_alive(access_token, proxy=proxy)

        if otp_provider is None:
            from core.email_provider import wait_for_otp as otp_provider

        # ---- 3. 全新干净 session 走 Codex OAuth ----
        session = BrowserSession(proxy=proxy)
        code_verifier, code_challenge = _generate_pkce()
        state = _generate_state()

        network_preflight(session)
        human_delay("navigate")
        _bootstrap_authorize(session, state, code_challenge)
        human_delay("navigate")

        # ---- 4. 提交邮箱 → 邮箱 OTP ----
        otp_after_ts = time.time()
        _submit_email(session, email)
        human_delay("form")
        email_otp = None
        for otp_attempt in range(1, 4):
            try:
                logger.info(f"[邮箱OTP] 等待验证码（第 {otp_attempt}/3 次）: {email}")
                email_otp = otp_provider(email, after_ts=otp_after_ts)
                break
            except Exception as exc:
                if otp_attempt >= 3:
                    raise
                logger.warning(
                    f"[邮箱OTP] 未收到，重新提交邮箱触发重发: "
                    f"{type(exc).__name__}: {str(exc)[:150]}"
                )
                otp_after_ts = time.time()
                _submit_email(session, email)
                human_delay("api")
        logger.info(f"[邮箱OTP] 收到：{email_otp}")
        human_delay("otp_input")
        _submit_email_otp(session, email_otp)
        human_delay("api")

        # ---- 5. 接码完成手机验证（按 REFRESH_DECODE_SMS_PROVIDERS 顺序，余额不足自动换家） ----
        phone_info = verify_phone_with_fallback(session, max_retries=max_sms_retries)
        result["phone"] = phone_info
        human_delay("post_auth")

        # ---- 6. 选 workspace → 拿 code → 换 token ----
        callback_url = _select_workspace_and_get_callback(session, state)
        code = _extract_code(callback_url, state)
        logger.info(f"[OAuth] 已拿到 authorization code：{code[:24]}...")

        token_resp = exchange_codex_token(session, code, code_verifier)
        id_claims = _parse_id_token(token_resp.get("id_token", ""))
        effective_email = id_claims.get("email") or email

        refresh_token = token_resp.get("refresh_token", "")
        if not refresh_token:
            raise PhoneVerifyRefreshError(
                f"token 响应缺少 refresh_token（scope 需含 offline_access）: "
                f"keys={list(token_resp.keys())}"
            )

        # ---- 7. 可选落盘 CPA 兼容凭证 ----
        file_path = None
        if save_credential:
            storage = build_codex_storage(token_resp, id_claims)
            file_path = str(
                save_codex_credential(storage, effective_email, id_claims.get("plan_type", ""))
            )
            logger.info(f"[落盘] 凭证已保存：{file_path}")

        result.update({
            "ok": True,
            "email": effective_email,
            "refresh_token": refresh_token,
            "access_token": token_resp.get("access_token", ""),
            "id_token": token_resp.get("id_token", ""),
            "expires_in": token_resp.get("expires_in", 0),
            "plan_type": id_claims.get("plan_type", ""),
            "account_id": id_claims.get("account_id", ""),
            "callback_url": callback_url,
            "file_path": file_path,
        })
        logger.info(
            f"[完成] {effective_email}: refresh_token={refresh_token[:16]}..., "
            f"plan={id_claims.get('plan_type') or 'unknown'}"
        )
        return result

    except AccountUnusableError as exc:
        result["error"] = f"账号已废（{exc.error_code}）"
        logger.warning(f"[失败] {result['email']}: {result['error']}")
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.error(f"[失败] {result['email']}: {result['error']}")
        logger.debug("详细错误:", exc_info=True)
        return result


# ============================================================
# CLI
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="输入 ChatGPT accessToken，GrizzlySMS 自动接码完成手机验证，输出 refresh_token"
    )
    parser.add_argument("access_token", help="ChatGPT Web 会话 accessToken")
    parser.add_argument("--email", help="账号邮箱（默认从 JWT 解析）")
    parser.add_argument("--proxy", help="代理，如 socks5h://user:pass@host:port（默认从 PROXY_POOL 抽）")
    parser.add_argument("--no-save", action="store_true", help="不落盘 codex_accounts 凭证文件")
    parser.add_argument("--skip-plan-check", action="store_true", help="跳过 AT 在线有效性校验")
    parser.add_argument("--sms-retries", type=int, default=None, help="接码换号重试次数（默认取配置）")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        logging.getLogger("core").setLevel(logging.INFO)

    result = run_phone_verify_refresh(
        access_token=args.access_token,
        email=args.email,
        proxy=args.proxy,
        save_credential=not args.no_save,
        skip_plan_check=args.skip_plan_check,
        max_sms_retries=args.sms_retries,
    )
    # 结果打到 stdout（敏感字段截断），完整结果由调用方拿返回值
    printable = dict(result)
    for key in ("refresh_token", "access_token", "id_token"):
        if printable.get(key):
            printable[key] = printable[key][:20] + "...(truncated)"
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
