import {pages, fields, choices, reasons, request} from './model.mjs';
const $ = id => document.getElementById(id);
const {appSDK:sdk, dashboardHost:host} = window.$wujie?.props || {};
const content = $('content'), notice = $('notice');
const escape = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const button = (id, text) => '<button data-vf="Button" data-act="'+id+'">'+text+'</button>';
const get = path => request(sdk,'GET',path);
const labels = {pause:'暂停渠道',resume:'恢复渠道','clear-cooldown':'清除冷却','reset-count':'重置今日计数',
  bind:'绑定已有媒体',fail:'确认失败并释放预留',retry:'允许重新提交',defaults:'恢复默认策略',
  open:'打开桌面',close:'关闭桌面',login:'启动登录', 'check-auth':'检查会话'};
let revision = 0, busy = false;
function page() { const slug = location.hash.split('/').pop(); return Object.hasOwn(pages,slug) ? slug : 'overview'; }
function overview(data) {
  const paused = data.status === 'paused';
  const facts = {'会员账号':data.account.email,'剩余积分':data.account.credits,'今日提交':data.today.submits,'今日积分':data.today.credits};
  if (data.account.tier) facts['会员等级'] = data.account.tier;
  return (paused ? '<div class="vf-banner danger" role="alert">'+escape(reasons[data.reason])+' · '+escape(new Date(data.since*1000).toLocaleString())+
    (data.call_id?' · '+escape(data.call_id):'')+(data.until?' · 冷却至 '+escape(new Date(data.until*1000).toLocaleString()):' · 需手动恢复或重新准入')+
    (data.lease_reconcile_timeout?' · 旧 GPU 租约对账超时':'')+' '+button('resume',labels.resume)+button('clear-cooldown',labels['clear-cooldown'])+'</div>' : '')+
    '<section class="card"><div class="section-head"><h2>渠道状态 · '+(paused?'已暂停':'运行中')+'</h2>'+(!paused?button('pause',labels.pause):'')+'</div><p>授权状态 · '+
    (data.account.authorized === null ? '无法观测' : data.account.authorized?'会话有效':'需要登录')+'</p><div class="c-facts">'+
    Object.entries(facts).map(([k,v])=>'<div><small>'+k+'</small><strong>'+escape(v)+'</strong></div>').join('')+'</div><div class="g-account-footer"><small>策略 · 并发 '+escape(data.policy.concurrency)+' · 最小间隔 '+escape(data.policy.min_interval_seconds)+' 秒 · '+(data.no_gpu?'无 GPU 独立模式':data.lease_valid?'已持有 GPU 租约':'GPU 尚未准入')+'</small><div class="actions">'+button('reset-count',labels['reset-count'])+'</div></div></section>';
}
function policy(data) {
  return '<h2>节奏与风控策略</h2><p>四个限额项填 0 表示不限制。冷却填 0 表示只暂停，需手动恢复。</p><form id="policy-form"><section class="card g-form-grid">'+
    Object.entries(fields).map(([key,label])=>'<label>'+label+(choices[key]?'<select name="'+key+'">'+Object.entries(choices[key]).map(([v,l])=>'<option value="'+v+'" '+(data[key]===v?'selected':'')+'>'+l+'</option>').join('')+'</select>':
    '<input required type="number" step="1" min="'+(key==='concurrency'?1:0)+'" name="'+key+'" value="'+escape(data[key])+'">')+'</label>').join('')+
    '</section><button type="submit">保存策略</button></form>'+button('defaults',labels.defaults);
}
function table(head,rows) {
  return rows.length?'<div class="card"><table><thead><tr>'+head.map(h=>'<th>'+h+'</th>').join('')+'</tr></thead><tbody>'+rows.join('')+'</tbody></table></div>':'<div class="empty">暂无记录</div>';
}
function calls(data) {
  return table(['时间／任务','来源','操作／模型','状态','积分','处理'],data.map(r=>'<tr data-id="'+escape(r.call_id)+'" data-rev="'+Number(r.revision)+'"><td>'+escape(r.started?new Date(r.started*1000).toLocaleString():null)+'<small>'+escape(r.request_id)+'</small></td><td>'+escape(r.capability==='video'?'Video MCP':'Image MCP')+'</td><td>'+escape(r.model)+'</td><td>'+
    escape(r.state)+'</td><td>'+escape(r.credits)+'</td><td>'+(r.state==='submission_unknown'?['bind','fail','retry'].map(k=>button(k,labels[k])).join(''):'—')+'</td></tr>'));
}
async function refresh() {
  const selected=page(), current=++revision;
  for (const a of $('tabs').children) a.setAttribute('aria-current',a.dataset.page===selected?'page':'false');
  content.textContent='加载中…';
  try {
    const data=await get(({overview:'/overview',policy:'/policy',log:'/calls',audit:'/audit',desktop:'/desktop'})[selected]);
    if(current!==revision)return;
    if(selected==='overview')content.innerHTML=overview(data);
    if(selected==='policy')content.innerHTML=policy(data);
    if(selected==='log')content.innerHTML=calls(data);
    if(selected==='audit')content.innerHTML=table(['时间','操作者','动作','详情'],data.map(r=>'<tr>'+[new Date(r.time*1000).toLocaleString(),r.actor,r.action,JSON.stringify(r.detail)].map(v=>'<td>'+escape(v)+'</td>').join('')+'</tr>'));
    if(selected==='desktop'){
      content.innerHTML='<section class="card"><h2>登录桌面</h2><p>'+(data.open?'桌面会话已开启':data.available?'桌面未打开':'桌面服务尚未接入')+'</p>'+button(data.open?'close':'open',data.open?labels.close:labels.open)+(data.open?button('login',labels.login):'')+button('check-auth',labels['check-auth'])+'</section>';
      if(data.open && typeof data.viewer_path==='string' && /^\/[a-zA-Z0-9/_-]+$/.test(data.viewer_path) && !data.viewer_path.startsWith('//')){
        const frame=document.createElement('iframe');
        frame.title='KasmVNC 登录桌面'; frame.className='desktop-view'; frame.src=data.viewer_path;
        content.append(frame);
      }
    }
  } catch(e){if(current===revision){notice.textContent=e.message;content.innerHTML=button('refresh','重试');}}
  host?.resize?.(document.documentElement.scrollHeight);
}
async function mutate(method,path,body) {
  if(busy)return;
  busy=true;
  try {const data=await request(sdk,method,path,body); await refresh(); notice.textContent=path.endsWith('check-auth')?(data.authorized?'会话有效':'需要重新授权'):'操作已完成';}
  catch(e){notice.textContent=e.message;}finally{busy=false;}
}
$('tabs').innerHTML=Object.entries(pages).map(([k,v])=>'<a data-page="'+k+'" href="#/apps/google-flow/'+k+'">'+v+'</a>').join('');
content.addEventListener('submit',e=>{
  e.preventDefault();
  const data=Object.fromEntries(new FormData(e.target));
  for(const k of Object.keys(data))if(!choices[k])data[k]=Number(data[k]);
  mutate('PUT','/policy',data);
});
content.addEventListener('click',e=>{
  const act=e.target.closest('[data-act]')?.dataset.act;
  if(!act||busy)return;
  if(act==='refresh')return refresh();
  const row=e.target.closest('tr');
  $('media-label').hidden=act!=='bind'; $('media').value='';
  $('question').textContent=labels[act]+'？';
  const dialog=$('confirm');dialog.returnValue='';
  dialog.addEventListener('close',()=>{
    if(dialog.returnValue!=='yes')return;
    if(row)return mutate('POST','/calls/'+row.dataset.id+'/reconcile',{action:act,revision:Number(row.dataset.rev),media_id:act==='bind'?$('media').value:null});
    const path=act==='defaults'?'/policy/defaults':['open','close','login','check-auth'].includes(act)?'/desktop/'+act:'/channel/'+act;
    mutate('POST',path,{});
  },{once:true}); dialog.showModal();
});
addEventListener('hashchange',refresh);
const subscriptions=[];
if(sdk?.navigation?.subscribe){
  subscriptions.push(sdk.navigation.subscribe(({path})=>{
    const selected=path.replace(/^\//,'') || 'overview';
    const hash='#/apps/google-flow/'+(Object.hasOwn(pages,selected)?selected:'overview');
    if(location.hash!==hash)location.hash=hash;
  }));
  $('tabs').addEventListener('click',async event=>{
    const selected=event.target.closest('[data-page]')?.dataset.page;
    if(!selected)return;
    event.preventDefault();
    try{await sdk.navigation.request({target:'app',path:selected==='overview'?'/':'/'+selected});}
    catch(error){notice.textContent=error.message;}
  });
}
if(sdk?.theme?.subscribe)subscriptions.push(sdk.theme.subscribe(theme=>{
  document.documentElement.dataset.theme=theme;
}));
addEventListener('pagehide',()=>{++revision;for(const unsubscribe of subscriptions)unsubscribe();},{once:true});
host?.ready?.();
host?.resize?.(document.documentElement.scrollHeight);
refresh();
