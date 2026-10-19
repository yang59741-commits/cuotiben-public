(function initSettingsState(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.settings=window.Cuoti.settings||{};
  const dirtyFields=new Set();

  const edits=new Map();
  window.Cuoti.settings.state={
    active:false,revision:0,dirtyFields,edits,
    markDirty(field){dirtyFields.add(field);edits.set(field,(edits.get(field)||0)+1);}
  };
})();
