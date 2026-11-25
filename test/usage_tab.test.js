// 用量 tab：renderUsage / loadUsage / ?tab=usage 深链 / ?demo=1 夹具。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { load, json } = require('./harness');

// 假 DOM 的 textContent 不聚合子节点：递归拼出来
const text = (el) => (el == null ? '' : (el.children && el.children.length ? el.children.map(text).join('') : String(el.textContent ?? '')));
const walk = (el, out = []) => { if (el) { out.push(el); (el.children || []).forEach((k) => walk(k, out)); } return out; };

const day = (date, extra = {}) => ({
  date,
  total: { cost_usd: 66.7, tokens_m: 92.1, runs: 3, sessions: 50 },
  by_source: { worker: { cost_usd: 35.8, runs: 3 }, hermes_cron_heartbeat: { cost_usd: 24.1, sessions: 47 }, hermes_cron_other: { cost_usd: 6.8, sessions: 3 },
    hermes_main: { cost_usd: 0, turns: 0 }, hermes_local: { cost_usd: 0, sessions: 0 } },
  by_model: { 'opus-5-5': { cost_usd: 60, tokens_m: 80 }, 'fable-5-1': { cost_usd: 6.7, tokens_m: 2 } },
  failures: { error_max_turns: 1, permission_denials: 2, exit_nonzero: 0, ids: [{ task_id: 'm-1', kind: 'error_max_turns' }] },
  top_tasks: [{ task_id: 't-1', model: 'opus-5-5', cost_usd: 10.45, turns: 49, denials: 0, template: 'lookup', subtype: 'success' }],
  waste: [{ rule: 'R-CRON-IDLE', count: 46, cost_usd: 24.1 }],
  ...extra,
});
const usage = (generated, days) => ({ schema: 1, generated, quota: { seven_day_pct: null, source: 'none' }, days, week: { iso: '2026-W48', cost_usd: 100, vs_last_week_pct: -12, fable_cost_usd: 0 } });

test('今日卡：$、token、Hermes vs worker、额度待标定', () => {
  const app = load();
  app.get('renderUsage')(usage(new Date().toISOString(), [day('2026-11-23'), day('2026-11-22')]));
  const t = text(app.$('u-today'));
  assert.match(t, /\$66\.7/);
  assert.match(t, /92\.1M token/);
  assert.match(t, /Hermes \$30\.9 · worker \$35\.8/);
  assert.match(t, /额度待标定/);
  assert.strictEqual(app.$('u-body').hidden, false);
  assert.strictEqual(app.$('u-empty').hidden, true);
  assert.strictEqual(app.$('u-bars').children.length, 2);
  assert.match(text(app.$('u-top')), /t-1/);
  assert.match(text(app.$('u-waste')), /R-CRON-IDLE/);
  assert.match(text(app.$('u-fail')), /m-1/);
});

test('有额度读数时显示百分比，不显示待标定', () => {
  const app = load();
  const u = usage(new Date().toISOString(), [day('2026-11-23')]);
  u.quota = { seven_day_pct: 43.2, resets_at: '2026-11-27T17:00:00-08:00', source: 'manual' };
  app.get('renderUsage')(u);
  const t = text(app.$('u-today'));
  assert.match(t, /周额度 7d：43%/);
  assert.doesNotMatch(t, /额度待标定/);
});

test('数据超过 2 小时：更新时间带 ⚠', () => {
  const app = load();
  app.get('renderUsage')(usage(new Date(Date.now() - 3 * 3600e3).toISOString(), [day('2026-11-23')]));
  assert.match(app.$('u-fresh').textContent, /⚠/);
  app.get('renderUsage')(usage(new Date().toISOString(), [day('2026-11-23')]));
  assert.doesNotMatch(app.$('u-fresh').textContent, /⚠/);
});

test('数据里的字符串只当文本：不会生成 HTML 元素', () => {
  const app = load();
  const evil = '<img src=x onerror=alert(1)>';
  app.get('renderUsage')(usage(new Date().toISOString(), [day('2026-11-23', { top_tasks: [{ task_id: evil, model: evil, cost_usd: 1, turns: 1 }] })]));
  const tags = walk(app.$('u-top')).map((e) => e.tagName).filter(Boolean);
  assert.ok(!tags.includes('IMG'));
  assert.match(text(app.$('u-top')), /<img src=x/);
});

test('没有 usage.json（404）：显示提示，主体隐藏', async () => {
  const app = load({ fetch: async () => json(404, {}) });
  await app.get('loadUsage')('tok');
  assert.strictEqual(app.$('u-body').hidden, true);
  assert.match(app.$('u-empty').textContent, /还没有用量数据/);
});

test('loadUsage 读到数据就渲染', async () => {
  const app = load({ fetch: async () => json(200, usage(new Date().toISOString(), [day('2026-11-23')])) });
  await app.get('loadUsage')('tok');
  assert.match(text(app.$('u-today')), /\$66\.7/);
});

test('?tab=usage 深链直接进用量 tab', () => {
  const app = load({ search: '?tab=usage', token: '' });
  assert.strictEqual(app.get('document.body.dataset.tab'), 'usage');
});

test('?demo=1 夹具：30 天、Top 10、额度待标定', () => {
  const app = load({ search: '?demo=1&tab=usage' });
  const u = app.get('demoUsage')();
  assert.strictEqual(u.days.length, 30);
  assert.strictEqual(app.$('u-top').children.length, 10);
  assert.strictEqual(app.$('u-bars').children.length, 7);
  assert.match(text(app.$('u-today')), /额度待标定/);
});
