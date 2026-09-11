# -*- coding: utf-8 -*-
import difflib
import os
import tempfile
import time
import uuid
from project_manager import checked_text, digest, MAX_FILE


def full_read(store, project, path):
    target = store.path(project, path)
    if not target.exists():
        return None
    with target.open('rb') as f:
        raw = f.read(MAX_FILE+1)
    if len(raw) > MAX_FILE or b'\x00' in raw:
        raise ValueError('仅支持不超过 2 MiB 的文本文件')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('仅支持 UTF-8 工程代码')


def propose(store, project, path, content=None, old=None, new=None, expected_sha=None):
    before = full_read(store, project, path)
    actual = digest(before.encode()) if before is not None else None
    if expected_sha != actual:
        raise ValueError('文件已变化或未读取，请重新读取后使用最新 sha256；新文件使用 null')
    if old is not None:
        checked_text(old, '匹配文字')
        checked_text(new, '替换文字')
        if before is None or not old or before.count(old) != 1:
            raise ValueError('匹配文字必须在原文件中恰好出现一次')
        content = before.replace(old, new, 1)
    checked_text(content, '新文件内容', MAX_FILE)
    if len(content.encode()) > MAX_FILE:
        raise ValueError('新文件超过 2 MiB')
    if before == content:
        raise ValueError('没有文件变化')
    diff = ''.join(difflib.unified_diff((before or '').splitlines(True), content.splitlines(True),
                                     fromfile=path if before is not None else '/dev/null', tofile=path))
    value = dict(id=uuid.uuid4().hex, project=project, kind='file', path=path,
                 before=before, after=content, before_sha=actual, after_sha=digest(content.encode()),
                 diff=diff, state='pending', created=time.time())
    return store.put('change', value)


def atomic_write(target, content):
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = (target.stat().st_mode & 0o777) if target.exists() else 0o644
    fd, name = tempfile.mkstemp(prefix='.uso-', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, mode)
        os.replace(name, str(target))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def decide(store, change_id, accept):
    with store.lock:
        change = store.get('change', change_id)
        if change['state'] != 'pending':
            raise ValueError('该修改已处理，不能重复应用')
        if accept:
            current = full_read(store, change['project'], change['path'])
            if current != change['before']:
                raise ValueError('文件在预览后发生变化；未覆盖，请拒绝后重新生成')
            # Write-ahead state permits recovery across a crash after os.replace.
            change['state'] = 'applying'
            store.put('change', change)
            atomic_write(store.path(change['project'], change['path']), change['after'])
            change['state'] = 'accepted'
        else:
            change['state'] = 'rejected'
        store.put('change', change)
        return dict(change_id=change_id, path=change['path'], state=change['state'])


def rollback(store, change_id):
    with store.lock:
        change = store.get('change', change_id)
        if change['state'] != 'accepted':
            raise ValueError('只能撤销已接受的修改')
        current = full_read(store, change['project'], change['path'])
        if current != change['after']:
            raise ValueError('文件已被后续修改，撤销会覆盖新内容，已停止')
        target = store.path(change['project'], change['path'])
        change['state'] = 'reverting'
        store.put('change', change)
        if change['before'] is None:
            target.unlink()
        else:
            atomic_write(target, change['before'])
        change['state'] = 'rolled_back'
        store.put('change', change)
        return dict(change_id=change_id, state='rolled_back', path=change['path'])


def recover(store):
    """Resolve write-ahead states without replaying a write or overwriting new edits."""
    for change in store.list('change', 100000):
        if change['state'] not in ('applying', 'reverting', 'pending'):
            continue
        if change['state'] == 'pending':
            change['state'] = 'rejected'
            change['recovery'] = '服务重启，尚未应用的提案已失效'
        else:
            try:
                current = full_read(store, change['project'], change['path'])
                if current == change['after']:
                    change['state'] = 'accepted'
                elif current == change['before']:
                    change['state'] = 'rolled_back' if change['state']=='reverting' else 'rejected'
                else:
                    change['state'] = 'conflict'
                change['recovery'] = '按重启后的文件内容核对；未重放写入'
            except (ValueError, OSError):
                change['state'] = 'conflict'
                change['recovery'] = '文件无法核对；原文备份仍保存在记录中'
        store.put('change', change)
