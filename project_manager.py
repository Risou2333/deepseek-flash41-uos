# -*- coding: utf-8 -*-
"""Local projects and durable agent records, Python 3.7 standard library."""
from contextlib import contextmanager
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import uuid

EXCLUDED = {'.git', '.svn', '.hg', 'node_modules', '__pycache__', '.venv', 'venv',
            'dist', 'build', '.ssh', '.gnupg', '.aws', '.config'}
SECRET_PATTERNS = ('.env', '.env.*', '*.pem', '*.key', 'credentials*', 'config.json',
                   'id_rsa*', 'id_ed25519*', '*.p12', '*.pfx', '*.db', '*.sqlite*')
MAX_FILE = 2 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def checked_text(value, name, maximum=120000):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise ValueError(name + ' 格式或长度不合法')
    return value


class Store:
    def __init__(self, data):
        self.data = Path(data).resolve() / 'agent'
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(self.data), 0o700)
        self.lock = threading.RLock()
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT, updated REAL, PRIMARY KEY(kind,id))')
        os.chmod(str(self.data / 'agent.db'), 0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.data / 'agent.db'), timeout=15)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def put(self, kind, value):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)',
                      (kind, value['id'], json.dumps(value, ensure_ascii=False), time.time()))
        return value

    def get(self, kind, ident):
        checked_text(ident, '编号', 64)
        with self.connect() as c:
            row = c.execute('SELECT value FROM records WHERE kind=? AND id=?', (kind, ident)).fetchone()
        if not row:
            raise ValueError('记录不存在')
        return json.loads(row[0])

    def list(self, kind, limit=100):
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute(
                'SELECT value FROM records WHERE kind=? ORDER BY updated DESC LIMIT ?', (kind, limit))]

    def open(self, path):
        checked_text(path, '工程目录', 4096)
        root = Path(path).expanduser().resolve(strict=True)
        if not root.is_dir() or root == Path.home().resolve() or len(root.parts) < 3:
            raise ValueError('请选择具体工程文件夹，不能选择系统根目录或整个用户目录')
        for forbidden in [self.data.parent, Path('/etc'), Path('/proc'), Path('/sys'), Path('/dev')]:
            if root == forbidden or forbidden in root.parents or root in forbidden.parents:
                raise ValueError('该目录包含客户端数据或系统敏感目录，不能作为工程')
        ident = digest(str(root).encode())[:32]
        try:
            project = self.get('project', ident)
        except ValueError:
            project = dict(id=ident, name=root.name, root=str(root), memory='', created=time.time())
        self.put('project', project)
        self.scan(ident)
        return self.get('project', ident)

    def path(self, project_id, relative, directory=False):
        checked_text(relative, '相对路径', 4096)
        part = Path(relative)
        if not relative or part.is_absolute() or '..' in part.parts:
            raise ValueError('只允许工程内相对路径')
        if any(p in EXCLUDED or any(fnmatch.fnmatch(p.lower(), pat) for pat in SECRET_PATTERNS) for p in part.parts):
            raise ValueError('该路径属于默认排除的目录或敏感文件')
        root = Path(self.get('project', project_id)['root'])
        if root.is_symlink() or root.resolve() != root:
            raise ValueError('工程根目录已变化，请重新打开')
        current = root
        for p in part.parts:
            current = current / p
            if current.is_symlink():
                raise ValueError('不允许通过符号链接访问文件')
        resolved = current.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError('路径超出工程目录')
        if current.exists():
            mode = current.stat()
            if directory:
                if not stat.S_ISDIR(mode.st_mode):
                    raise ValueError('路径不是目录')
            elif not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1:
                raise ValueError('只支持普通文件，不支持硬链接')
        return current

    def scan(self, project_id):
        project = self.get('project', project_id)
        root = Path(project['root'])
        self.path(project_id, '.', directory=True)
        result = []
        started = time.monotonic()
        limited = False
        for folder, dirs, names in os.walk(str(root), followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith('.') and not (Path(folder)/d).is_symlink())
            for name in sorted(names):
                relative = (Path(folder) / name).relative_to(root).as_posix()
                try:
                    p = self.path(project_id, relative)
                    st = p.stat()
                    result.append(dict(path=relative, size=st.st_size, modified=st.st_mtime, language=p.suffix))
                except (ValueError, OSError):
                    continue
                if len(result) >= 20000 or time.monotonic()-started > 10:
                    limited = True
                    break
            if limited:
                break
        index = dict(id=project_id, files=result, truncated=limited, updated=time.time())
        self.put('index', index)
        project['file_count'] = len(result)
        self.put('project', project)
        return index

    def read(self, project_id, path, start=1, lines=200):
        if type(start) is not int or start < 1 or type(lines) is not int or not 1 <= lines <= 500:
            raise ValueError('行号或行数不合法（每页 1–500 行）')
        target = self.path(project_id, path)
        with target.open('rb') as f:
            raw = f.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE or b'\x00' in raw:
            raise ValueError('文件超过 2 MiB 或为二进制，请拆分或使用专用工具')
        try:
            content = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('工程代码需为 UTF-8；未自动转码覆盖原文件')
        all_lines = content.splitlines(True)
        selected = ''.join(all_lines[start-1:start-1+lines])
        return dict(path=path, content=selected[:24000], start=start,
                    next_start=start+lines if start-1+lines < len(all_lines) else None,
                    total_lines=len(all_lines), sha256=digest(raw), truncated=len(selected)>24000)

    def search(self, project_id, query, glob='*'):
        checked_text(query, '搜索内容', 500)
        checked_text(glob, '文件匹配', 200)
        if not query:
            raise ValueError('搜索内容不能为空')
        matches = []
        scanned = 0
        started = time.monotonic()
        entries = self.get('index', project_id)['files']
        for entry in entries:
            if not fnmatch.fnmatch(entry['path'], glob) or entry['size'] > MAX_FILE:
                continue
            try:
                target = self.path(project_id, entry['path'])
                with target.open('rb') as f:
                    raw = f.read(MAX_FILE+1)
                if len(raw)>MAX_FILE or b'\x00' in raw:
                    continue
                content = raw.decode('utf-8')
            except (OSError, ValueError):
                continue
            scanned += 1
            for num, line in enumerate(content.splitlines(), 1):
                if query.casefold() in line.casefold():
                    matches.append(dict(path=entry['path'], line=num, text=line[:500]))
                    if len(matches) >= 60:
                        return dict(matches=matches, truncated=True, scanned=scanned)
            if time.monotonic()-started > 5:
                return dict(matches=matches, truncated=True, scanned=scanned)
        return dict(matches=matches, truncated=self.get('index', project_id)['truncated'], scanned=scanned)
