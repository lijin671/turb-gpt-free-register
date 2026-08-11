# 参考仓库评估：akihitohyh/at-maker（2026-08-06）

来源：https://github.com/akihitohyh/at-maker（MIT，2026-07-10 创建，~280KB，TypeScript，85★）
定位：ChatGPT 注册 + AT/RT 提取 + chatgpt2api 上传。CLI/WebUI 双模式，
     微软邮箱池（OAuth/IMAP/Graph/HTTP API）、TempMail.lol、YYDS Mail。

## 结论

与本地项目功能高度重叠（本地为 Python 超集），但 OTP 健壮性设计值得学，
本次已落地其中两块；其余为低优先级/环境相关，见下。

## 已落地（本地移植）

| at-maker 实现 | 本地对应 |
|---|---|
| verification-matcher.ts decodeQuotedPrintable（多字节 QP） | core/otp_utils.py::decode_quoted_printable（早前已移植） |
| looksLikeJunkCode（YYYYMM/年份/跟踪号/连号噪声） | core/otp_utils.py::looks_like_junk_code（本次） |
| markVerificationCodeRejected / rejectedCodesByEmail | core/otp_utils.py::mark_otp_rejected / rejected_otp_codes（本次，8 个 provider 接入） |
| extractVerificationCode 强上下文优先 | core/otp_utils.py::extract_otp 上下文窗口（既有，语义等价） |

## 未移植、评估后暂缓（记录备查）

1. **OTP 基线 seenIds（prepareOtpBaseline）**：发送 OTP 前快照邮件 ID，只认新邮件。
   本地用 after_ts 时间戳 + 30s 时钟容差，语义等价；新增拒绝码记忆后风险进一步收敛。
   若未来某邮箱源时间戳不可靠，可移植「ID 基线 + 2 分钟容差」。
2. **IMAP 会话池 + IDLE TTL**（hotmail.ts）：本地 outlook_client 每次轮询重建连接，
   高频批量注册时可参考，当前非瓶颈。
3. **mailapi.icu HTTP 取件行格式**（`邮箱----https://mailapi.icu/key?...`，type=html→json）：
   本地 outlook_client 无此形态；若采购的是 HTTP API 取件账号而非 IMAP/OAuth，再补。
4. **AbortController 任务立即中断**：本地 WebUI job stop 已覆盖注册任务；
   OTP 轮询线程的中断可后续加。
5. **SOCKS4/5 代理**：at-maker 代理支持 SOCKS；本地树脂池为 HTTP(S) 形态，无需求不移植。
6. **Graph 自动模式（scope 判定 JWT→Graph / IMAP scope→IMAP）**：本地 outlook_client
   已有 graph/outlook/imap 多模式与 token kind 探测，覆盖。

## 其余

- chatgpt2api.ts 上传结构、registration-runner 线程模型、device-profile 指纹：
  本地 cpa_manager_import / registration_service / session.py 均已覆盖或更强。
- 镜像已存 `../at-maker-ref/`。
