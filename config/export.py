# -*- coding: utf-8 -*-
"""注册后自动导出到 ChatGPT-to-API 的配置。"""
from config.env_loader import apply_env_overrides

# ChatGPT-to-API access_tokens.json 路径（{email: {token, puid}}）
CHATGPT2API_TOKENS_FILE: str = "/home/zzx/research-repos/ChatGPT-to-API/access_tokens.json"

# 注册成功后是否立即写入 access_tokens.json（token 寿命短，必须趁热导入）
AUTO_EXPORT_TO_CHATGPT2API: bool = True

# 是否同步更新 ChatGPT-to-API/proxies.txt（写入 PROXY_POOL 静态代理）
AUTO_EXPORT_PROXIES_TXT: bool = True

# token 复活成功后是否立即重导出到 access_tokens.json（复活换的新 token 也要趁热写生产池）
AUTO_REEXPORT_AFTER_REVIVE: bool = True

# ============================================================
# CPA Manager Plus（18317）认证文件即时导入
# 注意：CPA Manager Plus 的管理密钥(cpamp_...)与 cli-proxy-api(8317)
# 的 CPA_MANAGEMENT_KEY 不是同一个，见 .env CPA_MANAGER_PLUS_KEY。
# ============================================================

# CPA Manager Plus 管理接口地址
CPA_MANAGER_PLUS_BASE: str = "http://127.0.0.1:18317"

# CPA Manager Plus 管理密钥（docker logs cpa-manager-plus 首启日志可见 cpamp_...）
CPA_MANAGER_PLUS_KEY: str = ""

# 注册成功后是否立即导入 CPA Manager Plus（token 寿命短，必须趁热导入）
AUTO_IMPORT_TO_CPA_MANAGER: bool = True

# 导入后是否调 /models 验证模型发现
CPA_IMPORT_VERIFY_MODELS: bool = True

# token 复活成功后是否立即重导入 CPA Manager Plus（新 token 同样寿命短）
AUTO_REIMPORT_AFTER_REVIVE: bool = True

apply_env_overrides(globals(), {
    'CHATGPT2API_TOKENS_FILE': 'str',
    'AUTO_EXPORT_TO_CHATGPT2API': 'bool',
    'AUTO_EXPORT_PROXIES_TXT': 'bool',
    'AUTO_REEXPORT_AFTER_REVIVE': 'bool',
    'CPA_MANAGER_PLUS_BASE': 'str',
    'CPA_MANAGER_PLUS_KEY': 'str',
    'AUTO_IMPORT_TO_CPA_MANAGER': 'bool',
    'CPA_IMPORT_VERIFY_MODELS': 'bool',
    'AUTO_REIMPORT_AFTER_REVIVE': 'bool',
})
