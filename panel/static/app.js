'use strict';
const $ = id => document.getElementById(id);
let csrf = '', view = 'overview', timer, metricBusy = false;
const titles = {overview:'概览',sites:'网站管理',services:'系统服务',audit:'操作日志',firewall:'防火墙'};
function text(id, value) { $(id).textContent = value; }
function bytes(value) { const units=['B','KB','MB','GB','TB']; let n=Number(value), i=0; while(n>=1024&&i<4){n/=1024;i++;} return n.toFixed(i?1:0)+' '+units[i]; }
function toast(message, error=false) { text('toast',message); $('toast').classList.toggle('error-toast',error); $('toast').hidden=false; clearTimeout(toast.timeout); toast.timeout=setTimeout(()=>$('toast').hidden=true,6000); }
async function api(path, method='GET', data) {
  const response=await fetch('/api/'+path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:data===undefined?undefined:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok){if(response.status===401 && path!=='login')showLogin();throw new Error(result.error||'请求失败');}
  return result;
}
function showLogin(){firewallState=null;firewallDraft=[];firewallDirty=false;clearInterval(timer);$('shell').hidden=true;$('login').hidden=false;csrf='';document.querySelectorAll('dialog[open]').forEach(d=>d.close());}
function loggedIn(data){csrf=data.csrf;text('username',data.user);$('login').hidden=true;$('shell').hidden=false;switchView(view);clearInterval(timer);timer=setInterval(()=>{if(view==='overview'&&!document.hidden)refreshMetrics();},5000);}
async function busy(button, callback){button.disabled=true;try{await callback();}catch(error){toast(error.message,true);}finally{button.disabled=false;}}
async function switchView(next){view=next;document.querySelectorAll('.view').forEach(el=>el.hidden=el.id!=='view-'+next);document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.view===next));text('breadcrumb',titles[next]);$('sidebar').classList.remove('open');$('menu').setAttribute('aria-expanded','false');try{if(next==='overview')await refreshMetrics();if(next==='sites')await loadSites();if(next==='services')await loadServices();if(next==='audit')await loadAudit();if(next==='firewall')await loadFirewall();}catch(error){toast(error.message,true);}}
async function refreshMetrics(){if(metricBusy)return;metricBusy=true;try{const data=await api('metrics');text('host',data.host);text('host-detail','Linux '+data.kernel+' · '+data.cores+' 核心');text('connection','已连接');text('cpu',data.cpu.toFixed(1)+'%');$('cpu-bar').value=data.cpu;text('cores',data.cores+' 个逻辑核心');for(const name of ['memory','disk']){text(name,data[name].percent.toFixed(1)+'%');$(name+'-bar').value=data[name].percent;text(name+'-size',bytes(data[name].used)+' / '+bytes(data[name].total));}const days=Math.floor(data.uptime/86400),hours=Math.floor(data.uptime%86400/3600);text('uptime',days+'天 '+hours+'时');text('load','平均负载 '+data.load.map(x=>x.toFixed(2)).join(' / '));text('net-in',bytes(data.network.received));text('net-out',bytes(data.network.sent));text('updated','更新于 '+new Date().toLocaleTimeString()+' · 每 5 秒更新');}catch(error){text('connection','连接中断');text('updated','数据更新失败：'+error.message+' · 页面可能显示上次数据');}finally{metricBusy=false;}}
function node(tag,value,className){const el=document.createElement(tag);if(value!==undefined)el.textContent=value;if(className)el.className=className;return el;}
function table(target,headers){const el=node('table'),head=node('thead'),row=node('tr'),body=node('tbody');headers.forEach(h=>row.append(node('th',h)));head.append(row);el.append(head,body);$(target).replaceChildren(el);return body;}
function cell(row,value){const td=node('td',value);row.append(td);return td;}
function empty(target,title,message){const el=node('div',undefined,'empty');el.append(node('strong',title),node('p',message));$(target).replaceChildren(el);}
function button(label,handler,danger=false){const el=node('button',label,danger?'danger':'');el.type='button';el.addEventListener('click',()=>busy(el,handler));return el;}
async function loadSites(){const data=await api('sites');if(!data.sites.length){empty('sites-list','还没有网站','点击“新建网站”，部署你的第一个站点。');return;}const body=table('sites-list',['域名 / 目录','类型','监听','操作']);for(const site of data.sites){const row=node('tr'),domain=cell(row,site.domain);domain.append(node('small',site.kind==='static'?site.root:'127.0.0.1:'+site.port));cell(row,site.kind==='static'?'静态网站':'反向代理');cell(row,'HTTP · 80');const actions=cell(row);actions.className='actions';actions.append(button('移除',async()=>{if(!confirm('移除 '+site.domain+' 的 Nginx 配置？网站文件会保留。'))return;const result=await api('sites/'+encodeURIComponent(site.domain)+'/delete','POST',{});toast(result.message);await loadSites();},true));body.append(row);}}
async function loadServices(){const data=await api('services'),body=table('services-list',['服务','状态','启动配置','操作']);for(const svc of data.services){const row=node('tr');cell(row,svc.name);const status=cell(row);const available=svc.load==='loaded';status.append(node('span',!available?(svc.load==='not-found'?'未安装':svc.load==='unsupported'?'容器不支持':'不可用'):svc.active==='active'?'运行中':svc.active==='failed'?'失败':'已停止','status'+(svc.active==='active'?'':' off')));cell(row,svc.enabled||'—');const actions=cell(row);actions.className='actions';if(available){for(const [op,label] of [['start','启动'],['restart','重启'],['stop','停止']]){actions.append(button(label,async()=>{if(!confirm('确认'+label+' '+svc.name+'？这可能中断正在使用该服务的连接。'))return;await api('services/'+svc.name+'/'+op,'POST',{});toast('服务操作已完成');await loadServices();},op==='stop'));}actions.append(button('日志',async()=>{text('logs-title',svc.name+' · 最近 100 条日志');text('log-output','正在读取…');$('logs-dialog').showModal();try{const result=await api('logs/'+svc.name);text('log-output',result.text||'暂无日志');}catch(error){text('log-output',error.message);}}));}body.append(row);}}
async function loadAudit(){const data=await api('audit');if(!data.entries.length){empty('audit-list','暂无记录','管理操作会记录在这里。');return;}const body=table('audit-list',['时间','用户 / 来源','操作','结果','详情']);for(const entry of data.entries){const row=node('tr');cell(row,new Date(entry.ts*1000).toLocaleString());cell(row,entry.username).append(node('small',entry.ip));cell(row,entry.action);cell(row,entry.ok?'成功':'失败');cell(row,entry.details);body.append(row);}}
$('login-form').addEventListener('submit',async event=>{event.preventDefault();text('login-error','');const form=event.target,submit=form.querySelector('button[type=submit]');submit.disabled=true;try{const data=await api('login','POST',{username:form.elements.username.value,password:form.elements.password.value});form.elements.password.value='';loggedIn(data);}catch(error){text('login-error',error.message);}finally{submit.disabled=false;}});
$('logout').addEventListener('click',()=>busy($('logout'),async()=>{await api('logout','POST',{});showLogin();}));
document.querySelectorAll('[data-view]').forEach(el=>el.addEventListener('click',()=>switchView(el.dataset.view)));
$('menu').addEventListener('click',()=>{$('sidebar').classList.toggle('open');$('menu').setAttribute('aria-expanded',String($('sidebar').classList.contains('open')));});
$('refresh').addEventListener('click',refreshMetrics);
$('refresh-services').addEventListener('click',()=>busy($('refresh-services'),loadServices));
$('refresh-audit').addEventListener('click',()=>busy($('refresh-audit'),loadAudit));
$('new-site').addEventListener('click',()=>{text('site-error','');$('site-dialog').showModal();});
$('site-kind').addEventListener('change',()=>{const proxy=$('site-kind').value==='proxy';$('port-label').hidden=!proxy;$('site-form').elements.port.required=proxy;});
$('site-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.target,submit=form.querySelector('[type=submit]');submit.disabled=true;text('site-error','');try{await api('sites','POST',{domain:form.elements.domain.value.trim().toLowerCase(),kind:form.elements.kind.value,port:Number(form.elements.port.value)});$('site-dialog').close();form.reset();$('port-label').hidden=true;form.elements.port.required=false;toast('网站配置已保存，Nginx 已收到重载请求');await loadSites();}catch(error){text('site-error',error.message);}finally{submit.disabled=false;}});
document.querySelectorAll('.close-dialog').forEach(el=>el.addEventListener('click',()=>el.closest('dialog').close()));
api('session').then(loggedIn).catch(showLogin);

let firewallState=null, firewallDraft=[], firewallEdit=-1, firewallDirty=false;
function markFirewallDirty(){firewallDirty=true;text('firewall-dirty','有未保存修改 · 点击保存后生效');renderFirewall();}
async function loadFirewall(){
  if(firewallDirty&&!confirm('刷新将放弃未保存的防火墙修改，继续？'))return;
  const data=await api('firewall');firewallState=data;firewallDraft=data.rules.map(r=>({...r}));firewallDirty=false;
  text('firewall-status',data.available?'iptables 可用 · IPv4 入站规则':data.reason);
  text('firewall-protected','保护 TCP 端口：'+data.protected_ports.join('、')+'；本机回环与已建立连接不受本面板阻止规则影响。');
  text('firewall-system',data.system_rules||'无法读取');text('firewall-active',data.active_rules||'尚未创建管理链');text('firewall-dirty','暂无未保存修改');
  $('new-firewall-rule').disabled=!data.available;$('save-firewall').disabled=!data.available;renderFirewall();
}
function editFirewall(index=-1){firewallEdit=index;const form=$('firewall-form');form.reset();if(index>=0){for(const [key,value] of Object.entries(firewallDraft[index])){if(form.elements[key])form.elements[key].value=value;}}text('firewall-dialog-title',index<0?'添加规则':'编辑规则');$('firewall-dialog').showModal();}
function renderFirewall(){
  if(!firewallDraft.length){empty('firewall-list','暂无自定义规则','系统已有防火墙仍然有效。');return;}
  const body=table('firewall-list',['顺序','协议 / 端口','来源','动作','备注','操作']);
  firewallDraft.forEach((rule,index)=>{const row=node('tr');cell(row,String(index+1));cell(row,rule.protocol.toUpperCase()+' / '+rule.port);cell(row,rule.source);cell(row,rule.verdict==='ACCEPT'?'允许':'阻止');cell(row,rule.note);const actions=cell(row);actions.className='actions';
    if(firewallState.available){actions.append(button('编辑',async()=>editFirewall(index)),button('删除',async()=>{firewallDraft.splice(index,1);markFirewallDirty();},true));
      if(index>0)actions.append(button('↑',async()=>{[firewallDraft[index-1],firewallDraft[index]]=[firewallDraft[index],firewallDraft[index-1]];markFirewallDirty();}));
      if(index<firewallDraft.length-1)actions.append(button('↓',async()=>{[firewallDraft[index+1],firewallDraft[index]]=[firewallDraft[index],firewallDraft[index+1]];markFirewallDirty();}));}
    body.append(row);});
}
$('new-firewall-rule').addEventListener('click',()=>{if(firewallDraft.length>=32){toast('最多 32 条规则',true);return;}editFirewall();});
$('refresh-firewall').addEventListener('click',()=>busy($('refresh-firewall'),loadFirewall));
$('firewall-form').addEventListener('submit',event=>{event.preventDefault();const f=event.target.elements;const rule={protocol:f.protocol.value,port:Number(f.port.value),source:f.source.value.trim(),verdict:f.verdict.value,note:f.note.value.trim()};if(rule.protocol==='tcp'&&rule.verdict==='DROP'&&firewallState.protected_ports.includes(rule.port)){toast('这是 SSH 或面板保护端口，不能阻止',true);return;}if(firewallEdit<0)firewallDraft.push(rule);else firewallDraft[firewallEdit]=rule;$('firewall-dialog').close();markFirewallDirty();});
$('save-firewall').addEventListener('click',()=>busy($('save-firewall'),async()=>{if(!firewallState?.available)return;if(!confirm('立即应用 '+firewallDraft.length+' 条 IPv4 入站规则？阻止规则会影响新的访问连接。'))return;await api('firewall','POST',{version:firewallState.version,rules:firewallDraft});firewallDirty=false;toast('防火墙规则已应用并保存');await loadFirewall();}));
window.addEventListener('beforeunload',event=>{if(firewallDirty){event.preventDefault();event.returnValue='';}});
