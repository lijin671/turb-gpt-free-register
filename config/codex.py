# -*- coding: utf-8 -*-
"""
注册成功后自动跑 Codex OAuth 授权的配置项。
设置 ENABLE_CODEX = False 可完全跳过此步骤。

参数来源：CLIProxyAPI 源码 internal/auth/codex/openai_auth.go + pkce.go，
对照 https://github.com/router-for-me/CLIProxyAPI 逐行确认。
"""
from config.env_loader import env_str, apply_env_overrides


# 是否启用 Codex OAuth 授权（False = 跳过，不影响注册结果）
ENABLE_CODEX: bool = False

# Codex OAuth 客户端 ID（固定值，来自 CLIProxyAPI openai_auth.go:27 ClientID）
CODEX_CLIENT_ID: str = "app_EMoamEEZ73f0CkXaXp7hrann"

# 授权端点（openai_auth.go:25 AuthURL）
CODEX_AUTH_URL: str = "https://auth.openai.com/oauth/authorize"

# 换 token 端点（openai_auth.go:26 TokenURL）
CODEX_TOKEN_URL: str = "https://auth.openai.com/oauth/token"

# 回调地址（openai_auth.go:28 RedirectURI）
# 注意：本地并不真的起这个 server，只用来拦截重定向并从 Location 提取 code。
CODEX_REDIRECT_URI: str = "http://localhost:1455/auth/callback"

# OAuth scopes（openai_auth.go:75 GenerateAuthURL 里的 scope）
CODEX_SCOPE: str = "openid email profile offline_access"

# 输出目录名（仅名字，运行时拼到项目根；与 OUTLOOK_ACCOUNTS_FILE 同级风格）
CODEX_OUTPUT_DIRNAME: str = "codex_accounts"

# 请求超时（秒）
CODEX_REQUEST_TIMEOUT: int = 30


# ============================================================
# Codex 授权方式（2026-06-15 改造）
#
# 旧方案"复用注册的已登录 session"会撞 /choose-an-account 卡死；
# 新方案用全新干净 session 从头登录，走 OpenAI 标准风控路径
# （邮箱 OTP → 手机短信验证 → 选 workspace → 拿 code），
# 手机验证靠接码平台 GrizzlySMS 自动收码。
# ============================================================

# 注册成功后是否自动跑 Codex 授权（True=自动，False=跳过）
ENABLE_CODEX_AUTO: bool = False

# Codex OAuth 授权驱动：
#   "protocol" = 原有 curl_cffi 协议授权
#   "roxy"     = 调用 RoxyBrowser 指纹浏览器完成授权页面/手机验证/回调捕获
#   "cloak"       = 调用 CloakBrowser 完成授权页面/手机验证/回调捕获
#   "browser_use" = 调用 Browser Use Cloud 完成授权页面/手机验证/回调捕获
#   "same_as_registration" = 跟随 REGISTRATION_DRIVER
CODEX_OAUTH_DRIVER: str = "cloak"




# ============================================================
# CPA 管理接口（Codex 授权地址由 CPA 生成，本地只负责跑登录并提交回调）
# ============================================================

# 授权地址来源：
#   "cpa"   = 通过 CPA 管理接口 /v0/management/codex-auth-url 生成（推荐）
#   "local" = 使用本模块保留的本地 PKCE 生成逻辑（兼容旧方案）
CODEX_AUTH_URL_SOURCE: str = "cpa"

# CPA 管理页面或服务地址，例如 http://localhost:8317/admin/oauth
# 实际请求会取 origin，调用：
#   GET  /v0/management/codex-auth-url
#   POST /v0/management/oauth-callback
CPA_MANAGEMENT_URL: str = "http://127.0.0.1:8317/management.html"#/oauth"

# CPA 管理密钥，同时作为 Authorization: Bearer 和 X-Management-Key
CPA_MANAGEMENT_KEY: str = env_str("CPA_MANAGEMENT_KEY", "")

# CPA 管理接口请求超时（秒）
CPA_REQUEST_TIMEOUT: int = 30

# 提交 OAuth callback 给 CPA 的重试次数/基础间隔。
# 遇到 409 Timeout waiting for OAuth callback、网络超时或 5xx 时，会按同一个 callback URL 重试。
CPA_CALLBACK_SUBMIT_RETRIES: int = 5
CPA_CALLBACK_SUBMIT_RETRY_DELAY: int = 6

# CPA 未返回完整 auth json 时，是否仍在本地 codex_accounts/ 记录一份回调提交凭据
CPA_SAVE_CALLBACK_RECEIPT: bool = True

# ============================================================
# 接码平台（手机短信验证用）
# SMS_PROVIDER:
#   "grizzly" = GrizzlySMS，接口说明见 https://api.grizzlysms.com
#   "l"       = 本地 L 取号服务，接口说明见 L_API.md
#   "h"       = 本地 H 取号服务，接口说明见 H_API.md
#   "hero"    = HeroSMS（sms-activate 兼容，菲律宾 GCash 接码），
#               配置见 config/plus.py 的 HERO_SMS_*，出口代理优先走 PROXY_POOL 轮换
# 服务码注意：GrizzlySMS 的 OpenAI/ChatGPT 服务码是 "dr"（API 名称 "AI Chat"），
#             不是 "openai"（实测 2026-08-07，见 Clqx/gpt-token-refresher README）
# ============================================================

SMS_PROVIDER: str = "l"

# 接码 API 基址（GET handler）
SMS_API_BASE: str = "https://api.grizzlysms.com/stubs/handler_api.php"

# 接码 API 密钥（在 GrizzlySMS 后台 → 设置 获取）
# 留空时 Codex 授权的手机验证步会失败；如不需要 Codex 自动授权，把 ENABLE_CODEX_AUTO=False。
SMS_API_KEY: str = env_str("SMS_API_KEY", "")

# 服务代码：OpenAI = "dr"
SMS_SERVICE: str = "openai"

# 国家代码：葡萄牙 = "117" / 美国 = "187"
SMS_COUNTRY: str = "10"

# 单个号愿意支付的最高价格（留空=不限）。透传给 getNumber 的 maxPrice。
SMS_MAX_PRICE: str = ""

# 一个号收不到短信/被拒时，换号重试的最大次数
SMS_MAX_RETRIES: int = 10

# 单个号等待短信的最长秒数（超时则取消该号换下一个）
SMS_CODE_WAIT: int = 120

# 轮询接码平台查短信的间隔（秒）
SMS_POLL_INTERVAL: int = 5

# 接码平台 HTTP 请求超时（秒）
SMS_REQUEST_TIMEOUT: int = 30

# ── 解码保号（AT→refresh_token）────────────────────────────────────────
# 注册成功后是否自动跑 phone_verify_refresh：
#   AT → 接码 → Codex OAuth → refresh_token + CPA 兼容凭证
#   （codex_accounts/codex-{email}.json，可直接导入 CPA/CLIProxyAPI 长期使用）
# 默认 False：不调用、不产生任何接码费用。仅当用户明确说"解码保号"时才置 true。
REFRESH_DECODE_ENABLED: bool = False

# 解码保号的接码平台尝试顺序，逗号分隔，形如 "grizzly,hero:4,hero:187"。
# 冒号后是该次尝试用的 hero 国家码（grizzly 忽略）。
# 某一家返回 NO_BALANCE / 无号 / 重试耗尽时自动换下一家，全部失败才算失败。
#
# 实测（2026-08-13，跑真号验证）：
#   - Grizzly：余额 $0.03 见底，永远 NO_BALANCE。充值 ≥$2 后是唯一可行平台
#     （08-08 用它连产 12 个号，美国实体号 $0.13/个，秒到码）。
#   - HeroSMS：余额 $1.00，dr(OpenAI) 号池很大（菲律宾 4 $0.0275、英国 16
#     $0.0413、印尼 6/巴西 73 $0.0495、美国 187 $0.605），但**每个号都被
#     OpenAI add-phone 拒收**：`fraud_guard`（"suspicious behavior from phone
#     numbers similar to yours"）或 `phone_number_in_use`。菲律宾/英国/美国
#     各试 2-3 号全灭。号会退款（成本≈0），但每号白耗 2-3 分钟。
#     → 因此默认**不**把 hero 放进 plan，避免拖慢 cron 批量注册。
#     风控放松或换号商后想重新启用：改成 "grizzly,hero:4,hero:16,hero:187"。
REFRESH_DECODE_SMS_PROVIDERS: str = "grizzly"

# hero 分支的服务码/默认国家/限价（覆盖 config/plus.py 的 GCash 默认值）
REFRESH_DECODE_HERO_SERVICE: str = "dr"
REFRESH_DECODE_HERO_COUNTRY: int = 4
REFRESH_DECODE_HERO_MAX_PRICE: float = 0.0

# 解码保号前的 AT 在线校验重试次数。resin 池里同一 sid 会粘住某个上游节点，
# 实测 api/chatgpt 可达率只有一部分（Pokemon ~85%，Premium ~10%），
# CONNECT tunnel failed / SSL EOF 这类网络错不代表 token 死了 → 换 sid 重试。
REFRESH_DECODE_ALIVE_CHECK_ATTEMPTS: int = 4


# ============================================================
# H 取号服务（SMS_PROVIDER="h" 时使用）
# ============================================================

# H API 基址，例如本地后台：http://localhost:8788
H_API_BASE: str = "http://localhost:8788"

# H 后台授权码，对应 H_API.md 里的 Authorization: Bearer <ADMIN_AUTH_CODE>
H_ADMIN_AUTH_CODE: str = env_str("H_ADMIN_AUTH_CODE", "")

# H 返回的号码如果不含国家码，可在这里补前缀；留空则直接使用 H 返回的 item.phone。
H_PHONE_PREFIX: str = ""

# H 取号方式：
#   "reusable" = 优先复用号码，调用 /api/admin/h/take-reusable-phone（默认）
#   "new"      = 每次取新号，调用 /api/admin/h/take-phone
H_PHONE_ACQUIRE_MODE: str = "reusable"


# ============================================================
# L 取号服务（SMS_PROVIDER="l" 时使用）
# ============================================================

# L API 基址，例如本地后台：http://localhost:8788
L_API_BASE: str = "http://localhost:8788"

# L 后台授权码，对应 L_API.md 里的 Authorization: Bearer <ADMIN_AUTH_CODE>
L_ADMIN_AUTH_CODE: str = env_str("L_ADMIN_AUTH_CODE", "")

# L 返回的号码如果不含国家码，可在这里补前缀；例如美国本地 10 位号填 "1"。
# 留空则直接使用 L 返回的 item.phone。
L_PHONE_PREFIX: str = ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'ENABLE_CODEX_AUTO': 'bool', 'CODEX_OAUTH_DRIVER': 'str', 'CODEX_AUTH_URL_SOURCE': 'str', 'CPA_MANAGEMENT_URL': 'str', 'CPA_MANAGEMENT_KEY': 'str', 'CPA_REQUEST_TIMEOUT': 'int', 'CPA_SAVE_CALLBACK_RECEIPT': 'bool', 'SMS_PROVIDER': 'str', 'SMS_COUNTRY': 'str', 'SMS_SERVICE': 'str', 'SMS_MAX_RETRIES': 'int', 'SMS_CODE_WAIT': 'int', 'SMS_API_KEY': 'str', 'REFRESH_DECODE_ENABLED': 'bool', 'REFRESH_DECODE_SMS_PROVIDERS': 'str', 'REFRESH_DECODE_HERO_SERVICE': 'str', 'REFRESH_DECODE_HERO_COUNTRY': 'int', 'REFRESH_DECODE_HERO_MAX_PRICE': 'float', 'REFRESH_DECODE_ALIVE_CHECK_ATTEMPTS': 'int', 'H_API_BASE': 'str', 'H_ADMIN_AUTH_CODE': 'str', 'H_PHONE_PREFIX': 'str', 'H_PHONE_ACQUIRE_MODE': 'str', 'L_API_BASE': 'str', 'L_ADMIN_AUTH_CODE': 'str', 'L_PHONE_PREFIX': 'str'})
