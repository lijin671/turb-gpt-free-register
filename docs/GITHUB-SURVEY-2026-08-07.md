# GitHub 调研 2026-08-07（free 号池方向）

> 背景：GCash/HeroSMS 支付渠道线暂缓。重新从 GitHub 找可并入主线的开源项目。
> 主线：free 号「注册即用 → chatgpt2api 变现」，痛点是 AT 注册后 8~15 分钟被吊销。

## 已移植（本机已就绪）

### core/phone_verify_refresh.py（来自 Clqx/turb-gpt-free-register fork）
- **功能**：ChatGPT accessToken → GrizzlySMS 自动接码完成手机验证 → Codex OAuth（本地 PKCE）→
  输出 **refresh_token** + 新 AT（CPA 兼容凭证落盘 codex_accounts/codex-{email}.json）。
- **价值**：拿到 refresh_token 后号可长期复用，摆脱「注册后 8~15 分钟吊销就得重注册」的死循环。
- 依赖本地全部存在（codex_oauth.build_codex_storage/save_codex_credential、
  chatgpt_plan.token_claims、openai_auth.network_preflight、sms_provider），import 即用。
- 已加 `__main__` 项目根 sys.path 引导，可直接 CLI：
  ```bash
  .venv/bin/python core/phone_verify_refresh.py <ACCESS_TOKEN> --email xxx@example.com
  ```
- 单测 6 个（JWT 解析/过期、接码上下文 SMS_PROVIDER 恢复、全流程 mock、失败路径）。
- 注意：模块运行期把 SMS_PROVIDER 临时切 grizzly（`_ForceGrizzlyProvider`）并恢复，
  OpenAI/ChatGPT 的 GrizzlySMS 服务码是 **`dr`**（"AI Chat"），不是 openai。

## 候选项目（未并入，按优先级）

| 项目 | ★ | 活跃 | 价值 | 建议 |
|---|---|---|---|---|
| myfanhua/turb-gpt-free-register（upstream） | 726 | 08-07 | 领先本地 23 提交：通用取码兼容(08-05)、查活功能+换IP重试(08-03)、mailcom 改密、sub2api 配置、name_samples | 选择性移植（本地二开 plus/GCash 会冲突，勿整体 merge） |
| xiaoguzuiniu/gpt-free-register | 185 | 08-07 | 纯协议 + curl_cffi TLS 指纹 + 单进程多线程并发 + Codex OAuth 落 CLIProxyAPI 凭证 + WebUI | 参考并发注册与凭证落库设计 |
| FakeOAI/tokens | 414 | 08-07 | 号池调度管理：多平台模型统一转 OpenAI/Anthropic/Gemini API，支持 Claude Code/Codex/GeminiCli | 变现层备选（对比 chatgpt2api） |
| Clqx/gpt-token-refresher | 0 | 08-06 | AT→refresh_token 独立 WebUI（依赖本机已移植的 core/phone_verify_refresh.py） | 如需 WebUI 版保活可部署 |
| Clqx/gpt-account-register | 0 | 08-06 | email→注册→AT WebUI，relay 到 refresher | 与 refresher 配套 |
| hzhsec/cc-sync | 3 | 07-19 | cc-switch.db 导出 ChatGPT OAuth 凭证 → CPA/Sub2API/Cockpit/9Router 格式 + Auth0 refresh_token 轮换 | 凭证格式转换参考 |
| slippersheepig/ChatGPT-to-API | 24 | 06-04 | 支持不登录/账密/refresh_token 登录的 ChatGPT-to-API 增强 | 对比本机 ChatGPT-to-API |
| XyraSinclair/codexpool | 0 | 07-10 | 多 Pro 账号合成一个 Codex 登录，token 注入代理热切换 | 号池玩法参考 |
| ZSCGR/rt-to-at | 0 | 25-06 | chatgpt refresh token → access token | 小工具参考 |

## 待办（可选）
1. 用现存 free 号（如 id=76/77）试跑 `phone_verify_refresh`，拿第一个 refresh_token 验证保活链路
   （需 GrizzlySMS key + SMS_SERVICE=dr 配置）。
2. 评估 xiaoguzuiniu 并发注册的「多线程 + 凭证落库」是否值得移植进 main.py 批量。
3. 拉 upstream myfanhua 08-01~08-05 的「通用取码兼容/查活换IP重试」补丁（本地已移植查活子集）。

## 补充评估（08-07 晚）

### upstream「通用取码兼容」补丁：不建议整体合并
- 2577013 / 19b7950 主要改 `core/generic_api_mail_client.py`（upstream +289 行），
  但**本地该文件是重写版（240 行 vs upstream ~700 行）**，直接 merge 会整体冲突。
- 结论：如需该功能（更多通用取码 API 格式兼容），应把 upstream 版按「支持的取码格式列表」
  逐项对照本地版补缺口，而不是整文件替换。默认先不动。

### GCash/HeroSMS 线收尾
- 实测号 699787976 已释放，HeroSMS 余额回到 $1.3082（测试花费已退回）。
- 配置保持可用：SMS_PROVIDER=hero / HERO_SMS_SERVICE=bc / HERO_SMS_COUNTRY=4。
- 代码保留（hero_sms.py、gcash_registrar.py、plus.py GCash 段），随时可恢复启用。

### xiaoguzuiniu/gpt-free-register 并发设计评估：无需移植
- 已拉取源码对比（/tmp/xiaoguzuiniu-src）：与本地**同源同基代码**
  （main.py 的 run_parallel_batch / ThreadPoolExecutor 实现与本地完全一致，
  核心文件 diff 均 100~1000 行为本地二开增量所致）。
- 它的独有卖点（Cloudflare 域名邮箱 + QQ IMAP、WebUI、失败号回收）本地均已具备
  （qqmail_client.py、webui/、account_liveness 回收）。
- 结论：无独有功能需要移植；本地功能集已覆盖并超出。

### GrizzlySMS 接入已就位（只差 key）
- .env.example 已注明：**OpenAI/ChatGPT 服务码 = `dr`**（不是 openai）；
  config/codex.py 注释同步。
- 拿到 SMS_API_KEY 后一条命令验证：
  ```bash
  SMS_PROVIDER=grizzly SMS_SERVICE=dr python tools/check_sms_provider.py --json
  ```
