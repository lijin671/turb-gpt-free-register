#!/bin/bash
# token_revival 守护进程看门狗 — 确保进程持久运行
cd /home/zzx/research-repos/turb-gpt-free-register
while true; do
  # 检查进程是否在运行
  if ! pgrep -f "revive_daemon.py" > /dev/null 2>&1; then
    echo "[$(date '+%H:%M:%S')] 守护进程未运行，重新启动..."
    nice -n 19 python3 tools/revive_daemon.py --interval 1200 --limit 3 >> /tmp/revive_daemon.log 2>&1 &
    echo "[$(date '+%H:%M:%S')] 已启动 PID: $!"
  fi
  sleep 60
done
