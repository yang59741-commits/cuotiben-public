'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const listeners = {};
const storage = new Map([['cuoti.mathPreview.i-content', '1']]);

class ContractElement {
  constructor(selector) {
    this.selector = selector;
    this.dataset = {};
  }
  matches(selector) { return selector === this.selector; }
}

class ContractPane extends ContractElement {
  constructor(id) {
    super('.math-live-preview[data-preview-for]');
    this.dataset.previewFor = id;
    this.bear = new ContractElement('.math-live-preview-bear');
    this.open = false;
  }
  querySelector(selector) {
    return selector === '.math-live-preview-bear' ? this.bear : null;
  }
}

const remembered = new ContractPane('i-content');
const collapsed = new ContractPane('s-note');
const context = {
  window: {Cuoti: {ui: {}}},
  document: {
    readyState: 'complete',
    querySelectorAll(selector) {
      return selector === '.math-live-preview[data-preview-for]' ? [remembered, collapsed] : [];
    },
    querySelector() { return null; },
    getElementById() { return null; },
    addEventListener(type, listener) { listeners[type] = listener; },
  },
  localStorage: {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { storage.set(key, String(value)); },
  },
  CSS: {escape: value => String(value)},
  setTimeout,
  clearTimeout,
};
context.window.window = context.window;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(path.join(root, 'cuoti_app', 'static', 'js', 'ui', 'editor.js'), 'utf8'),
  context,
  {filename: 'ui/editor.js'},
);

function toggle(pane, open) {
  if (open !== undefined) pane.open = open;
  listeners.toggle({target: pane});
}

function animationEnd(pane, animationName) {
  listeners.animationend({target: pane.bear, animationName});
}

// A remembered open preview comes back awake without replaying the wake-up.
assert.equal(remembered.open, true);
assert.equal(collapsed.open, false);
toggle(remembered);
assert.equal(remembered.bear.dataset.motion, undefined);
assert.equal(remembered.dataset.previewRestoring, undefined);

// Collapsing dozes the bear off; only its own frame animation clears the motion.
toggle(remembered, false);
assert.equal(remembered.bear.dataset.motion, 'doze');
assert.equal(storage.get('cuoti.mathPreview.i-content'), '0');
animationEnd(remembered, 'previewBearPop');
assert.equal(remembered.bear.dataset.motion, 'doze');
animationEnd(remembered, 'previewBearDoze');
assert.equal(remembered.bear.dataset.motion, undefined);

// Opening wakes it up; toggling again mid-way switches straight to the new motion.
toggle(collapsed, true);
assert.equal(collapsed.bear.dataset.motion, 'wake');
assert.equal(storage.get('cuoti.mathPreview.s-note'), '1');
toggle(collapsed, false);
assert.equal(collapsed.bear.dataset.motion, 'doze');
animationEnd(collapsed, 'previewBearWake');
assert.equal(collapsed.bear.dataset.motion, 'doze');

console.log('LIVE_PREVIEW_BEAR_CONTRACT_OK');
