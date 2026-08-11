# 参考仓库评估：napnow/sleep-reg（2026-08-06）

来源：https://github.com/napnow/sleep-reg（MIT，2026-08-05 创建，~59KB，Python）
定位：ChatGPT 半自动注册机（grokRegister-cpa 同源架构），协议模式 + Playwright 浏览器模式，
     DuckMail/Outlook/Cloudflare Worker 邮箱，Sentinel PoW/VM、Turnstile 纯 Python 解算，结果上传 gpt2api。

## 结论

与本地项目同源（grokRegister-cpa 架构链），本地项目已是其功能超集，**总体参考价值中低**。
但本地代码中已有 7 处直接引用 sleep-reg 的实现/思路（多为移植后增强），
另有 2 个细节尚未移植，值得评估。

## 已引用点（本地已落地，勿重复）

| sleep-reg 实现 | 本地对应（已移植+增强） |
|---|---|
| `gpt_register.py::_chatgpt_web_authorize` 直接构造 authorize 短路径 | `core/chatgpt_auth.py::build_direct_authorize_url`（额外带 `ext-passkey-client-capabilities=11111`，来自本地 ARE 抓包） |
| `gpt_register.py` 等不到码 -> 显式 send_otp -> 再等一轮 | `main.py` OTP 超时补发逻辑（`OTP_RESEND_ON_TIMEOUT`） |
| `gpt_register.py` 密码注册分支（user/register + 显式 send） | `core/openai_auth.py::register_user`（`PROTOCOL_PASSWORD_BRANCH_ENABLED`） |
| `protocol/turnstile.py` 纯 Python 解算（OrderedMap + XOR + 虚拟执行） | `core/turnstile_solver.py`（MIT 移植，增强边界） |
| `scripts/turnstile_mint.py` Playwright mint | `core/turnstile_browser_mint.py` + `tools/turnstile_mint.py` |
| `protocol/sentinel_vm.py::_ensure_sdk` 版本发现 + 按版本缓存 | `core/sentinel_sdk.py`（TTL + 多 CDN + 本地 vendor 回退） |
| `email_providers/duckmail.py` mail.tm / `outlook.py` +alias | `core/mailtm_client.py`、`core/outlook_client.py`、`config/email.py`（本地为多来源调度超集） |

## 尚未移植、值得评估的 2 点

1. **OTP 重新发送携带 Sentinel 头**
   - sleep-reg `gpt_register.py::_send_otp`：GET `/api/accounts/email-otp/send` 时带
     `openai-sentinel-token`（复用 authorize/continue 阶段 token）+ `openai-sentinel-so-token`。
   - 本地 `core/openai_auth.py::send_email_otp` 只带导航头，无 sentinel。
   - 现状：本地 HAR 对齐结论是 validate 不带 sentinel（`SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE=False`），
     send 阶段从未验证过带 sentinel 的形态。若重发遇到 4xx（403/429 sentinel 风控），
     可 A/B：复用 authorize_continue 的 token 重发，或新 mint `email_otp_send` flow 后重发。
   - 状态：**已落地**（2026-08-06）——`SEND_SENTINEL_ON_EMAIL_OTP_SEND` 开关 +
     `send_email_otp` 可选参数 + `main.py::_resend_otp` 三处调用点统一。

2. **浏览器模式复制本机 Chrome profile（Cookies/Local State/Preferences → 临时目录）**
   - sleep-reg `browser_register.py::_chrome_profile_copy`：用真实 Chrome 持久化上下文做
     CF/sentinel 步骤，规避锁文件。
   - 本地浏览器驱动（cloakbrowser/roxybrowser/skyvern/browser_use）未采用此技巧；
     若未来浏览器兜底链路需要"真 Chrome 指纹 + 持久会话"，可作为参考。

## 其余低价值项（本地已覆盖或无需移植）

- `cpa_upload.py`：`{"accounts":[...]}` 上传 → 本地 `cpa_manager_import.py`/`chatgpt2api_export.py` 更强。
- `connectivity.py`/`test_setup.py` 预检 → 本地 `check_protocol_chain.py`/`daily_health_check.py` 等。
- `sso_to_auth_json.py` → 本地 `chatgpt2api_export.py`/`account_export.py`。
- `chatgpt_register_ttk.py` tkinter GUI → 本地 `webui/` 更完整。
- `email_providers/cloudflare.py` admin allocate/watch → 本地 `cf_temp_mail_client.py` 已对齐 grokRegister-cpa。
- `yyds` 占位 provider → 作者自述不可用，跳过。

## 结论

sleep-reg 作为轻量参考实现值得保留镜像（已存 `../sleep-reg-ref/`），
不需要整体移植；唯一建议动手的是「send_otp 携带 sentinel」的 A/B 开关。
