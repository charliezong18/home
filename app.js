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
// 编辑流：弹窗「保存」= 往 home-data 的 edits.jsonl 追加一行；Mac mini 每 5 分钟落进 vault 卡（collectors/apply_edits.py）
const EDITS_PATH = '/repos/charliezong18/review/contents/edits.jsonl';
const TIER = { now: 'now', build: 'build', learn: 'learn', ice: 'ice' };
const STATUS = { active: '推进中', waiting: '等别人', you: '卡在你', ice: '冷冻', done: '完成' };
let lastData = null;
const localEdits = [];      // 已保存、home.json 还没反映的编辑；重新读取时叠回去，免得乐观更新被旧数据冲掉

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
  overlayEdits(data);
  lastData = data;
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

// 可复制 ID：点一下进剪贴板，成功短暂变「已复制」。三级：
// 1) 同步 textarea + execCommand('copy')（须在点击手势内同步跑；showModal 的 dialog 会让外部节点 inert，所以 textarea 挂在按钮所在的 dialog 里）
// 2) navigator.clipboard.writeText  3) 都失败才回退 prompt() 让人手动复制
function copyId(btn, id) {
  const done = () => {
    const old = btn.dataset.id;
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = old; }, 1200);
  };
  const fallback = () => { window.prompt('复制这个 ID：', id); };
  const legacyCopy = () => {
    const ta = document.createElement('textarea');
    ta.value = id;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
    (btn.closest('dialog') || document.body).appendChild(ta);
    let ok = false;
    try { ta.focus({ preventScroll: true }); ta.select(); ta.setSelectionRange(0, id.length); ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
    btn.focus({ preventScroll: true });
    return ok;
  };
  if (legacyCopy()) { done(); return; }
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(id).then(done, fallback);
  else fallback();
}

function idBtn(id, cls = '') {
  if (!id) return null;
  return h('button', { type: 'button', class: `cid ${cls}`.trim(), 'data-id': id, title: '点击复制 ID', 'aria-label': `复制 ID ${id}`,
    onclick: (e) => { e.stopPropagation(); copyId(e.currentTarget, id); } }, id);
}

// 单行：编号 · 标题 · 天数（样稿 C 的密度）。opts.id = 可复制 ID，占编号列（折 = #PR号，读物 = r:…）
function row(k, title, href, v, opts = {}) {
  const ext = /^https?:/i.test(href || '') && !href.startsWith(MENXIA);
  return h('div', { class: `row${opts.old ? ' old' : ''}${opts.wide ? ' wide' : ''}` },
    opts.id ? idBtn(opts.id, 'k') : h('span', { class: 'k' }, k),
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
    h('span', { class: 't' }, `${p.icon || ''} ${p.title}`.trim(), h('span', { class: 'm' }, [dueText(p.days), p.deadline_note].filter(Boolean).join(' · '), ' ', idBtn(p.id)))))
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
      fkids.push(row(`#${f.n}`, f.title, f.url, waited(d), { id: `#${f.n}`, old: k !== 'agent' && d > 3, label: `#${f.n} ${f.title}，${label}，已等 ${waited(d)}` }));
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
        h('button', { type: 'button', onclick: () => openProject(p), 'aria-label': `${p.title}，进度 ${pct ?? '未知'}%${pendMark(p) ? `，${pendMark(p)}` : ''}，打开详情` }, `${p.icon || ''} ${p.title}`.trim()),
        h('span', {}, pendMark(p) ? h('span', { class: 'pend', title: pendMark(p), 'aria-hidden': 'true' }, '● ') : null, p.state === 'you' ? h('span', { class: 'you' }, '卡在你 ') : null, pct == null ? '—' : `${pct}%`)),
      h('div', { class: 'bar', 'aria-hidden': 'true' }, fill),
      h('div', { class: 'nx' }, h('em', {}, p.dusty ? `蒙尘${Number.isFinite(dust) ? ` ${dust} 天` : ''}` : 'next'), p.next || '—'));
  }),
  ...(active.length ? [] : [empty('没有进行中的项目卡。')]),
  ...(projects.length > shown.length ? [h('p', { class: 'more' }, `还有 ${projects.length - shown.length} 张（learn / ice / done 或超出封顶）→ Obsidian Dashboard`)] : []));

  // Q3 读物：永不催——不进朱色、不显示逾期
  $('read-sum').textContent = `弘文馆 ${lib.length} · 新链接 ${newLinks.length}`;
  $('lib-count').textContent = String(lib.length);
  $('library').replaceChildren(...(lib.length
    ? lib.slice(0, CAP.lib).map((f) => row(`#${f.n}`, f.title, f.url, waited(daysSince(f.created)), { id: `#${f.n}`, label: `弘文馆 #${f.n} ${f.title}` }))
    : [empty('弘文馆空着', '门下 kind:读物 的折会出现在这里。')]),
  ...[more(lib.length - CAP.lib, '篇', MENXIA)].filter(Boolean));
  $('links-count').textContent = `新 ${newLinks.length}`;
  $('links').replaceChildren(...(links.length
    ? links.slice(0, CAP.links).map((r) => row('link', `${r.title || r.url}${r.why ? ` —「${r.why}」` : ''}`, r.url, r.added ? md(r.added) : '', { id: r.rid, wide: !!r.rid, label: r.title || r.url }))
    : [empty('还没丢过链接', '在任何 App 分享给 Telegram 里的 Hermes，会出现在这里。')]),
  ...[more(links.length - CAP.links, '条')].filter(Boolean));
}

// 行内 markdown 最小渲染：**粗体** `code` [[wikilink]]→文本 裸 URL→链接。直接建 DOM 节点（textContent），天然不经 HTML 解析
function mdInline(s) {
  const out = []; let i = 0;
  for (const m of String(s || '').matchAll(/\*\*(.+?)\*\*|`([^`]+)`|\[\[([^\]]+)\]\]|(https?:\/\/[^\s<>)\]]+)/g)) {
    out.push(s.slice(i, m.index)); i = m.index + m[0].length;
    out.push(m[1] ? h('strong', {}, m[1]) : m[2] ? h('code', {}, m[2]) : m[3] ? m[3].split('|').pop() : h('a', { href: m[4], target: '_blank', rel: 'noopener noreferrer' }, m[4]));
  }
  out.push(String(s || '').slice(i));
  return out;
}

// 卡行 / 弹窗上的 ● 标记：pending_edits = 还没落进 vault 卡；hermes_edits = 附言等 Hermes 处理（build_home 写入）
const pendMark = (p) => (p.pending_edits ? '编辑待同步' : p.hermes_edits ? '附言待 Hermes 处理' : '');

let curProject = null;

function openProject(p) {
  curProject = p;
  fillProject(p);
  setEditing(false);
  $('pd-msg').hidden = true;
  $('proj-dialog').showModal();
  measureMore();
}

function measureMore() {
  const el = $('pd-status');
  $('pd-more').hidden = el.scrollHeight <= el.clientHeight + 1;   // 须在 showModal 后量
}

function fillProject(p) {
  const S = p.sections || {};
  $('pd-title').textContent = `${p.icon || ''} ${p.title}`;
  const pid = $('pd-id');
  pid.dataset.id = p.id || '';
  pid.textContent = p.id || '';
  pid.hidden = !p.id;
  $('pd-badges').replaceChildren(...[p.tier && TIER[p.tier], STATUS[p.status] || p.status, pendMark(p) && `● ${pendMark(p)}`].filter(Boolean).map((t) => h('span', { class: `pd-badge${t === '卡在你' ? ' you' : ''}${t.startsWith('●') ? ' pend' : ''}` }, t)));
  $('pd-next').replaceChildren(...(p.next ? [h('span', { class: 'pd-lab' }, '下一步'), ...mdInline(p.next)] : []));
  $('pd-next').hidden = !p.next;
  const dl = p.deadline ? daysUntil(p.deadline) : NaN;
  const meta = [];
  if (p.deadline) meta.push(h('span', { class: Number.isFinite(dl) && dl < 0 ? 'late' : '' }, `截止 ${p.deadline}${p.deadline_note ? ` ${p.deadline_note}` : ''}${Number.isFinite(dl) && dl < 0 ? `（已过 ${-dl} 天）` : ''}`));
  meta.push(`更新于 ${p.updated || '未知'}`);
  if (p.dusty) meta.push(`蒙尘 ${daysSince(p.updated)} 天`);
  $('pd-meta').replaceChildren(...meta.flatMap((m, k) => (k ? [' · ', m] : [m])));
  const ms = S.milestones || [];
  $('pd-ms-wrap').hidden = !ms.length;
  $('pd-ms-h').textContent = `里程碑 ${ms.filter((m) => m.done).length}/${ms.length}`;
  $('pd-ms').replaceChildren(...ms.map((m) => h('li', { class: m.done ? 'done' : '' }, h('span', { class: 'ck', 'aria-hidden': 'true' }, m.done ? '☑' : '☐'), h('span', {}, ...mdInline(m.text)))));
  const bl = S.blockers || [];
  $('pd-bl-wrap').hidden = !bl.length;
  $('pd-bl').replaceChildren(...bl.map((b) => h('li', {}, ...mdInline(b))));
  const st = (S.status_text || p.summary || '').split('\n').filter((l) => l.trim());
  $('pd-st-wrap').hidden = !st.length;
  $('pd-status').replaceChildren(...st.map((l) => h('p', {}, ...mdInline(l.replace(/^\s*[-*+]\s+/, '• ')))));
  $('pd-status').classList.add('clamp');
  $('pd-more').hidden = true;
  $('pd-more').textContent = '展开';
  const lg = (S.log || []).slice(-3).reverse();
  $('pd-log-wrap').hidden = !lg.length;
  $('pd-log').replaceChildren(...lg.map((l) => h('li', {}, ...mdInline(l))));
  const ob = safeUrl(p.obsidian);
  $('pd-obsidian').href = ob || '#';
  $('pd-obsidian').hidden = !ob;
  $('pd-edit-btn').hidden = !p.id;   // 没有 ID 的卡无从定位，不给编辑
}

// ---- 编辑 → 保存 → 触发更新 ----
function setEditing(on) {
  $('proj-dialog').classList.toggle('editing', on);
  $('pd-edit').hidden = !on;
  if (!on) return;
  const p = curProject;
  const opts = (sel, map, cur) => {
    const keys = Object.keys(map);
    if (cur && !keys.includes(cur)) keys.unshift(cur);   // 卡上有非法旧值也照显，不悄悄改掉
    $(sel).replaceChildren(h('option', { value: '' }, '—'), ...keys.map((k) => h('option', { value: k }, map[k] ? `${k} · ${map[k]}` : k)));
    $(sel).value = cur || '';
  };
  $('pe-next').value = p.next || '';
  opts('pe-status', STATUS, p.status);
  opts('pe-tier', TIER, p.tier);
  $('pe-deadline').value = /^\d{4}-\d{2}-\d{2}$/.test(p.deadline || '') ? p.deadline : '';
  const ms = (p.sections || {}).milestones || [];
  $('pe-ms-wrap').hidden = !ms.length;
  $('pe-ms').replaceChildren(...ms.map((m, i) => {
    const cb = h('input', { type: 'checkbox', 'data-i': String(i) });
    cb.checked = !!m.done;
    return h('label', { class: 'pe-ck' }, cb, h('span', {}, m.text));
  }));
  $('pe-note').value = '';
  $('pe-err').textContent = '';
  $('pe-save').disabled = false;
  $('pe-next').focus();
}

// 只收改动过的字段 / 里程碑
function collectEdit(p) {
  const fields = {};
  const cur = { next: $('pe-next').value.replace(/\s+/g, ' ').trim(), status: $('pe-status').value, tier: $('pe-tier').value, deadline: $('pe-deadline').value };
  for (const [k, v] of Object.entries(cur)) if (v !== (p[k] || '')) fields[k] = v;
  if ('status' in fields && !fields.status) delete fields.status;   // 「—」不算改：status / tier 不支持清空
  if ('tier' in fields && !fields.tier) delete fields.tier;
  const ms = (p.sections || {}).milestones || [];
  const milestones = [...$('pe-ms').querySelectorAll('input[type=checkbox]')]
    .map((cb) => ({ m: ms[+cb.dataset.i], done: cb.checked }))
    .filter((x) => x.m && x.done !== !!x.m.done)
    .map((x) => ({ text: x.m.text, done: x.done }));
  return { fields, milestones, note: $('pe-note').value.trim() };
}

const b64enc = (s) => {
  const bytes = new TextEncoder().encode(s);
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
};
const b64dec = (s) => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\s/g, '')), (c) => c.charCodeAt(0)));

// GET 取 sha → 末尾追加一行 → PUT；别人同时写（409/422 sha 不符）重试 2 次
async function appendEdit(rec) {
  const headers = { Authorization: `Bearer ${getToken()}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' };
  for (let attempt = 0; attempt < 3; attempt++) {
    const g = await fetch(`${API}${EDITS_PATH}?ref=home-data`, { cache: 'no-store', headers });
    let text = '', sha;
    if (g.ok) { const j = await g.json(); text = b64dec(j.content || ''); sha = j.sha; }
    else if (g.status !== 404) throw new Error(`读 edits.jsonl 失败：GitHub ${g.status}`);
    if (text && !text.endsWith('\n')) text += '\n';
    const r = await fetch(API + EDITS_PATH, {
      method: 'PUT', headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: `edits: ${rec.id} ${rec.target}`, branch: 'home-data', content: b64enc(text + JSON.stringify(rec) + '\n'), ...(sha ? { sha } : {}) }),
    });
    if (r.ok) return;
    if (r.status === 409 || r.status === 422) continue;
    if (r.status === 401) throw new Error('token 已失效（401）');
    if (r.status === 403 || r.status === 404) throw new Error(`这把 token 没有 review 仓的 Contents: Write 权限（${r.status}）。到 GitHub fine-grained token 设置里给 review 仓加上 Contents: Read and write。`);
    throw new Error(`写入失败：GitHub ${r.status}`);
  }
  throw new Error('连续 3 次撞上别人同时写入，请稍后再点保存');
}

// 乐观更新：本地数据先改成保存后的样子，并记一笔 pending，直到 home.json 比这次保存新
function applyLocal(p, e) {
  Object.assign(p, e.fields);
  if ('status' in e.fields) p.state = STATUS[e.fields.status] ? e.fields.status : 'active';
  const ms = (p.sections || {}).milestones || [];
  for (const m of e.milestones) { const t = ms.find((x) => x.text === m.text); if (t) t.done = m.done; }
  if (ms.length) {
    const d = ms.filter((x) => x.done).length;
    p.milestones = { done: d, total: ms.length };
  }
  if (e.fields && Object.keys(e.fields).length + e.milestones.length) p.pending_edits = (p.pending_edits || 0) + 1;
  if (e.note) p.hermes_edits = (p.hermes_edits || 0) + 1;
}

function overlayEdits(data) {
  const gen = new Date(data.generated || 0).getTime();
  for (let i = localEdits.length - 1; i >= 0; i--) if (new Date(localEdits[i].ts).getTime() <= gen - 60000) localEdits.splice(i, 1);
  for (const e of localEdits) {
    const p = (data.projects || []).find((x) => x.id === e.target);
    if (p) applyLocal(p, e);
  }
}

async function saveEdit(ev) {
  ev.preventDefault();
  const p = curProject;
  const { fields, milestones, note } = collectEdit(p);
  if (!Object.keys(fields).length && !milestones.length && !note) { $('pe-err').textContent = '没有改动。'; return; }
  if (!getToken()) { $('pe-err').textContent = '这台设备没有 token。'; return; }
  const now = new Date();
  const rnd = [...crypto.getRandomValues(new Uint8Array(2))].map((b) => b.toString(16).padStart(2, '0')).join('');
  const rec = { id: `e:${now.toISOString().replace(/[-:]|\.\d+/g, '')}-${rnd}`, ts: now.toISOString().replace(/\.\d+Z$/, 'Z'),
    target: p.id, fields, milestones, note, state: 'pending', from: 'web' };
  $('pe-save').disabled = true;
  $('pe-err').textContent = '保存中…';
  try {
    await appendEdit(rec);
  } catch (e) {
    $('pe-err').textContent = e.message;
    $('pe-save').disabled = false;
    return;
  }
  localEdits.push(rec);
  applyLocal(p, rec);
  fillProject(p);
  setEditing(false);
  measureMore();
  const msg = `已保存${Object.keys(fields).length || milestones.length ? '，卡片 ≤5 分钟同步' : ''}${note ? '；给 Hermes 的话 ≤15 分钟处理' : ''}。`;
  $('pd-msg').textContent = msg;
  $('pd-msg').hidden = false;
  setStatus(msg);
  if (lastData) render(lastData);
}

function setTab(tab) {
  document.body.dataset.tab = tab;
  document.querySelectorAll('.tabs [role=tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
}

document.querySelectorAll('.tabs [role=tab]').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));
$('reload').addEventListener('click', load);
$('pd-id').addEventListener('click', (e) => copyId(e.currentTarget, e.currentTarget.dataset.id));
$('pd-more').addEventListener('click', (e) => {
  const open = $('pd-status').classList.toggle('clamp');
  e.currentTarget.textContent = open ? '展开' : '收起';
});
$('pd-close').addEventListener('click', () => $('proj-dialog').close());
$('pd-edit-btn').addEventListener('click', () => { $('pd-msg').hidden = true; setEditing(true); });
$('pe-cancel').addEventListener('click', () => setEditing(false));
$('pd-edit').addEventListener('submit', saveEdit);
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
