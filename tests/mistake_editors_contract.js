'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const item = {
  id: 7,
  public_id: 'public-7',
  revision: 2,
  note: 'old note',
  subject_id: 3,
  chapter_id: 5,
  chapter: '旧章节',
};
const input = {
  value: '',
  dataset: {},
  dispatchEvent() {},
};
const status = {className: '', textContent: ''};
const conflict = {hidden: false, dataset: {}};
const buttons = [{disabled: false}, {disabled: false}];
const noteText = {textContent: 'old note'};
const display = {
  hidden: false,
  querySelector(selector) {
    assert.equal(selector, '.mc-note-text');
    return noteText;
  },
};
const editor = {
  hidden: false,
  querySelector(selector) {
    return {
      textarea: input,
      '.mc-note-save-status': status,
      '.mc-note-conflict': conflict,
    }[selector] || null;
  },
  querySelectorAll(selector) {
    assert.equal(selector, 'button');
    return buttons;
  },
};
const toggle = {
  title: '',
  attributes: {},
  setAttribute(name, value) {
    this.attributes[name] = value;
  },
};
const chapterOptions = [];
const chapterSelect = {
  value: '',
  dataset: {},
  disabled: true,
  replaceChildren(option) {
    chapterOptions.length = 0;
    chapterOptions.push(option);
  },
  add(option) {
    chapterOptions.push(option);
  },
  focus() {},
};
const chapterSave = {disabled: true};
const chapterCancel = {disabled: false};
const chapterStatus = {className: '', textContent: ''};
const chapterEditor = {
  id: 'mistake-chapter-editor-7',
  hidden: true,
  querySelector(selector) {
    return {
      '.mc-chapter-select': chapterSelect,
      '.js-chapter-save': chapterSave,
      '.mc-chapter-save-status': chapterStatus,
    }[selector] || null;
  },
  querySelectorAll(selector) {
    assert.equal(selector, 'select,button');
    return [chapterSelect, chapterSave, chapterCancel];
  },
};
const chapterToggle = {
  attributes: {},
  querySelector(selector) {
    assert.equal(selector, '.mc-chapter-label');
    return chapterLabel;
  },
  setAttribute(name, value) {
    this.attributes[name] = value;
  },
  focus() {},
};
const chapterLabel = {textContent: '旧章节'};
const mathCalls = [];
const chapterCalls = [];
let reloads = 0;
const updateResults = [
  {note: '先令 $f\'(x)=0$', revision: 3},
  {note: '', revision: 4},
];

const context = {
  window: {
    Cuoti: {
      mistakes: {
        state: {items: [item]},
        api: {
          async updateNote() {
            return updateResults.shift();
          },
          async chapters(subjectId) {
            chapterCalls.push(['chapters', subjectId]);
            return [{id: 5, name: '旧章节'}, {id: 6, name: '新章节'}];
          },
          async updateChapter(publicId, revision, chapterId) {
            chapterCalls.push(['update', publicId, revision, chapterId]);
            return {chapter_id: chapterId, chapter: '新章节', revision: 5};
          },
        },
        controller: {
          async load() {
            reloads += 1;
          },
        },
      },
    },
  },
  document: {
    getElementById(id) {
      if (id === 'mistake-note-editor-7') return editor;
      if (id === 'mistake-note-display-7') return display;
      if (id === 'mistake-chapter-editor-7') return chapterEditor;
      return null;
    },
    querySelector(selector) {
      if (selector === '.js-note-toggle[data-id="7"]') return toggle;
      if (selector === '.js-chapter-toggle[data-id="7"]') return chapterToggle;
      return null;
    },
    querySelectorAll(selector) {
      assert.equal(selector, '.mc-chapter-editor');
      return [chapterEditor];
    },
  },
  Option: class Option {
    constructor(text, value) {
      this.text = text;
      this.value = value;
    }
  },
  Event: class Event {},
  safeInteger(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) ? parsed : fallback;
  },
  renderMathNodes(target, options) {
    mathCalls.push({target, options});
    return Promise.resolve(true);
  },
  showToast() {},
};
context.window.window = context.window;
vm.createContext(context);
const source = fs.readFileSync(
  path.join(root, 'cuoti_app', 'static', 'js', 'mistakes', 'editors.js'),
  'utf8',
);
vm.runInContext(source, context, {filename: 'mistakes/editors.js'});

(async () => {
  const editors = context.window.Cuoti.mistakes.editors;

  input.value = '先令 $f\'(x)=0$';
  await editors.saveNote(7);
  assert.equal(noteText.textContent, '先令 $f\'(x)=0$');
  assert.equal(display.hidden, false);
  assert.equal(editor.hidden, true);
  assert.equal(mathCalls.length, 1);
  assert.equal(mathCalls[0].target, noteText);
  assert.equal(mathCalls[0].options.force, true);

  input.value = '';
  await editors.saveNote(7);
  assert.equal(noteText.textContent, '');
  assert.equal(display.hidden, true);
  assert.equal(mathCalls.length, 1);

  await editors.toggleChapter(7, chapterToggle);
  assert.equal(chapterEditor.hidden, false);
  assert.equal(chapterSelect.disabled, false);
  assert.equal(chapterSelect.value, '5');
  assert.deepEqual(chapterOptions.map(option => option.text), ['未归类', '旧章节', '新章节']);
  assert.deepEqual(chapterCalls[0], ['chapters', 3]);

  chapterSelect.value = '6';
  await editors.saveChapter(7);
  assert.deepEqual(chapterCalls[1], ['update', 'public-7', 4, 6]);
  assert.equal(item.chapter_id, 6);
  assert.equal(item.chapter, '新章节');
  assert.equal(item.revision, 5);
  assert.equal(chapterLabel.textContent, '新章节');
  assert.equal(chapterEditor.hidden, true);
  assert.equal(reloads, 1);

  process.stdout.write('MISTAKE_EDITORS_CONTRACT_OK\n');
})().catch(error => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
