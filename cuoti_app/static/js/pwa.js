(function(){
  const banner=document.getElementById('offline-shell-banner'),updateBanner=document.getElementById('pwa-update-banner'),updateButton=document.getElementById('pwa-update-refresh'),dismissButton=document.getElementById('pwa-update-dismiss');
  let waitingWorker=null;
  function syncConnectivity(){const offline=!navigator.onLine;document.body.classList.toggle('offline-shell',offline);document.body.classList.toggle('has-shell-notice',offline);if(banner)banner.hidden=!offline}
  window.addEventListener('online',()=>{syncConnectivity();location.reload()});window.addEventListener('offline',syncConnectivity);syncConnectivity();
  document.addEventListener('click',event=>{if(!navigator.onLine&&event.target.closest('.main button,.main input,.main select,.main textarea')){event.preventDefault();event.stopPropagation()}},true);
  if(!('serviceWorker' in navigator)||!window.isSecureContext)return;
  navigator.serviceWorker.addEventListener('controllerchange',()=>location.reload());
  navigator.serviceWorker.register('/sw.js',{scope:'/'}).then(registration=>{
    const announce=worker=>{waitingWorker=worker;if(updateBanner){updateBanner.hidden=false;document.body.classList.add('has-pwa-update')}};
    if(registration.waiting)announce(registration.waiting);
    registration.addEventListener('updatefound',()=>{const worker=registration.installing;worker?.addEventListener('statechange',()=>{if(worker.state==='installed'&&navigator.serviceWorker.controller)announce(worker)})});
  }).catch(error=>console.warn('Service worker registration failed:',error.message));
  updateButton?.addEventListener('click',()=>waitingWorker?.postMessage({type:'ACTIVATE_UPDATE'}));
  dismissButton?.addEventListener('click',()=>{if(updateBanner){updateBanner.hidden=true;document.body.classList.remove('has-pwa-update')}});
})();
