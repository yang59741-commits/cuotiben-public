'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const context = {
  window: {Cuoti: {dailyReview: {}}},
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
  emptyStateHTML(title) {
    return `<div class="empty-state">${context.escapeHTML(title)}</div>`;
  },
};
context.window.window = context.window;
vm.createContext(context);
const source = fs.readFileSync(
  path.join(root, 'cuoti_app', 'static', 'js', 'daily-review', 'render.js'),
  'utf8',
);
vm.runInContext(source, context, {filename: 'daily-review/render.js'});
const render = context.window.Cuoti.dailyReview.render;

// Stored result + assistance map to four explicit outcomes; legacy unknown stays distinct.
assert.equal(render.outcome(null, 'unknown'), 'pending');
assert.equal(render.outcome('correct', 'none'), 'solo');
assert.equal(render.outcome('correct', 'hint'), 'hint');
assert.equal(render.outcome('correct', 'unknown'), 'correct_unknown');
assert.equal(render.outcome('again', 'hint'), 'again');
assert.equal(render.outcome('incomplete', 'none'), 'incomplete');
assert.deepEqual(Array.from(render.CHOICES), ['solo', 'hint', 'again', 'incomplete']);

const pending = render.normalize({
  id: 11, round_id: 4, subject_id: 2, chapter_id: 9, public_id: 'p-11',
  subject: '<数学分析>', chapter: '极限', qnum: '3<script>',
  content: '<b>bad</b> $x$', note: '先看条件', reasons: '方法不会,计算错误',
  classification: {status: 'manual', type_name: '中值定理', knowledge_points: ['Rolle'], methods: []},
});
assert.equal(pending.subjectId, 2);
assert.equal(pending.chapterId, 9);
const pendingHTML = render.card(pending, {index: 2, current: true, hintOpen: false, hintUsed: false});
assert.match(pendingHTML, /class="daily-q is-pending is-current"/);
assert.match(pendingHTML, /data-daily-review-item="11"/);
assert.doesNotMatch(pendingHTML, /<script>|<b>bad/);
for (const choice of ['solo', 'hint', 'again', 'incomplete']) {
  assert.match(pendingHTML, new RegExp(`js-daily-review-result" data-item-id="11" data-result="${choice}"`));
}
assert.doesNotMatch(pendingHTML, /data-evidence-key|借助提示<\/label>/, 'no hidden assistance checkbox');
assert.match(pendingHTML, /id="daily-hints-11" hidden/);
assert.match(pendingHTML, /aria-controls="daily-hints-11"/);
assert.match(pendingHTML, /class="daily-viewed-note" hidden/);

// After hints were viewed, "提示后答对" is suggested instead of silently changing a label.
const viewedHTML = render.card(pending, {index: 2, current: true, hintOpen: true, hintUsed: true});
assert.match(viewedHTML, /daily-rec-btn js-daily-review-result is-suggested" data-item-id="11" data-result="hint"/);
assert.match(viewedHTML, /daily-rec-btn js-daily-review-result is-muted" data-item-id="11" data-result="solo"/);
assert.match(viewedHTML, />独立答对</);
assert.doesNotMatch(viewedHTML, /id="daily-hints-11" hidden/);

// Queued (undo window) and recorded items collapse to one row.
const queuedHTML = render.card(pending, {index: 2, queued: {choice: 'again'}});
assert.match(queuedHTML, /class="daily-q is-done is-queued"/);
assert.match(queuedHTML, /即将记录：再次答错/);
const done = render.normalize({...pending, id: 12, completion_result: 'correct', assistance: 'hint'});
const doneHTML = render.card(done, {index: 3});
assert.match(doneHTML, /data-outcome="hint"/);
assert.match(doneHTML, /id="daily-q-detail-12" hidden/);
assert.doesNotMatch(doneHTML, /js-daily-review-result/);
assert.match(render.card(done, {index: 3, expanded: true}), /aria-expanded="true"/);

// Progress and chips count every outcome; empty filters stay disabled.
const counts = {total: 5, pending: 2, solo: 1, hint: 1, correct_unknown: 0, again: 1, incomplete: 0};
const progress = render.progress(counts);
assert.match(progress, /3<small>\/ 5 已记录<\/small>/);
assert.match(progress, /daily-seg-solo" style="flex-grow:1"/);
assert.doesNotMatch(progress, /daily-seg-correct_unknown/);
const chips = render.chips(counts, 'pending');
assert.match(chips, /data-daily-chip="pending" aria-pressed="true">待记录<b>2<\/b>/);
assert.match(chips, /data-daily-chip="incomplete" aria-pressed="false" disabled>/);
assert.equal(render.chips({total: 0}, 'all'), '');
assert.match(render.finish({...counts, pending: 0, again: 1}), /data-daily-open="generate"/);

// Empty batch offers the generation drawer rather than a dead end.
assert.match(render.list([], () => ({}), {hasSession: false}), /data-daily-open="generate"/);
assert.match(render.list([], () => ({}), {hasSession: true}), /这个筛选下没有题目/);
const grouped = render.list([pending, done], item => ({index: item.id}), {grouped: true});
assert.equal((grouped.match(/class="review-type-group"/g) || []).length, 1);

// Remediation and type summary render inside the drawer with escaped content.
const remediation = render.remediationRows([
  {source_item_id: 5, subject: '数学分析', chapter: '<级数>', qnum: '8', due: true, due_on: '2026-09-24', again_count: 2, status: '未掌握', public_id: 'x'},
]);
assert.match(remediation, /data-remediation-id="5" checked/);
assert.doesNotMatch(remediation, /<级数>/);
const summary = render.summaryTable([
  {primary_type_code: 'MA 1', type_name: '<Stolz>', practice_mode: 'mixed', independent_accuracy: 0.5, independent_sample_count: 4, completion_rate: 1, unique_mistake_count: 4, assisted_count: 1, unknown_count: 0, remediation_count: 1, recovered_count: 0},
]);
assert.match(summary, /primary_type_code=MA%201/);
assert.match(summary, /<b>50%<\/b>/);
assert.doesNotMatch(summary, /<Stolz>/);

// Detached historical items retain content/results but expose no write controls.
context.window.Cuoti.classification={inline:()=>{throw new Error('read-only history exposed live classification')}};
for(const result of [null,'again']){
  const archived=render.normalize({id:99,mistake_id:42,read_only:true,content:'历史快照',completion_result:result,classification:{type_name:'原分类'}});
  const html=render.card(archived);
  assert.equal(archived.readOnly,true);
  assert.match(html,/历史只读/);
  assert.match(html,/历史快照/);
  assert.match(html,/原分类/);
  assert.doesNotMatch(html,/js-daily-review-result|data-daily-pick/);
}
console.log('daily review render contract ok');
