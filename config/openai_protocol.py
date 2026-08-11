# -*- coding: utf-8 -*-
"""
OpenAI / ChatGPT OAuth 协议固定参数

来自抓包，OpenAI 自己的 client_id 是固定值。
SENTINEL_SV 是 sdk.js 的版本号，会随 OpenAI 更新而变化。
默认开启 SENTINEL_SDK_AUTO_UPDATE 自动发现（core.sentinel_sdk），
写死值仅作为离线/探测失败时的回退，手动更新时去
https://chatgpt.com/sentinel/<version>/sdk.js 找当前版本。
"""

# OAuth 客户端 ID（固定）
OPENAI_CLIENT_ID = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"

# OAuth scopes
OPENAI_SCOPE = (
    "openid email profile offline_access "
    "model.request model.read "
    "organization.read organization.write"
)

# OAuth audience
OPENAI_AUDIENCE = "https://api.openai.com/v1"

# OAuth 回调（chatgpt.com 端）
OPENAI_REDIRECT_URI = "https://chatgpt.com/api/auth/callback/openai"

# Sentinel SDK 版本号（影响 sentinel iframe URL 与 referer header）
SENTINEL_SV = "20260219f9f6"

# Sentinel /req 请求入口。默认 sentinel.openai.com（2026-08-05 live 验证可用）；
# sleep-reg 与 sentinel-runner.js official 模式使用 chatgpt.com，二者历史上均可，
# 需要切换时通过 SENTINEL_REQ_ORIGIN 环境变量覆盖即可。
SENTINEL_REQ_ORIGIN = "https://sentinel.openai.com"
SENTINEL_REQ_URL = f"{SENTINEL_REQ_ORIGIN}/backend-api/sentinel/req"

# sdk.js 版本自动发现（参考 sleep-reg protocol/sentinel_vm.py 的 _ensure_sdk）：
#   bootstrap 页面源码里包含当前版本号 https://chatgpt.com/sentinel/<version>/sdk.js
SENTINEL_SDK_BOOTSTRAP_URL = "https://chatgpt.com/backend-api/sentinel/sdk.js"
# 下载 sdk.js 时按顺序尝试的 CDN 主机；script_src 指纹使用第一个主机（与真实前端一致）
SENTINEL_SDK_CDN_HOSTS = ("https://chatgpt.com", "https://sentinel.openai.com")
# 是否自动发现并缓存最新 sdk.js；False 时始终用 SENTINEL_SV + 项目自带 sdk.js
SENTINEL_SDK_AUTO_UPDATE = True
# 自动发现结果的内存缓存时长（秒），避免每个请求都去探测版本
SENTINEL_SDK_TTL_SECONDS = 3600

# ChatGPT 页面 build 标识（用于 Sentinel p[6] / documentElement data-build 模拟）
OPENAI_BUILD_ID = "prod-fb4a8a2a751dfec391053cfd7b01c52699ccf78c"

# ChatGPT 前端 CES / API 上报头，来自 2026-07-19 抓包。
OAI_CLIENT_BUILD_NUMBER = "8370486"
OAI_CLIENT_VERSION = OPENAI_BUILD_ID

# Statsig / Analytics SDK 版本，纯协议补齐前端同形态链路时使用。
STATSIG_CLIENT_KEY = "client-nb0qtYlZuy2tCMN5s5ncnuIBCJncjRViT0IzFm7GqST"
STATSIG_SDK_VERSION = "3.32.6"
STATSIG_SDK_TYPE = "javascript-client"
AB_CLIENT_KEY = "client-tN5GMyzpIPKXd3KNv7ANIfiqjRSvNNTTWbZdbdabF58"
AB_SDK_VERSION = "3.32.4"

# HAR 中 email-otp/validate 未携带 Sentinel；默认按 HAR 对齐，保留开关便于回退。
SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE = False

# OTP 重新发送（email-otp/send）是否携带 Sentinel 头（参考 sleep-reg _send_otp：
# 复用/新 mint authorize_continue token）。默认关闭（HAR 对齐）；开启后
# 在等码超时/验证码过期补发时 A/B 验证是否降低 4xx 风控概率。
SEND_SENTINEL_ON_EMAIL_OTP_SEND = False

# 协议注册的密码分支（A/B 分流到 create-account/password 时先 user/register 设密码再收 OTP）。
# False 时遇到密码页直接报错（与旧行为一致），True 时自动走密码分支完成注册。
PROTOCOL_PASSWORD_BRANCH_ENABLED = True

# OTP 等码超时后显式补发一次再等（参考 sleep-reg gpt_register.py 的容错）。
# False 时等码超时直接失败（与旧行为一致）。
OTP_RESEND_ON_TIMEOUT = True

# 阶段1（chatgpt.com NextAuth CSRF/signin 握手）失败时，是否降级为直接构造
# auth.openai.com/api/accounts/authorize 短路径（参考 sleep-reg gpt_register.py
# _chatgpt_web_authorize）。默认关闭：短路径绕过 NextAuth，属备用入口，
# 先在目标环境验证过再开启，避免盲目消耗邮箱。
AUTHORIZE_SHORT_PATH_FALLBACK = False

# 是否补齐 HAR 中 ChatGPT Web 首屏 bootstrap 预热链路。
CHATGPT_ANON_BOOTSTRAP_ENABLED = True
CHATGPT_AUTH_BOOTSTRAP_ENABLED = True
# True 时预热失败会中断主流程；默认 False，仅记录日志并继续。
CHATGPT_BOOTSTRAP_STRICT = False

# ---- .env overrides ----
from config.env_loader import apply_env_overrides
apply_env_overrides(globals(), {
    'SENTINEL_SV': 'str',
    'SENTINEL_REQ_ORIGIN': 'str',
    'SENTINEL_SDK_AUTO_UPDATE': 'bool',
    'SENTINEL_SDK_TTL_SECONDS': 'int',
    'PROTOCOL_PASSWORD_BRANCH_ENABLED': 'bool',
    'OTP_RESEND_ON_TIMEOUT': 'bool',
    'SEND_SENTINEL_ON_EMAIL_OTP_SEND': 'bool',
    'AUTHORIZE_SHORT_PATH_FALLBACK': 'bool',
})
SENTINEL_REQ_URL = f"{SENTINEL_REQ_ORIGIN}/backend-api/sentinel/req"
