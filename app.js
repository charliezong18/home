// AI 主页 —— 纯静态、无依赖。数据全部运行时从私有仓 review 的 home-data 分支读，页面本身不含任何数据。
// 鉴权与门下同源共用：localStorage 键 `zhupi.token`（同 menxia/src/github.js 的 getToken/setToken）。
// 呈现：邸报（review #185 批定）。三问顺序：今天什么需要我（头条 + 到期）→ 手头的事到哪了（折务 / 项目）→ 有什么值得读。
'use strict';

const API = 'https://api.github.com';
const TOKEN_KEY = 'zhupi.token';
const DATA_PATH = '/repos/charliezong18/review/contents/home.json?ref=home-data';
const STALE_H = { menxia: 4, dashboard: 4, readings: 4 };   // 3h 心跳 + 30min 采集周期 + 余量
const MENXIA = 'https://charliezong18.github.io/menxia/';
// 每栏封顶条数（宽屏 1280×800 一屏放下），超出显示「还有 N 条 →」，不做无限滚动
const CAP = { due: 6, proj: 6, lib: 5, links: 3 };
const FOLDER_GROUPS = [['你拍', '待你拍', 5], ['你读', '待你读', 3], ['agent', '等 agent', 2]];
const DUE_DAYS = 14;        // 到期栏窗口；过期超过 14 天的项目卡 deadline 视为卡片没更新，不上栏
const LEAD_DAYS = 30;       // 头条候选的截止窗口

const $ = (id) => document.getElementById(id);
const getToken = () => { try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; } };
const setToken = (t) => localStorage.setItem(TOKEN_KEY, t.trim());

// 只用 textContent 建节点，数据里的任何字符串都不会被当 HTML 解析
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'onclick') el.addEventListener('click', v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const safeUrl = (u) => (/^(https?:|obsidian:)/i.test(u || '') ? u : null);

function ago(iso) {
  if (!iso) return '未知';
  const min = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  if (min < 48 * 60) return `${Math.round(min / 60)} 小时前`;
  return `${Math.round(min / 1440)} 天前`;
}

const daysUntil = (d) => Math.round((new Date(`${d}T00:00:00`) - new Date(new Date().toDateString())) / 86400000);
const daysSince = (iso) => (iso ? Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 86400000)) : NaN);
const waited = (d) => (!Number.isFinite(d) ? '' : d === 0 ? '今天' : `${d} 天`);
const md = (d) => d.slice(5);   // YYYY-MM-DD → MM-DD

function dueText(d) {
  if (d < 0) return `已过期 ${-d} 天`;
  return ['今天', '明天', '后天'][d] || `${d} 天后`;
}

// 报头日期：二〇二六年十月十三日 · 周二
function cnDate(dt) {
  const D = '〇一二三四五六七八九';
  const n2 = (n) => (n < 10 ? D[n] : `${n >= 20 ? D[Math.floor(n / 10)] : ''}十${n % 10 ? D[n % 10] : ''}`);
  const y = String(dt.getFullYear()).split('').map((c) => D[+c]).join('');
  return `${y}年${n2(dt.getMonth() + 1)}月${n2(dt.getDate())}日 · 周${'日一二三四五六'[dt.getDay()]}`;
}

function setStatus(msg, isErr) { const s = $('status'); s.textContent = msg || ''; s.classList.toggle('err', !!isErr); }

function showAuth(msg) {
  $('grid').hidden = true;
  $('auth').hidden = false;
  $('auth-err').textContent = msg || '';
  $('auth-input').focus();
}

async function load() {
  const token = getToken();
  if (!token) return showAuth('');
  setStatus('读取中…');
  let res;
  try {
    res = await fetch(API + DATA_PATH, {
      cache: 'no-store',
      headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github.raw+json', 'X-GitHub-Api-Version': '2022-11-28' },
    });
  } catch (e) {
    return setStatus(`网络错误：${e.message}`, true);
  }
  if (res.status === 401) return showAuth('token 已失效（401），请换一把。门下那边也需要同步换。');
  if (res.status === 404) return showAuth('读不到 review 仓的 home-data（404）：这把 token 可能没授权 review 仓的 Contents: Read。');
  if (!res.ok) return setStatus(`GitHub ${res.status}：${res.statusText}${res.status === 403 ? '（可能限流，稍后再试）' : ''}`, true);
  let data;
  try { data = await res.json(); } catch { return setStatus('home.json 不是合法 JSON', true); }
  $('auth').hidden = true;
  $('grid').hidden = false;
  render(data);
  setStatus('');
}

function srcBadge(data, name) {
  const s = (data.sources || {})[name] || {};
  const hours = s.updated ? (Date.now() - new Date(s.updated)) / 3600000 : Infinity;
  const stale = !s.ok || hours > (STALE_H[name] || 4);
  const title = s.ok ? `数据更新于 ${s.updated || '未知'}` : `数据停在 ${ago(s.updated)}，Mac mini 下一轮会重试（已失败 ${s.consecutive_failures} 次：${s.last_error || ''}）`;
  return { text: s.ok ? ago(s.updated) : `停在 ${ago(s.updated)}`, stale, title };
}

const more = (n, unit, href) => (n > 0 ? h(href ? 'a' : 'p', { class: 'more', href }, `还有 ${n} ${unit}${href ? ' → 门下' : ''}`) : null);
const empty = (b, rest) => h('p', { class: 'empty' }, h('b', {}, b), rest || null);

// 单行：编号 · 标题 · 天数（样稿 C 的密度）
function row(k, title, href, v, opts = {}) {
  const ext = /^https?:/i.test(href || '') && !href.startsWith(MENXIA);
  return h('div', { class: `row${opts.old ? ' old' : ''}` },
    h('span', { class: 'k' }, k),
    h('a', { class: 't', href: safeUrl(href), 'aria-label': `${opts.label || title}${ext ? '（新窗口打开）' : ''}`,
      target: ext ? '_blank' : null, rel: ext ? 'noopener noreferrer' : null }, title),
    h('span', { class: 'v', 'aria-hidden': 'true' }, v));
}

// 头条规则（#185 第 3 条）：需要你做判断、且有硬截止 → 截止最近的一件。
// 候选 = 折务 wait=你拍 且带 deadline 的折 + 卡在你（state=you）且带 deadline 的项目卡。付账等「动作」不进头条。
function pickLead(data, live) {
  const folders = ((data.folders || {}).oldest || {})['你拍'] || [];
  return [
    ...folders.filter((f) => f.deadline).map((f) => ({ days: daysUntil(f.deadline), deadline: f.deadline, note: f.deadline_note || '截止',
      title: `#${f.n} ${f.title}`, href: f.url, act: `去门下拍 #${f.n} →`, dek: `${f.kind || '折'} · 已挂 ${waited(daysSince(f.created))}` })),
    ...live.filter((p) => p.state === 'you' && p.deadline).map((p) => ({ days: daysUntil(p.deadline), deadline: p.deadline, note: p.deadline_note || '截止',
      title: `${p.icon || ''} ${p.title}`.trim(), href: p.obsidian, act: '在 Obsidian 打开 →', dek: p.next ? `下一步：${p.next}` : (p.summary || '') })),
  ].filter((c) => Number.isFinite(c.days) && c.days >= -DUE_DAYS && c.days <= LEAD_DAYS).sort((a, b) => a.days - b.days)[0] || null;
}

function renderLead(data, live, counts) {
  const oldest = (data.folders || {}).oldest || {};
  const lead = pickLead(data, live);
  const nYou = counts['你拍'] || 0;
  const kids = [];
  if (lead) {
    const remain = lead.days < 0 ? `已过期 ${-lead.days} 天` : lead.days === 0 ? '今天截止' : `还剩 ${lead.days} 天`;
    kids.push(h('div', { class: 'kicker hot' }, `头条 · 卡在你 · ${remain}`),
      h('h2', {}, h('a', { href: safeUrl(lead.href) }, `${lead.title}：${lead.note} ${md(lead.deadline)}，${remain}`)),
      lead.dek ? h('p', { class: 'dek' }, lead.dek) : null,
      safeUrl(lead.href) ? h('a', { class: 'act', href: safeUrl(lead.href) }, lead.act) : null);
  } else {
    kids.push(h('div', { class: 'kicker' }, '头条'),
      h('h2', {}, '今天没有硬截止'),
      h('p', { class: 'dek' }, nYou
        ? `门下有 ${nYou} 折等你拍，没有一件带硬截止；按等得最久的先拍。`
        : '门下没有等你拍的折。'),
      h('a', { class: 'act', href: MENXIA }, nYou ? `去门下拍 ${nYou} 折 →` : '去门下 →'));
  }
  // 次条：等你拍 / 等你读 各最旧一折（头条已占的那折不重复）
  const second = [['你拍', '等你拍 · 最旧'], ['你读', '等你读 · 最旧']].map(([k, label]) => {
    const f = (oldest[k] || [])[0];
    if (!f || (lead && lead.href === f.url)) return null;
    const d = daysSince(f.created);
    return h('div', {},
      h('div', { class: `kicker${d > 3 ? ' soon' : ''}` }, label),
      h('h3', {}, h('a', { href: safeUrl(f.url), 'aria-label': `#${f.n} ${f.title}，已挂 ${waited(d)}` }, `#${f.n} ${f.title}`)),
      h('p', {}, `${f.kind || '折'} · 已挂 ${waited(d)}`));
  }).filter(Boolean);
  if (second.length) kids.push(h('div', { class: 'second' }, second));
  $('lead').replaceChildren(...kids);
}

function render(data) {
  $('today-date').textContent = cnDate(new Date());
  $('gen-age').textContent = `生成于 ${ago(data.generated)}`;
  $('fresh').textContent = [['menxia', '折务'], ['dashboard', '项目'], ['readings', '链接']]
    .map(([k, l]) => `${l} ${srcBadge(data, k).text}`).join(' · ');

  document.querySelectorAll('.src').forEach((el) => {
    const b = srcBadge(data, el.dataset.src);
    el.textContent = b.text;
    el.title = b.title;
    el.setAttribute('aria-label', b.title);
    el.classList.toggle('stale', b.stale);
  });

  const counts = (data.folders || {}).counts || {};
  const oldest = (data.folders || {}).oldest || {};
  const projects = data.projects || [];
  const live = projects.filter((p) => p.status !== 'done' && p.status !== 'ice' && p.tier !== 'ice');
  const due = live.filter((p) => p.deadline)
    .map((p) => ({ ...p, days: daysUntil(p.deadline) }))
    .filter((p) => Number.isFinite(p.days) && p.days >= -DUE_DAYS && p.days <= DUE_DAYS)
    .sort((a, b) => a.days - b.days);
  const lib = data.library || [];
  const links = (data.readings || []).filter((r) => !r.folded);
  const newLinks = links.filter((r) => r.status === 'new');

  // 计数条（≤5 个）：点数字直达对应 tab
  const countBtn = (label, n, tab, cls) => h('button', { type: 'button', class: `count ${cls || ''}`, onclick: () => setTab(tab), 'aria-label': `${label} ${n}` }, h('b', {}, n), label);
  const overdue = due.some((p) => p.days < 0);
  $('counts').replaceChildren(
    countBtn('待你拍', counts['你拍'] || 0, 'folders', counts['你拍'] ? 'hot' : ''),
    countBtn('待你读', counts['你读'] || 0, 'folders'),
    countBtn(`${DUE_DAYS} 天内到期`, due.length, 'today', overdue ? 'hot' : due.some((p) => p.days <= 3) ? 'soon' : ''),
    countBtn('新读物', newLinks.length, 'reading'),
  );

  // Q1 头条 + 到期栏
  renderLead(data, live, counts);
  $('due').replaceChildren(...(due.length ? due.slice(0, CAP.due).map((p) => h('li', { class: p.days < 0 ? 'over' : p.days <= 3 ? 'soon' : '' },
    h('span', { class: 'd' }, md(p.deadline)),
    h('span', { class: 't' }, `${p.icon || ''} ${p.title}`.trim(), h('span', { class: 'm' }, [dueText(p.days), p.deadline_note].filter(Boolean).join(' · ')))))
    : [h('li', { class: 'empty' }, `${DUE_DAYS} 天内没有到期的事。`)]),
  ...(due.length > CAP.due ? [h('li', { class: 'empty' }, `还有 ${due.length - CAP.due} 件 · 见 Obsidian Dashboard`)] : []));

  // Q2 折务：待你拍 / 待你读 / 等 agent，等得久的在前，单行
  $('folder-sum').textContent = `共 ${(data.folders || {}).total ?? 0} 折`;
  const fkids = [];
  for (const [k, label, cap] of FOLDER_GROUPS) {
    const n = counts[k] || 0;
    if (!n && k !== '你拍') continue;
    fkids.push(h('h3', { class: 'sub' }, `${label} ${n}`));
    if (!n) { fkids.push(empty('门下没有等你拍的折。')); continue; }
    const list = (oldest[k] || []).slice(0, cap);
    for (const f of list) {
      const d = daysSince(f.created);
      fkids.push(row(`#${f.n}`, f.title, f.url, waited(d), { old: k !== 'agent' && d > 3, label: `#${f.n} ${f.title}，${label}，已等 ${waited(d)}` }));
    }
    fkids.push(more(n - list.length, '折', MENXIA));
  }
  $('folders').replaceChildren(...fkids.filter(Boolean));

  // Q2 项目：now / build，卡在你优先，封顶
  const rank = { you: 0, waiting: 1, active: 2, ice: 3, done: 4 };
  const active = live.filter((p) => ['now', 'build'].includes(p.tier))
    .sort((a, b) => ((rank[a.state] ?? 9) - (rank[b.state] ?? 9)) || (a.tier === b.tier ? 0 : a.tier === 'now' ? -1 : 1));
  const shown = active.slice(0, CAP.proj);
  $('proj-sum').textContent = `${projects.length} 张卡 · now/build ${active.length}`;
  $('projects').replaceChildren(...shown.map((p) => {
    const pct = Number.isFinite(+p.progress) && p.progress != null ? Math.max(0, Math.min(100, +p.progress)) : null;
    const fill = h('i', {});
    fill.style.width = `${pct ?? 0}%`;   // CSSOM 赋值，不走 style 属性（CSP style-src 'self'）
    const dust = p.dusty ? daysSince(p.updated) : NaN;
    return h('div', { class: `proj${p.dusty ? ' dust' : ''}` },
      h('div', { class: 'n' },
        h('button', { type: 'button', onclick: () => openProject(p), 'aria-label': `${p.title}，进度 ${pct ?? '未知'}%，打开详情` }, `${p.icon || ''} ${p.title}`.trim()),
        h('span', {}, p.state === 'you' ? h('span', { class: 'you' }, '卡在你 ') : null, pct == null ? '—' : `${pct}%`)),
      h('div', { class: 'bar', 'aria-hidden': 'true' }, fill),
      h('div', { class: 'nx' }, h('em', {}, p.dusty ? `蒙尘${Number.isFinite(dust) ? ` ${dust} 天` : ''}` : 'next'), p.next || '—'));
  }),
  ...(active.length ? [] : [empty('没有进行中的项目卡。')]),
  ...(projects.length > shown.length ? [h('p', { class: 'more' }, `还有 ${projects.length - shown.length} 张（learn / ice / done 或超出封顶）→ Obsidian Dashboard`)] : []));

  // Q3 读物：永不催——不进朱色、不显示逾期
  $('read-sum').textContent = `弘文馆 ${lib.length} · 新链接 ${newLinks.length}`;
  $('lib-count').textContent = String(lib.length);
  $('library').replaceChildren(...(lib.length
    ? lib.slice(0, CAP.lib).map((f) => row(`#${f.n}`, f.title, f.url, waited(daysSince(f.created)), { label: `弘文馆 #${f.n} ${f.title}` }))
    : [empty('弘文馆空着', '门下 kind:读物 的折会出现在这里。')]),
  ...[more(lib.length - CAP.lib, '篇', MENXIA)].filter(Boolean));
  $('links-count').textContent = `新 ${newLinks.length}`;
  $('links').replaceChildren(...(links.length
    ? links.slice(0, CAP.links).map((r) => row('link', `${r.title || r.url}${r.why ? ` —「${r.why}」` : ''}`, r.url, r.added ? md(r.added) : '', { label: r.title || r.url }))
    : [empty('还没丢过链接', '在任何 App 分享给 Telegram 里的 Hermes，会出现在这里。')]),
  ...[more(links.length - CAP.links, '条')].filter(Boolean));
}

function openProject(p) {
  $('pd-title').textContent = `${p.icon || ''} ${p.title}`;
  $('pd-next').textContent = p.next ? `下一步：${p.next}` : '';
  $('pd-progress').textContent = `进度 ${p.progress ?? '—'}% · 里程碑 ${p.milestones?.done ?? 0}/${p.milestones?.total ?? 0}${p.deadline ? ` · deadline ${p.deadline} ${p.deadline_note || ''}` : ''}`;
  $('pd-summary').textContent = p.summary || '';
  $('pd-updated').textContent = `卡片更新于 ${p.updated || '未知'}${p.dusty ? ' · 蒙尘' : ''}`;
  const ob = safeUrl(p.obsidian);
  $('pd-obsidian').href = ob || '#';
  $('pd-obsidian').hidden = !ob;
  $('proj-dialog').showModal();
}

function setTab(tab) {
  document.body.dataset.tab = tab;
  document.querySelectorAll('.tabs [role=tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
}

document.querySelectorAll('.tabs [role=tab]').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));
$('reload').addEventListener('click', load);
$('pd-close').addEventListener('click', () => $('proj-dialog').close());
$('proj-dialog').addEventListener('click', (e) => { if (e.target === $('proj-dialog')) $('proj-dialog').close(); });
$('auth-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const v = $('auth-input').value.trim();
  if (!v) return;
  try { setToken(v); } catch { $('auth-err').textContent = '存不进 localStorage（无痕模式？）'; return; }
  $('auth-input').value = '';
  load();
});
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && !$('grid').hidden) load(); });

// 手机左右滑切 tab
let x0 = null;
const TABS = ['today', 'folders', 'projects', 'reading'];
document.addEventListener('touchstart', (e) => { x0 = e.touches[0].clientX; }, { passive: true });
document.addEventListener('touchend', (e) => {
  if (x0 == null || window.matchMedia('(min-width: 700px)').matches) return;
  const dx = e.changedTouches[0].clientX - x0; x0 = null;
  if (Math.abs(dx) < 60) return;
  const i = TABS.indexOf(document.body.dataset.tab);
  setTab(TABS[Math.max(0, Math.min(TABS.length - 1, i + (dx < 0 ? 1 : -1)))]);
}, { passive: true });

setTab('today');
load();
