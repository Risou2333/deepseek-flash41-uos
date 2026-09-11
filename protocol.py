# -*- coding: utf-8 -*-
"""DeepSeek Flash request building, bounded image input and SSE decoding."""
import base64
import json

MODEL = 'deepseek-flash'
DEFAULTS = dict(effort='high', max_tokens=0, context_chars=96000, system='', json_output=False, image_detail='original')

def settings(value):
    if not isinstance(value, dict):
        raise ValueError('设置必须为对象')
    out = dict(DEFAULTS)
    out.update({k: v for k, v in value.items() if k in out})
    if out['effort'] not in ('none', 'low', 'high', 'max'):
        raise ValueError('思考强度不合法')
    if type(out['max_tokens']) is not int or out['max_tokens'] not in (0,8192,16384,32768,65536,131072):
        raise ValueError('输出上限不合法')
    if type(out['context_chars']) is not int or out['context_chars'] not in (48000,96000,192000):
        raise ValueError('上下文预算不合法')
    if not isinstance(out['system'], str) or len(out['system']) > 6000:
        raise ValueError('自定义指令最多 6000 字符')
    if type(out['json_output']) is not bool or out['image_detail'] not in ('low','original'):
        raise ValueError('输出格式或图片精度不合法')
    return out

def images(value):
    if not isinstance(value, list) or len(value)>3:
        raise ValueError('每轮最多 3 张图片')
    result=[]
    total=0
    for item in value:
        if not isinstance(item,dict) or not isinstance(item.get('name'),str) or len(item['name'])>255:
            raise ValueError('图片信息不合法')
        data=item.get('data','')
        if not isinstance(data,str) or len(data)>7000000:
            raise ValueError('图片过大')
        raw=base64.b64decode(data,validate=True)
        total+=len(raw)
        if not raw or total>5*1024*1024:
            raise ValueError('本轮图片合计限 5 MiB')
        mime=None
        if raw.startswith(b'\x89PNG\r\n\x1a\n'): mime='image/png'
        elif raw.startswith(b'\xff\xd8\xff'): mime='image/jpeg'
        elif raw[:6] in (b'GIF87a',b'GIF89a'): mime='image/gif'
        elif raw[:4]==b'RIFF' and raw[8:12]==b'WEBP': mime='image/webp'
        if mime is None:
            raise ValueError('仅支持真实 JPEG、PNG、GIF、WebP 文件')
        result.append(dict(name=item['name'],data=data,mime=mime))
    return result

def message(role, text, pictures, detail):
    if role!='user' or not pictures:
        return dict(role=role,content=text)
    return dict(role=role,content=[dict(type='text',text=text)]+[
        dict(type='image_url',image_url=dict(url='data:'+p['mime']+';base64,'+p['data'],detail=detail)) for p in pictures])

def build(rows, text, pictures, options):
    system=options['system'].strip()
    if options['json_output']:
        system+='\nReturn a valid JSON object only. 使用 JSON 格式输出，不使用 Markdown 代码围栏。'
    prefix=[dict(role='system',content=system)] if system else []
    selected=[]
    used=len(text)+len(system)
    if used>options['context_chars']:
        raise ValueError('本轮文字超过当前上下文预算，请提高预算或拆分文件')
    image_bytes=sum(len(p['data']) for p in pictures)
    image_count=len(pictures)
    for i in range(len(rows)-2,-1,-2):
        pair=rows[i:i+2]
        n=sum(len(r['content']) for r in pair)
        imgs=[json.loads(r.get('images','[]')) for r in pair]
        b=sum(len(p['data']) for group in imgs for p in group)
        count=sum(len(group) for group in imgs)
        if used+n>options['context_chars'] or image_bytes+b>8*1024*1024 or image_count+count>12:
            break
        selected=[message(r['role'],r['content'],group,options['image_detail']) for r,group in zip(pair,imgs)]+selected
        used+=n
        image_bytes+=b
        image_count+=count
    payload=dict(model=MODEL,messages=prefix+selected+[message('user',text,pictures,options['image_detail'])],stream=True,stream_options={'include_usage':True},thinking={'type':'disabled' if options['effort']=='none' else 'enabled'})
    if options['effort']!='none':payload['reasoning_effort']=options['effort']
    if options['max_tokens']:payload['max_tokens']=options['max_tokens']
    if options['json_output']:payload['response_format']={'type':'json_object'}
    return payload, (len(rows)-len(selected))//2

def sse(lines):
    """Support comments, CRLF, empty keepalives and multiline SSE data."""
    parts=[]
    size=0
    for raw in lines:
        line=raw.decode('utf-8').rstrip('\r\n')
        if not line:
            if parts:
                yield '\n'.join(parts)
                parts=[]
                size=0
        elif line.startswith('data:'):
            part=line[5:]
            if part.startswith(' '):part=part[1:]
            parts.append(part)
            size+=len(part)
            if size>2000000:raise ValueError('单个流事件超过本地保护上限')
    if parts:yield '\n'.join(parts)
