# 零元 Plus 开通流程

> 基于帖子《【篡改猴脚本】零元Plus直卡绑定 懒人专享》和油猴脚本 1.5.0 的实际 API 调用提取为 Python 模块。

## 流程概览

```
┌─────────────────────────────────────────────────────┐
│  前置准备                                           │
│  ├─ 指纹浏览器 (AdsPower / 干净 Chrome Profile)      │
│  ├─ JP 节点 + US 节点                               │
│  ├─ 卡段 BIN 523686 (Mastercard) / 4513 (Visa)      │
│  └─ 可用邮箱                                        │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│ 阶段1: 日区注册账号                                  │
│ 1. 指纹浏览器开 JP 节点                              │
│ 2. 访问 chatgpt.com → 注册                          │
│ 3. 收验证码 → 设置密码                               │
│ 4. ✅ 日区免费账号                                   │
├─────────────────────────────────────────────────────┤
│ 阶段2: 美区提取 Token                               │
│ 1. 切 US 节点                                       │
│ 2. 登录 chatgpt.com                                 │
│ 3. F12 → Application → Local Storage → token        │
│ 4. ✅ 拿到 accessToken + accountId                   │
├─────────────────────────────────────────────────────┤
│ 阶段3: 切菲律宾结算 (API)                            │
│ POST /backend-api/payments/checkout                 │
│ 设置 country=PH, currency=PHP                       │
│ ✅ 得到 PHP 计价结算短链                              │
├─────────────────────────────────────────────────────┤
│ 阶段4: 创建 SetupIntent (API)                        │
│ POST /backend-api/payments/payment_method            │
│ ✅ 获取 Stripe client_secret                         │
├─────────────────────────────────────────────────────┤
│ 阶段5: Stripe 绑卡 (API)                             │
│ 1. 创建 Stripe PaymentMethod                        │
│ 2. 确认 SetupIntent                                  │
│ ✅ 卡绑定成功                                        │
├─────────────────────────────────────────────────────┤
│ 阶段6: 验证绑定 (API)                                │
│ GET /backend-api/payments/payment_methods             │
│ ✅ 显示已绑卡列表                                     │
├─────────────────────────────────────────────────────┤
│ 阶段7: 验证 Plus 订阅                                │
│ GET /api/auth/session → plan=plus ✅                 │
│ 🎉 ChatGPT Plus 零元开通成功！                       │
└─────────────────────────────────────────────────────┘
```

## 文件结构

```
core/
├── plus_zero.py          # 核心模块：流程的 Python 实现
├── plus_integration.py   # 集成模块：注册后自动触发
└── plus_subscription.py  # 原有的 Plus 订阅模块（保留不动）

config/
└── plus.py               # 零元 Plus 配置（追加在末尾）

tools/
└── zero_plus.py          # CLI 入口

docs/
└── zero-plus-workflow.md # 本文档
```

## 使用方法

### 1. CLI 模式

```bash
# 仅切菲（获取结算链接，不绑卡）
python tools/zero_plus.py \
  --token YOUR_ACCESS_TOKEN \
  --account-id YOUR_ACCOUNT_ID \
  --switch-only

# 完整流程（切菲 + 绑卡 + 验证）
python tools/zero_plus.py \
  --token YOUR_ACCESS_TOKEN \
  --account-id YOUR_ACCOUNT_ID \
  --card 5236861234567890 \
  --exp-month 12 \
  --exp-year 29 \
  --cvc 123

# 带详细日志
python tools/zero_plus.py \
  --token YOUR_TOKEN \
  --account-id YOUR_AID \
  --card 5236861234567890 \
  --exp-month 12 \
  --exp-year 29 \
  --cvc 123 \
  --verbose
```

### 2. Python 代码调用

```python
from core.plus_zero import run_zero_plus

result = run_zero_plus(
    access_token="YOUR_ACCESS_TOKEN",
    account_id="YOUR_ACCOUNT_ID",
    email="user@example.com",
    proxy="socks5://127.0.0.1:1080",
    card_number="5236861234567890",
    exp_month="12",
    exp_year="2029",
    cvc="123",
    card_name="CHATGPT USER",
)

print(result)
```

### 3. 注册后自动触发

```python
# 在 registration_service.py 中调用
from core.plus_integration import try_zero_plus_after_registration

result = try_zero_plus_after_registration(
    email="user@example.com",
    access_token="xxx",
    account_id="xxx",
    card_number="5236861234567890",
    exp_month="12",
    exp_year="29",
    cvc="123",
)
```

### 4. 批量处理

```bash
# 创建账号文件 accounts.jsonl
# 每行一个 JSON:
# {"email": "a@x.com", "access_token": "xxx", "account_id": "yyy"}

python -c "
from core.plus_integration import try_zero_plus_with_account_file
results = try_zero_plus_with_account_file(
    'accounts.jsonl',
    card_number='5236861234567890',
    exp_month='12',
    exp_year='29',
    cvc='123',
)
for r in results:
    print(f\"{r['email']}: {'✅' if r['ok'] else '❌'} {r['message']}\")
"
```

## 配置

在 `config/plus.py` 中设置：

```python
# 启用零元 Plus 开通
ENABLE_ZERO_PLUS = True

# 绑卡方式: "api" (推荐) 或 "browser"
ZERO_PLUS_BIND_MODE = "api"

# 促销活动 ID（留空则不用促销）
ZERO_PLUS_PROMO_CAMPAIGN_ID = "plus-1-month-free"
```

## 卡段说明

| BIN | 类型 | 说明 |
|-----|------|------|
| 523686 | Mastercard | 帖子确认可用 |
| 4513 | Visa | 存活率下降，很多段已死 |
| 451311 | Visa | 原有配置中的卡头，成本约 ¥3.90 |

## 常见问题

### Q: 绑卡成功后未激活 Plus
A: 绑卡成功后需在 ChatGPT 页面填写 US 地址并确认订阅。API 模式绑卡后，脚本会验证订阅状态，如果未激活会自动提示。

### Q: 切菲 API 返回 400
A: 检查 accessToken 是否过期，或确认账号是美区登录状态。需要在 US 节点下执行切菲。

### Q: SetupIntent 创建失败
A: 确认 account_id 正确。可能需要先完成一些账号前置条件（如设置密码、验证邮箱）。

### Q: 卡被拒
A: BIN 523686 存活率最高；4513 段已死很多。尝试更换卡段或虚拟卡平台。
