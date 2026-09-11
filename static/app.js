'use strict';
(function(){
var $=function(id){return document.getElementById(id);}, current=null, records=[], conversations=[], attachments=[], busy=false, fileBusy=false, needsSetup=false;
var defaults={effort:'high',max_tokens:0,context_chars:96000,system:'',json_output:false,image_detail:'original'};
function el(tag,text,cls){var n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function status(text){$('status').textContent=text;}
function parse(value,fallback){try{return typeof value==='string'?JSON.parse(value):(value||fallback);}catch(e){return fallback;}}
function fail(e){status(e.message||'操作失败');}
function request(path,body){var opt={credentials:'same-origin'};if(body!==undefined){opt.method='POST';opt.headers={'Content-Type':'application/json','X-Requested-With':'DeepSeekClient'};opt.body=JSON.stringify(body);}return fetch(path,opt);}
async function api(path,body){var r=await request(path,body),d=await r.json();if(!r.ok){if(r.status===401){$('workspace').hidden=true;$('login').hidden=false;}throw Error(d.error||'请求失败');}return d;}
function inline(node,text){text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g).forEach(function(s){var link=/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/.exec(s);if(link){var a=el('a',link[1]);a.href=link[2];a.target='_blank';a.rel='noopener noreferrer';node.appendChild(a);}else if(s.slice(0,2)==='**'&&s.slice(-2)==='**')node.appendChild(el('strong',s.slice(2,-2)));else if(s[0]==='`'&&s.slice(-1)==='`')node.appendChild(el('code',s.slice(1,-1)));else node.appendChild(document.createTextNode(s));});}
function markdown(node,text){
 node.textContent='';var lines=text.split('\n'),code=null;
 function cells(line){return line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(function(c){return c.trim();});}
 for(var i=0;i<lines.length;i++){
  var line=lines[i];if(/^\s*```/.test(line)){if(code)code=null;else{code=el('pre','');node.appendChild(code);}continue;}
  if(code){code.textContent+=line+'\n';continue;}
  if(i+1<lines.length&&line.indexOf('|')>=0&&/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i+1])){
   var wrap=el('div',undefined,'table-wrap'),table=el('table'),head=el('tr');cells(line).forEach(function(c){var th=el('th');inline(th,c);head.appendChild(th);});table.appendChild(head);i+=2;
   while(i<lines.length&&lines[i].indexOf('|')>=0&&lines[i].trim()){var tr=el('tr');cells(lines[i]).forEach(function(c){var td=el('td');inline(td,c);tr.appendChild(td);});table.appendChild(tr);i++;}i--;wrap.appendChild(table);node.appendChild(wrap);continue;
  }
  var heading=/^(#{1,4})\s+(.*)$/.exec(line),list=/^\s*(?:[-*]|\d+\.)\s+(.*)$/.exec(line),quote=/^>\s?(.*)$/.exec(line);
  var row=el(heading?(heading[1].length<=2?'h2':'h3'):quote?'blockquote':'p');inline(row,heading?heading[2]:list?'• '+list[1]:quote?quote[1]:line);node.appendChild(row);
 }
}
function download(name,text,type){var url=URL.createObjectURL(new Blob([text],{type:type||'text/plain;charset=utf-8'})),a=el('a');a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(function(){URL.revokeObjectURL(url);},1000);}
async function copy(text){try{await navigator.clipboard.writeText(text);status('已复制');}catch(e){var t=el('textarea',text);document.body.appendChild(t);t.select();document.execCommand('copy');t.remove();status('已复制');}}
function usageText(meta){var u=meta.usage;if(!u)return '用量：API 未返回';var parts=[];[['prompt_tokens','输入'],['completion_tokens','输出'],['total_tokens','总计'],['prompt_cache_hit_tokens','缓存命中']].forEach(function(p){if(u[p[0]]!==undefined)parts.push(p[1]+' '+u[p[0]]);});return parts.length?parts.join(' · ')+' tokens':'用量：API 未返回';}
function details(node,title,text){var d=el('details'),p=el('pre',text);d.appendChild(el('summary',title));d.appendChild(p);node.appendChild(d);return d;}
function renderRecord(r){
 var box=el('article',undefined,'message '+r.role),body=el('div',undefined,'body');box.appendChild(el('div',r.role==='user'?'你':'DeepSeek Flash','role'));box.appendChild(body);
 if(r.role==='user')body.appendChild(el('p',r.content));else markdown(body,r.content);
 parse(r.images,[]).forEach(function(p){var image=el('img');image.src='data:'+p.mime+';base64,'+p.data;image.alt=p.name;image.className='chat-image';box.appendChild(image);});
 if(r.reasoning)details(box,'思考过程（点击展开）',r.reasoning);
 var m=parse(r.metadata,{});
 if(r.role==='assistant'){
  var controls=el('div',undefined,'reply-controls'),b=el('button','复制回答','quiet');b.onclick=function(){copy(r.content);};controls.appendChild(b);
  if(m.finish_reason==='length')controls.appendChild(el('span','达到输出上限，可点击继续。','warning'));
  if(m.json_output&&m.json_valid===false)controls.appendChild(el('span','返回内容未通过 JSON 对象校验。','warning'));
  box.appendChild(controls);
  if(m.requested_model){box.appendChild(el('p',usageText(m),'usage'));details(box,'调用详情 · '+(m.returned_model||'未返回模型标识'),JSON.stringify(m,null,2));}
  else box.appendChild(el('p','旧版记录未保存调用信息，不能追溯验证本条模型。','usage'));
 }
 $('messages').appendChild(box);return {box:box,body:body};
}
function scroll(force){var n=$('messages');if(force||n.scrollHeight-n.scrollTop-n.clientHeight<220)n.scrollTop=n.scrollHeight;}
function drawRecords(){ $('messages').textContent='';records.forEach(renderRecord);if(records.length){var c=el('button','继续上一条回复','quiet');c.disabled=busy;c.onclick=function(){$('input').value='请继续上一条尚未完成的回答，避免重复已有内容。';$('input').focus();};$('messages').appendChild(c);} }
function paintHistory(){ $('history').textContent='';conversations.filter(function(c){return c.title.toLowerCase().indexOf($('search').value.toLowerCase())>=0;}).forEach(function(c){var b=el('button',c.title,c.id===current?'active':'');b.disabled=busy||fileBusy;b.onclick=function(){select(c.id).catch(fail);};$('history').appendChild(b);});}
async function refresh(){conversations=await api('/api/conversations');paintHistory();}
function readOptions(){return {effort:$('effort').value,max_tokens:Number($('maxTokens').value),context_chars:Number($('contextChars').value),system:$('system').value,json_output:$('jsonOutput').checked,image_detail:$('imageDetail').value};}
function applyOptions(s){s=Object.assign({},defaults,s);$('effort').value=s.effort;$('maxTokens').value=s.max_tokens;$('contextChars').value=s.context_chars;$('system').value=s.system;$('jsonOutput').checked=s.json_output;$('imageDetail').value=s.image_detail;}
function clearAttachments(){attachments=[];$('file').value='';paintAttachments();}
function paintAttachments(){ $('attachments').textContent='';attachments.forEach(function(a,i){var box=el('div',undefined,'attachment-item');box.appendChild(el('b',a.name));var remove=el('button','移除','quiet');remove.disabled=busy||fileBusy;remove.onclick=function(){attachments.splice(i,1);paintAttachments();};box.appendChild(remove);if(a.kind==='image'){var img=el('img');img.src='data:'+a.mime+';base64,'+a.data;img.alt=a.name;img.className='chat-image';box.appendChild(img);}else details(box,'预览文字 · '+a.text.length+' 字符',a.text);$('attachments').appendChild(box);}); }
async function select(id){if(busy||fileBusy)return;records=await api('/api/messages/'+id);current=id;var c=conversations.find(function(c){return c.id===id;});$('title').textContent=c?c.title:'新对话';applyOptions(parse(c&&c.settings,{}));drawRecords();clearAttachments();$('input').value='';paintHistory();scroll(true);}
function setBusy(value){busy=value;['new','delete','export','exportJson','attach','rename','account','credentials','effort','logout','saveOptions','maxTokens','contextChars','system','jsonOutput','imageDetail'].forEach(function(id){$(id).disabled=value||fileBusy;});$('input').disabled=value;$('send').disabled=value||fileBusy;$('send').hidden=value;$('stop').hidden=!value;$('stop').disabled=false;paintHistory();paintAttachments();}
async function enter(){var state=await api('/api/status');if(state.version!=='1.1.0')throw Error('当前端口运行的是旧版服务。请先关闭旧服务终端，再启动新版 start.sh。');$('version').textContent=state.version;$('login').hidden=true;$('workspace').hidden=false;defaults=state.defaults;applyOptions(defaults);status(state.configured?'Key 来源：'+state.key_source+'。点击发送才调用模型；可先进行连接与用量诊断。':'尚未配置 API Key，请在侧栏“Key 与密码”中填写。');await refresh();}
async function boot(){if(location.protocol==='file:'){$('loginError').textContent='请先运行 bash start.sh，再访问 http://127.0.0.1:8765；直接双击 index.html 不会调用 API。';return;}
 try{var b=await api('/api/bootstrap');if(b.version!=='1.1.0')throw Error('检测到旧版服务，请关闭旧终端并启动新版。');needsSetup=b.needs_setup;$('firstSetup').hidden=!needsSetup;$('confirmBox').hidden=!needsSetup;$('loginTitle').textContent=needsSetup?'首次配置':'登录工作台';$('loginHint').textContent=needsSetup?'填写官方 API Key，并设置仅用于本机登录的密码。':'输入本地访问密码，已有 Key 和聊天记录会继续使用。';$('loginButton').textContent=needsSetup?'保存并进入工作台 →':'进入工作台 →';if(!needsSetup)await enter();}catch(e){if(e.message!=='请先登录')$('loginError').textContent=e.message;}}
$('loginForm').onsubmit=async function(e){e.preventDefault();$('loginButton').disabled=true;$('loginError').textContent='';try{if(needsSetup){await api('/api/setup',{api_key:$('firstKey').value,password:$('password').value,confirm:$('confirmPassword').value});needsSetup=false;$('firstKey').value='';$('confirmPassword').value='';$('firstSetup').hidden=true;$('confirmBox').hidden=true;}await api('/api/login',{password:$('password').value});$('password').value='';await enter();}catch(err){$('loginError').textContent=err.message;}finally{$('loginButton').disabled=false;}};
$('new').onclick=async function(){try{var c=await api('/api/new',{});await refresh();await select(c.id);$('input').focus();}catch(e){fail(e);}};
$('search').oninput=paintHistory;
$('optionsToggle').onclick=function(){$('options').hidden=!$('options').hidden;};
$('saveOptions').onclick=async function(){try{if(!current){var c=await api('/api/new',{});current=c.id;}await api('/api/settings',{id:current,settings:readOptions()});await refresh();status('本对话设置已保存');}catch(e){fail(e);}};
$('logout').onclick=async function(){try{await api('/api/logout',{});location.reload();}catch(e){fail(e);}};
$('delete').onclick=async function(){if(!current||!confirm('删除当前对话及全部记录？无法撤销。'))return;try{await api('/api/delete',{id:current});current=null;records=[];drawRecords();clearAttachments();$('title').textContent='新对话';await refresh();}catch(e){fail(e);}};
$('rename').onclick=async function(){if(!current)return;var title=prompt('输入新标题',$('title').textContent);if(!title)return;try{await api('/api/rename',{id:current,title:title});$('title').textContent=title;await refresh();}catch(e){fail(e);}};
$('export').onclick=function(){if(!records.length)return status('当前没有已保存内容');var text='# '+$('title').textContent+'\n\n'+records.map(function(r){return '## '+(r.role==='user'?'用户':'DeepSeek Flash')+'\n\n'+r.content+parse(r.images,[]).map(function(i){return '\n\n[图片：'+i.name+'；此 Markdown 不嵌入原图]';}).join('');}).join('\n\n');download('DeepSeek-对话.md',text,'text/markdown;charset=utf-8');};
$('exportJson').onclick=function(){if(!records.length)return status('当前没有已保存内容');download('DeepSeek-对话与调用记录.json',JSON.stringify({client_version:'1.1.0',title:$('title').textContent,settings:readOptions(),messages:records.map(function(r){return Object.assign({},r,{metadata:parse(r.metadata,{}),images:parse(r.images,[])});})},null,2),'application/json');status('JSON 包含消息、原图、思考过程和调用记录，不含 API Key。');};
$('attach').onclick=function(){$('file').click();};
function readFile(file){return new Promise(function(resolve,reject){var r=new FileReader();r.onerror=function(){reject(Error('读取文件失败'));};r.onload=function(){resolve(r.result);};r.readAsDataURL(file);});}
$('file').onchange=async function(){var files=Array.prototype.slice.call($('file').files);if(!files.length)return;fileBusy=true;setBusy(false);status('正在本机处理附件…');try{for(var i=0;i<files.length;i++){var f=files[i];if(f.size>5*1024*1024)throw Error('单文件限 5 MiB');if(attachments.length>=6)throw Error('最多 6 个附件（其中图片最多 3 张）');var data=await readFile(f);if(/\.(png|jpe?g|gif|webp)$/i.test(f.name)){var pics=attachments.filter(function(a){return a.kind==='image';});if(pics.length>=3||pics.reduce(function(n,a){return n+a.bytes;},0)+f.size>5*1024*1024)throw Error('图片最多 3 张，合计限 5 MiB');attachments.push({kind:'image',name:f.name,data:data.split(',')[1],mime:('image/'+(f.name.toLowerCase().match(/\.([^.]+)$/)[1].replace('jpg','jpeg'))),bytes:f.size});}else{var d=await api('/api/extract',{name:f.name,data:data.split(',')[1]});attachments.push({kind:'text',name:d.name,text:d.text});}}status('附件已准备；请核对预览。点击发送时文字和图片才提交至 DeepSeek。');}catch(e){fail(e);}finally{fileBusy=false;$('file').value='';setBusy(false);}};
function showPanel(kind){$('panel').hidden=false;$('diagnostics').hidden=kind!=='diagnostics';$('credentialsForm').hidden=kind!=='credentials';$('panelTitle').textContent=kind==='diagnostics'?'连接与用量诊断':'修改 Key 与密码';$('panelStatus').textContent='';}
$('account').onclick=function(){showPanel('diagnostics');};$('credentials').onclick=function(){showPanel('credentials');};$('closePanel').onclick=function(){$('panel').hidden=true;$('credentialsForm').reset();};
$('refreshAccount').onclick=async function(){this.disabled=true;$('panelStatus').textContent='正在查询官方模型列表和余额…';try{var d=await api('/api/account',{});$('accountResult').textContent=JSON.stringify(d,null,2);$('panelStatus').textContent='查询完成。列表和余额成功不等于已完成模型生成；可点击计费测试。';}catch(e){$('panelStatus').textContent=e.message;}finally{this.disabled=false;}};
$('credentialsForm').onsubmit=async function(e){e.preventDefault();var b=this.querySelector('button');b.disabled=true;try{await api('/api/credentials',{old_password:$('oldPassword').value,api_key:$('newKey').value,password:$('newPassword').value,confirm:$('newConfirm').value});this.reset();location.reload();}catch(err){$('panelStatus').textContent=err.message;}finally{b.disabled=false;}};
$('probe').onclick=async function(){if(busy||fileBusy)return;this.disabled=true;try{var c=await api('/api/new',{});await refresh();await select(c.id);$('effort').value='none';$('maxTokens').value='8192';$('system').value='';$('jsonOutput').checked=false;$('input').value='连接测试：请只回复“API连接成功”。';$('panel').hidden=true;await send();}catch(e){fail(e);}finally{this.disabled=false;}};
$('stop').onclick=async function(){this.disabled=true;try{await api('/api/stop',{id:current});status('停止请求已提交；等待当前网络读取结束。取消不保证撤销已发生的 API 费用。');}catch(e){fail(e);}};
async function send(){
 if(busy||fileBusy)return;var input=$('input').value.trim();if(!input)return status('请先输入任务');
 var text=input+attachments.filter(function(a){return a.kind==='text';}).map(function(a){return '\n\n<文件材料 name='+JSON.stringify(a.name)+'>\n'+a.text+'\n</文件材料>';}).join('');
 if(text.length>120000)return status('提问与文件文字合计超过 120000 字符，请拆分');
 var pictures=attachments.filter(function(a){return a.kind==='image';}).map(function(a){return {name:a.name,data:a.data,mime:a.mime};}),opts=readOptions();
 if(text.length+opts.system.length>opts.context_chars)return status('内容超过所选上下文预算，请在对话设置提高预算或拆分文件');
 setBusy(true);var answer='',reasoning='',meta={},terminal=false,saved=false,result,reader,reasonBox,reasonPre,lastPaint=0;
 try{
  if(!current){var c=await api('/api/new',{});current=c.id;await refresh();}
  drawRecords();renderRecord({role:'user',content:text,images:pictures});result=renderRecord({role:'assistant',content:''});result.box.querySelector('.usage').remove();
  reasonBox=el('details');reasonBox.hidden=true;reasonBox.appendChild(el('summary','正在思考（点击展开）'));reasonPre=el('pre','');reasonBox.appendChild(reasonPre);result.box.insertBefore(reasonBox,result.body);reasonBox.ontoggle=function(){if(reasonBox.open)reasonPre.textContent=reasoning;};
  status('正在连接官方 API…');scroll(true);
  var response=await request('/api/chat',{id:current,message:text,images:pictures,settings:opts});if(!response.ok){var err=await response.json();throw Error(err.error);}
  if(!response.body||!response.body.getReader)throw Error('浏览器不支持流式读取，请使用较新的系统浏览器');reader=response.body.getReader();var decoder=new TextDecoder('utf-8'),buffer='';
  function consume(line){if(!line.trim())return;var e=JSON.parse(line);if(e.type==='error')throw Error(e.error);if(e.type==='meta'){meta=e.metadata;status('请求已发送，等待 API 返回。'+(e.omitted_turns?'省略最早 '+e.omitted_turns+' 轮上下文。':''));}if(e.type==='reasoning'){reasoning+=e.text;reasonBox.hidden=false;}if(e.type==='content')answer+=e.text;
   if(Date.now()-lastPaint>200){result.body.textContent=answer;if(reasonBox.open)reasonPre.textContent=reasoning;scroll();lastPaint=Date.now();}
   if(e.type==='cancelled'){terminal=true;status('已停止。本轮未保存，输入与附件保留。');}
   if(e.type==='done'){terminal=true;saved=true;meta=e.metadata;}
  }
  while(true){var chunk=await reader.read();buffer+=decoder.decode(chunk.value||new Uint8Array(),{stream:!chunk.done});var lines=buffer.split('\n');buffer=lines.pop();lines.forEach(consume);if(chunk.done)break;}if(buffer.trim())consume(buffer);if(!terminal)throw Error('连接提前结束，请打开历史核对是否保存后再重试');
  if(saved){$('input').value='';clearAttachments();records=await api('/api/messages/'+current);await refresh();var found=conversations.find(function(c){return c.id===current;});$('title').textContent=found.title;drawRecords();$('connection').textContent='最近成功返回：'+(meta.returned_model||'模型字段缺失')+' · '+usageText(meta);status('已保存。'+usageText(meta)+(meta.finish_reason==='length'?'；已达输出上限，可继续。':'')+(meta.omitted_turns?'；省略 '+meta.omitted_turns+' 轮。':''));}
  else{markdown(result.body,answer);reasonPre.textContent=reasoning;}
 }catch(e){if(result){markdown(result.body,answer);result.box.appendChild(el('p','本轮未确认成功：'+e.message,'warning'));}fail(e);}
 finally{if(reader)try{await reader.cancel();}catch(ignore){}setBusy(false);scroll();}
}
$('send').onclick=send;$('input').onkeydown=function(e){if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();send();}};
Array.prototype.forEach.call(document.querySelectorAll('[data-prompt]'),function(b){b.onclick=function(){$('input').value=b.getAttribute('data-prompt');$('input').focus();};});
boot();
}());
