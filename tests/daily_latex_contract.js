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

const context = {
  window: {Cuoti: {ui: {}, dailyAnalysis: {}}},
  document: {
    getElementById() {
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
const ids = guide.catalog.map(item => item.id);
assert.equal(new Set(ids).size, ids.length);
assert.ok(guide.catalog.every(item => item.id && item.cat && item.name && item.src));
assert.ok(guide.catalog.every(item => guide.categories.includes(item.cat)));
assert.equal(guide.catalog.filter(item => item.cat === '级数').length, 25);
assert.equal(guide.catalog.filter(item => item.cat === '关系').length, 9);
for (const id of ['cauchy-sequence', 'cauchy-series', 'cauchy-product', 'ratio-test', 'expand-exp', 'cauchy-schwarz', 'eps-delta']) {
  assert.ok(ids.includes(id), id);
}
// Every toolbar insertion and symbol command is a non-empty LaTeX fragment.
assert.ok(guide.toolbar.flat().every(button => button.delim || button.pop || button.ins.before));
const relation = guide.symbols.rel.sections.flatMap(section => section.glyphs || []).map(entry => entry.cmd);
for (const command of ['<', '>', '\\le', '\\ge', '\\leqslant', '\\geqslant', '\\ll', '\\gg', '\\neq', '\\approx']) {
  assert.ok(relation.includes(command), command);
}

// Math context: $…$, $$…$$, \(…\), \[…\] and escaped dollars.
assert.equal(guide.mathContextAt('求 $x', 4), true);
assert.equal(guide.mathContextAt('求 $x$ 后', 6), false);
assert.equal(guide.mathContextAt('$$\\sum', 6), true);
assert.equal(guide.mathContextAt('$$a$$ b', 7), false);
assert.equal(guide.mathContextAt('\\(a', 3), true);
assert.equal(guide.mathContextAt('\\[a\\] b', 7), false);
assert.equal(guide.mathContextAt('价格 \\$5', 6), false);

// Outside math the default wrap is $…$; long templates use $$…$$; inside math nothing is added.
assert.deepEqual(
  JSON.parse(JSON.stringify(guide.planInsertion('题目 ', 3, {before: '\\frac{', after: '}{}', ph: 'a'}))),
  {before: '$\\frac{', after: '}{}$', placeholder: 'a', useSelection: true, selectContent: true},
);
assert.equal(guide.planInsertion('题目 ', 3, {before: '\\sum', block: true}).before, '$$\\sum');
assert.equal(guide.planInsertion('题目 ', 3, {before: '\\sum', block: true}).selectContent, false);
assert.equal(guide.planInsertion('$x', 2, {before: '\\le'}).before, '\\le');
assert.equal(guide.planInsertion('', 0, {ph: 'x', wrap: 'display'}, 'paren').before, '\\[');
assert.equal(guide.planInsertion('', 0, {ph: 'x'}, 'paren').after, '\\)');
assert.deepEqual(JSON.parse(JSON.stringify(guide.commandInsertion('$a', 2, '\\alpha'))), {before: '\\alpha '});
assert.deepEqual(JSON.parse(JSON.stringify(guide.commandInsertion('a', 1, '\\alpha'))), {before: '\\alpha'});
assert.deepEqual(JSON.parse(JSON.stringify(guide.commandInsertion('$a', 2, '<'))), {before: '<'});

const matrix = guide.matrixInsertion(2, 3, 'vmatrix');
assert.equal(matrix.before, '\\begin{vmatrix}\n');
assert.equal(matrix.after, ' &  & ' + ' \\\\\n' + ' &  & \n\\end{vmatrix}');
assert.equal(matrix.caret, 'start');
assert.equal(guide.planInsertion('', 0, matrix).before, '$$\\begin{vmatrix}\n');
assert.equal(guide.matrixInsertion(0, 99, 'bad').before, '\\begin{pmatrix}\n');

// The editor helper applies a plan exactly, including the empty first matrix cell.
const cell = new ContractTextarea('A=');
cell.selectionStart = cell.selectionEnd = 2;
context.window.Cuoti.ui.editor.insertAtSelection(cell, {...guide.planInsertion(cell.value, 2, guide.matrixInsertion(1, 2)), preventScroll: true});
assert.equal(cell.value, 'A=$$\\begin{pmatrix}\n & \n\\end{pmatrix}$$');
assert.equal(cell.selectionStart, cell.selectionEnd);
assert.equal(cell.selectionStart, 'A=$$\\begin{pmatrix}\n'.length);

assert.deepEqual(Array.from(guide.filterCatalog({query: '柯西乘积'}), item => item.id), ['cauchy-product']);
assert.deepEqual(Array.from(guide.filterCatalog({category: '最近使用', recent: ['ratio-test', 'missing', 'frac']}), item => item.id), ['ratio-test', 'frac']);
assert.equal(guide.filterCatalog({category: '级数'}).length, 25);
assert.equal(guide.filterCatalog({}).length, guide.catalog.length);
assert.ok(guide.filterSymbols('alpha').some(group => group.glyphs.some(entry => entry.cmd === '\\alpha')));
assert.ok(guide.filterSymbols('≤').some(group => group.glyphs.some(entry => entry.cmd === '\\le')));
assert.ok(guide.filterSymbols('希腊字母').length >= 2);
assert.equal(guide.filterSymbols('  ').length, 0);

process.stdout.write('DAILY_LATEX_CONTRACT_OK\n');
