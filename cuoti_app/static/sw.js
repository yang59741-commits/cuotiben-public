const BUILD_ID='cuoti-shell-__CUOTI_BUILD_ID__';
const SHELL_ASSETS=[
  '/',
  '/static/css/app.css',
  '/static/css/tokens.css',
  '/static/css/layout/base.css',
  '/static/css/components/surfaces.css',
  '/static/css/pages/stats.css',
  '/static/css/components/controls.css',
  '/static/css/pages/exam.css',
  '/static/css/components/overlays.css',
  '/static/css/pages/settings.css',
  '/static/css/components/jobs.css',
  '/static/css/layout/responsive.css',
  '/static/css/pages/review.css',
  '/static/css/pages/mistakes.css',
  '/static/js/icons.js',
  '/static/js/ui/render.js',
  '/static/js/ui/selects.js',
  '/static/js/ui/editor.js',
  '/static/js/feature-loader.js',
  '/static/js/core.js',
  '/static/js/daily.js',
  '/static/js/jobs.js',
  '/static/js/remote.js',
  '/static/js/pwa.js',
  '/static/img/logo.png',
  '/static/img/pwa-192.png',
  '/static/img/pwa-512.png',
  '/static/img/pwa-maskable-512.png',
  '/static/manifest.webmanifest'
];
const LAZY_ASSETS=new Set([
  '/static/js/latex-guide.js',
  '/static/js/mistakes/state.js',
  '/static/js/mistakes/api.js',
  '/static/js/mistakes/render.js',
  '/static/js/mistakes/editors.js',
  '/static/js/mistakes/exports.js',
  '/static/js/mistakes.js',
  '/static/js/daily-analysis/state.js',
  '/static/js/daily-analysis/api.js',
  '/static/js/daily-analysis/render.js',
  '/static/js/daily-analysis/controller.js',
  '/static/js/daily-analysis.js',
  '/static/js/stats.js',
  '/static/js/review/state.js',
  '/static/js/review/evidence.js',
  '/static/js/review/api.js',
  '/static/js/review/render.js',
  '/static/js/review/controller.js',
  '/static/js/review.js',
  '/static/js/daily-review/state.js',
  '/static/js/daily-review/api.js',
  '/static/js/daily-review/render.js',
  '/static/js/daily-review/controller.js',
  '/static/js/daily-review.js',
  '/static/js/data.js',
  '/static/js/ui/async.js',
  '/static/js/exam/state.js',
  '/static/js/exam/api.js',
  '/static/js/exam/selection.js',
  '/static/js/exam/history.js',
  '/static/js/exam/preview.js',
  '/static/js/exam/attempts.js',
  '/static/js/exam/controller.js',
  '/static/js/exam.js',
  '/static/js/library.js',
  '/static/js/settings/state.js',
  '/static/js/settings/api.js',
  '/static/js/settings/render.js',
  '/static/js/settings.js'
]);

async function precacheShell(){
  const cache=await caches.open(BUILD_ID);
  await Promise.all(SHELL_ASSETS.map(async asset=>{
    const url=new URL(asset,self.location.origin);
    url.searchParams.set('build',BUILD_ID);
    const response=await fetch(new Request(url,{cache:'reload'}));
    if(!response.ok)throw new Error(`无法缓存应用资源：${asset}`);
    await cache.put(asset,response);
  }));
}
self.addEventListener('install',event=>event.waitUntil(precacheShell()));
self.addEventListener('message',event=>{
  if(event.data?.type==='ACTIVATE_UPDATE')self.skipWaiting();
});
self.addEventListener('activate',event=>{
  event.waitUntil(
    caches.keys()
      .then(keys=>Promise.all(
        keys.filter(key=>key.startsWith('cuoti-shell-')&&key!==BUILD_ID)
          .map(key=>caches.delete(key))
      ))
      .then(()=>self.clients.claim())
  );
});

async function currentBuildAsset(request,{fresh=false}={}){
  const cache=await caches.open(BUILD_ID);
  const cached=await cache.match(request);
  if(cached)return cached;
  const response=await fetch(fresh?new Request(request,{cache:'no-cache'}):request);
  if(response.ok)await cache.put(request,response.clone());
  return response;
}

self.addEventListener('fetch',event=>{
  const request=event.request;
  if(request.method!=='GET'){
    event.respondWith(fetch(request));
    return;
  }
  const url=new URL(request.url);
  if(url.origin!==self.location.origin)return;
  if(url.pathname.startsWith('/api/')||url.pathname.endsWith('.pdf')){
    event.respondWith(fetch(request));
    return;
  }
  if(request.mode==='navigate'){
    event.respondWith(
      fetch(request)
        .then(async response=>{
          const cache=await caches.open(BUILD_ID);
          await cache.put('/',response.clone());
          return response;
        })
        .catch(()=>caches.match('/'))
    );
    return;
  }
  if(LAZY_ASSETS.has(url.pathname)){
    event.respondWith(currentBuildAsset(request,{fresh:true}));
    return;
  }
  if(url.pathname.startsWith('/static/'))event.respondWith(currentBuildAsset(request));
});
