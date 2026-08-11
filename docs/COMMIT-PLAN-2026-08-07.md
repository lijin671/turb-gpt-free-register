# 待提交清单（2026-08-07 晚整理，提交前请确认）

当前：83 个新增文件 + 41 个修改文件（含多日历史二开，一直未提交）。
建议按 5 个提交分批，避免单个提交过大；或直接一个提交全收。

## 提交 1：GitHub 调研 + refresh_token 保活移植（本轮新增）
- core/phone_verify_refresh.py（Clqx fork 移植：AT→接码→Codex OAuth→refresh_token）
- tests/test_phone_verify_refresh.py（6 用例）
- docs/GITHUB-SURVEY-2026-08-07.md、docs/COMMIT-PLAN-2026-08-07.md
- config/codex.py（SMS_PROVIDER=hero 注释 + GrizzlySMS 服务码 dr 提示）
- .env.example（GrizzlySMS 服务码 dr 注释）
- 提交信息：feat: 移植 phone_verify_refresh（AT→refresh_token 保活）+ GitHub 调研记录

## 提交 2：GCash/HeroSMS 接码渠道（本轮新增，已实测）
- core/hero_sms.py、core/gcash_registrar.py、tools/hero_sms_cli.py、tools/gcash_registrar_cli.py
- core/sms_provider.py（hero 平台分支 + 后台取消 + 代理轮换）
- config/plus.py（GCash/HeroSMS/ENABLE_GCASH_REGISTER 配置）、core/db.py（update_job extra）
- main.py（--gcash-register + _maybe_register_gcash）、core/registration_service.py（gcash 字段落库）
- tests/test_hero_sms.py、tests/test_sms_provider_hero.py、tests/test_gcash_register_integration.py
- docs/gcash-channel.md
- 提交信息：feat: GCash/HeroSMS 接码渠道（实测 $0.0605/号，菲律宾 id=4 服务码 bc）

## 提交 3：历史二开核心功能（此前未提交）
- core/account_liveness.py、core/live_check_service.py、core/account_pool.py、core/token_revival.py
- core/ip_discipline.py、core/sentinel_sdk.py、core/hcaptcha_audio.py、core/hcaptcha_grid.py
- core/plus_bind_service.py、core/turnstile_browser_mint.py、core/mailtm_client.py
- tools/auto_import_chatgpt2api.py、tools/live_check_accounts.py、tools/revive_accounts.py 等
- webui/*（批量查活/导入/编辑）
- 提交信息：feat: 号池查活/保活/IP纪律/hCaptcha/CPA 导入等二开

## 提交 4：历史二开测试（与提交 3 配套）
- tests/test_account_token_update.py、test_check_account_pool.py、test_hcaptcha_*.py、
  test_ip_discipline.py、test_live_check_*.py、test_main_*.py、test_plus_*.py、
  test_sentinel_sdk.py、test_token_revival.py、test_turnstile_*.py 等
- 提交信息：test: 二开功能配套测试

## 提交 5：文档与状态
- docs/REF-*.md、docs/STATUS-2026-08-0{5,6,7}.md、docs/protocol_*.md、docs/zero-plus-workflow.md
- 提交信息：docs: 状态日志与参考文档

## 注意
- .env 不入库（已在 .gitignore）；HERO_SMS_API_KEY / SMS_API_KEY 只放 .env。
- 提交前建议再跑一次：timeout 900 .venv/bin/python -m unittest discover -s tests
