// Statistics page entry; the implementation lives in static/js/stats/ (format → charts → render → controller).
(function registerStatsFeature(){
  'use strict';

  const controller=window.Cuoti.stats.controller;
  window.Cuoti.features.register('stats',Object.freeze({
    mount:controller.mount,
    unmount:controller.unmount,
    refresh:controller.refresh
  }));
})();
