// AI 主页 —— 纯静态、无依赖。数据全部运行时从私有仓 review 的 home-data 分支读，页面本身不含任何数据。
// 鉴权与门下同源共用：localStorage 键 `zhupi.token`（同 menxia/src/github.js 的 getToken/setToken）。
'use strict';

const API = 'https://api.github.com';
const TOKEN_KEY = 'zhupi.token';
const DATA_PATH = '/repos/charliezong18/review/contents/home.json?ref=home-data';
const STALE_H = { menxia: 4, dashboard: 4, readings: 4 };   // 3h 心跳 + 30min 采集周期 + 余量
const CAP = 8;                                             // 每栏封顶条数，不做无限滚动
const STATE_DOT = { you: 'red', waiting: 'yellow', active: 'green', ice: 'grey', done: 'teal' };
const WAIT_ORDER = [['你拍', 'red'], ['你读', 'yellow'], ['agent', 'green'], ['闲', 'grey'], ['未标注', 'grey']];

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
  const title = s.ok ? `数据更新于 ${s.updated || '未知'}` : `采集失败 ${s.consecutive_failures} 次：${s.last_error || ''}`;
  return { text: `${stale ? '⚠ ' : ''}${ago(s.updated)}`, stale, title };
}

function render(data) {
  $('today-date').textContent = new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'short' });
  $('gen-age').textContent = `生成于 ${ago(data.generated)}`;

  document.querySelectorAll('.src').forEach((el) => {
    const b = srcBadge(data, el.dataset.src);
    el.textContent = b.text;
    el.title = b.title;
    el.classList.toggle('stale', b.stale);
    el.closest('.card')?.classList.toggle('is-stale', b.stale && el.parentElement.tagName === 'H2');
  });

  const counts = (data.folders || {}).counts || {};
  const projects = data.projects || [];
  const due = projects
    .filter((p) => p.deadline && p.status !== 'done' && p.tier !== 'ice')
    .map((p) => ({ ...p, days: daysUntil(p.deadline) }))
    .filter((p) => p.days >= -14 && p.days <= 30)
    .sort((a, b) => a.days - b.days);
  const links = (data.readings || []).filter((r) => !r.folded);
  const newLinks = links.filter((r) => r.status === 'new');

  // 顶部计数：点数字直达对应 tab
  const countBtn = (label, n, tab, cls) => h('button', { type: 'button', class: `count ${cls || ''}`, onclick: () => setTab(tab) }, `${label} `, h('b', {}, n));
  $('counts').replaceChildren(
    countBtn('待你拍', counts['你拍'] || 0, 'folders', 'red'),
    countBtn('待你读', counts['你读'] || 0, 'folders', 'yellow'),
    countBtn('7 天内到期', due.filter((p) => p.days <= 7).length, 'today', 'red'),
    countBtn('新读物', newLinks.length, 'reading', 'green'),
  );

  // 今天 / 到期（P1 只有项目卡 deadline）
  $('due').replaceChildren(...(due.length ? due.slice(0, CAP).map((p) => h('li', { class: p.days < 7 ? 'red' : p.days < 30 ? 'yellow' : '' },
    h('span', { class: 'when' }, p.days < 0 ? `过期 ${-p.days} 天` : p.days === 0 ? '今天' : `${p.days} 天后`),
    ' ', `${p.icon || ''} ${p.title}`, p.deadline_note ? h('span', { class: 'muted' }, ` · ${p.deadline_note}`) : null))
    : [h('li', { class: 'muted' }, '30 天内没有项目 deadline')]));

  // 折务：计数 + 你拍 / 你读 / agent 的最旧几折（封顶 CAP）
  $('folder-counts').replaceChildren(...WAIT_ORDER.filter(([k]) => counts[k]).map(([k, c]) => h('span', { class: `pill ${c}` }, `${k} ${counts[k]}`)));
  const oldest = (data.folders || {}).oldest || {};
  const rows = [];
  for (const [k, c] of WAIT_ORDER.slice(0, 3)) for (const f of oldest[k] || []) rows.push({ ...f, k, c });
  $('folders').replaceChildren(...(rows.length ? rows.slice(0, CAP).map((f) => h('li', { class: f.c },
    h('span', { class: 'tag' }, f.k), ' ', h('a', { href: safeUrl(f.url) }, `#${f.n} ${f.title}`),
    h('span', { class: 'muted' }, ` · ${ago(f.created)}`)))
    : [h('li', { class: 'muted' }, '没有等你的折')]));

  // 项目：now / build，红黄优先，封顶
  const rank = { you: 0, waiting: 1, active: 2, ice: 3, done: 4 };
  const active = projects.filter((p) => ['now', 'build'].includes(p.tier) && p.status !== 'done' && p.status !== 'ice')
    .sort((a, b) => (rank[a.state] - rank[b.state]) || (a.tier === b.tier ? 0 : a.tier === 'now' ? -1 : 1));
  $('projects').replaceChildren(...active.slice(0, CAP).map((p) => h('li', { class: `proj ${STATE_DOT[p.state] || ''}` },
    h('button', { type: 'button', class: 'linklike', onclick: () => openProject(p) },
      h('span', { class: 'bar', 'aria-label': `进度 ${p.progress ?? '?'}%` }, h('span', { style: null, class: 'fill', 'data-p': p.progress ?? 0 })),
      ` ${p.icon || ''} ${p.title}`, p.dusty ? ' 🕸' : ''),
    p.next ? h('div', { class: 'next muted' }, `next: ${p.next}`) : null)));
  document.querySelectorAll('.fill').forEach((el) => { el.style.width = `${Math.max(0, Math.min(100, +el.dataset.p))}%`; });
  const hidden = projects.length - Math.min(active.length, CAP);
  $('projects-more').textContent = hidden > 0 ? `另有 ${hidden} 张卡（learn / ice / done 或超出封顶）在 Obsidian Dashboard` : '';

  // 读物：弘文馆 + 丢的链接
  const lib = data.library || [];
  $('lib-count').textContent = `在读 ${lib.length}`;
  $('library').replaceChildren(...(lib.length ? lib.slice(0, 6).map((f) => h('li', {}, h('a', { href: safeUrl(f.url) }, `#${f.n} ${f.title}`)))
    : [h('li', { class: 'muted' }, '弘文馆空')]),
  ...(lib.length > 6 ? [h('li', {}, h('a', { class: 'more', href: 'https://charliezong18.github.io/menxia/' }, `全部 ${lib.length} → 门下`))] : []));
  $('links-count').textContent = `新 ${newLinks.length}`;
  $('links').replaceChildren(...(links.length ? links.slice(0, 6).map((r) => h('li', {},
    h('a', { href: safeUrl(r.url), rel: 'noopener noreferrer', target: '_blank' }, r.title || r.url),
    r.why ? h('span', { class: 'muted' }, ` — ${r.why}`) : null))
    : [h('li', { class: 'muted' }, '还没丢过链接：在任何 App 分享给 Telegram 里的 Hermes')]));
}

function openProject(p) {
  $('pd-title').textContent = `${p.icon || ''} ${p.title}`;
  $('pd-next').textContent = p.next ? `下一步：${p.next}` : '';
  $('pd-progress').textContent = `进度 ${p.progress ?? '—'}% · 里程碑 ${p.milestones?.done ?? 0}/${p.milestones?.total ?? 0}${p.deadline ? ` · deadline ${p.deadline} ${p.deadline_note || ''}` : ''}`;
  $('pd-summary').textContent = p.summary || '';
  $('pd-updated').textContent = `卡片更新于 ${p.updated || '未知'}${p.dusty ? ' 🕸 蒙尘' : ''}`;
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
