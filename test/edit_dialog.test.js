// H6：deadline 不是严格 YYYY-MM-DD（如 2026-12、2026-12-01 前）时输入框显示为空；只改 next 不能顺带把 deadline 清空。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { load } = require('./harness');

function openEditor(project) {
  const app = load();
  app.ctx.__p = project;
  app.get('curProject = __p');
  app.get('setEditing')(true);
  return app;
}

const card = (deadline) => ({ id: 'p:demo', title: '示例', next: '旧的下一步', status: 'active', tier: 'now', deadline, sections: {} });

for (const dl of ['2026-12', '2026-12-01 前', 'Q4']) {
  test(`非标准 deadline「${dl}」：只改 next 不发 deadline`, () => {
    const p = card(dl);
    const app = openEditor(p);
    assert.strictEqual(app.$('pe-deadline').value, '');
    app.$('pe-next').value = '新的下一步';
    const { fields } = app.get('collectEdit')(p);
    assert.deepStrictEqual({ ...fields }, { next: '新的下一步' });
  });
}

test('非标准 deadline：用户真填了日期才发', () => {
  const p = card('2026-12');
  const app = openEditor(p);
  app.$('pe-deadline').value = '2026-12-15';
  assert.deepStrictEqual({ ...app.get('collectEdit')(p).fields }, { deadline: '2026-12-15' });
});

test('标准 deadline：清空仍然发 deadline: ""', () => {
  const p = card('2026-12-01');
  const app = openEditor(p);
  assert.strictEqual(app.$('pe-deadline').value, '2026-12-01');
  app.$('pe-deadline').value = '';
  assert.deepStrictEqual({ ...app.get('collectEdit')(p).fields }, { deadline: '' });
});

test('什么都没改 → 没有字段', () => {
  const p = card('2026-12-01 前');
  const app = openEditor(p);
  assert.deepStrictEqual({ ...app.get('collectEdit')(p).fields }, {});
});
