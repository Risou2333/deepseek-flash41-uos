# -*- coding: utf-8 -*-
"""Bounded local text extraction; original documents are never uploaded."""
import io
import posixpath
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_FILE = 5 * 1024 * 1024
MAX_TEXT = 100000

def xml(raw):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('不支持含外部实体或 DTD 的文件')
    return ET.fromstring(raw)

def local(tag):
    return tag.rsplit('}', 1)[-1]

def extract(name, raw):
    if not raw or len(raw) > MAX_FILE:
        raise ValueError('文件为空或超过 5 MB')
    ext = Path(name).suffix.lower()
    if ext in ('.txt', '.md', '.csv', '.tsv', '.json', '.log'):
        text = None
        for encoding in ('utf-8-sig', 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'gb18030'):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeError:
                pass
        if text is None or '\x00' in text:
            raise ValueError('无法识别文本编码，请另存为 UTF-8')
    elif ext in ('.docx', '.xlsx'):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                entries = z.infolist()
                if len(entries) > 2000 or sum(i.file_size for i in entries) > 20 * 1024 * 1024:
                    raise ValueError('解压后文件过大，请拆分')
                if ext == '.docx':
                    root = xml(z.read('word/document.xml'))
                    paragraphs = []
                    for p in root.iter():
                        if local(p.tag) == 'p':
                            paragraphs.append(''.join((n.text or '') if local(n.tag) == 't' else '\t' if local(n.tag) == 'tab' else '\n' if local(n.tag) == 'br' else '' for n in p.iter()))
                    text = '\n'.join(paragraphs)
                else:
                    shared = []
                    if 'xl/sharedStrings.xml' in z.namelist():
                        shared = [''.join(t.text or '' for t in n.iter() if local(t.tag) == 't') for n in xml(z.read('xl/sharedStrings.xml'))]
                    rels = {r.attrib['Id']: r.attrib.get('Target', '') for r in xml(z.read('xl/_rels/workbook.xml.rels')) if r.attrib.get('TargetMode') != 'External'}
                    lines = ['[Excel 文本提取：公式仅读取文件中已有缓存值，不重新计算；日期可能为序列数。]']
                    for sheet in xml(z.read('xl/workbook.xml')).iter():
                        if local(sheet.tag) != 'sheet':
                            continue
                        rid = next((v for k, v in sheet.attrib.items() if local(k) == 'id'), '')
                        target = rels.get(rid, '')
                        path = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
                        if not path.startswith('xl/') or path not in z.namelist():
                            continue
                        lines.append('\n工作表：' + sheet.attrib.get('name', ''))
                        for row in xml(z.read(path)).iter():
                            if local(row.tag) != 'row':
                                continue
                            cells = []
                            for cell in row:
                                value = next((v.text or '' for v in cell if local(v.tag) == 'v'), '')
                                kind = cell.attrib.get('t')
                                if kind == 's':
                                    value = shared[int(value)]
                                elif kind == 'inlineStr':
                                    value = ''.join(t.text or '' for t in cell.iter() if local(t.tag) == 't')
                                if any(local(v.tag) == 'f' for v in cell) and not value:
                                    value = '[公式无缓存值]'
                                cells.append(cell.attrib.get('r', '') + '=' + value)
                            lines.append('\t'.join(cells))
                    text = '\n'.join(lines)
        except (zipfile.BadZipFile, ET.ParseError, KeyError, IndexError) as e:
            raise ValueError('Office 文件损坏、加密或结构不受支持') from e
    elif ext == '.pdf':
        if not shutil.which('pdftotext'):
            raise ValueError('PDF 提取需要系统 poppler-utils：sudo apt install poppler-utils')
        with tempfile.TemporaryDirectory(prefix='deepseek-') as folder:
            source, output = Path(folder) / 'input.pdf', Path(folder) / 'output.txt'
            source.write_bytes(raw)
            try:
                result = subprocess.run(['pdftotext', '-f', '1', '-l', '50', '-layout', '-enc', 'UTF-8', str(source), str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            except subprocess.TimeoutExpired:
                raise ValueError('PDF 解析超时，请拆分文件')
            if result.returncode or not output.exists():
                raise ValueError('PDF 无法解析，可能已加密或损坏')
            if output.stat().st_size > 200000:
                raise ValueError('PDF 文本过长，请拆分文件')
            text = output.read_text(encoding='utf-8')
            if not text.strip():
                raise ValueError('未提取到文字，扫描件须先 OCR，本版不含 OCR')
            text = '[PDF 仅提取前 50 页文字，不含图像；请核对预览完整性。]\n' + text
    else:
        raise ValueError('支持 TXT、MD、CSV、TSV、JSON、LOG、DOCX、XLSX、文字型 PDF；旧版 DOC/XLS 请先另存')
    text = text.strip()
    if not text:
        raise ValueError('未提取到可用文字')
    if len(text) > MAX_TEXT:
        raise ValueError('文本超过 100000 字符，请拆分后导入；未自动截断')
    return text
