(function initDevelopmentPerformanceObserver(){
  'use strict';
  if(!('PerformanceObserver' in window))return;
  const samples=[];
  const keep=entry=>{
    samples.push(Object.freeze({
      type:String(entry.entryType||''),
      name:entry.entryType==='navigation'?'navigation':String(entry.name||'').slice(0,80),
      duration:Math.round(Number(entry.duration||0)*1000)/1000,
      startTime:Math.round(Number(entry.startTime||0)*1000)/1000
    }));
    if(samples.length>200)samples.splice(0,samples.length-200);
  };
  try{
    const observer=new PerformanceObserver(list=>list.getEntries().forEach(keep));
    observer.observe({entryTypes:['navigation','paint','largest-contentful-paint','longtask']});
    window.Cuoti=window.Cuoti||{};
    window.Cuoti.performance=Object.freeze({
      snapshot:()=>samples.map(sample=>({...sample})),
      clear:()=>{samples.length=0},
      disconnect:()=>observer.disconnect()
    });
  }catch(error){
    // Unsupported entry types are allowed to fail silently in development.
  }
})();
