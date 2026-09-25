(function initSettingsState(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.settings=window.Cuoti.settings||{};
  const dirtyFields=new Set();

  window.Cuoti.settings.state=Object.freeze({dirtyFields});
})();
