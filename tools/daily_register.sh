#!/bin/bash
# 每日自动注册 GPT 账号（低 CPU，每天 5 个）
# 用法: crontab -e → 0 */6 * * * /home/zzx/research-repos/turb-gpt-free-register/tools/daily_register.sh
# 或: setsid bash tools/daily_register.sh >> /tmp/daily_register.log 2>&1 &

cd /home/zzx/research-repos/turb-gpt-free-register

# 加载环境变量
set -a
source .env 2>/dev/null
set +a

# 确保容器运行
docker start resin cpa-manager-plus cli-proxy-api grok-clearance-flaresolverr chatgpt2api 2>/dev/null

# 等待容器就绪
sleep 10

# 低 CPU 注册 5 个号
COUNT=${REGISTER_COUNT:-5}
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始注册 $COUNT 个账号..."

nice -n 19 python3 main.py -n "$COUNT" --workers 1 --delay 60 --verbose --continue-on-fail 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 注册完成"
echo ""

# 水位报告
python3 tools/check_account_pool.py 2>&1

echo ""
# CPA 消费验证
curl -s -X POST http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer sk-cpa-32dee3e75379628dfc66e48e75290c8f86c2ed9f0ccadad1" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' 2>/dev/null | python3 -c "
import sys,json
try:
    d = json.load(sys.stdin)
    if 'choices' in d:
        print(f'CPA 消费: ✅ {d[\"choices\"][0][\"message\"][\"content\"]} ({d.get(\"model\",\"?\")})')
    else:
        print(f'CPA 消费: ❌ {d}')
except:
    print('CPA 消费: ❌ 解析失败')
" 2>/dev/null
