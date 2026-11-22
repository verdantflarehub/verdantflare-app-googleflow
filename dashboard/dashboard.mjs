import {pages, fields, choices, reasons, request} from './model.mjs';
const $ = id => document.getElementById(id);
const {appSDK:sdk, dashboardHost:host} = window.$wujie?.props || {};
const content = $('content'), notice = $('notice');
const escape = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const button = (id, text, cls='', extra='') => '<button type="button" data-vf="Button" data-act="'+id+'"'+(cls?' class="'+cls+'"':'')+extra+'>'+text+'</button>';
const get = path => request(sdk,'GET',path);
const labels = {
  pause:'暂停', resume:'恢复', 'clear-cooldown':'清除冷却',
  bind:'绑定已有媒体', fail:'确认失败并释放预留', retry:'允许重新提交',
  defaults:'恢复默认策略', refresh_balance:'刷新', open_policy:'策略',
  auth:'授权', edit_acct:'编辑', delete_acct:'删除',
  close_desktop:'直接关闭桌面', finish_desktop:'完成登录并关闭桌面',
  reopen_desktop:'重新打开窗口', add_account:'+ 新增账号'
};
let revision = 0, busy = false, lastViewerPath = '/apps/google-flow/desktop/', desktopPopup = null, desktopPoll = null, desktopSessionId = null, desktopAccountId = null;
let cachedAccounts = [], cachedProxies = [], activePolicyAccount = 'default', filterAccount = '';

function parseRoute() {
  const parts = location.hash.replace(/^#\/apps\/google-flow\/?/, '').split('/').filter(Boolean);
  if (parts[0] === 'policy') return {slug: 'policy', accountId: parts[1] || activePolicyAccount || 'default'};
  const slug = Object.hasOwn(pages, parts[0]) ? parts[0] : 'accounts';
  return {slug, accountId: null};
}

function fmtUpdated(ts) {
  if (!ts) return '—';
  const diff = Math.max(0, Math.floor(Date.now()/1000 - ts));
  const clock = new Date(ts * 1000).toTimeString().slice(0, 8);
  if (diff < 60) return '刚刚（' + clock + '）';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前（' + clock + '）';
  return new Date(ts * 1000).toLocaleString();
}

function proxyTag(acct) {
  if (!acct.proxy_url) return '<span class="gf-pill">默认出口 :1082</span>';
  if (acct.proxy_name) return '<span class="gf-pill gf-pill-proxy" title="' + escape(acct.proxy_url) + '">代理 · ' + escape(acct.proxy_name) + '</span>';
  const m = acct.proxy_url.match(/:(\d+)$/);
  return '<span class="gf-pill gf-pill-proxy" title="' + escape(acct.proxy_url) + '">独立代理 ' + escape(m ? ':' + m[1] : acct.proxy_url) + '</span>';
}

function accountCard(acct) {
  const aid = acct.id;
  const paused = acct.status === 'paused';
  const disabled = !acct.enabled;
  const live = acct.account || {};
  const usage = acct.usage || {};
  const email = live.email || (live.authorized ? '已授权会话' : '未登录账号');
  const authText = disabled ? '已停用' : paused ? '已暂停' : live.authorized === null ? '无法观测' : live.authorized ? '会话有效' : '需要登录';
  const badgeCls = disabled ? 'neutral' : paused ? 'paused' : live.authorized ? 'ok' : 'warn';
  const attr = ' data-account="' + escape(aid) + '"';
  const pauseRow = paused ? (
    '<div class="vf-banner danger gf-inline-banner" role="alert"><span>' + escape(reasons[acct.pause?.reason] || '账号已暂停') +
    (acct.pause?.until ? ' · 冷却至 ' + escape(new Date(acct.pause.until * 1000).toLocaleString()) : ' · 需手动恢复') +
    '</span><span class="actions">' + button('resume', '恢复', '', attr) + button('clear-cooldown', '清除冷却', '', attr) + '</span></div>'
  ) : '';
  const delBtn = (!acct.enabled && aid !== 'default') ? button('delete_acct', labels.delete_acct, 'danger', attr) : '';
  return '<section class="card gf-acct-card" data-account-card="' + escape(aid) + '">' + pauseRow +
    '<div class="section-head"><div class="gf-acct-title"><div><div class="gf-acct-head-line"><h2>' + escape(acct.label) + '</h2>' +
    '<span class="gf-badge ' + badgeCls + '">' + escape(authText) + '</span>' +
    '<span class="gf-pill">优先级 P' + escape(acct.priority) + '</span>' + proxyTag(acct) +
    '</div><p class="subline">' + escape(email) + ' · ID: <span class="gf-mono-inline">' + escape(aid) + '</span> · 端口 ' + escape(acct.port) + '</p></div></div>' +
    '<div class="actions">' +
    button('refresh_balance', labels.refresh_balance, '', attr) +
    button('open_policy', labels.open_policy, '', attr) +
    button('edit_acct', labels.edit_acct, '', attr) +
    button('auth', labels.auth, '', attr) +
    (disabled ? '' : button(paused ? 'resume' : 'pause', paused ? labels.resume : labels.pause, '', attr)) +
    delBtn +
    '</div></div><div class="c-facts">' +
    '<div><small>剩余积分</small><strong class="num">' + escape(live.credits) + '</strong></div>' +
    '<div><small>预留积分</small><strong class="num">' + escape(usage.reserved ?? 0) + '</strong></div>' +
    '<div><small>余额更新时间</small><strong class="gf-fact-sm">' + escape(fmtUpdated(live.updated_at)) + '</strong></div>' +
    '<div><small>并发与间隔</small><strong class="gf-fact-sm">并发 ' + escape(acct.policy?.concurrency) + ' · 间隔 ' + escape(acct.policy?.min_interval_seconds) + 's</strong></div>' +
    '</div></section>';
}

function accountsView(data) {
  if (data.desktop?.viewer_path) lastViewerPath = data.desktop.viewer_path;
  const desktopOpen = Boolean(data.desktop?.open);
  if (desktopOpen) {
    if (data.desktop?.session_id != null) desktopSessionId = data.desktop.session_id;
    if (data.desktop?.account_id) desktopAccountId = data.desktop.account_id;
  } else {
    desktopSessionId = null;
    desktopAccountId = null;
  }
  const desktopBanner = desktopOpen ? (
    '<div class="gf-banner" role="status"><div><strong>登录桌面运行中</strong> · 登录完成后直接关闭浏览器登录标签页即可自动保存并同步余额。</div>' +
    '<div class="actions">' + button('reopen_desktop', labels.reopen_desktop) +
    button('finish_desktop', labels.finish_desktop, 'gf-primary') +
    button('close_desktop', labels.close_desktop) + '</div></div>'
  ) : '';
  const accounts = Array.isArray(data.accounts) && data.accounts.length ? data.accounts : [];
  cachedAccounts = accounts;
  const s = data.summary || {
    total_accounts: accounts.length,
    active_accounts: accounts.filter(a => a.enabled && a.status === 'active' && a.account?.authorized).length,
    max_accounts: 8,
    total_credits: data.account?.credits ?? 0,
    total_reserved: data.today?.reserved ?? 0,
  };
  const summaryBar = '<div class="ledger-summary gf-summary-bar">' +
    '<span>总可用积分 <b class="num">' + escape(s.total_credits) + '</b></span>' +
    '<span>活跃账号 <b class="num">' + escape(s.active_accounts) + ' / ' + escape(s.total_accounts) + '</b></span>' +
    '<span>总预留积分 <b class="num">' + escape(s.total_reserved) + '</b></span>' +
    '<span>槽位配额 <b class="num">' + escape(s.total_accounts) + ' / ' + escape(s.max_accounts) + '</b></span>' +
    '</div>';
  return desktopBanner + summaryBar + '<div class="g-account-list">' + accounts.map(accountCard).join('') + '</div>';
}

function policyView(data, accountLabel) {
  const num = (key, label, unit, help, min) =>
    '<label class="gf-field"><span class="gf-field-label">' + label + '<span class="gf-field-unit">' + unit + '</span></span>' +
    '<input required type="number" step="1" min="' + min + '" name="' + key + '" value="' + escape(data[key]) + '">' +
    '<small>' + help + '</small></label>';
  const sel = (key, label, help) =>
    '<label class="gf-field"><span class="gf-field-label">' + label + '</span><select name="' + key + '">' +
    Object.entries(choices[key]).map(([v, l]) => '<option value="' + v + '" ' + (data[key] === v ? 'selected' : '') + '>' + l + '</option>').join('') +
    '</select><small>' + help + '</small></label>';
  return '<form id="policy-form"><section class="card gf-policy-card">' +
    '<div class="gf-form-section"><div class="gf-form-section-head"><h3>并发与提交节奏</h3><p>控制账号「' + escape(accountLabel) + '」的并发任务数与请求频率。</p></div>' +
    '<div class="gf-form-grid">' +
    num('concurrency', '同时执行的生成数', '个任务', '建议单账号保持 1，避免同一浏览器会话互相抢锁', 1) +
    num('min_interval_seconds', '两次提交最小间隔', '秒', '相邻两次提交之间的最短等待时间，0 表示不额外限速', 0) +
    '</div></div>' +
    '<div class="gf-form-section"><div class="gf-form-section-head"><h3>风控与异常处理</h3><p>触发上游异常或网络中断时仅暂停当前账号，不影响其余活跃账号继续接单。</p></div>' +
    '<div class="gf-form-grid">' +
    sel('on_unusual_activity', '遇到 403 UNUSUAL_ACTIVITY', 'Google Flow 返回异常活动拦截时的响应方式') +
    num('cooldown_minutes', '自动冷却时长', '分钟', '填 0 表示暂停后需手动恢复；大于 0 时到期自动恢复', 0) +
    sel('on_submission_unknown', '提交结果不明时', '网络超时且无法确认上游是否已扣分时的处理策略') +
    '</div></div>' +
    '<div class="gf-policy-foot"><small>修改仅对当前账号生效，点击顶部面包屑「Google Flow」返回账号列表。</small><div class="actions">' +
    button('defaults', '恢复默认') + '<button type="submit" data-vf="Button" class="gf-primary">保存策略</button></div></div></section></form>';
}

function filterBar() {
  const opts = ['<option value="">全部账号</option>'].concat(
    cachedAccounts.map(a => '<option value="' + escape(a.id) + '"' + (filterAccount === a.id ? ' selected' : '') + '>' + escape(a.label) + ' (' + escape(a.id) + ')</option>')
  ).join('');
  return '<div class="toolbar gf-filter-bar"><label class="gf-filter-label">按账号筛选 <select id="acct-filter">' + opts + '</select></label></div>';
}

function table(head, rows) {
  return rows.length
    ? '<div class="card"><table><thead><tr>' + head.map(h => '<th>' + h + '</th>').join('') + '</tr></thead><tbody>' + rows.join('') + '</tbody></table></div>'
    : '<div class="empty">暂无记录</div>';
}

function callsView(data) {
  const list = filterAccount ? data.filter(r => (r.account_id || 'default') === filterAccount) : data;
  return filterBar() + table(['时间／任务', '账号', '来源', '操作／模型', '状态', '积分', '处理'], list.map(r =>
    '<tr data-id="' + escape(r.call_id) + '" data-rev="' + Number(r.revision) + '"><td>' +
    escape(r.started ? new Date(r.started * 1000).toLocaleString() : null) + '<small>' + escape(r.request_id) + '</small></td><td>' +
    escape(r.account_label || r.account_id || 'default') + '</td><td>' +
    escape(r.capability === 'video' ? 'Video MCP' : 'Image MCP') + '</td><td>' + escape(r.model) + '</td><td>' +
    escape(r.state) + '</td><td class="num">' + escape(r.credits) + '</td><td>' +
    (r.state === 'submission_unknown' ? ['bind', 'fail', 'retry'].map(k => button(k, labels[k])).join('') : '—') + '</td></tr>'
  ));
}

function auditView(data) {
  const list = filterAccount ? data.filter(r => (r.account_id || r.detail?.account_id) === filterAccount) : data;
  return filterBar() + table(['时间', '账号', '操作者', '动作', '详情'], list.map(r =>
    '<tr>' + [new Date(r.time * 1000).toLocaleString(), r.account_id || r.detail?.account_id || '—', r.actor, r.action, JSON.stringify(r.detail)].map(v => '<td>' + escape(v) + '</td>').join('') + '</tr>'
  ));
}

function modelsView() {
  const row = (name, id, specs, credits) =>
    '<tr><td><strong>' + name + '</strong><small class="gf-mono">' + id + '</small></td><td><div class="gf-specs">' +
    specs.map(([k, v]) => '<span class="gf-spec"><b>' + k + '：</b>' + v + '</span>').join('') +
    '</div></td><td><strong class="num">' + credits + '</strong></td></tr>';
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

function go(slug, accountId) {
  const suffix = slug === 'policy' && accountId ? 'policy/' + accountId : slug;
  const path = suffix === 'accounts' ? '/' : '/' + suffix;
  if (sdk?.navigation?.request && slug !== 'policy') {
    sdk.navigation.request({target:'app', path}).catch(() => { location.hash = '#/apps/google-flow/' + suffix; });
  } else {
    location.hash = '#/apps/google-flow/' + suffix;
  }
}

function findAccount(id) {
  return cachedAccounts.find(a => a.id === id);
}

async function refresh() {
  const {slug: selected, accountId} = parseRoute();
  const current = ++revision;
  const isPolicy = selected === 'policy';
  if (isPolicy) activePolicyAccount = accountId || 'default';
  $('tabs').hidden = isPolicy;
  const policyAcct = isPolicy ? findAccount(activePolicyAccount) : null;
  const policyLabel = policyAcct?.label || activePolicyAccount;
  if (isPolicy) {
    $('breadcrumb').innerHTML = '应用中心 / 渠道管理 / <button type="button" class="gf-bc-link" data-act="bc_root">Google Flow</button> / ' + escape(policyLabel) + ' / 策略配置';
    $('page-title').textContent = '策略配置 · ' + policyLabel;
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
    const endpoint = isPolicy
      ? '/accounts/' + encodeURIComponent(activePolicyAccount) + '/policy'
      : {accounts:'/overview', log:'/calls', audit:'/audit'}[selected];
    const data = await get(endpoint);
    if (current !== revision) return;
    if (selected === 'accounts') content.innerHTML = accountsView(data);
    if (isPolicy) content.innerHTML = policyView(data, policyLabel);
    if (selected === 'log') content.innerHTML = callsView(data);
    if (selected === 'audit') content.innerHTML = auditView(data);
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

function stopDesktopWatch() {
  if (desktopPoll) { clearInterval(desktopPoll); desktopPoll = null; }
  desktopPopup = null;
}

function watchDesktopPopup(win, accountId, sessionId) {
  stopDesktopWatch();
  desktopPopup = win;
  desktopAccountId = accountId || desktopAccountId;
  if (sessionId != null) desktopSessionId = sessionId;
  if (!win) return;
  const watchedSession = desktopSessionId;
  const watchedAccount = desktopAccountId;
  desktopPoll = setInterval(async () => {
    try {
      if (!desktopPopup || desktopPopup.closed) {
        if (busy) return;
        stopDesktopWatch();
        busy = true;
        notice.textContent = '检测到登录网页已关闭，正在自动关闭桌面并同步授权…';
        try {
          const closeBody = watchedSession != null ? {session_id: watchedSession} : {};
          const closed = await request(sdk, 'POST', '/desktop/close', closeBody);
          if (!closed?.stale_ignored && watchedAccount) {
            const auth = await request(sdk, 'POST', '/accounts/' + encodeURIComponent(watchedAccount) + '/action/refresh', {});
            await refresh();
            notice.textContent = auth?.authorized ? '登录桌面已自动关闭，授权有效（余额：' + (auth.credits ?? '—') + '）' : '登录桌面已自动关闭，尚未检测到有效会话';
          } else {
            await refresh();
          }
        } catch (err) {
          notice.textContent = err.message;
          await refresh();
        } finally {
          busy = false;
        }
      }
    } catch {
      stopDesktopWatch();
    }
  }, 400);
}

async function openAccountDesktop(accountId) {
  if (busy) return;
  busy = true;
  notice.textContent = '正在为账号开启登录桌面…';
  try {
    const path = accountId ? '/accounts/' + encodeURIComponent(accountId) + '/desktop/login' : '/desktop/open';
    const data = await request(sdk, 'POST', path, {});
    const vp = typeof data?.viewer_path === 'string' && /^\/[a-zA-Z0-9/_-]+$/.test(data.viewer_path) ? data.viewer_path : lastViewerPath;
    lastViewerPath = vp;
    desktopSessionId = data?.session_id ?? null;
    desktopAccountId = accountId || data?.account_id || 'default';
    const win = window.open(vp, '_blank');
    if (win) {
      watchDesktopPopup(win, desktopAccountId, desktopSessionId);
      await refresh();
      notice.textContent = '登录桌面已在新标签页打开，登录完成后直接关闭该标签页即可自动生效';
    } else {
      await refresh();
      notice.textContent = '浏览器拦截了登录弹窗，请允许弹窗或点击「重新打开窗口」';
    }
  } catch (e) {
    notice.textContent = e.message;
  } finally {
    busy = false;
  }
}

async function openAccountDialog(acct) {
  const isEdit = Boolean(acct);
  $('acct-dialog-title').textContent = isEdit ? '编辑账号 · ' + acct.label : '新增 Google Flow 账号';
  $('acct-id').value = isEdit ? acct.id : '';
  $('acct-label').value = isEdit ? acct.label : '';
  $('acct-priority').value = isEdit ? acct.priority : 100;
  $('acct-enabled-wrap').hidden = !isEdit;
  $('acct-enabled').checked = isEdit ? Boolean(acct.enabled) : true;
  try {
    const res = await get('/proxies');
    cachedProxies = Array.isArray(res?.items) ? res.items : [];
  } catch { cachedProxies = []; }
  const currentVal = (isEdit && acct.proxy_id) ? (acct.proxy_id + ':' + acct.proxy_revision) : 'default';
  let found = currentVal === 'default';
  const opts = ['<option value="default"' + (currentVal === 'default' ? ' selected' : '') + '>默认出口（系统默认 :1082）</option>'];
  for (const p of cachedProxies) {
    const val = p.proxy_id + ':' + p.revision;
    const chosen = val === currentVal;
    const expired = p.expires_at && Date.parse(p.expires_at) <= Date.now();
    const unavail = p.status !== 'active' || expired;
    if (chosen) found = true;
    opts.push('<option value="' + escape(val) + '"' + (chosen ? ' selected' : '') + (unavail && !chosen ? ' disabled' : '') + '>' +
      escape(p.name) + '（' + escape(p.protocol) + (unavail ? ' · 暂不可用' : '') + '）</option>');
  }
  if (!found && isEdit && acct.proxy_id) {
    opts.push('<option value="' + escape(currentVal) + '" selected>' + escape(acct.proxy_name || '当前代理') + '（已变更）</option>');
  }
  $('acct-proxy').innerHTML = opts.join('');
  $('acct-dialog').showModal();
}

$('tabs').innerHTML = Object.entries(pages).map(([k, v]) => '<a data-page="' + k + '" href="#/apps/google-flow/' + k + '">' + v + '</a>').join('');
$('acct-cancel').addEventListener('click', () => $('acct-dialog').close());
$('acct-form').addEventListener('submit', async e => {
  e.preventDefault();
  const id = $('acct-id').value.trim();
  const rawProxy = $('acct-proxy').value;
  let proxy_ref = {mode: 'default'};
  if (rawProxy && rawProxy !== 'default') {
    const [proxy_id, rev] = rawProxy.split(':');
    proxy_ref = {mode: 'proxy', proxy_id, revision: Number(rev)};
  }
  const payload = {
    label: $('acct-label').value.trim(),
    priority: Number($('acct-priority').value),
    proxy_ref,
  };
  if (id) payload.enabled = $('acct-enabled').checked;
  $('acct-dialog').close();
  if (id) {
    await mutate('PUT', '/accounts/' + encodeURIComponent(id), payload, '账号配置已更新');
  } else {
    await mutate('POST', '/accounts', payload, '新账号已创建，请点击「授权」完成登录');
  }
});

content.addEventListener('change', e => {
  if (e.target.id === 'acct-filter') {
    filterAccount = e.target.value;
    refresh();
  }
});

$('dashboard').addEventListener('click', async e => {
  const btn = e.target.closest('[data-act]');
  const act = btn?.dataset.act;
  if (!act) return;
  const aid = btn.dataset.account || activePolicyAccount || 'default';
  if (act === 'bc_root') return go('accounts');
  if (act === 'open_policy') return go('policy', aid);
  if (act === 'add_account') return openAccountDialog(null);
  if (act === 'edit_acct') return openAccountDialog(findAccount(aid));
  if (act === 'auth') return openAccountDesktop(aid);
  if (act === 'reopen_desktop') {
    const win = window.open(lastViewerPath, '_blank');
    if (win) watchDesktopPopup(win, desktopAccountId, desktopSessionId);
    else notice.textContent = '浏览器拦截了登录弹窗，请允许本站点弹窗';
    return;
  }
  if (act === 'refresh') return refresh();
  if (act === 'refresh_balance') {
    notice.textContent = '正在实时刷新余额…';
    const d = await mutate('POST', '/accounts/' + encodeURIComponent(aid) + '/action/refresh', {}, '');
    if (d) notice.textContent = d.authorized ? '余额已刷新：' + (d.credits ?? '—') : '会话已失效，请重新授权';
    return;
  }
  if (act === 'finish_desktop') {
    if (busy) return;
    stopDesktopWatch();
    busy = true;
    const closeBody = desktopSessionId != null ? {session_id: desktopSessionId} : {};
    const targetAcct = desktopAccountId;
    notice.textContent = '正在校验会话并关闭桌面…';
    try {
      const closed = await request(sdk, 'POST', '/desktop/close', closeBody);
      if (closed?.stale_ignored) {
        await refresh();
        notice.textContent = '桌面会话已变更，已刷新最新状态';
      } else {
        if (targetAcct) {
          await request(sdk, 'POST', '/accounts/' + encodeURIComponent(targetAcct) + '/action/refresh', {});
        }
        await refresh();
        notice.textContent = '已关闭登录桌面并刷新账号列表';
      }
    } catch (err) { notice.textContent = err.message; }
    finally { busy = false; }
    return;
  }
  if (act === 'close_desktop') {
    if (busy) return;
    stopDesktopWatch();
    const closeBody = desktopSessionId != null ? {session_id: desktopSessionId} : {};
    const closed = await mutate('POST', '/desktop/close', closeBody, '');
    if (closed) notice.textContent = closed.stale_ignored ? '桌面会话已变更，已刷新最新状态' : '登录桌面已关闭';
    return;
  }
  if (busy) return;
  const row = e.target.closest('tr');
  $('media-label').hidden = act !== 'bind';
  $('media').value = '';
  $('question').textContent = (labels[act] || act) + (act === 'delete_acct' ? '（' + aid + '）' : '') + '？';
  const dialog = $('confirm');
  dialog.returnValue = '';
  dialog.addEventListener('close', () => {
    if (dialog.returnValue !== 'yes') return;
    if (row) return mutate('POST', '/calls/' + row.dataset.id + '/reconcile', {action:act, revision:Number(row.dataset.rev), media_id:act==='bind'?$('media').value:null});
    if (act === 'delete_acct') return mutate('DELETE', '/accounts/' + encodeURIComponent(aid), undefined, '账号已删除');
    if (act === 'defaults') return mutate('POST', '/accounts/' + encodeURIComponent(aid) + '/policy/defaults', {}, '已恢复默认策略');
    mutate('POST', '/accounts/' + encodeURIComponent(aid) + '/action/' + act, {});
  }, {once:true});
  dialog.showModal();
});

content.addEventListener('submit', e => {
  if (e.target.id !== 'policy-form') return;
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  for (const k of Object.keys(data)) if (!choices[k]) data[k] = Number(data[k]);
  mutate('PUT', '/accounts/' + encodeURIComponent(activePolicyAccount) + '/policy', data, '策略已保存');
});

addEventListener('hashchange', refresh);
const subscriptions = [];
if (sdk?.navigation?.subscribe) {
  subscriptions.push(sdk.navigation.subscribe(({path}) => {
    const clean = path.replace(/^\//, '') || 'accounts';
    const hash = '#/apps/google-flow/' + clean;
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
addEventListener('pagehide', () => { ++revision; stopDesktopWatch(); for (const unsubscribe of subscriptions) unsubscribe(); }, {once:true});
host?.ready?.();
host?.resize?.(document.documentElement.scrollHeight);
refresh();
