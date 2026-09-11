# -*- coding: utf-8 -*-
import time
import uuid
from tools.terminal import execute


def inspect(store, project, action, cancel=None):
    root = store.path(project, '.', directory=True)
    # Disable external diff/textconv/pagers and hooks on read-only operations.
    prefix = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false']
    commands = {'status': ['status', '--short', '--untracked-files=normal', '--', '.'],
                'diff': ['diff', '--no-ext-diff', '--no-textconv', 'HEAD', '--', '.']}
    if action not in commands:
        raise ValueError('Git 仅开放 status、diff；checkpoint 保存 diff，不自动提交用户文件')
    if action == 'diff':
        names = execute(prefix+['diff', '--relative', '--name-only', '-z', 'HEAD', '--', '.'],
                        root, cancel=cancel, timeout=30, internal=True)
        if names['returncode'] != 0 or names['stopped']:
            return names
        paths = []
        for name in names['output'].split('\x00'):
            if not name:
                continue
            try:
                store.path(project, name)
                paths.append(name)
            except (ValueError, OSError):
                pass
        if not paths:
            return dict(output='', returncode=0, stopped=None, excluded_sensitive=True)
        result = execute(prefix+['diff', '--relative', '--no-ext-diff', '--no-textconv', 'HEAD', '--']+paths[:200],
                         root, cancel=cancel, timeout=30, internal=True)
        result['files_truncated'] = len(paths)>200
        result['excluded_sensitive'] = True
        return result
    return execute(prefix+commands[action], root, cancel=cancel, timeout=30, internal=True)


def checkpoint(store, project, cancel=None):
    result = dict(id=uuid.uuid4().hex, project=project, created=time.time(),
                  status=inspect(store, project, 'status', cancel), diff=inspect(store, project, 'diff', cancel))
    store.put('checkpoint', result)
    return result
