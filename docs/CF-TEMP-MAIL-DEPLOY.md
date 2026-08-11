# Cloudflare Temp Email 部署 + turb 集成指南

## 一、为什么用 cloudflare_temp_email

| 对比项 | DuckMail (mail.lijin671.com) | cloudflare_temp_email |
|--------|-------------------------------|----------------------|
| 部署 | VPS Docker SMTP | Cloudflare Worker (免费) |
| 成本 | VPS 月费 | 0 成本（CF 免费额度） |
| 解析 | 本地 Python email | Rust WASM mail-parser (服务端) |
| 取码 | SMTP 读信 API | REST API + JWT |
| 并发 | 单 VPS 性能上限 | CF 全球边缘节点无限并发 |
| 稳定性 | VPS 可用性 | CF SLA 99.99% |

## 二、部署步骤

### 2.1 准备域名
1. 在 Cloudflare 注册/转入一个域名（例 `mail-temp.example.com`）
2. 确认域名已添加到 Cloudflare（DNS 由 CF 管理）

### 2.2 部署 Backend (Worker)
```bash
cd /home/zzx/research-repos/cloudflare_temp_email/worker

# 复制配置模板
cp wrangler.toml.template wrangler.toml

# 编辑 wrangler.toml
# - 修改 name = "your-mail-worker"
# - 修改 domain = ["mail-temp.example.com"]
# - 确认 D1 database 配置

# 安装依赖
pnpm install

# 创建 D1 数据库
npx wrangler d1 create mail-db
# 将返回的 database_id 填入 wrangler.toml

# 初始化数据库
npx wrangler d1 execute mail-db --file=../db/schema.sql

# 部署
npx wrangler deploy

# 记录 Worker URL: https://your-mail-worker.your-account.workers.dev
```

### 2.3 部署 Email Routing（收信）
1. CF Dashboard → 域名 → Email → Email Routing
2. 启用 Email Routing
3. Catch-all → 转发到 Worker

### 2.4 部署 Frontend（可选）
```bash
cd /home/zzx/research-repos/cloudflare_temp_email/frontend
pnpm install
# 编辑 .env: VITE_API_BASE=https://your-mail-worker.your-account.workers.dev
pnpm build
npx wrangler pages deploy dist
```

### 2.5 设置管理密码（关键）
在 CF Dashboard → Worker → Settings → Variables:
```
ADMIN_PASSWORDS = ["your-strong-admin-password"]
SITE_PASSWORDS = []  # 可选，前端访问密码
```

## 三、turb 集成配置

### 3.1 .env 配置
```bash
# 邮箱来源设为 cloudflare
EMAIL_SOURCE=cloudflare

# Cloudflare Worker API 地址
CLOUDFLARE_API_BASE=https://your-mail-worker.your-account.workers.dev

# 管理密码 = 上面设置的 ADMIN_PASSWORD
CLOUDFLARE_API_KEY=your-strong-admin-password

# 认证模式：用 admin 路径创建地址（绕过 Turnstile）
CLOUDFLARE_AUTH_MODE=x-admin-auth
CLOUDFLARE_PATH_ACCOUNTS=/admin/new_address

# 可用域名列表（逗号或换行分隔）
CLOUDFLARE_DEFAULT_DOMAINS=mail-temp.example.com

# 请求超时
CLOUDFLARE_REQUEST_TIMEOUT=20

# 邮箱名长度
CLOUDFLARE_NAME_LENGTH=10
```

### 3.2 工作流
1. turb 调用 `POST /admin/new_address { name, domain }` 创建邮箱 → 拿到 JWT
2. turb 用 OpenAI 注册流程发送验证码到该邮箱
3. turb 轮询 `GET /api/parsed_mails` (Bearer JWT) → 服务端已解析好的 text/html
4. 从 text/html 提取 6 位 OTP
5. settle 机制防止读到旧码

### 3.3 优化点（本次新增）
1. **parsed_mails API 支持**：`cf_temp_mail_client.py` 新增 `_list_parsed_mails()` 和 `_get_parsed_mail()`
2. **增强 OTP 提取**：`fetch_latest_otp_enhanced()` 优先使用 parsed_mails，回退 raw mails
3. **Rust WASM 解析**：cloudflare_temp_email 服务端用 Rust mail-parser 解析 MIME，比本地 Python 更可靠

## 四、优势
- **0 成本**：CF Worker 免费额度 100K 请求/天
- **全球分发**：CF 边缘节点，延迟低
- **高并发**：可批量创建数百个邮箱并行取码
- **无需 SMTP**：纯 HTTP API
- **绕过 Turnstile**：用 admin 路径创建邮箱，无需人机验证
