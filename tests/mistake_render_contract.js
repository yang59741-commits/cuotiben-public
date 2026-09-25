'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const context = {
  window: {Cuoti: {mistakes: {}}},
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
  icon(name) {
    return `<svg data-test-icon="${name}"></svg>`;
  },
  problemPreviewHTML(content, variant = '') {
    return `<div class="math problem-preview ${variant}">${context.escapeHTML(content)}</div>`;
  },
};
context.window.window = context.window;
vm.createContext(context);
const source = fs.readFileSync(
  path.join(root, 'cuoti_app', 'static', 'js', 'mistakes', 'render.js'),
  'utf8',
);
vm.runInContext(source, context, {filename: 'mistakes/render.js'});

const renderer = context.window.Cuoti.mistakes.render;
assert.deepEqual(Array.from(renderer.reasons('计算错误, 概念不清')), ['计算错误', '概念不清']);
assert.match(renderer.truncate('第一行\n' + '长'.repeat(400)), /第一行\n…（已截断）$/);

const item = {
  id: 7,
  public_id: 'public-7',
  subject_id: 3,
  chapter_id: 5,
  date_added: '2026-08-09',
  subject: '<数学分析>',
  chapter: '<极限>',
  qnum: '12<script>',
  content: '<script>bad()</script>',
  note: '<img src=x> 先令 $f(x)=0$，再求 \\(x+1\\)',
  reasons: ['计算错误'],
  status: '未掌握',
};
const html = renderer.list([item], {
  selectedPublicIds: new Set(['public-7']),
  reasonOptions: ['计算错误', '概念不清'],
});
assert.match(html, /class="mistake-card selected"/);
assert.match(html, /&lt;数学分析&gt;/);
assert.match(html, /&lt;极限&gt;/);
assert.match(html, /class="mc-chapter js-chapter-toggle"/);
assert.match(html, /aria-controls="mistake-chapter-editor-7"/);
assert.match(html, /class="mc-chapter-editor" id="mistake-chapter-editor-7" hidden/);
const unclassified = renderer.card({...item, chapter: null, chapter_id: null}, {
  selectedPublicIds: new Set(),
  reasonOptions: [],
});
assert.match(unclassified, />未归类</);
assert.match(html, /12&lt;script&gt;/);
assert.match(html, /&lt;img src=x&gt;/);
assert.match(html, /class="mc-note-text math"/);
assert.match(html, /\$f\(x\)=0\$/);
assert.match(html, /\\\(x\+1\\\)/);
assert.doesNotMatch(html, /<script>/);
assert.match(html, /data-reason="计算错误" aria-pressed="true"/);
assert.match(html, /data-reason="概念不清" aria-pressed="false"/);
assert.match(html, /js-analyze-day/);

process.stdout.write('MISTAKE_RENDER_CONTRACT_OK\n');
