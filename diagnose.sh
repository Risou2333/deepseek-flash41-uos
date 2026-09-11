#!/usr/bin/env bash
set -u
python3 - <<'PY'
import platform, sys, shutil
print('系统：', platform.platform())
print('架构：', platform.machine(), '字节序：', sys.byteorder)
print('Python：', platform.python_version())
for name in ['ssl','sqlite3','urllib.request','xml.etree.ElementTree']:
    try:
        __import__(name)
        print(name + ': OK')
    except ImportError:
        print(name + ': 缺失')
print('PDF 提取：', shutil.which('pdftotext') or '未安装，可选安装 poppler-utils')
print('Git：', shutil.which('git') or '未安装，工程读写仍可使用')
print('本诊断不读取 API Key、不调用付费 API。')
PY
