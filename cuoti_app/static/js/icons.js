const ICON_PATHS=Object.freeze({
  plus:'<path d="M12 5v14M5 12h14"/>',
  bookOpen:'<path d="M3 5.5A2.5 2.5 0 0 1 5.5 3H11v16H5.5A2.5 2.5 0 0 0 3 21.5z"/><path d="M21 5.5A2.5 2.5 0 0 0 18.5 3H13v16h5.5a2.5 2.5 0 0 1 2.5 2.5z"/>',
  listChecks:'<path d="m3 6 1.5 1.5L7 5"/><path d="M10 6h11"/><path d="m3 12 1.5 1.5L7 11"/><path d="M10 12h11"/><path d="m3 18 1.5 1.5L7 17"/><path d="M10 18h11"/>',
  filePlus:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M12 12v6M9 15h6"/>',
  refresh:'<path d="M20.5 12a8.5 8.5 0 1 1-2.5-6l2.5 2.5"/><path d="M20.5 3.5v5h-5"/>',
  fileText:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h8M8 9h2"/>',
  chart:'<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M8 17v-4M12.5 17V8M17 17v-6"/>',
  download:'<path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M5 21h14"/>',
  upload:'<path d="M12 21V9m0 0 4 4m-4-4-4 4"/><path d="M5 3h14"/>',
  paperclip:'<path d="m21.4 11.1-9.2 9.1a6 6 0 0 1-8.4-8.4L13 2.6a4 4 0 0 1 5.6 5.6l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/>',
  trash:'<path d="M3 6h18M8 6V4h8v2M19 6l-1 15H6L5 6M10 11v6M14 11v6"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  history:'<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
  clipboard:'<rect width="16" height="18" x="4" y="4" rx="2"/><path d="M9 4V2h6v2M8 9h8M8 13h8M8 17h5"/>',
  layoutGrid:'<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/>',
  circleHelp:'<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 4 2c-1 .7-1.5 1.1-1.5 2M12 17h.01"/>',
  chevronDown:'<path d="m6 9 6 6 6-6"/>',
  sparkle:'<path d="M12 2.8Q13.7 10.3 21.2 12Q13.7 13.7 12 21.2Q10.3 13.7 2.8 12Q10.3 10.3 12 2.8z"/>',
  sparkles:'<path d="m12 3-1.2 3.2L8 7.5l2.8 1.3L12 12l1.2-3.2L16 7.5l-2.8-1.3z"/><path d="m5 13-.8 2.2L2 16l2.2.8L5 19l.8-2.2L8 16l-2.2-.8zM19 13l.7 1.8 1.8.7-1.8.7L19 18l-.7-1.8-1.8-.7 1.8-.7z"/>',
  calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/>',
  message:'<path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/>',
  edit:'<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L8 18l-4 1 1-4Z"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  x:'<path d="M18 6 6 18M6 6l12 12"/>',
  undo:'<path d="M9 7 4 12l5 5"/><path d="M20 17a7 7 0 0 0-7-7H4"/>',
  quote:'<path d="M8 11H4a5 5 0 0 1 5-5v2a3 3 0 0 0-3 3v1h2v6H3v-7M20 11h-4a5 5 0 0 1 5-5v2a3 3 0 0 0-3 3v1h2v6h-5v-7"/>',
  target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  archive:'<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v11h14V9M9 13h6"/>',
  lightbulb:'<path d="M9 18h6M10 22h4"/><path d="M8.5 15.5a6 6 0 1 1 7 0c-.8.6-1.5 1.4-1.5 2.5h-4c0-1.1-.7-1.9-1.5-2.5z"/>',
  bold:'<path d="M6 4h8a4 4 0 0 1 0 8H6z"/><path d="M6 12h9a4 4 0 1 1 0 8H6z"/>',
  italic:'<path d="M10 4h7M7 20h7M14 4 10 20"/>',
  alignLeft:'<path d="M4 6h16M4 10h12M4 14h16M4 18h10"/>',
  brackets:'<path d="M8 3H5v18h3M16 3h3v18h-3"/>',
  code:'<path d="m8 9-3 3 3 3M16 9l3 3-3 3M14 5l-4 14"/>',
  listOrdered:'<path d="M10 6h10M10 12h10M10 18h10"/><path d="M4 4h1v4M4 8h2M4 11a2 2 0 1 1 3.3 1.5L4 15h4M4 18h2a1.5 1.5 0 1 1 0 3H4"/>',
  cloudUpload:'<path d="M16 16l-4-4-4 4M12 12v9"/><path d="M20.4 17.5A5 5 0 0 0 18 8h-1.3A7 7 0 0 0 3.4 10.7 4.5 4.5 0 0 0 5.5 19H8"/>',
  loader:'<path d="M21 12a9 9 0 1 1-6.2-8.6"/>',
  settings:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1z"/>',
  eye:'<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="2.5"/>',
  more:'<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  arrowRight:'<path d="M5 12h14M13 6l6 6-6 6"/>',
  circleAlert:'<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5h.01"/>',
  shieldCheck:'<path d="M12 3 5 6v5.2c0 4.3 2.9 8.1 7 9.8 4.1-1.7 7-5.5 7-9.8V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
  rotateCcw:'<path d="M3.5 12a8.5 8.5 0 1 0 2.8-6.3L3.5 8.2"/><path d="M3.5 3.5v4.7h4.7"/>',
  barsHorizontal:'<rect x="3" y="4" width="13" height="4" rx="1"/><rect x="3" y="10" width="18" height="4" rx="1"/><rect x="3" y="16" width="9" height="4" rx="1"/>',
  pie:'<path d="M20.5 13.5A8.5 8.5 0 1 1 10.5 3.5v10z"/><path d="M14 2.8a8.5 8.5 0 0 1 7.2 7.2H14z"/>',
  circleCheck:'<circle cx="12" cy="12" r="9"/><path d="m8.5 12.2 2.4 2.4 4.6-5"/>',
  circleDashed:'<circle cx="12" cy="12" r="9" stroke-dasharray="2.6 3.05"/>',
  bookmarkPlus:'<path d="M18 21 12 17l-6 4V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2z"/><path d="M12 7v6M9 10h6"/>',
  notebook:'<rect x="5" y="3" width="15" height="18" rx="2"/><path d="M3 8h4M3 16h4M10.5 9h5.5M10.5 13h3.5"/>',
  repeat:'<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>',
  calendarClock:'<path d="M21 10V7a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h6"/><path d="M16 3v4M8 3v4M3 10h8"/><circle cx="17" cy="17" r="4.5"/><path d="M17 15v2l1.3 1"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
  examPaper:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="m7.5 12.5 1.5 1.5 2.5-2.5M14 13h2.5M7.5 17.5h9"/>',
  files:'<path d="M15 2H9a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V7z"/><path d="M15 2v5h5"/><path d="M4 7v12a2 2 0 0 0 2 2h9"/>',
  sliders:'<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  trendLine:'<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="m7 15 4-4 3 3 5.5-6"/>',
  activity:'<path d="M22 12h-4l-3 8L9 4l-3 8H2"/>',
  copy:'<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  database:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  tag:'<path d="M12.6 2.6A2 2 0 0 0 11.2 2H4a2 2 0 0 0-2 2v7.2a2 2 0 0 0 .6 1.4l8.7 8.7a2.4 2.4 0 0 0 3.4 0l6.6-6.6a2.4 2.4 0 0 0 0-3.4z"/><circle cx="7.5" cy="7.5" r="1.3"/>',
  filter:'<path d="M3 5h18l-7 8.5V19l-4 2v-7.5z"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.5h.01"/>',
  triangleAlert:'<path d="M10.3 3.9 2.4 17.5a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4.5M12 17h.01"/>',
  circleX:'<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/>',
  eyeOff:'<path d="m3 3 18 18"/><path d="M10.6 6.1Q11.3 6 12 6c6.5 0 10 6 10 6a17 17 0 0 1-2.7 3.4M6.6 6.6C3.8 8.3 2 12 2 12s3.5 6 10 6c1.9 0 3.5-.5 4.9-1.2"/><path d="M9.9 9.9a2.5 2.5 0 0 0 3.5 3.5"/>',
  variable:'<path d="M8 21s-4-3-4-9 4-9 4-9M16 3s4 3 4 9-4 9-4 9"/><path d="m15 9-6 6M9 9l6 6"/>',
  squareSigma:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M16 9V7.5H8l4 4.5-4 4.5h8V15"/>',
  proof:'<path d="M4 6h16M4 11h16M4 16h9"/><rect x="16" y="14" width="4" height="4" rx=".5"/>',
  alignEquals:'<path d="M12 3.5v17" stroke-dasharray="1.4 2.6"/><path d="M4 7h5M15 7h5M6 12h3M15 12h2.5M3 17h6M15 17h4"/>',
  bell:'<path d="M6 16.5V11a6 6 0 1 1 12 0v5.5l1.5 1.5h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
  table:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M3 14.5h18M9.5 9v11"/>',
  play:'<path d="M8 5.5v13l10.5-6.5z"/>',
  pause:'<path d="M9 5.5v13M15 5.5v13"/>',
  zap:'<path d="M13 2.5 4.5 13.5H11l-1 8 8.5-11H12z"/>',
  key:'<circle cx="7.5" cy="15.5" r="4"/><path d="m10.4 12.6 9.1-9.1M16 6l2.5 2.5M13.5 8.5l2 2"/>',
  globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.6 3.8 5.6 3.8 9s-1.2 6.4-3.8 9c-2.6-2.6-3.8-5.6-3.8-9s1.2-6.4 3.8-9z"/>',
  brain:'<path d="M12 5.5a3 3 0 0 0-5.6-1.4A3 3 0 0 0 4 8.5a3.2 3.2 0 0 0 .4 5.8A3 3 0 0 0 8 19.5a3 3 0 0 0 4 .5z"/><path d="M12 5.5a3 3 0 0 1 5.6-1.4A3 3 0 0 1 20 8.5a3.2 3.2 0 0 1-.4 5.8A3 3 0 0 1 16 19.5a3 3 0 0 1-4 .5z"/><path d="M12 5.5V20"/>',
  route:'<path d="M3 12h5"/><path d="M8 12 14 6h6M8 12l6 6h6"/><path d="m17.5 3.5 2.5 2.5-2.5 2.5M17.5 15.5l2.5 2.5-2.5 2.5"/>',
  gauge:'<path d="M4.5 18a8.5 8.5 0 1 1 15 0"/><path d="m12 13 4-5"/><circle cx="12" cy="13" r="1.2"/>',
  chevronRight:'<path d="m9 6 6 6-6 6"/>',
  minus:'<path d="M5 12h14"/>'
});

const UNKNOWN_ICON_WARNINGS=new Set();

function icon(name,className=''){
  let paths=Object.prototype.hasOwnProperty.call(ICON_PATHS,name)?ICON_PATHS[name]:'';
  if(!paths){
    if(!UNKNOWN_ICON_WARNINGS.has(name)){
      UNKNOWN_ICON_WARNINGS.add(name);
      console.warn(`Unknown icon "${name}"; showing circleHelp instead. Add it to ICON_PATHS in icons.js.`);
    }
    paths=ICON_PATHS.circleHelp;
  }
  const classes=['icon',className].filter(Boolean).join(' ');
  return `<svg class="${classes}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths}</svg>`;
}

function mountIcons(root=document){
  root.querySelectorAll('[data-icon]').forEach(slot=>{
    slot.innerHTML=icon(slot.dataset.icon,slot.dataset.iconClass||'');
    slot.classList.add('icon-slot');
  });
}

window.icon=icon;
window.mountIcons=mountIcons;
document.addEventListener('DOMContentLoaded',()=>mountIcons());
