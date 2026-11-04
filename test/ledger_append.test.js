// H1：账本 > 1 MB 时 Contents API 返回 content:"" / encoding:"none"，旧代码把它当空文件、带 sha 覆盖成一行。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { load, json, b64 } = require('./harness');

const PATH = '/repos/charliezong18/review/contents/edits.jsonl';
const BLOB = '/repos/charliezong18/review/git/blobs/';

// 假 GitHub：记下所有请求，PUT 时把新内容存起来
function fakeGitHub(file) {
  const calls = [];
  let put = null;
  const fetch = async (url, opts = {}) => {
    const u = url.replace('https://api.github.com', '');
    calls.push({ method: opts.method || 'GET', u });
    if ((opts.method || 'GET') === 'PUT') { put = JSON.parse(opts.body); return json(200, {}); }
    if (u.startsWith(BLOB)) return file.blob ? json(200, file.blob) : json(404, {});
    if (u.startsWith(PATH)) return file.contents ? json(200, file.contents) : json(404, {});
    throw new Error('unexpected ' + u);
  };
  return { fetch, calls, put: () => put };
}

const LINE = '{"id":"e:new"}';
const putText = (gh) => Buffer.from(gh.put().content, 'base64').toString('utf8');

test('小文件：base64 内容原样保留并追加', async () => {
  const old = '{"id":"e:1"}\n';
  const gh = fakeGitHub({ contents: { type: 'file', sha: 's1', size: Buffer.byteLength(old), encoding: 'base64', content: b64(old) } });
  const app = load({ fetch: gh.fetch });
  await app.get('appendLine')(PATH, LINE, 'm');
  assert.strictEqual(putText(gh), old + LINE + '\n');
  assert.strictEqual(gh.put().sha, 's1');
  assert.ok(!gh.calls.some((c) => c.u.startsWith(BLOB)), '小文件不必读 blob');
});

test('> 1 MB：encoding none → 改读 git/blobs/<sha>，历史全部保留', async () => {
  const old = '{"id":"e:old","note":"' + 'x'.repeat(1100000) + '"}\n';
  const size = Buffer.byteLength(old);
  const gh = fakeGitHub({
    contents: { type: 'file', sha: 'big', size, encoding: 'none', content: '' },
    blob: { sha: 'big', size, encoding: 'base64', content: b64(old) },
  });
  const app = load({ fetch: gh.fetch });
  await app.get('appendLine')(PATH, LINE, 'm');
  assert.ok(gh.calls.some((c) => c.u === BLOB + 'big'));
  assert.strictEqual(putText(gh), old + LINE + '\n');
  assert.strictEqual(gh.put().sha, 'big');
});

test('读不全（content 空但 size > 0、blob 也读不到）→ 拒绝写入，绝不 PUT', async () => {
  const gh = fakeGitHub({ contents: { type: 'file', sha: 'big', size: 2000000, encoding: 'none', content: '' } });
  const app = load({ fetch: gh.fetch });
  await assert.rejects(app.get('appendLine')(PATH, LINE, 'm'), /拒绝写入/);
  assert.strictEqual(gh.put(), null);
});

test('字节数与 size 不符 → 拒绝写入', async () => {
  const gh = fakeGitHub({ contents: { type: 'file', sha: 's', size: 999, encoding: 'base64', content: b64('short\n') } });
  const app = load({ fetch: gh.fetch });
  await assert.rejects(app.get('appendLine')(PATH, LINE, 'm'), /不完整/);
  assert.strictEqual(gh.put(), null);
});

test('超过 LEDGER_MAX → 拒绝写入（要求轮转）', async () => {
  const gh = fakeGitHub({ contents: { type: 'file', sha: 's', size: 26 * 1024 * 1024, encoding: 'none', content: '' } });
  const app = load({ fetch: gh.fetch });
  await assert.rejects(app.get('appendLine')(PATH, LINE, 'm'), /轮转/);
  assert.strictEqual(gh.put(), null);
});

test('文件不存在（404）→ 新建，不带 sha', async () => {
  const gh = fakeGitHub({});
  const app = load({ fetch: gh.fetch });
  await app.get('appendLine')(PATH, LINE, 'm');
  assert.strictEqual(putText(gh), LINE + '\n');
  assert.ok(!('sha' in gh.put()));
});

test('空文件（size 0）→ 正常追加', async () => {
  const gh = fakeGitHub({ contents: { type: 'file', sha: 'e', size: 0, encoding: 'base64', content: '' } });
  const app = load({ fetch: gh.fetch });
  await app.get('appendLine')(PATH, LINE, 'm');
  assert.strictEqual(putText(gh), LINE + '\n');
  assert.strictEqual(gh.put().sha, 'e');
});
