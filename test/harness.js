// 在 node:vm 里加载 app.js：最小假 DOM + 可替换的 fetch。只供 node --test 用，不进页面。
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Node {}

function fakeEl(tag = 'div') {
  const el = new Node();
  Object.assign(el, {
    tagName: tag.toUpperCase(), value: '', textContent: '', hidden: false, disabled: false, checked: false, open: false,
    dataset: {}, style: {}, children: [],
    classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute() { return null; },
    replaceChildren(...k) { this.children = k; }, append(...k) { this.children.push(...k); }, appendChild(k) { this.children.push(k); },
    querySelectorAll() { return []; }, querySelector() { return null; }, closest() { return null; },
    focus() {}, select() {}, setSelectionRange() {}, close() {}, showModal() {}, remove() {},
  });
  return el;
}

function load({ token = 't0ken', fetch } = {}) {
  const els = new Map();
  const document = {
    getElementById(id) { if (!els.has(id)) els.set(id, fakeEl()); return els.get(id); },
    createElement: fakeEl,
    createTextNode: (t) => Object.assign(new Node(), { textContent: t }),
    querySelectorAll: () => [], addEventListener() {}, body: fakeEl('body'), visibilityState: 'visible',
    execCommand: () => false,
  };
  const store = { 'zhupi.token': token };
  const ctx = {
    document, Node, console, URLSearchParams, TextEncoder, TextDecoder, atob, btoa, crypto, setTimeout, clearTimeout,
    location: { search: '', pathname: '/', href: '' },
    history: { replaceState() {} },
    localStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } },
    matchMedia: () => ({ matches: false }),
    fetch: (...a) => ctx.fetchImpl(...a),
    fetchImpl: fetch || (async () => ({ ok: false, status: 404, json: async () => ({}) })),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8'), ctx, { filename: 'app.js' });
  const get = (name) => vm.runInContext(name, ctx);
  return { ctx, get, $: (id) => document.getElementById(id) };
}

const json = (status, obj) => ({ ok: status >= 200 && status < 300, status, json: async () => obj });
const b64 = (s) => Buffer.from(s, 'utf8').toString('base64');

module.exports = { load, json, b64 };
