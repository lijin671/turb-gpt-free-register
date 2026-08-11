#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注册即用：监视本机账号库，新账号落库后立即导入 chatgpt2api 并做模型冒烟。

用途：free 号池变现。注册机完成注册后 OpenAI 常在 ~10-30 分钟内吊销 AT，
因此必须在账号落库的第一时间导入上游（chatgpt2api）并开始出图/对话。

用法:
  python3 tools/auto_import_chatgpt2api.py                # 监视并自动导入+冒烟
  IMPORT_PROMPT="..." python3 tools/auto_import_chatgpt2api.py
  CHAT_PROBE_MODELS="gpt-5.6-sol,gpt-5.6-luna,gpt-5.5,gpt-4o" python3 ...
  CHATGPT2API_BASE=http://127.0.0.1:3001 \
  CHATGPT2API_AUTH_KEY=xxx \
  PROXY_HOST_REWRITE=127.0.0.1:2260=100.108.233.62:2260 \
  python3 tools/auto_import_chatgpt2api.py

行为:
  - 已导入的账号 id 记录在 CHATGPT2API_IMPORTED_STATE（默认 /tmp/chatgpt2api_imported.txt）
  - 导入成功（added/synced>0）后默认【只导入、不冒烟】（AUTO_IMPORT_IMAGE_PROBE /
    AUTO_IMPORT_CHAT_PROBE / AUTO_IMPORT_UPSTREAM_PROBE 任一设为 1 才开启对应冒烟）：
      1) 生图冒烟 1 张 → /tmp/verify_id<id>.png
      2) 对话冒烟：逐个尝试 CHAT_PROBE_MODELS（默认含 gpt-5.6-sol / gpt-5.6-luna / gpt-5.5 / gpt-4o）
      3) 尽力拉取上游 /backend-api/models，记录含 5.6/luna/sol 的 slug
  - 注：2026-08-08 实测多号同窗口冒烟会触发整批 AT 吊销，故默认全关
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

from core import db

BASE = os.environ.get('CHATGPT2API_BASE', 'http://127.0.0.1:3001').rstrip('/')
AUTH_KEY = os.environ.get('CHATGPT2API_AUTH_KEY', '')
if not AUTH_KEY:
    env_path = os.environ.get('CHATGPT2API_ENV_FILE', '/tmp/chatgpt2api/.env')
    if os.path.exists(env_path):
        for line in open(env_path, encoding='utf-8'):
            if line.startswith('CHATGPT2API_AUTH_KEY='):
                AUTH_KEY = line.split('=', 1)[1].strip().strip('"').strip("'")
                break
STATE_FILE = os.environ.get('CHATGPT2API_IMPORTED_STATE', '/tmp/chatgpt2api_imported.txt')
PROMPT = os.environ.get('IMPORT_PROMPT', 'a cat floating in space, digital art')
PROXY_REWRITE = os.environ.get('PROXY_HOST_REWRITE', '127.0.0.1:2260=100.108.233.62:2260')
CHAT_PROBE_MODELS = [m.strip() for m in os.environ.get(
    'CHAT_PROBE_MODELS',
    'gpt-5.6-sol,gpt-5.6-luna,gpt-5.5,gpt-4o,gpt-5-mini,gpt-4.1',
).split(',') if m.strip()]

# 冒烟开关：默认全部关闭。多号同窗口探测（生图+6 模型对话）是整批 AT 被
# OpenAI 服务端吊销的直接诱因（2026-08-08 两轮实测）；按用户要求只保留导入，
# 需要验证真实模型时用 1 个号手动打 1-2 发（钩子会记录 resolved_model_slug）。
ENABLE_IMAGE_PROBE = os.environ.get('AUTO_IMPORT_IMAGE_PROBE', '0') == '1'
ENABLE_CHAT_PROBE = os.environ.get('AUTO_IMPORT_CHAT_PROBE', '0') == '1'
ENABLE_UPSTREAM_PROBE = os.environ.get('AUTO_IMPORT_UPSTREAM_PROBE', '0') == '1'


def _rewrite_proxy(proxy: str) -> str:
    old, _, new = PROXY_REWRITE.partition('=')
    if old and new:
        return proxy.replace(old, new)
    return proxy


def seen_ids() -> set[int]:
    try:
        return {int(x) for x in open(STATE_FILE, encoding='utf-8').read().split() if x.strip()}
    except FileNotFoundError:
        return set()


def mark_seen(ids: list[int]) -> None:
    with open(STATE_FILE, 'a', encoding='utf-8') as f:
        f.write(''.join(f'{i}\n' for i in ids))


def post(path: str, payload: dict, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path, data=data, method='POST',
        headers={'Authorization': 'Bearer ' + AUTH_KEY, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def probe_chat_models(aid: int, email: str) -> None:
    for model in CHAT_PROBE_MODELS:
        body = {
            'model': model,
            'messages': [{'role': 'user', 'content': '只回复两个字：正常'}],
            'max_tokens': 32,
            'stream': False,
        }
        try:
            out = post('/v1/chat/completions', body, timeout=120)
            content = ''
            try:
                content = out['choices'][0]['message']['content']
            except Exception:
                pass
            print(f'[probe] id={aid} {email} model={model} OK content={content[:40]!r}', flush=True)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            try:
                j = json.loads(detail)
                detail = json.dumps(j.get('error') or j, ensure_ascii=False)[:300]
            except Exception:
                pass
            print(f'[probe] id={aid} {email} model={model} HTTP{exc.code} {detail}', flush=True)
        except Exception as exc:
            print(f'[probe] id={aid} {email} model={model} ERR {type(exc).__name__}: {exc}', flush=True)


def probe_upstream_models(aid: int, email: str, at: str, proxy: str) -> None:
    try:
        from core.session import BrowserSession
        session = BrowserSession(proxy=proxy or None, detect_exit_geo=False)
        resp = session.get('https://chatgpt.com/backend-api/models',
                           headers={'Authorization': f'Bearer {at}'}, timeout=30)
        if resp.status_code != 200:
            print(f'[upstream-models] id={aid} HTTP{resp.status_code}', flush=True)
            return
        slugs = sorted({(m or {}).get('slug') for m in (resp.json().get('models') or []) if m})
        interesting = [s for s in slugs if any(k in s for k in ('5.6', 'luna', 'sol', '4o', '5.5'))]
        print(f'[upstream-models] id={aid} total={len(slugs)} interesting={interesting}', flush=True)
    except Exception as exc:
        print(f'[upstream-models] id={aid} ERR {type(exc).__name__}: {exc}', flush=True)


def main() -> None:
    if not AUTH_KEY:
        print('[auto-import] CHATGPT2API_AUTH_KEY 未配置，退出', flush=True)
        raise SystemExit(2)
    print('[auto-import] watching for new accounts ...', flush=True)
    while True:
        try:
            rows = db.list_accounts(limit=100000)
            seen = seen_ids()
            new = [r for r in rows if int(r.get('id') or 0) not in seen]
            new.sort(key=lambda r: int(r.get('id') or 0))
            for r in new:
                aid = int(r['id'])
                email = r.get('email') or ''
                at = r.get('access_token') or ''
                proxy = r.get('proxy_used') or ''
                print(f'[auto-import] new account id={aid} email={email} at_len={len(at)}', flush=True)
                mark_seen([aid])
                if not at or not proxy:
                    print(f'[auto-import] skip id={aid}: missing at/proxy', flush=True)
                    continue
                imported = False
                try:
                    imp = post('/api/accounts', {'accounts': [{'access_token': at, 'proxy': _rewrite_proxy(proxy)}]})
                    print('[auto-import] import:', json.dumps(
                        {k: imp.get(k) for k in ('added', 'skipped', 'synced', 'removed_ids')},
                        ensure_ascii=False), flush=True)
                    errs = imp.get('errors') or []
                    if errs:
                        print('[auto-import] import errors:', json.dumps(errs, ensure_ascii=False)[:500], flush=True)
                    if (imp.get('added') or 0) or (imp.get('synced') or 0):
                        imported = True
                except Exception as exc:
                    print('[auto-import] import exception:', type(exc).__name__, exc, flush=True)
                if imported:
                    if ENABLE_IMAGE_PROBE:
                        try:
                            out = post('/v1/images/generations',
                                       {'model': 'gpt-image-2', 'prompt': PROMPT, 'n': 1, 'response_format': 'b64_json'},
                                       timeout=300)
                            data = out.get('data') or []
                            print(f'[auto-import] image gen ok: n={len(data)}', flush=True)
                            if data and data[0].get('b64_json'):
                                img = base64.b64decode(data[0]['b64_json'])
                                path = f'/tmp/verify_id{aid}.png'
                                with open(path, 'wb') as f:
                                    f.write(img)
                                print(f'[auto-import] saved {path} bytes={len(img)}', flush=True)
                        except urllib.error.HTTPError as exc:
                            print('[auto-import] image HTTPError', exc.code, exc.read().decode()[:400], flush=True)
                        except Exception as exc:
                            print('[auto-import] image exception:', type(exc).__name__, exc, flush=True)
                    if ENABLE_CHAT_PROBE:
                        probe_chat_models(aid, email)
                if ENABLE_UPSTREAM_PROBE:
                    probe_upstream_models(aid, email, at, proxy)
        except Exception as exc:
            print('[auto-import] loop error:', type(exc).__name__, exc, flush=True)
        time.sleep(2)


if __name__ == '__main__':
    main()
