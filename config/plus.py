# -*- coding: utf-8 -*-
"""
ChatGPT Plus 低成本订阅配置。

根据教程帖子整理：
  - 虚拟卡 BIN 前缀 451311，成本约 2.8 元/张
  - 从 chatgpt.com/api/auth/session 提取 session 凭证提交到支付网关
  - US 地址生成器：usaddressgen.com
  - 支付网关：pay.153.ink
"""
from config.env_loader import apply_env_overrides

# ---------------------------------------------------------------------------
# Plus 订阅开关
# ---------------------------------------------------------------------------

# 注册成功后是否自动触发 Plus 订阅流程
ENABLE_PLUS_SUBSCRIPTION: bool = False

# ---------------------------------------------------------------------------
# 支付网关
# ---------------------------------------------------------------------------

# 支付网关地址（接受 session 凭证并执行绑卡订阅）
PLUS_PAYMENT_GATEWAY: str = "https://pay.153.ink/"

# 支付网关请求超时（秒）
PLUS_PAYMENT_TIMEOUT: int = 30

# 支付网关重试次数
PLUS_PAYMENT_MAX_RETRIES: int = 3

# 重试间隔（秒）
PLUS_PAYMENT_RETRY_DELAY: float = 2.0

# ---------------------------------------------------------------------------
# 虚拟信用卡
# ---------------------------------------------------------------------------

# 虚拟卡 BIN 前缀（卡头）
PLUS_CARD_BIN: str = "451311"

# 虚拟卡成本（元/张），仅作记录
PLUS_CARD_COST: float = 3.90

# 卡号长度（维萨卡 16 位）
PLUS_CARD_NUMBER_LENGTH: int = 16

# 卡信息来源模式：
#   "manual"    = 手工填入（WebUI 中配置）
#   "pool"      = 从卡号池文件读取（每行一张卡）
#   "generated" = 自动生成 BIN 匹配的虚拟卡号（仅供测试）
PLUS_CARD_SOURCE: str = "manual"

# 手动填入的卡信息（当 card_source = "manual" 时使用）
PLUS_CARD_NUMBER: str = ""
PLUS_CARD_EXP_MONTH: str = ""
PLUS_CARD_EXP_YEAR: str = ""
PLUS_CARD_CVV: str = ""

# 卡池文件路径（当 card_source = "pool" 时使用）
# 格式：每行一张卡，格式 "card_number|exp_month|exp_year|cvv"
PLUS_CARD_POOL_FILE: str = ""

# ---------------------------------------------------------------------------
# US 地址生成
# ---------------------------------------------------------------------------

# 是否使用 usaddressgen.com 生成美国地址
PLUS_USE_USADDRESSGEN: bool = True

# US 地址生成接口
PLUS_USADDRESSGEN_URL: str = "https://usaddressgen.com/"

# 如果接口不可用，使用内置备用美国地址
PLUS_BACKUP_ADDRESSES: list = [
    {
        "street": "221B Baker Street",
        "city": "New York",
        "state": "NY",
        "zip": "10001",
        "country": "US",
    },
    {
        "street": "350 Fifth Avenue",
        "city": "New York",
        "state": "NY",
        "zip": "10118",
        "country": "US",
    },
    {
        "street": "1600 Amphitheatre Parkway",
        "city": "Mountain View",
        "state": "CA",
        "zip": "94043",
        "country": "US",
    },
]

# ---------------------------------------------------------------------------
# 浏览器控制台脚本
# ---------------------------------------------------------------------------

# 用于在支付网关控制台中执行的脚本（蓝奏云内文件），此处存储脚本内容或下载地址
PLUS_CONSOLE_SCRIPT_URL: str = "https://wwbnt.lanzoul.com/iBPcr407dd2f"

# 本地缓存的脚本内容（下载后填入，或留空从 HTTP 拉取）
PLUS_CONSOLE_SCRIPT: str = ""

# 脚本文件路径
PLUS_CONSOLE_SCRIPT_PATH: str = ""

# ---------------------------------------------------------------------------
# 代理（跑 Plus 用独立代理，避免关联）
# ---------------------------------------------------------------------------

# Plus 订阅使用的代理模式：
#   "auto"   = 跟随注册代理
#   "pool"   = 从专用 Plus 代理池抽取
#   "direct" = 直连
PLUS_PROXY_MODE: str = "auto"

# Plus 专用代理池
PLUS_PROXY_POOL: list = []

# Plus 专用固定代理（优先于 pool）
PLUS_PROXY: str = ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'ENABLE_PLUS_SUBSCRIPTION': 'bool',
    'PLUS_CARD_SOURCE': 'str',
    'PLUS_CARD_NUMBER': 'str',
    'PLUS_CARD_EXP_MONTH': 'str',
    'PLUS_CARD_EXP_YEAR': 'str',
    'PLUS_CARD_CVV': 'str',
    'PLUS_CARD_POOL_FILE': 'str',
    'PLUS_PROXY_MODE': 'str',
    'PLUS_PROXY_POOL': 'list_str_multiline',
    'PLUS_PROXY': 'str',
})

# ---------------------------------------------------------------------------
# 代理池集成（帖子推荐：github.com/strongshuai/proxy-checker）
# ---------------------------------------------------------------------------

# 是否启用 proxy-checker 集成自动导入免费代理
PLUS_PROXY_CHECKER_ENABLED: bool = False

# proxy-checker 服务地址（本地或远程）
PLUS_PROXY_CHECKER_URL: str = "http://127.0.0.1:5000"

# 从 proxy-checker 拉取的代理类型
#   "http" | "https" | "socks4" | "socks5" | "socks5h"
PLUS_PROXY_CHECKER_TYPE: str = "socks5"

# proxy-checker 代理列表 API 路径
PLUS_PROXY_CHECKER_API_PATH: str = "/api/proxies/txt"

# 超时
PLUS_PROXY_CHECKER_TIMEOUT: int = 15

# ══════════════════════════════════════════════════════════════════════════
# 零元 Plus 开通（切菲 + Stripe 直绑）
# ══════════════════════════════════════════════════════════════════════════

# 是否启用零元 Plus 开通（注册成功后自动触发）
ENABLE_ZERO_PLUS: bool = False

# Stripe Publishable Key（留空则自动从 client_secret 推断）
ZERO_PLUS_STRIPE_KEY: str = ""

# 切菲结算 API payload 中的 entry_point
ZERO_PLUS_ENTRY_POINT: str = "all_plans_pricing_modal"

# 计划名称
ZERO_PLUS_PLAN_NAME: str = "chatgptplusplan"

# 菲律宾结算参数
ZERO_PLUS_COUNTRY: str = "PH"
ZERO_PLUS_CURRENCY: str = "PHP"

# 促销活动 ID（留空则不用促销）
ZERO_PLUS_PROMO_CAMPAIGN_ID: str = "plus-1-month-free"

# 绑卡方式：
#   "api"    = 直接调用 Stripe REST API（纯协议，最快）
#   "browser"= 通过浏览器执行 Stripe.js Card Element（兜底）
ZERO_PLUS_BIND_MODE: str = "api"

# 绑卡后是否自动验证订阅状态
ZERO_PLUS_VERIFY_AFTER_BIND: bool = True

# 绑卡成功但未激活时，是否自动走 checkout confirm 激活链路
# （POST /payments/checkout/update + snapshot + confirm，2026-08 前端实测端点）
ZERO_PLUS_ACTIVATE_AFTER_BIND: bool = True

# Stripe API 绑卡失败且特征为"脏 IP/卡被拒"（payment_method_not_approved /
# Payment was not approved / card_declined 等）时，自动换出口 IP 重试的次数。
# 每次重试调用 pick_proxy() 拿新树脂 sid → 新出口 IP；换完仍失败才走浏览器兜底。
ZERO_PLUS_BIND_PROXY_RETRIES: int = 2

# OpenAI 新策略（2026-08-05 起）：billing country 必须匹配账号注册国家，
# 切菲换区会返回 400 "Billing country must match request country"。
# 开启后：切菲失败时自动降级为账号自身国家结算（非 0 元，但保留绑卡链路可用）。
ZERO_PLUS_COUNTRY_LOCK_FALLBACK: bool = True

# 备用 US 地址（用于绑卡后的 billing address）
ZERO_PLUS_US_ADDRESSES: list = [
    {"street": "221B Baker Street",  "city": "New York",      "state": "NY", "zip": "10001", "country": "US"},
    {"street": "350 Fifth Avenue",   "city": "New York",      "state": "NY", "zip": "10118", "country": "US"},
    {"street": "1600 Amphitheatre Parkway", "city": "Mountain View", "state": "CA", "zip": "94043", "country": "US"},
    {"street": "123 Main Street",    "city": "Los Angeles",   "state": "CA", "zip": "90012", "country": "US"},
    {"street": "456 Oak Avenue",     "city": "Chicago",       "state": "IL", "zip": "60607", "country": "US"},
]

# ---- .env overrides for WebUI ----
apply_env_overrides(globals(), {
    'ENABLE_ZERO_PLUS': 'bool',
    'ZERO_PLUS_STRIPE_KEY': 'str',
    'ZERO_PLUS_BIND_MODE': 'str',
    'ZERO_PLUS_COUNTRY_LOCK_FALLBACK': 'bool',
    'ZERO_PLUS_ACTIVATE_AFTER_BIND': 'bool',
    'ZERO_PLUS_BIND_PROXY_RETRIES': 'int',
})

# ══════════════════════════════════════════════════════════════════════════
# GCash 渠道（真实菲律宾钱包支付 Plus）+ HeroSMS 接码
# ══════════════════════════════════════════════════════════════════════════

# 启用 GCash 支付渠道（真实付款，PHP 计价）
ENABLE_GCASH_PLUS: bool = False

# GCash 模式运行方式：
#   "checkout"    = 切菲结算后输出 Stripe Checkout URL / 二维码，人工或扫码端完成支付（推荐起步）
#   "full"        = checkout + 轮询支付结果 + 自动验证 Plus（需配合真机扫码）
GCASH_PLUS_MODE: str = "checkout"

# 支付完成后轮询验证超时（秒）
GCASH_PLUS_PAYMENT_TIMEOUT: int = 900

# 轮询间隔（秒）
GCASH_PLUS_POLL_INTERVAL: int = 15

# 本地二维码展示端口（mode=checkout 时启动一个临时页面显示结算 QR/URL）
GCASH_PLUS_QR_PORT: int = 8787

# ── HeroSMS 接码（菲律宾号）─────────────────────────────────────────────
# API Key（必填，放 .env: HERO_SMS_API_KEY=...）
HERO_SMS_API_KEY: str = ""

# API Base（默认官方 sms-activate 兼容端点）
HERO_SMS_BASE_URL: str = "https://hero-sms.com/stubs/handler_api.php"

# 请求出口代理（批量时轮换出口 IP 绕过"10分钟2码/IP"限制；留空走本机 IP）
HERO_SMS_PROXY: str = ""

# 服务码（HeroSMS 实测 2026-08-07：GCash 服务码 = "bc"，见 getServicesList）
HERO_SMS_SERVICE: str = "bc"

# 国家 id（HeroSMS 实测 2026-08-07：菲律宾 = 4，见 getCountries）
HERO_SMS_COUNTRY: int = 4

# 单号最高价（美元，留空不限）
HERO_SMS_MAX_PRICE: float = 0.0

# 等待验证码超时（秒）
HERO_SMS_WAIT_TIMEOUT: int = 240

# 轮询间隔（秒）
HERO_SMS_POLL_INTERVAL: int = 5

# 是否优先用 getAllSms 取码（含 text 兜底）
HERO_SMS_PREFER_ALL_SMS: bool = True

# ── 集成进 ChatGPT 注册流程 ──────────────────────────────────────────────
# 教程 8.8 顺序：注册 GPT → 先提链 → 再注册 GCash（HeroSMS 收码时间充裕）。
# ChatGPT 号注册成功后，自动跑「提链」（EXTRACT_LINK_API_BASE/CDK 需在
# config/extract_link.py 或 .env 配置；提链入口 US / 出口 JP 由提链服务端控制）。
ENABLE_EXTRACT_AFTER_REGISTER: bool = False

# ChatGPT 号注册成功后，自动跑 GCash 号注册（HeroSMS 买菲号 + 接码 + ADB 驱动 App）。
# 产出 {chatgpt_email, gcash_phone} 记录，供 run_gcash_checkout 绑定 Plus 使用。
ENABLE_GCASH_REGISTER: bool = False

# 注册机批量并发时，每个 worker 的 HeroSMS 请求是否轮换出口代理
# （绕"10分钟2码/IP"限制；True 时从 PROXY_POOL 抽，配了 HERO_SMS_PROXY 则优先用它）
GCASH_REGISTER_ROTATE_PROXY: bool = True

# GCash 注册机：ADB 设备串号（留空则只打印接码步骤，不操作设备）
GCASH_ADB_SERIAL: str = ""

# GCash App 包名 / Activity（华为出境易等渠道可能不同）
GCASH_APP_PACKAGE: str = "com.globe.gcash.android"

# GCash 注册机：注册信息（菲律宾姓名/生日等，格式见 gcash_registrar.py）
GCASH_REGISTER_PROFILE: dict = {}

# ---- .env overrides ----
apply_env_overrides(globals(), {
    'ENABLE_GCASH_PLUS': 'bool',
    'GCASH_PLUS_MODE': 'str',
    'GCASH_PLUS_PAYMENT_TIMEOUT': 'int',
    'GCASH_PLUS_QR_PORT': 'int',
    'HERO_SMS_API_KEY': 'str',
    'HERO_SMS_BASE_URL': 'str',
    'HERO_SMS_PROXY': 'str',
    'HERO_SMS_SERVICE': 'str',
    'HERO_SMS_COUNTRY': 'int',
    'HERO_SMS_MAX_PRICE': 'float',
    'HERO_SMS_WAIT_TIMEOUT': 'int',
    'HERO_SMS_POLL_INTERVAL': 'int',
    'ENABLE_EXTRACT_AFTER_REGISTER': 'bool',
    'ENABLE_GCASH_REGISTER': 'bool',
    'GCASH_REGISTER_ROTATE_PROXY': 'bool',
    'GCASH_ADB_SERIAL': 'str',
    'GCASH_APP_PACKAGE': 'str',
})
