import {pages, fields, choices, reasons, request} from './model.mjs';
const $ = id => document.getElementById(id);
const {appSDK:sdk, dashboardHost:host} = window.$wujie?.props || {};
const content = $('content'), notice = $('notice');
const escape = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const button = (id, text, cls='') => '<button type="button" data-vf="Button" data-act="'+id+'"'+(cls?' class="'+cls+'"':'')+'>'+text+'</button>';
const get = path => request(sdk,'GET',path);
const labels = {
  pause:'暂停', resume:'恢复', 'clear-cooldown':'清除冷却',
  bind:'绑定已有媒体', fail:'确认失败并释放预留', retry:'允许重新提交',
  defaults:'恢复默认策略', refresh_balance:'刷新', open_policy:'策略',
  auth:'授权', close_desktop:'直接关闭桌面', finish_desktop:'完成登录并关闭桌面',
  reopen_desktop:'重新打开窗口', add_account:'+ 新增账号'
};
let revision = 0, busy = false, lastViewerPath = '/apps/google-flow/desktop/', lastEmail = 'Google Flow 账号';

function routeSlug() {
  const slug = location.hash.split('/').pop();
  if (slug === 'policy') return 'policy';
  return Object.hasOwn(pages, slug) ? slug : 'accounts';
}

function fmtUpdated(ts) {
  if (!ts) return '—';
  const diff = Math.max(0, Math.floor(Date.now()/1000 - ts));
  const clock = new Date(ts * 1000).toTimeString().slice(0, 8);
  if (diff < 60) return '刚刚（' + clock + '）';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前（' + clock + '）';
  return new Date(ts * 1000).toLocaleString();
}

function accountsView(data) {
  const paused = data.status === 'paused';
  const email = data.account?.email || '未登录账号';
  lastEmail = email;
  if (data.desktop?.viewer_path) lastViewerPath = data.desktop.viewer_path;
  const desktopOpen = Boolean(data.desktop?.open);
  const pauseBanner = paused ? (
    '<div class="vf-banner danger" role="alert">' + escape(reasons[data.reason] || '渠道已暂停') +
    (data.since ? ' · ' + escape(new Date(data.since * 1000).toLocaleString()) : '') +
    (data.call_id ? ' · ' + escape(data.call_id) : '') +
    (data.until ? ' · 冷却至 ' + escape(new Date(data.until * 1000).toLocaleString()) : ' · 需手动恢复') +
    ' <span class="actions">' + button('resume', '恢复') + button('clear-cooldown', '清除冷却') + '</span></div>'
  ) : '';
  const desktopBanner = desktopOpen ? (
    '<div class="gf-banner" role="status"><div><strong>登录桌面运行中</strong> · 请在新窗口中完成 Google Flow 登录。</div>' +
    '<div class="actions">' + button('reopen_desktop', labels.reopen_desktop) +
    button('finish_desktop', labels.finish_desktop, 'gf-primary') +
    button('close_desktop', labels.close_desktop) + '</div></div>'
  ) : '';
  const authText = data.account?.authorized === null ? '无法观测' : data.account?.authorized ? '会话有效' : '需要登录';
  const badgeCls = paused ? 'paused' : data.account?.authorized ? 'ok' : 'warn';
  const badgeText = paused ? '已暂停' : authText;
  return pauseBanner + desktopBanner +
    '<section class="card"><div class="section-head"><div class="gf-acct-title"><h2>' + escape(email) + '</h2>' +
    '<span class="gf-badge ' + badgeCls + '">' + escape(badgeText) + '</span></div>' +
    '<div class="actions">' + button('refresh_balance', labels.refresh_balance) +
    button('open_policy', labels.open_policy) + button('auth', labels.auth) +
    button(paused ? 'resume' : 'pause', paused ? labels.resume : labels.pause) +
    '</div></div><div class="c-facts">' +
    '<div><small>剩余积分</small><strong>' + escape(data.account?.credits) + '</strong></div>' +
    '<div><small>预留积分</small><strong>' + escape(data.today?.reserved ?? 0) + '</strong></div>' +
    '<div><small>余额更新时间</small><strong class="gf-fact-sm">' + escape(fmtUpdated(data.account?.updated_at)) + '</strong></div>' +
    '<div><small>并发与间隔</small><strong class="gf-fact-sm">并发 ' + escape(data.policy?.concurrency) + ' · 间隔 ' + escape(data.policy?.min_interval_seconds) + 's</strong></div>' +
    '</div></section>';
}

function policyView(data) {
  const num = (key, label, unit, help, min) =>
    '<label class="gf-field"><span class="gf-field-label">' + label + '<span class="gf-field-unit">' + unit + '</span></span>' +
    '<input required type="number" step="1" min="' + min + '" name="' + key + '" value="' + escape(data[key]) + '">' +
    '<small>' + help + '</small></label>';
  const sel = (key, label, help) =>
    '<label class="gf-field"><span class="gf-field-label">' + label + '</span><select name="' + key + '">' +
    Object.entries(choices[key]).map(([v, l]) => '<option value="' + v + '" ' + (data[key] === v ? 'selected' : '') + '>' + l + '</option>').join('') +
    '</select><small>' + help + '</small></label>';
  return '<form id="policy-form"><section class="card gf-policy-card">' +
    '<div class="gf-form-section"><div class="gf-form-section-head"><h3>并发与提交节奏</h3><p>控制单个 Google Flow 账号的并发任务数与请求频率（本渠道不设今日提交或今日积分限额）。</p></div>' +
    '<div class="gf-form-grid">' +
    num('concurrency', '同时执行的生成数', '个任务', '建议单账号保持 1，避免同一浏览器会话互相抢锁', 1) +
    num('min_interval_seconds', '两次提交最小间隔', '秒', '相邻两次提交之间的最短等待时间，0 表示不额外限速', 0) +
    '</div></div>' +
    '<div class="gf-form-section"><div class="gf-form-section-head"><h3>风控与异常处理</h3><p>触发上游异常或网络中断时的自动保护动作，防止账号被持续风控或重复扣分。</p></div>' +
    '<div class="gf-form-grid">' +
    sel('on_unusual_activity', '遇到 403 UNUSUAL_ACTIVITY', 'Google Flow 返回异常活动拦截时的响应方式') +
    num('cooldown_minutes', '自动冷却时长', '分钟', '填 0 表示暂停后需手动恢复；大于 0 时到期自动恢复', 0) +
    sel('on_submission_unknown', '提交结果不明时', '网络超时且无法确认上游是否已扣分时的处理策略') +
    '</div></div>' +
    '<div class="gf-policy-foot"><small>修改仅对当前账号生效，点击顶部面包屑「Google Flow」返回账号列表。</small><div class="actions">' +
    button('defaults', '恢复默认') + '<button type="submit" data-vf="Button" class="gf-primary">保存策略</button></div></div></section></form>';
}

function table(head, rows) {
  return rows.length
    ? '<div class="card"><table><thead><tr>' + head.map(h => '<th>' + h + '</th>').join('') + '</tr></thead><tbody>' + rows.join('') + '</tbody></table></div>'
    : '<div class="empty">暂无记录</div>';
}

function callsView(data) {
  return table(['时间／任务', '来源', '操作／模型', '状态', '积分', '处理'], data.map(r =>
    '<tr data-id="' + escape(r.call_id) + '" data-rev="' + Number(r.revision) + '"><td>' +
    escape(r.started ? new Date(r.started * 1000).toLocaleString() : null) + '<small>' + escape(r.request_id) + '</small></td><td>' +
    escape(r.capability === 'video' ? 'Video MCP' : 'Image MCP') + '</td><td>' + escape(r.model) + '</td><td>' +
    escape(r.state) + '</td><td>' + escape(r.credits) + '</td><td>' +
    (r.state === 'submission_unknown' ? ['bind', 'fail', 'retry'].map(k => button(k, labels[k])).join('') : '—') + '</td></tr>'
  ));
}

function modelsView() {
  const row = (name, id, specs, credits) =>
    '<tr><td><strong>' + name + '</strong><small class="gf-mono">' + id + '</small></td><td><div class="gf-specs">' +
    specs.map(([k, v]) => '<span class="gf-spec"><b>' + k + '：</b>' + v + '</span>').join('') +
    '</div></td><td><strong>' + credits + '</strong></td></tr>';
  return '<section class="card"><h2>视频模型</h2><p class="gf-note">预留积分为网关保守上限，Google AI Ultra 实际扣分通常低于或等于预留值；未列出的参考类型表示不支持。</p><table><thead><tr><th>模型</th><th>支持规格</th><th>预留积分</th></tr></thead><tbody>' +
    row('Omni 1.2 Flash', 'omni-1.2-flash', [['时长','3–15 秒'],['比例','16:9 / 9:16'],['分辨率','720p（可选 360p）'],['超分导出','1080p'],['帧率','24 fps'],['参考图片','最多 7 张'],['参考视频','1 段']], '10') +
    row('Veo 3.1 Lite', 'veo-lite', [['时长','4 / 6 / 8 秒'],['比例','16:9 / 9:16'],['分辨率','720p'],['帧率','24 fps'],['参考图片','最多 3 张']], '10') +
    row('Veo 3.1 Fast', 'veo-fast', [['时长','4 / 6 / 8 秒'],['比例','16:9 / 9:16'],['分辨率','720p'],['超分导出','1080p'],['帧率','24 fps'],['参考图片','最多 3 张']], '20') +
    row('Veo 3.1 Quality', 'veo-quality', [['时长','4 / 6 / 8 秒'],['比例','16:9 / 9:16'],['分辨率','720p'],['超分导出','1080p'],['帧率','24 fps']], '100') +
    '</tbody></table></section>' +
    '<section class="card"><h2>图像模型</h2><p class="gf-note">Google Flow 图像生成不消耗视频积分（计入账号每日图像配额）。</p><table><thead><tr><th>模型</th><th>支持规格</th><th>预留积分</th></tr></thead><tbody>' +
    row('Nano Banana 2', 'nano2', [['比例','16:9 / 9:16 / 1:1'],['分辨率','1K'],['超分导出','2K / 4K'],['参考图片','最多 3 张']], '0') +
    row('Nano Banana 2.1', 'nano21', [['比例','16:9 / 9:16 / 1:1'],['分辨率','1K'],['超分导出','2K / 4K'],['参考图片','最多 3 张']], '0') +
    row('Nano Banana Pro', 'nano-pro', [['比例','16:9 / 9:16 / 1:1'],['分辨率','1K'],['超分导出','2K / 4K'],['参考图片','最多 3 张']], '0') +
    '</tbody></table></section>';
}

function go(slug) {
  const path = slug === 'accounts' ? '/' : '/' + slug;
  if (sdk?.navigation?.request) {
    sdk.navigation.request({target:'app', path}).catch(() => { location.hash = '#/apps/google-flow/' + slug; });
  } else {
    location.hash = '#/apps/google-flow/' + slug;
  }
}

async function refresh() {
  const selected = routeSlug(), current = ++revision;
  const isPolicy = selected === 'policy';
  $('tabs').hidden = isPolicy;
  if (isPolicy) {
    $('breadcrumb').innerHTML = '应用中心 / 渠道管理 / <button type="button" class="gf-bc-link" data-act="bc_root">Google Flow</button> / ' + escape(lastEmail) + ' / 策略配置';
    $('page-title').textContent = '策略配置 · ' + lastEmail;
    $('head-actions').innerHTML = '';
  } else {
    $('breadcrumb').textContent = '应用中心 / 渠道管理 / Google Flow';
    $('page-title').textContent = 'Google Flow';
    $('head-actions').innerHTML = selected === 'accounts' ? button('add_account', labels.add_account, 'gf-primary') : '';
    for (const a of $('tabs').children) a.setAttribute('aria-current', a.dataset.page === selected ? 'page' : 'false');
  }
  if (selected === 'models') {
    content.innerHTML = modelsView();
    host?.resize?.(document.documentElement.scrollHeight);
    return;
  }
  content.textContent = '加载中…';
  try {
    const endpoint = {accounts:'/overview', policy:'/policy', log:'/calls', audit:'/audit'}[selected];
    const data = await get(endpoint);
    if (current !== revision) return;
    if (selected === 'accounts') content.innerHTML = accountsView(data);
    if (selected === 'policy') content.innerHTML = policyView(data);
    if (selected === 'log') content.innerHTML = callsView(data);
    if (selected === 'audit') content.innerHTML = table(['时间', '操作者', '动作', '详情'], data.map(r =>
      '<tr>' + [new Date(r.time * 1000).toLocaleString(), r.actor, r.action, JSON.stringify(r.detail)].map(v => '<td>' + escape(v) + '</td>').join('') + '</tr>'
    ));
  } catch (e) {
    if (current === revision) { notice.textContent = e.message; content.innerHTML = button('refresh', '重试'); }
  }
  host?.resize?.(document.documentElement.scrollHeight);
}

async function mutate(method, path, body, okMsg) {
  if (busy) return;
  busy = true;
  try {
    const data = await request(sdk, method, path, body);
    await refresh();
    notice.textContent = okMsg || '操作已完成';
    return data;
  } catch (e) {
    notice.textContent = e.message;
  } finally {
    busy = false;
  }
}

async function openDesktopPopup() {
  if (busy) return;
  busy = true;
  notice.textContent = '正在开启登录桌面…';
  try {
    const data = await request(sdk, 'POST', '/desktop/open', {});
    const vp = typeof data?.viewer_path === 'string' && /^\/[a-zA-Z0-9/_-]+$/.test(data.viewer_path) ? data.viewer_path : lastViewerPath;
    lastViewerPath = vp;
    window.open(vp, '_blank', 'noopener');
    await refresh();
    notice.textContent = '登录桌面已开启并在新窗口打开';
  } catch (e) {
    notice.textContent = e.message;
  } finally {
    busy = false;
  }
}

$('tabs').innerHTML = Object.entries(pages).map(([k, v]) => '<a data-page="' + k + '" href="#/apps/google-flow/' + k + '">' + v + '</a>').join('');

$('dashboard').addEventListener('click', async e => {
  const act = e.target.closest('[data-act]')?.dataset.act;
  if (!act) return;
  if (act === 'bc_root') return go('accounts');
  if (act === 'open_policy') return go('policy');
  if (act === 'add_account' || act === 'auth') return openDesktopPopup();
  if (act === 'reopen_desktop') { window.open(lastViewerPath, '_blank', 'noopener'); return; }
  if (act === 'refresh') return refresh();
  if (act === 'refresh_balance') {
    notice.textContent = '正在实时刷新余额…';
    const d = await mutate('POST', '/desktop/check-auth', {}, '');
    if (d) notice.textContent = d.authorized ? '余额已刷新：' + (d.credits ?? '—') : '会话已失效，请重新授权';
    return;
  }
  if (act === 'finish_desktop') {
    if (busy) return;
    busy = true;
    notice.textContent = '正在校验会话并关闭桌面…';
    try {
      const auth = await request(sdk, 'POST', '/desktop/check-auth', {});
      await request(sdk, 'POST', '/desktop/close', {});
      await refresh();
      notice.textContent = auth?.authorized ? '授权有效，已刷新余额并关闭登录桌面' : '桌面已关闭（当前尚未检测到有效会话）';
    } catch (err) { notice.textContent = err.message; }
    finally { busy = false; }
    return;
  }
  if (act === 'close_desktop') return mutate('POST', '/desktop/close', {}, '登录桌面已关闭');
  if (busy) return;
  const row = e.target.closest('tr');
  $('media-label').hidden = act !== 'bind';
  $('media').value = '';
  $('question').textContent = (labels[act] || act) + '？';
  const dialog = $('confirm');
  dialog.returnValue = '';
  dialog.addEventListener('close', () => {
    if (dialog.returnValue !== 'yes') return;
    if (row) return mutate('POST', '/calls/' + row.dataset.id + '/reconcile', {action:act, revision:Number(row.dataset.rev), media_id:act==='bind'?$('media').value:null});
    const path = act === 'defaults' ? '/policy/defaults' : '/channel/' + act;
    mutate('POST', path, {});
  }, {once:true});
  dialog.showModal();
});

content.addEventListener('submit', e => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  for (const k of Object.keys(data)) if (!choices[k]) data[k] = Number(data[k]);
  mutate('PUT', '/policy', data, '策略已保存');
});

addEventListener('hashchange', refresh);
const subscriptions = [];
if (sdk?.navigation?.subscribe) {
  subscriptions.push(sdk.navigation.subscribe(({path}) => {
    const selected = path.replace(/^\//, '') || 'accounts';
    const target = selected === 'policy' || Object.hasOwn(pages, selected) ? selected : 'accounts';
    const hash = '#/apps/google-flow/' + target;
    if (location.hash !== hash) location.hash = hash;
  }));
  $('tabs').addEventListener('click', async event => {
    const selected = event.target.closest('[data-page]')?.dataset.page;
    if (!selected) return;
    event.preventDefault();
    go(selected);
  });
}
if (sdk?.theme?.subscribe) subscriptions.push(sdk.theme.subscribe(theme => {
  document.documentElement.dataset.theme = theme;
}));
addEventListener('pagehide', () => { ++revision; for (const unsubscribe of subscriptions) unsubscribe(); }, {once:true});
host?.ready?.();
host?.resize?.(document.documentElement.scrollHeight);
refresh();
