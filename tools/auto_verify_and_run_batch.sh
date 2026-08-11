#!/usr/bin/env bash
# 自动流程：等待 mail.lijin.ug.cx 权威 A 稳定为非回环 -> 单号端到端实测 -> 成功则批量 20
# 日志: /tmp/dns_watch_0808.log
set -u
NS1=111.230.44.144
NS2=138.124.102.177
DOMAIN=mail.lijin.ug.cx
LOG=/tmp/dns_watch_0808.log
WORK=/home/zzx/research-repos/turb-gpt-free-register

log() { echo "$(date +%H:%M:%S) [watch] $*" >> "$LOG"; }

log "开始监控 $DOMAIN 权威 A 记录（ns1=$NS1）"

STABLE=""
STABLE_IP=""
A_LIST=""
for i in $(seq 1 45); do
  A1=$(dig +noall +answer A "$DOMAIN" @"$NS1" | awk '{print $NF}' | sort -u | tr '\n' ' '); A1=${A1% }
  A2=$(dig +noall +answer A "$DOMAIN" @"$NS2" | awk '{print $NF}' | sort -u | tr '\n' ' '); A2=${A2% }
  A_CNT1=$(echo "$A1" | wc -w); A_CNT2=$(echo "$A2" | wc -w)
  log "轮询 $i: ns1=[$A1] ns2=[$A2]"
  if [ "$A_CNT1" = "1" ] && [ "$A_CNT2" = "1" ] && [ "$A1" = "$A2" ] && [ "$A1" != "127.0.0.1" ]; then
    A_LIST="$A1"
    if [ "$STABLE" = "1" ] && [ "$A_LIST" = "$STABLE_IP" ]; then
      log "DNS 稳定: 唯一 A=$A_LIST -> 启动单号端到端验证"
      cd "$WORK" || exit 1
      rm -f /tmp/batch_reg_test2.log
      setsid nohup .venv/bin/python main.py -n 1 --continue-on-fail --delay 5 > /tmp/batch_reg_test2.log 2>&1 < /dev/null & disown
      log "单号验证已启动 pid=$!"
      # 等待单号进程结束（最多 9 分钟）
      for j in $(seq 1 54); do
        if ! pgrep -f "main.py -n 1 " > /dev/null; then break; fi
        sleep 10
      done
      if grep -qE "成功 1 / 尝试 1|注册成功" /tmp/batch_reg_test2.log; then
        log "单号验证成功 -> 启动批量 20"
        rm -f /tmp/batch_reg_0808_20.log
        setsid nohup .venv/bin/python main.py -n 20 --continue-on-fail --delay 5 > /tmp/batch_reg_0808_20.log 2>&1 < /dev/null & disown
        log "批量 20 已启动 pid=$!"
      else
        tail -n 5 /tmp/batch_reg_test2.log >> "$LOG"
        log "单号验证失败（OTP 未收到）。若 A=$A_LIST 非 107.174.133.11，需在面板改为 107.174.133.11"
      fi
      exit 0
    else
      STABLE="1"; STABLE_IP="$A_LIST"
    fi
  else
    STABLE=""; STABLE_IP=""
  fi
  sleep 20
done
log "45 次轮询未就绪，最后 A=[$A_LIST]"
