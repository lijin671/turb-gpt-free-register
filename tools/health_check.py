#!/usr/bin/env python3
"""GPT 闭环健康检查脚本。

检查关键指标，输出报告。
可加入 cron 定时执行。

用法: python3 tools/health_check.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 配置
CHATGPT2API_BASE = "http://127.0.0.1:3001"
CHATGPT2API_KEY = "Iq43lk6czc464qlAaV3N4QswsbkLaAdZ4pZopwGDI3o"
CPA_BASE = "http://127.0.0.1:8317"
CPA_KEY = "sk-cpa-32dee3e75379628dfc66e48e75290c8f86c2ed9f0ccadad1"
TURB_DIR = Path(__file__).parent.parent

def check_docker_containers():
    """检查 Docker 容器状态。"""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
        capture_output=True, text=True, timeout=10
    )
    containers = {}
    for line in result.stdout.strip().split("\n"):
        if "\t" in line:
            name, status = line.split("\t", 1)
            containers[name] = status
    return containers

def check_chatgpt2api():
    """检查 chatgpt2api 账号数。"""
    try:
        import requests
        resp = requests.get(
            f"{CHATGPT2API_BASE}/api/accounts",
            headers={"Authorization": f"Bearer {CHATGPT2API_KEY}"},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            return {"ok": True, "count": data.get("total", 0)}
    except:
        pass
    return {"ok": False, "count": 0}

def check_cpa():
    """检查 CPA 模型数。"""
    try:
        import requests
        resp = requests.get(
            f"{CPA_BASE}/v1/models",
            headers={"Authorization": f"Bearer {CPA_KEY}"},
            timeout=10
        )
        if resp.status_code == 200:
            models = resp.json().get("data", [])
            gpt = [m for m in models if "gpt" in m.get("id", "").lower()]
            return {"ok": True, "models": len(models), "gpt": len(gpt)}
    except:
        pass
    return {"ok": False, "models": 0, "gpt": 0}

def check_cpa_consume():
    """检查 CPA 消费。"""
    try:
        import requests
        resp = requests.post(
            f"{CPA_BASE}/v1/chat/completions",
            headers={"Authorization": f"Bearer {CPA_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
            timeout=30
        )
        if resp.status_code == 200 and "choices" in resp.json():
            return {"ok": True}
    except:
        pass
    return {"ok": False}

def check_turb_accounts():
    """检查 turb 账号库存。"""
    token_file = TURB_DIR / "注册成功的token.txt"
    if token_file.exists():
        with open(token_file) as f:
            return {"count": sum(1 for _ in f)}
    return {"count": 0}

def check_revive_daemon():
    """检查 revive_daemon 是否运行。"""
    result = subprocess.run(["pgrep", "-f", "revive_daemon.py"], capture_output=True, text=True)
    return {"running": bool(result.stdout.strip())}

def check_cron():
    """检查 cron 定时任务。"""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    has_register = "daily_register" in result.stdout
    return {"daily_register": has_register}

def main():
    print("=" * 60)
    print(f"GPT 闭环健康检查 — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # Docker 容器
    containers = check_docker_containers()
    print("\n📦 Docker 容器:")
    required = ["resin", "cli-proxy-api", "cpa-manager-plus", "chatgpt2api", "grok-clearance-flaresolverr"]
    for name in required:
        status = containers.get(name, "未运行")
        ok = "✅" if "Up" in status else "❌"
        print(f"  {ok} {name}: {status}")
    
    # chatgpt2api
    c2a = check_chatgpt2api()
    print(f"\n📊 chatgpt2api: {'✅' if c2a['ok'] else '❌'} {c2a['count']} 账号")
    if c2a["count"] < 5:
        print(f"  ⚠️ 账号数低于 5！需要注册新号")
    
    # CPA
    cpa = check_cpa()
    print(f"\n📊 CPA: {'✅' if cpa['ok'] else '❌'} {cpa['models']} 模型 ({cpa['gpt']} GPT)")
    
    # CPA 消费
    consume = check_cpa_consume()
    print(f"\n🔄 CPA 消费: {'✅' if consume['ok'] else '❌'}")
    
    # turb 账号
    turb = check_turb_accounts()
    print(f"\n📦 turb 账号: {turb['count']} 个")
    
    # revive_daemon
    revive = check_revive_daemon()
    print(f"\n🔄 revive_daemon: {'✅ 运行中' if revive['running'] else '❌ 未运行'}")
    
    # cron
    cron = check_cron()
    print(f"\n⏰ cron daily_register: {'✅' if cron['daily_register'] else '❌'}")
    
    # 总结
    all_ok = (
        c2a["ok"] and cpa["ok"] and consume["ok"] and revive["running"] and cron["daily_register"]
    )
    print(f"\n{'='*60}")
    print(f"总结: {'✅ 全部正常' if all_ok else '⚠️ 存在异常'}")
    print(f"{'='*60}")
    
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
