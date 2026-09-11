# -*- coding: utf-8 -*-
"""Bounded process groups. User-approved code has OS user permissions."""
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time

ALLOWED = {'python3', 'python', 'pytest', 'make', 'cmake', 'node', 'npm'}


def validate(argv):
    if not isinstance(argv, list) or not 1 <= len(argv) <= 80 or any(
            not isinstance(a, str) or not a or '\x00' in a or len(a)>4000 for a in argv):
        raise ValueError('命令必须为有限长度的 argv 字符串数组')
    if argv[0] not in ALLOWED:
        raise ValueError('可申请执行：' + ', '.join(sorted(ALLOWED)))
    return list(argv)


def execute(argv, cwd, cancel=None, timeout=120, internal=False):
    if not internal:
        validate(argv)
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise ValueError('命令时限为 1–300 秒')
    # Do not inherit API credentials, PYTHONPATH, NODE_OPTIONS, or shell startup hooks.
    clean_path = '/usr/local/bin:/usr/bin:/bin'
    binary = shutil.which(argv[0], path=clean_path)
    if not binary:
        raise ValueError('本机未安装命令：' + argv[0])
    env = dict(PATH=clean_path, LANG='C.UTF-8', LC_ALL='C.UTF-8',
               HOME=str(Path.home()), GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL='/dev/null', GIT_PAGER='cat')
    if cancel is not None and cancel.is_set():
        return dict(argv=argv, output='', returncode=None, stopped='cancelled', elapsed_seconds=0)
    process = subprocess.Popen([binary]+argv[1:], cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    reader = selectors.DefaultSelector()
    reader.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    reason = None
    started = time.monotonic()
    try:
        while reader.get_map():
            if cancel is not None and cancel.is_set():
                reason = 'cancelled'
                break
            if time.monotonic()-started > timeout:
                reason = 'timeout'
                break
            for key, _ in reader.select(0.1):
                data = os.read(key.fileobj.fileno(), 8192)
                if not data:
                    reader.unregister(key.fileobj)
                    continue
                output.extend(data[:65536-len(output)])
                if len(output) >= 65536:
                    reason = 'output_limit'
                    break
            if reason:
                break
        if not reason:
            # A process may close stdout but continue running.
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    reason = 'cancelled'
                    break
                if time.monotonic()-started > timeout:
                    reason = 'timeout'
                    break
                time.sleep(0.05)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        reader.close()
        process.stdout.close()
    return dict(argv=argv, output=output.decode('utf-8', errors='replace'),
                returncode=process.returncode, stopped=reason, elapsed_seconds=round(time.monotonic()-started, 2))
