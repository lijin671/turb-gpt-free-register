# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）

Resin 动态会话：
    resin 代理支持在用户名中注入 `-session-{SID}` 实现每会话独立出口 IP。
    为避免静态 sid 被 ChatGPT 风控拉黑（HTTP 403），pick_proxy() 每次调用
    都会生成全新随机 sid，保证每次注册/绑卡拿到新鲜 IP。

1ip1号 纪律（论坛经验：同一出口 IP 短时间注册多个号会连坐死号）：
    core.ip_discipline 在注册管线里强制"一个 IP 一个号"：静态代理按 host:port
    去重，冷却窗口（IP_COOLDOWN_SECONDS）内不重复分配；树脂动态会话天然唯一。
    pick_disciplined_proxy() 返回满足纪律的代理，池不足时返回 None 由调用方等待。
"""
from config.env_loader import apply_env_overrides
import logging
import random
import re
import secrets
import string
import time

logger = logging.getLogger(__name__)


# 本地代理入口；实际出口地区以代理/分流规则为准。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；本地代理端口未监听时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 2
PLAN_CHECK_RETRY_DELAY = 1.5

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3

# ---- 1ip1号 纪律（core.ip_discipline 使用）----
# 开启后注册管线强制一个 IP 一个号（树脂动态会话天然满足；静态代理按 host:port 去重）
IP_DISCIPLINE_ENABLED = True
# 同一 IP 使用后的冷却窗口（秒），冷却期内不再分配给新账号
IP_COOLDOWN_SECONDS = 1800
# 成功注册后的 IP 冷却窗口（秒）：论坛经验（2708795）同一 IP 短时间注册多号会连坐死号，
# 且成功号 token 常 ~30 分钟内被服务端吊销；静态代理上同一天复用"成功过的 IP"再注册
# 仍有连坐风险，故成功冷却默认 24h（树脂动态会话 sid 天然唯一，不受影响）
IP_SUCCESS_COOLDOWN_SECONDS = 86400
# 同一 IP 最多允许的注册账号数（1 = 严格 1ip1号）
MAX_ACCOUNTS_PER_IP = 1
# 注册管线等待可用 IP 的最长秒数（池被冷却/占满时自节流），超过则任务失败
IP_DISCIPLINE_MAX_WAIT_SECONDS = 600

# 注册出口地区偏好（教程 8.8：日区 IP 才有机会刷出 Plus 试用资格 + GCash 支付）
#   ""  = 不限（老行为）
#   "jp" = 优先日本出口；"us" = 美国出口
# 提链阶段入口 US/出口 JP 由提链服务端控制，与这里无关。
REGISTRATION_PREFER_REGION: str = ""


# resin 会话 sid 参数注入位置（用户名中的占位标记）
_RESIN_SESSION_PATTERNS = [
    # 形如 {Name}.cli-session-{sid}:token@host:port
    re.compile(r'([A-Za-z]+\.cli)-session-[A-Za-z0-9]+(:)'),
]


def _fresh_sid(length: int = 8) -> str:
    """生成随机 sid，确保每次注册拿到独立出口 IP。"""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _ensure_fresh_session(proxy: str) -> str:
    """
    对 resin 代理（含 -session- 占位或纯 Premium.cli）注入/替换随机 sid。

    若代理 URL 中已含 -session-{sid}，则替换为新 sid；
    若为 Premium.cli 纯账号，则追加 -session-{sid}。
    非 resin 代理原样返回。
    """
    if not proxy:
        return proxy
    # 只处理 resin 的 {Name}.cli 账号（Premium / Pokemon / Default 等）
    if not re.search(r'[A-Za-z]+\.cli', proxy):
        return proxy
    sid = _fresh_sid()
    # 已含 -session-xxx → 替换
    replaced = re.sub(
        r'([A-Za-z]+\.cli)-session-[A-Za-z0-9]+',
        r'\1-session-' + sid,
        proxy,
    )
    if replaced != proxy:
        return replaced
    # 纯 {Name}.cli → 追加 -session-{sid}
    return re.sub(
        r'([A-Za-z]+\.cli)',
        r'\1-session-' + sid,
        proxy,
        count=1,
    )


def proxy_ip_key(proxy: str) -> str:
    """
    返回代理对应的"出口 IP 键"，用于 1ip1号 纪律去重/冷却。

    - resin 动态会话（含 `-session-{sid}`）：每个 sid 对应独立出口 IP，
      键 = 去掉密码后的完整 URL（sid 保留，天然唯一）
    - 静态代理（无 .cli）：键 = scheme://host:port（同一 IP 每次相同）
    - 空串/None：键 = ""（直连，视为固定出口，同样受纪律约束）
    """
    if not proxy:
        return ""
    # 去掉密码，避免把敏感信息写进状态文件/日志
    try:
        import re as _re
        cleaned = _re.sub(r'(://[^:/@]+):([^@]*)@', r'\1:***@', proxy)
    except Exception:
        cleaned = proxy
    if re.search(r'[A-Za-z]+\.cli', proxy):
        # resin：sid 在用户名里，每个 sid 一个 IP → URL 本身即键
        return cleaned
    # 静态代理：去掉用户名密码，只留 scheme://host:port
    m = re.match(r'^(https?|socks5h?|http)://[^/]*?([^:@/]+)(?::[0-9]+)?(/|$)', proxy)
    try:
        from urllib.parse import urlparse
        parsed = urlparse(proxy)
        scheme = parsed.scheme or "proxy"
        host = parsed.hostname or ""
        port = parsed.port or ""
        suffix = f":{port}" if port else ""
        return f"{scheme}://{host}{suffix}"
    except Exception:
        return cleaned


def pick_proxy() -> str:
    """从代理池中随机抽取一个代理 URL；池为空时返回空串（即不使用代理）。

    resin 代理每次调用都会注入全新随机 sid → 每次注册独立出口 IP。
    """
    if not PROXY_POOL:
        return ""
    chosen = random.choice(PROXY_POOL)
    return _ensure_fresh_session(chosen)


def pick_disciplined_proxy(*, owner: str = "", cooldown: int | None = None, max_per_ip: int | None = None) -> str | None:
    """
    1ip1号 纪律选代理：从池里挑一个"当前可用"的代理（IP 不在冷却、未超账号数上限）。

    返回代理 URL（resin 已注入全新 sid）；无可用代理时返回 None（调用方应等待/跳过）。
    关闭纪律（IP_DISCIPLINE_ENABLED=False）时退化为 pick_proxy()。
    """
    from core.ip_discipline import claim_proxy, is_ip_free
    if not IP_DISCIPLINE_ENABLED:
        return pick_proxy()
    if not PROXY_POOL:
        return ""
    # 随机打乱候选顺序，避免每次都从同一位置开始
    candidates = list(PROXY_POOL)
    random.shuffle(candidates)
    for chosen in candidates:
        proxy = _ensure_fresh_session(chosen)
        free, reason = is_ip_free(proxy, cooldown=cooldown, max_per_ip=max_per_ip)
        if not free:
            continue
        if claim_proxy(proxy, owner=owner or "registration"):
            return proxy
    return None


_REGION_PROBE_TARGET = "https://www.cloudflare.com/cdn-cgi/trace"


def probe_exit_region(proxy: str, timeout: float = 20.0) -> tuple[str, str]:
    """探测代理出口地区。

    Returns:
        (country, ip)：country 取 Cloudflare trace 的 loc（如 JP/US/SG，大写）；
        探测失败或非 200 返回 ("", "")。
    """
    if not proxy:
        return ("", "")
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.get(
            _REGION_PROBE_TARGET,
            proxies={"http": proxy, "https": proxy},
            timeout=timeout,
            impersonate="chrome",
        )
        if resp.status_code != 200:
            return ("", "")
        text = resp.text
        loc = re.search(r"loc=(\S+)", text)
        ip = re.search(r"ip=(\S+)", text)
        return (loc.group(1).upper() if loc else "", ip.group(1) if ip else "")
    except Exception:
        return ("", "")


def pick_region_proxy(
    region: str,
    *,
    owner: str = "",
    max_attempts: int = 12,
    probe_timeout: float = 20.0,
) -> str | None:
    """
    按出口地区挑代理（1ip1号 纪律内）。

    每次从池中抽取全新 resin sid（= 新出口 IP）并探测出口地区，命中 region
    后按 IP 纪律 claim 再返回。max_attempts 次内未命中返回 None（调用方决定
    失败或继续）。region 为空时退化为 pick_disciplined_proxy()。

    Args:
        region: 目标地区，如 "jp" / "us"（大小写不敏感）
        owner: 纪律占用者标识（默认 registration）
        max_attempts: 最多探测次数（每次一个新出口 IP）
        probe_timeout: 单次地区探测超时（秒）
    """
    region = str(region or "").strip().upper()
    if not region:
        return pick_disciplined_proxy(owner=owner)
    if not PROXY_POOL:
        return ""
    from core.ip_discipline import claim_proxy, is_ip_free

    candidates = list(PROXY_POOL)
    random.shuffle(candidates)
    for _ in range(max(1, int(max_attempts))):
        chosen = random.choice(candidates)
        proxy = _ensure_fresh_session(chosen)
        if IP_DISCIPLINE_ENABLED:
            free, reason = is_ip_free(proxy)
            if not free:
                continue
        country, ip = probe_exit_region(proxy, timeout=probe_timeout)
        if country != region:
            logger.info(
                "[代理地区] 命中失败 sid=%s 出口=%s(%s)，继续探测 %s...",
                (proxy.split("@")[0].split("-")[-1] if "@" in proxy else "?"),
                ip or "?", country or "?", region,
            )
            continue
        if IP_DISCIPLINE_ENABLED and not claim_proxy(proxy, owner=owner or "registration"):
            continue
        logger.info("[代理地区] 命中 %s 出口: %s (%s)", region, ip or "?", country)
        return proxy
    return None


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'IP_DISCIPLINE_ENABLED': 'bool',
    'IP_COOLDOWN_SECONDS': 'int',
    'IP_SUCCESS_COOLDOWN_SECONDS': 'int',
    'MAX_ACCOUNTS_PER_IP': 'int',
    'IP_DISCIPLINE_MAX_WAIT_SECONDS': 'int',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
    'REGISTRATION_PREFER_REGION': 'str',
})
PROXY = pick_proxy()
