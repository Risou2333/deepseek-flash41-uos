#!/usr/bin/env python3
"""Register this folder as a per-user application; no root or file copying."""
import os
from pathlib import Path
root = Path(__file__).resolve().parent
script = str(root / 'start.sh')
# Desktop Entry quoted argument escaping, including literal field code percent.
quoted = '"' + script.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
folder = Path.home() / '.local/share/applications'
folder.mkdir(parents=True, exist_ok=True)
path = folder / 'deepseek-client.desktop'
path.write_text('[Desktop Entry]\nType=Application\nName=DeepSeek 工作助手\nComment=本地 DeepSeek API 客户端\nExec=/bin/bash ' + quoted + '\nIcon=applications-office\nTerminal=true\nCategories=Office;\n', encoding='utf-8')
os.chmod(str(path), 0o755)
print('已添加到应用菜单。请勿移动当前程序目录。关闭服务终端会停止服务。')
