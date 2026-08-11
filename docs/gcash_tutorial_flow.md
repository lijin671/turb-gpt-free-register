# GCash 零元 Plus 流程（教程 8.8 落地）

来源：linux.do 帖子「gcash流程已过，关于我遇见里面中的所有卡点（8.8）」（作者 kedre）。
本文把教程步骤映射到本仓库现有能力，并说明已做的代码改造。

## 教程要点 → 代码映射

| 教程步骤 | 仓库能力 | 状态 |
|---|---|---|
| 1. 注册 GCash：定位选**模糊定位**，直接显示菲律宾号码 | 手动（手机设置）；HeroSMS 接码自动部分见 `core/gcash_registrar.py` | 手动步骤 |
| 2. 注册 GPT：**iCloud 邮箱** + **日区 IP** 才有机会刷出试用资格 + GCash 支付 | 新增 `icloud` 邮箱源（`core/icloud_client.py`）+ `REGISTRATION_PREFER_REGION=jp`（`config/proxy.py`） | ✅ 已改造 |
| 3. 先注册 GPT + **提链**，再注册 GCash（HeroSMS 收码时间充裕） | `ENABLE_EXTRACT_AFTER_REGISTER=true` 时 `main.py` 注册成功后先 `extract_link_now()` 再 `_maybe_register_gcash()` | ✅ 已改造 |
| 4. 提链：入口 IP 用 US、出口 IP 用 JP | 提链服务端（如 pay.153.ink 控制台）控制；本仓库只提交 token 等结果 | 服务端配置 |
| 5. GCash 扫码排错：服务器繁忙→重新提链/换 IP；要求验证身份→点验证后加载时直接返回 | 手动；提链失败可重跑 `/api/accounts/extract-link` | 手动步骤 |
| 6. 提链工具：走支付平台风控，不直接支付 | `EXTRACT_LINK_API_BASE` / `EXTRACT_LINK_CDK` 指向提链服务 | 需填配置 |

## 需要的配置（.env）

```bash
# 邮箱：本仓库默认用自有域名池（ManyMail，mail.lijin.ug.cx）
# 若改用 iCloud：把 EMAIL_SOURCE 里的 manymail 换成 icloud，
# 并把邮箱写入 用于注册的icloud邮箱.txt（每行 email====App专用密码）
EMAIL_SOURCE=outlook,generic_api,mailnest,manymail

# 日区出口（注册 GPT 用）
REGISTRATION_PREFER_REGION=jp

# 提链服务（从提链控制台获取，如 pay.153.ink）
EXTRACT_LINK_API_BASE=https://PAY_153_API_BASE
EXTRACT_LINK_CDK=YOUR_CDK
EXTRACT_LINK_TYPE=pix
ENABLE_EXTRACT_AFTER_REGISTER=true

# GCash 注册（HeroSMS 已配置；ADB 设备串号填了才自动驱动 App）
ENABLE_GCASH_REGISTER=false   # 半自动：先手动跑 gcash_registrar_cli
GCASH_ADB_SERIAL=EMULATOR_SERIAL
```

## 运行顺序（推荐半自动）

```bash
# 1) 注册 GPT（日区 IP，自动领邮箱、取码）
python3 main.py --email "" --count 1 --workers 1   # 或 WebUI 批量注册

# 注册成功后（ENABLE_EXTRACT_AFTER_REGISTER=true 时已自动提链）
# 未开自动提链的话，在 WebUI 账号列表点「提链」，或：
python3 - <<'PY'
from core import db, extract_link_service
acc = db.get_account_by_email("REGISTERED_EMAIL")
print(extract_link_service.enqueue_account_extract(
    account_id=acc["id"], email=acc["email"],
    access_token=acc["access_token"], trigger="manual"))
PY

# 2) 注册 GCash（HeroSMS 接码 + ADB 驱动；需真机/模拟器装 GCash App，
#    定位权限选「模糊定位」，不要开开发者模式/未知应用）
python3 tools/gcash_registrar_cli.py --serial EMULATOR_SERIAL --profile profile.json

# 3) 切菲结算 → 输出二维码/URL → GCash App 扫码支付
python3 tools/zero_plus.py --token CHATGPT_AT --account-id AID --gcash --gcash-wait-paid
```

## 排错对照（教程第 5 条）

- 扫码提示**服务器繁忙**：重新提链；提链可能上次成功这次失败 → 换 IP 再提（WebUI 重跑或换 `PLUS_PROXY`）。
- 扫码要求**验证身份**：点击验证，在加载时直接返回，可继续后续操作。
- 注册 GPT 刷不到试用/GCash 支付：换号（试用资格随号，不随 IP 保留）。

## 注意

- 提链的「入口 US / 出口 JP」是提链服务端策略，本仓库不控制。
- HeroSMS 同一出口 IP 约 10 分钟只能接 2 次码：批量注册 GCash 时
  `GCASH_REGISTER_ROTATE_PROXY=true` 会自动从 PROXY_POOL 轮换出口。
- `REFRESH_DECODE_ENABLED` 保持关闭；只有明确说「解码保号」才开。
