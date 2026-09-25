'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
class ContractTextarea {
  constructor(value = '') {
    this.value = value;
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.events = [];
  }
  setRangeText(text, start, end) {
    this.value = this.value.slice(0, start) + text + this.value.slice(end);
  }
  focus() {}
  dispatchEvent(event) { this.events.push(event.type); }
}
class ContractEvent {
  constructor(type) { this.type = type; }
}

const search = {value: ''};
const context = {
  window: {Cuoti: {ui: {}, dailyAnalysis: {}}},
  document: {
    getElementById(id) {
      if (id === 'latex-guide-search') return search;
      return null;
    },
  },
  navigator: {},
  HTMLTextAreaElement: ContractTextarea,
  Event: ContractEvent,
  Date,
  safeInteger(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) ? parsed : fallback;
  },
  escapeHTML(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  },
  icon(name) { return `<svg data-test-icon="${name}"></svg>`; },
  emptyStateHTML(title) { return `<div>${context.escapeHTML(title)}</div>`; },
  renderMath() { return Promise.resolve(); },
  showToast() {},
};
context.window.window = context.window;
context.window.setTimeout = setTimeout;
vm.createContext(context);

function load(relative) {
  const source = fs.readFileSync(path.join(root, 'cuoti_app', 'static', 'js', relative), 'utf8');
  vm.runInContext(source, context, {filename: relative});
}

load(path.join('ui', 'editor.js'));
const textarea = new ContractTextarea('题目：');
textarea.selectionStart = textarea.selectionEnd = textarea.value.length;
const inserted = context.window.Cuoti.ui.editor.insertAtSelection(textarea, {
  before: '\\(', after: '\\)', placeholder: 'x^2',
});
assert.equal(textarea.value, '题目：\\(x^2\\)');
assert.equal(inserted.start, 5);
assert.equal(inserted.end, 8);
assert.equal(inserted.text, '\\(x^2\\)');
assert.deepEqual(textarea.events, ['input']);

load(path.join('daily-analysis', 'state.js'));
load(path.join('daily-analysis', 'render.js'));
const daily = context.window.Cuoti.dailyAnalysis.render.body({
  stale_count: 1,
  missing_count: 1,
  items: [{
    mistake_public_id: 'm"><script>bad()</script>', position: 1,
    subject: '<数学分析>', chapter: '<极限>', qnum: '<img src=x>', stale: true,
    generated_at: '2026-08-22T10:00:00+08:00',
    analysis: {
      note_assessment: '<b>错误</b>', correction: '<script>bad()</script>',
      hint: '\\(x<1\\)', exam_pattern: '极限题',
    },
  }],
});
assert.match(daily, /&lt;数学分析&gt;/);
assert.match(daily, /&lt;script&gt;bad\(\)&lt;\/script&gt;/);
assert.match(daily, /题目内容已变化/);
assert.doesNotMatch(daily, /<script>/);

load('latex-guide.js');
const guide = context.window.Cuoti.latexGuide;
assert.equal(guide.categories.length, 10);
assert.equal(guide.catalog.length, 20);
assert.ok(guide.catalog.every(item => item.id && item.category && item.title));
search.value = '矩阵';
assert.deepEqual(Array.from(guide.filtered(), item => item.id), ['matrix']);
search.value = '积分';
assert.deepEqual(Array.from(guide.filtered(), item => item.id), ['sum-integral']);
search.value = '箭头';
assert.deepEqual(Array.from(guide.filtered(), item => item.id), ['arrows-logic']);
search.value = '希腊';
assert.deepEqual(Array.from(guide.filtered(), item => item.id), ['greek']);
assert.ok(guide.catalog.find(item => item.id === 'greek').choices.includes('\\Omega'));
assert.equal(guide.choiceGlyphs['\\alpha'], 'α');
assert.match(guide.source(guide.catalog.find(item => item.id === 'cases')), /\\begin\{cases\}/);
assert.equal(guide.serializeMatrix([['a', 'b'], ['c', 'd']]), 'a & b ' + '\\\\' + '\nc & d');
assert.match(guide.pinnedSource('limit'), /\\lim/);
guide.matrixState.rows.splice(0, guide.matrixState.rows.length, ['1', '2'], ['3', '4']);
assert.equal(guide.matrixAction('add-row', 0), true);
assert.equal(guide.matrixState.rows.length, 3);
assert.equal(guide.matrixAction('remove-row', 1), true);
assert.equal(guide.matrixState.rows.length, 2);
assert.equal(guide.matrixAction('remove-col', 0), true);
assert.equal(guide.matrixState.rows[0].length, 1);

process.stdout.write('DAILY_LATEX_CONTRACT_OK\n');
