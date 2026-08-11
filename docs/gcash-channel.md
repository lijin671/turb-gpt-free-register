# GCash 渠道 + HeroSMS 接码（注册流程集成）

> 2026-08-07 社区实测渠道（linux.do《GPT Plus GCash最新渠道焚决》）：
> OpenAI 结算切菲律宾区 → Stripe Checkout 出 QR → GCash App 扫码完成 Plus 支付。
> 全程手机端**不要挂梯子**；菲律宾号接码只有 HeroSMS 稳定，但 **同一出口 IP 约 10 分钟只能接 2 次码**，批量必须轮换出口 IP。

## 一、整条链路

```
注册 ChatGPT free 号（本仓库注册机，已有）
  └─ ENABLE_GCASH_REGISTER=true 时自动追加：
       1. HeroSMS 买菲律宾号（HERO_SMS_SERVICE=gcash, country=6）
       2. HeroSMS 轮询等 GCash 注册验证码（getAllSms 优先）
       3. ADB 驱动 GCash App 填号/信息/验证码（GCASH_ADB_SERIAL 配置真机）
  └─ 产出记录 {chatgpt_email, gcash_phone, gcash_status}
       写入 注册任务.json（WebUI 任务表可见 gcash_phone/gcash_status）
  └─ 用 run_gcash_checkout 把号绑 Plus：
       python tools/zero_plus.py --token <TOKEN> --account-id <AID> --gcash --gcash-wait-paid
```

## 二、HeroSMS 客户端（core/hero_sms.py）

- Base URL：`https://hero-sms.com/stubs/handler_api.php`（sms-activate 兼容协议，全 GET + `api_key`）
- 已实现：getBalance / getCountries / getServicesList / getOperators / getPrices /
  getNumberV2(→getNumber 回退) / getStatus / getStatusV2 / getAllSms /
  setStatus(1 重发 / 3 再取 / 6 完成 / 8 取消) / cancel / complete / request_again /
  reactivationPrice / reactivate / wait_for_code（轮询封装）
- 错误映射：NO_NUMBERS / NO_BALANCE / BAD_SERVICE / BAD_STATUS / BAD_KEY / NO_ACTIVATION → 具体异常类
- 单元测试 15 个全绿：`python -m unittest tests.test_hero_sms`

## 三、注册流程集成（本改动）

新增配置（config/plus.py，.env 可覆盖）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `ENABLE_GCASH_REGISTER` | false | ChatGPT 号注册成功后自动跑 GCash 号注册（HeroSMS 接码 + ADB） |
| `GCASH_REGISTER_ROTATE_PROXY` | true | 批量并发时每个 worker 从 PROXY_POOL 轮换出口代理（绕 10分钟2码/IP）；配了 `HERO_SMS_PROXY` 则优先用它 |
| `HERO_SMS_API_KEY` | 空 | HeroSMS API Key（必填） |
| `HERO_SMS_SERVICE` / `HERO_SMS_COUNTRY` | gcash / 6 | 服务码 / 国家（菲律宾=6；以 getServicesList / getCountries 返回为准） |
| `HERO_SMS_MAX_PRICE` | 0（不限） | 单号最高价（美元） |
| `HERO_SMS_WAIT_TIMEOUT` / `HERO_SMS_POLL_INTERVAL` | 240 / 5 | 等码超时 / 轮询间隔（秒） |
| `GCASH_ADB_SERIAL` | 空 | adb devices 的设备串号；留空则只打印手动步骤，不操作设备 |
| `GCASH_APP_PACKAGE` | com.globe.gcash.android | GCash 包名（华为出境易等渠道可能不同） |
| `GCASH_REGISTER_PROFILE` | 空 | 菲律宾注册信息（姓名/生日/邮箱），JSON 文件路径或内联对象 |

行为约定：
- GCash 注册**不阻塞** ChatGPT 号结果：失败只记 `gcash_status=error: ...`，`success` 不变。
- 无 API Key 时记 `gcash_status=skipped_no_api_key` 并跳过。
- 注册失败自动 `cancel` 释放 HeroSMS 激活号。

### 用法

```bash
# 方式1：CLI 参数（等价 ENABLE_GCASH_REGISTER=true）
python main.py -n 1 --gcash-register

# 方式2：.env 配置后直接批量
#   ENABLE_GCASH_REGISTER=true
#   HERO_SMS_API_KEY=...
#   GCASH_ADB_SERIAL=emulator-5554
python main.py -n 3 --workers 1

# 单独验证接码链路（不操作设备）
python tools/gcash_registrar_cli.py --dry-run

# 单独跑一次 GCash 注册（真机）
python tools/gcash_registrar_cli.py --serial EMULATOR_SERIAL --profile profile.json

# HeroSMS 基础自检
python tools/hero_sms_cli.py balance
python tools/hero_sms_cli.py get-number --service gcash --country 6 --wait --auto-complete --proxy http://...
```

## 四、待你提供 / 校准项

1. **HERO_SMS_API_KEY**：发我后写入 `.env`，我用 `tools/hero_sms_cli.py balance` 实测；
   同时确认真实服务码（`find_service_code("gcash")`，可能不是 `gcash`）。
2. **ADB 真机/模拟器**：`adb devices` 拿到 serial 配置 `GCASH_ADB_SERIAL`；
   `core/gcash_registrar.py` 里的点击坐标是占位符，需 `adb shell uiautomator dump` 校准。
3. **批量 IP 轮换**：若本机出口 IP 接码被限（10分钟2码），把 `HERO_SMS_PROXY` 指向代理池，
   或保持 `GCASH_REGISTER_ROTATE_PROXY=true` 自动从 PROXY_POOL 抽。

## 五、接码平台抽象（SMS_PROVIDER=hero）

`core/sms_provider.py` 已接入 HeroSMS 作为统一接码平台：

- `.env` 设 `SMS_PROVIDER=hero` 后，Codex OAuth 的 /phone-verification 手机号验证
  与 GCash 注册都走 HeroSMS（acquire_number / wait_for_sms_code / complete / cancel 同一接口）。
- HeroSMS 请求出口代理：优先 `HERO_SMS_PROXY`，否则自动从 **PROXY_POOL** 轮换
  （每个 activation_id 全程绑定同一出口 IP，绕"10分钟2码/IP"限制）。
- 错误映射：HeroSMS 超时 → `SmsCodeTimeout`；NO_ACTIVATION/BAD_KEY → `SmsProviderError`；
  预检 `check_sms_availability` 支持 hero 分支（查余额，无 key 跳过）。
- 验证：`SMS_PROVIDER=hero python tools/check_sms_provider.py --json`
- 单测：`python -m unittest tests.test_sms_provider_hero`（11 个用例）

示例 .env：

```ini
SMS_PROVIDER=hero
# HeroSMS key（config/plus.py 读取）
HERO_SMS_API_KEY=...
# 代理：留空自动走 PROXY_POOL 轮换；或显式指定
HERO_SMS_PROXY=
```

## 六、2026-08-07 实测记录（key 已激活）

| 项目 | 实测值 |
|---|---|
| API Key 状态 | ✅ 有效，余额 $1.2477（买号测试后待释放回 $1.3） |
| 菲律宾国家 id | **4**（不是 sms-activate 的 6，已改默认配置） |
| GCash 服务码 | **bc**（`find_service_code("gcash")`，已改默认配置） |
| 单号价格 | **$0.0605**（country=4, service=bc, USD） |
| 库存 | **9678** 个号（physicalCount 6126） |
| 取号 | ✅ 实测成功 `Activation(id=699787976, phone=639756705060, cost=0.0605)` |
| 取消限制 | ⚠️ **120 秒最小激活时长**（`EARLY_CANCEL_DENIED`，minActivationTime=120）；`hero_sms.cancel()` 已内置等待重试，`sms_provider` 的 hero 取消走后台线程不阻塞主流程 |
| 预检 | ✅ `SMS_PROVIDER=hero python tools/check_sms_provider.py --json` → ok=true, balance=1.2477 |

注意：HeroSMS 的 GCash 验证码只在 GCash App 真正发起注册时才会下发；
取号后 120 秒内不能取消，批量时一个号从取号到出码/取消约 2-5 分钟。
