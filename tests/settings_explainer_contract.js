'use strict';

/* 设置页动画讲解的契约：脚本数据、代码绘制的界面演示和播放器时间轴。
   每个场景按 20ms 步长画完整条时间轴，检查：同一时刻画出同一帧；没有 NaN；颜色只引用已定义的设计变量；
   文字不小于 20；说明标注每行不超过 14 个字、最多两行，完整显示足够久，同时出现的标注互不遮挡；
   鼠标指针停稳了才点击；“减少动态效果”逐步查看的静帧停在标注完整显示、没有过渡的一刻；用到的图标都在图标库里。 */
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const staticPath = (...parts) => path.join(root, 'cuoti_app', 'static', ...parts);
const read = file => fs.readFileSync(file, 'utf8');

const warnings = [];
const context = {
  window: {Cuoti: {settings: {ui: {}}}},
  document: {addEventListener() {}},
  console: {log: console.log, error: console.error, warn: (...args) => warnings.push(args.join(' '))},
};
context.window.window = context.window;
vm.createContext(context);
const run = file => vm.runInContext(read(file), context, {filename: path.relative(root, file)});

run(staticPath('js', 'icons.js'));
run(staticPath('js', 'settings', 'explainer-scripts.js'));
run(staticPath('js', 'settings', 'explainer-player.js'));

const settingsPage = context.window.Cuoti.settings;
const scripts = settingsPage.explainerScripts;
const player = settingsPage.explainer;
const ids = Object.keys(scripts);
assert.deepEqual(ids, ['routing', 'usage', 'memory', 'question-bank', 'semantic', 'health', 'backup']);

// 场景注册经过播放器，同时留一份绘制函数给本契约逐帧检查；标注表和指针路线在第一次绘制时记下
const draws = new Map();
settingsPage.explainer = {...player, defineScene(id, draw) { draws.set(id, draw); player.defineScene(id, draw); }};
run(staticPath('js', 'settings', 'explainers', 'stage.js'));
const stage = settingsPage.explainerStage;
for (const name of ['bear', 'guide', 'bubble', 'GUIDE']) {
  assert.ok(!(name in stage), `stage.${name}: the explainers no longer draw a mascot`);
}
const noteLists = new Map();
const pointerPlans = new Map();
let drawing = '';
settingsPage.explainerStage = {...stage,
  notes(t, list) {
    if (!noteLists.has(drawing)) noteLists.set(drawing, list);
    return stage.notes(t, list);
  },
  pointer(t, keys, clicks, options) {
    if (!pointerPlans.has(drawing)) pointerPlans.set(drawing, {keys, clicks: clicks || []});
    return stage.pointer(t, keys, clicks, options);
  }};
for (const id of ids) run(staticPath('js', 'settings', 'explainers', `${id}.js`));

const iconKnown = name => {
  const before = warnings.length;
  context.window.icon(name);
  return warnings.length === before;
};

/* ── 脚本数据 ── */
for (const id of ids) {
  const script = scripts[id];
  assert.equal(script.id, id);
  assert.ok(Object.isFrozen(script) && Object.isFrozen(script.steps), `${id}: script data must be frozen`);
  assert.ok(script.title && script.question, `${id}: title and question`);
  assert.ok(iconKnown(script.icon), `${id}: unknown icon ${script.icon}`);
  assert.ok(script.duration >= 10000 && script.duration <= 16000, `${id}: about ten-odd seconds`);
  assert.ok(script.steps.length >= 3 && script.steps.length <= 5, `${id}: 3-5 steps`);
  assert.equal(script.steps[0].at, 0, `${id}: first step starts at 0`);
  script.steps.forEach((step, index) => {
    const end = index + 1 < script.steps.length ? script.steps[index + 1].at : script.duration;
    assert.ok(step.at < end, `${id}: steps must increase`);
    assert.ok(end - step.at >= 2000, `${id}: step ${index + 1} needs time to read`);
    assert.ok(step.label && step.caption && step.scene, `${id}: step ${index + 1} needs label, caption and scene`);
    assert.ok(iconKnown(step.icon), `${id}: unknown step icon ${step.icon}`);
    assert.ok(Number.isFinite(step.still) && step.still >= step.at && step.still < end, `${id}: step ${index + 1} still inside its step`);
  });
  assert.ok(script.cta && script.cta.label && [].concat(script.cta.target).every(Boolean), `${id}: try-it target`);
  for (const entry of script.glossary || []) assert.ok(entry.length === 2 && entry[0] && entry[1], `${id}: glossary pairs`);
}

/* ── 播放器时间轴 ── */
const {timeline} = player;
for (const id of ids) {
  const script = scripts[id];
  script.steps.forEach((step, index) => {
    assert.equal(timeline.stepIndexAt(script, step.at), index);
    if (index) assert.equal(timeline.stepIndexAt(script, step.at - 1), index - 1);
    assert.equal(timeline.stillTime(script, index), step.still);
  });
  assert.equal(timeline.stepEnd(script, script.steps.length - 1), script.duration);
}
const unauthored = {duration: 4000, steps: [{at: 0}, {at: 3000}]};
assert.equal(timeline.stillTime(unauthored, 0), 700);
assert.equal(timeline.stillTime(unauthored, 1), 3500);
assert.equal(timeline.clock(0), '0:00');
assert.equal(timeline.clock(65999), '1:05');

/* ── 页面入口与离线缓存 ── */
const template = read(path.join(root, 'cuoti_app', 'templates', 'partials', 'pages', 'settings.html'));
const triggers = [...template.matchAll(/data-explainer-open="([^"]+)"/g)].map(match => match[1]);
assert.deepEqual([...triggers].sort(), [...ids].sort(), 'every card except the first has exactly one explainer trigger');
const worker = read(staticPath('sw.js'));
for (const name of ['stage', ...ids]) {
  assert.ok(worker.includes(`'/static/js/settings/explainers/${name}.js'`), `sw.js caches explainers/${name}.js`);
}

/* ── 逐帧检查 ── */
const tokens = new Set([
  ...read(staticPath('css', 'tokens.css')).matchAll(/(--[a-z0-9-]+)\s*:/g),
  ...read(staticPath('css', 'pages', 'settings.css')).matchAll(/(--xp-[a-z0-9-]+)\s*:/g),
].map(match => match[1]));
const PAINT = /^(none|var\(--[a-z0-9-]+\))$/;
const STEP_MS = 20;
const FULL_MS = 600;
const digest = markup => crypto.createHash('sha1').update(markup).digest('hex');

function inspect(markup, where) {
  assert.equal(typeof markup, 'string', `${where}: draw returns markup`);
  assert.ok(!/NaN|undefined|Infinity|\[object /.test(markup), `${where}: invalid value in markup`);
  for (const [, attribute, value] of markup.matchAll(/\s(fill|stroke)="([^"]*)"/g)) {
    assert.match(value, PAINT, `${where}: ${attribute}="${value}" must reference a design token`);
  }
  for (const [, name] of markup.matchAll(/var\((--[a-z0-9-]+)\)/g)) {
    assert.ok(tokens.has(name), `${where}: ${name} is not defined in tokens.css or settings.css`);
  }
  for (const [, value] of markup.matchAll(/\sopacity="([^"]*)"/g)) {
    assert.ok(Number(value) >= 0 && Number(value) <= 1, `${where}: opacity ${value}`);
  }
  for (const [, value] of markup.matchAll(/font-size="([^"]*)"/g)) {
    assert.ok(Number(value) >= 20, `${where}: font-size ${value} is below 20`);
  }
}

/* 文字的实际字号 = 字号 × 外层各组的缩放 */
function smallestText(markup) {
  const scales = [1];
  let smallest = Infinity;
  for (const [, closing, name, attributes, selfClosing] of markup.matchAll(/<(\/?)([a-zA-Z]+)([^>]*?)(\/?)>/g)) {
    if (name !== 'g' && name !== 'text') continue;
    if (closing) { if (name === 'g') scales.pop(); continue; }
    if (name === 'g') {
      const scale = /scale\(([-\d.]+)\)/.exec(attributes);
      if (!selfClosing) scales.push(scales[scales.length - 1] * (scale ? Number(scale[1]) : 1));
      continue;
    }
    smallest = Math.min(smallest, Number(/font-size="([^"]+)"/.exec(attributes)[1]) * scales[scales.length - 1]);
  }
  return smallest;
}

/* 画面里的标注：完整显示 = 没有缩放、没有透明度（弹出完、未开始淡出） */
function noteStates(markup) {
  const states = new Map();
  for (const [, index, attributes] of markup.matchAll(/<g class="xp-callout" data-callout="(\d+)"([^>]*)>/g)) {
    states.set(Number(index), /scale\(|opacity=/.test(attributes) ? 'partial' : 'full');
  }
  return states;
}
const overlaps = (a, b) => a.left < b.left + b.w && b.left < a.left + a.w && a.top < b.top + b.h && b.top < a.top + a.h;

for (const id of ids) {
  const script = scripts[id];
  const draw = draws.get(id);
  assert.equal(typeof draw, 'function', `${id}: scene registered`);
  assert.ok(player.hasScene(id), `${id}: player knows the scene`);
  drawing = id;
  draw(0);

  // 标注表：按时间排；每行不超过 14 个字、最多两行；锚点在画布里
  const notes = noteLists.get(id);
  assert.ok(notes && notes.length >= script.steps.length, `${id}: every step explains itself with callouts`);
  notes.forEach((note, index) => {
    if (index) assert.ok(note.at >= notes[index - 1].at, `${id}: callouts sorted by time`);
    const lines = String(note.text).split('\n');
    assert.ok(lines.length <= 2 && lines.every(line => line && [...line].length <= 14), `${id}: callout "${note.text}" must be 1-2 short lines`);
    assert.ok(['top', 'bottom', 'left', 'right'].includes(note.side ?? 'top'), `${id}: callout side ${note.side}`);
    assert.ok(note.x >= 0 && note.x <= 640 && note.y >= 0 && note.y <= 360, `${id}: callout "${note.text}" anchored outside the canvas`);
    assert.ok(note.at >= 0 && (note.until == null || note.until > note.at), `${id}: callout "${note.text}" timing`);
  });
  // 同时完整显示的标注不能互相遮挡
  const windows = notes.map(note => [note.at + 260, Math.min(note.until ?? Infinity, script.duration)]);
  notes.forEach((note, i) => notes.forEach((other, j) => {
    if (j <= i || windows[i][0] >= windows[j][1] || windows[j][0] >= windows[i][1]) return;
    assert.ok(!overlaps(stage.noteBox(note), stage.noteBox(other)), `${id}: callouts "${note.text}" and "${other.text}" overlap`);
  }));

  // 指针：路线按时间排、停在画布里；每次点击前后都停稳
  const {keys, clicks} = pointerPlans.get(id) || {};
  assert.ok(keys && keys.length && clicks.length, `${id}: the pointer clicks something`);
  keys.forEach((key, index) => {
    if (index) assert.ok(key[0] >= keys[index - 1][0], `${id}: pointer keys sorted by time`);
    assert.ok(key[1] >= 0 && key[1] <= 620 && key[2] >= 0 && key[2] <= 345, `${id}: pointer leaves the canvas at ${key[0]}ms`);
  });
  for (const at of clicks) {
    assert.ok(at > keys[0][0] && at < script.duration, `${id}: click at ${at}ms`);
    const [x, y] = stage.track(at, keys);
    for (const t of [at - 120, at + 100]) {
      const [x1, y1] = stage.track(t, keys);
      assert.ok(Math.hypot(x1 - x, y1 - y) <= 1, `${id}: pointer must rest while clicking at ${at}ms`);
    }
  }

  const hashes = new Map();
  const shown = notes.map(() => 0);
  for (let t = 0; t <= script.duration; t += STEP_MS) {
    const markup = draw(t);
    const where = `${id} @${t}ms`;
    inspect(markup, where);
    hashes.set(t, digest(markup));
    if (t < script.duration) for (const [index, state] of noteStates(markup)) if (state === 'full') shown[index] += STEP_MS;
  }
  notes.forEach((note, index) => {
    assert.ok(shown[index] >= FULL_MS, `${id}: callout "${note.text}" is fully shown only ${shown[index]}ms`);
  });

  // 同一时刻永远画出同一帧：倒序再画一遍对比
  for (const t of [...hashes.keys()].reverse().filter(time => time % 200 === 0)) {
    assert.equal(digest(draw(t)), hashes.get(t), `${id}: frame ${t}ms must be deterministic`);
  }

  // 静帧：至少一条标注完整显示，没有正在弹出或淡出的标注，文字没有被缩小
  script.steps.forEach((step, index) => {
    const markup = draw(step.still);
    const where = `${id} step ${index + 1} still @${step.still}ms`;
    inspect(markup, where);
    const states = [...noteStates(markup).values()];
    assert.ok(states.includes('full'), `${where}: a callout is fully shown`);
    assert.ok(!states.includes('partial'), `${where}: no callout is mid-transition`);
    assert.ok(smallestText(markup) >= 19.5, `${where}: text scaled to ${smallestText(markup).toFixed(1)}`);
  });
  const last = draw(script.duration);
  assert.ok(smallestText(last) >= 19.5, `${id}: final frame text must be at full size`);
}

assert.deepEqual(warnings, [], 'scenes only use icons from icons.js');
console.log('SETTINGS_EXPLAINER_CONTRACT_OK');
