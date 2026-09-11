#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
python3 - <<'PY'
import sys, ssl, sqlite3, socket
if sys.version_info < (3,7):
    raise SystemExit('需要 Python 3.7 或更高版本')
s=socket.socket()
s.settimeout(1)
in_use=s.connect_ex(('127.0.0.1',8765))==0
s.close()
if in_use:
    raise SystemExit('8765 端口已有服务。请先在旧客户端终端按 Ctrl+C 停止，再运行新版。没有自动打开旧页面。')
PY
( python3 - <<'PY'
import json,time,webbrowser,urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8765/api/bootstrap',timeout=1) as r:
            data=json.load(r)
        if data.get('version')=='2.0.0':
            webbrowser.open('http://127.0.0.1:8765')
            break
    except Exception:
        time.sleep(0.3)
PY
) &
exec python3 app.py
