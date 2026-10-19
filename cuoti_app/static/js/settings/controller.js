(function initSettingsController(){
'use strict';
/* 设置页生命周期：进入时并行读取各卡片；AI 服务、运行状态和两类索引读完才算页面就绪，
   使用情况和 AI 记忆各自在卡片里显示读取状态，不拖慢就绪。离开时作废进行中的读取，收起弹层并清掉计时器。 */
const settingsPage=window.Cuoti.settings;
const settingsState=settingsPage.state,settingsAPI=settingsPage.api;

async function mount(){
  settingsState.active=true;
  settingsPage.routing.render();
  settingsPage.explainer.mountTriggers(document.getElementById('p-settings')||document);
  settingsPage.shell.mount();
  void settingsPage.diagnostics.loadAIUsage();
  void settingsPage.memory.loadAIMemory();
  await Promise.all([settingsPage.connection.mount(),settingsPage.diagnostics.loadRuntimeHealth(),
    settingsPage.maintenance.loadQuestionBankStatus(),settingsPage.maintenance.loadSemanticIndexStatus()]);
}

/* 某个模块收尾出错时，其余模块照常收尾，计时器最后统一清掉 */
function unmount(){
  settingsState.active=false;settingsState.revision+=1;
  settingsAPI.cancelReads();
  for(const name of ['connection','memory','diagnostics','maintenance','explainer','shell']){
    try{settingsPage[name].unmount()}catch(error){console.error(error)}
  }
  settingsPage.ui.unmount();
}

settingsPage.controller=Object.freeze({mount,unmount,refresh:mount});
})();
