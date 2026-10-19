(function initStatsCharts(){
  'use strict';

  const F=window.Cuoti.stats.format;
  const {count}=F;
  const SERIES=F.OUTCOMES;

  // One shared tooltip; every value is written with textContent.
  let tipElement=null;
  function tipNode(){
    if(!tipElement){
      tipElement=document.createElement('div');
      tipElement.className='stats-tip';
      tipElement.setAttribute('role','tooltip');
      document.body.append(tipElement);
    }
    return tipElement;
  }
  function showTip(model,x,y){
    const element=tipNode();
    element.replaceChildren();
    const add=(className,text)=>{const node=document.createElement('div');node.className=className;node.textContent=text;element.append(node)};
    add('stats-tip-title',model.title);
    if(model.value)add('stats-tip-value',model.value);
    for(const [label,value,color] of model.rows||[]){
      const row=document.createElement('div');row.className='stats-tip-row';
      const key=document.createElement('i');key.style.background=color;
      const name=document.createElement('span');name.textContent=label;
      const number=document.createElement('b');number.textContent=value;
      row.append(key,name,number);element.append(row);
    }
    if(model.foot)add('stats-tip-foot',model.foot);
    element.classList.add('is-on');
    const width=element.offsetWidth,height=element.offsetHeight;
    element.style.left=Math.max(8,Math.min(window.innerWidth-width-8,x-width/2))+'px';
    element.style.top=(y-height-12<8?y+18:y-height-12)+'px';
  }
  function hideTip(){tipElement?.classList.remove('is-on')}

  function topRounded(x,y,width,height,radius){
    if(height<=0)return '';
    const r=Math.min(radius,width/2,height);
    return `M${x.toFixed(1)},${(y+height).toFixed(1)}V${(y+r).toFixed(1)}Q${x.toFixed(1)},${y.toFixed(1)} ${(x+r).toFixed(1)},${y.toFixed(1)}H${(x+width-r).toFixed(1)}Q${(x+width).toFixed(1)},${y.toFixed(1)} ${(x+width).toFixed(1)},${(y+r).toFixed(1)}V${(y+height).toFixed(1)}Z`;
  }
  function sparkline(values,{width=96,height=28,stroke='#c9b39b',last='var(--accent)',gaps=null}={}){
    const points=values.map((value,index)=>value===null||!Number.isFinite(value)?null:[values.length===1?width:index/(values.length-1)*width,value]).filter(Boolean);
    if(points.length<2)return '';
    const numbers=points.map(point=>point[1]),low=Math.min(...numbers),span=Math.max(...numbers)-low||1;
    const xy=points.map(([x,value])=>[x,height-2-(value-low)/span*(height-4)]);
    const lines=xy.slice(1).map((point,index)=>`<line x1="${xy[index][0].toFixed(1)}" y1="${xy[index][1].toFixed(1)}" x2="${point[0].toFixed(1)}" y2="${point[1].toFixed(1)}" stroke="${stroke}" stroke-width="2" stroke-linecap="round"${gaps&&gaps[index]?' opacity=".45"':''}/>`).join('');
    const [lastX,lastY]=xy[xy.length-1];
    return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true" focusable="false">${lines}<circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="4" fill="${last}" stroke="var(--card)" stroke-width="2"/></svg>`;
  }
  function outcomeRows(row){
    return SERIES.map(item=>[item.label,String(count(row[item.key])),item.color]).filter(item=>item[1]!=='0');
  }

  // Study calendar: a planner grid for up to 8 weeks, a contribution graph beyond that.
  function calendar(host,{from,to,today,heat,onActive}){
    const rows=Array.isArray(heat)?heat:[];
    const byDay=new Map(rows.map(row=>[String(row.day),row])),max=Math.max(1,...rows.map(row=>count(row.reviews)));
    const start=F.addDays(from,-((F.parseDay(from).getDay()+6)%7)),end=F.addDays(to,(7-F.parseDay(to).getDay())%7);
    const days=[];for(let day=start;day<=end;day=F.addDays(day,1))days.push(day);
    const weeks=days.length/7,planner=weeks<=8;
    const range=`${F.dayLabel(from)}至${F.dayLabel(to)}`;
    let active=to;
    const model=day=>{
      const row=byDay.get(day);
      return row?{title:F.dayWeekLabel(day),value:`${count(row.reviews)} 次复习`,rows:outcomeRows(row)}:{title:F.dayWeekLabel(day),value:'没有复习',rows:[]};
    };
    if(planner){
      host.innerHTML=`<div class="study-calendar" role="grid" aria-label="学习日历，${escapeHTML(range)}"><div class="study-calendar-head" role="row">${'一二三四五六日'.split('').map(name=>`<span role="columnheader">${name}</span>`).join('')}</div>${
        Array.from({length:weeks},(_,week)=>`<div class="study-calendar-week" role="row">${days.slice(week*7,week*7+7).map(day=>{
          const date=F.parseDay(day),row=byDay.get(day);
          if(day<from||day>to)return `<span class="study-day is-out" aria-hidden="true"><span class="study-day-date">${date.getDate()}</span></span>`;
          const month=date.getDate()===1||day===from?`<span class="study-day-month">${date.getMonth()+1}月</span>`:'';
          return `<span class="study-day${day===today?' is-today':''}" role="gridcell" tabindex="${day===active?0:-1}" data-day="${day}" data-level="${F.heatmapLevel(row?.reviews,max)}" aria-label="${escapeHTML(F.dayWeekLabel(day))}，${row?`复习 ${count(row.reviews)} 次`:'没有复习'}"><span class="study-day-date">${date.getDate()}</span>${month}</span>`;
        }).join('')}</div>`).join('')}</div>`;
    }else{
      const width=Math.max(220,host.clientWidth||600),gap=3,left=20;
      const cell=Math.max(7,Math.min(15,Math.floor((width-left)/weeks)-gap)),pitch=cell+gap;
      let months='',previous='';
      const rects=days.map((day,index)=>{
        const column=Math.floor(index/7),row=index%7,date=F.parseDay(day);
        if(day>=from&&day<=to&&row===0&&String(date.getMonth())!==previous){previous=String(date.getMonth());months+=`<text x="${left+column*pitch}" y="9">${date.getMonth()+1}月</text>`}
        if(day<from||day>to)return '';
        const item=byDay.get(day);
        return `<rect data-day="${day}" x="${left+column*pitch}" y="${16+row*pitch}" width="${cell}" height="${cell}" rx="2" fill="var(--heat-${F.heatmapLevel(item?.reviews,max)})"${day===today?' stroke="var(--accent)" stroke-width="1.5"':''}/>`;
      }).join('');
      const labels=['一','三','五'].map((name,index)=>`<text x="0" y="${16+index*2*pitch+cell-1}">${name}</text>`).join('');
      host.innerHTML=`<svg class="study-graph" tabindex="0" role="img" aria-label="学习日历，${escapeHTML(range)}，共 ${rows.length} 个学习日；方向键逐日查看" width="${left+weeks*pitch}" height="${16+7*pitch}">${months}${labels}${rects}</svg>`;
    }
    const cells=()=>host.querySelectorAll('[data-day]');
    function setActive(day,{focus=false}={}){
      if(day<from||day>to)return;
      active=day;
      cells().forEach(cell=>{const on=cell.dataset.day===day;cell.classList.toggle('is-active',on);if(planner)cell.tabIndex=on?0:-1});
      const element=host.querySelector(`[data-day="${day}"]`);
      if(focus&&planner)element?.focus({preventScroll:true});
      if(element){const box=element.getBoundingClientRect();showTip(model(day),box.left+box.width/2,box.top)}
      onActive?.(day);
    }
    function clear(){cells().forEach(cell=>cell.classList.remove('is-active'));hideTip();onActive?.(null)}
    host.onpointerover=event=>{const cell=event.target.closest?.('[data-day]');if(cell)setActive(cell.dataset.day)};
    host.onpointerleave=clear;
    host.onfocusin=event=>{const cell=event.target.closest?.('[data-day]');setActive(cell?cell.dataset.day:active)};
    host.onfocusout=clear;
    host.onkeydown=event=>{
      const step=planner?{ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7}:{ArrowUp:-1,ArrowDown:1,ArrowLeft:-7,ArrowRight:7};
      if(event.key==='Escape'){clear();return}
      if(!(event.key in step))return;
      event.preventDefault();setActive(F.addDays(active,step[event.key]),{focus:true});
    };
    return {
      planner,
      link(linked){const set=new Set(linked||[]);cells().forEach(cell=>cell.classList.toggle('is-linked',set.has(cell.dataset.day)))},
      table:()=>[['日期','复习','答对','提示后','再练'],...rows.map(row=>[F.dayWeekLabel(row.day),count(row.reviews),count(row.pass),count(row.assisted),count(row.again)])]
    };
  }

  // Review trend: stacked pass/assisted/again bars, manual changes as diamonds under the baseline.
  function trend(host,{rows,granularity,onActive}){
    const list=Array.isArray(rows)?rows:[];
    const width=Math.max(260,host.clientWidth||600),height=214,margin={t:10,r:6,b:46,l:30};
    const plotWidth=width-margin.l-margin.r,plotHeight=height-margin.t-margin.b,total=list.length,slot=plotWidth/Math.max(1,total),barWidth=Math.max(2,Math.min(20,slot*.64));
    const max=F.niceMax(Math.max(1,...list.map(row=>count(row.pass)+count(row.assisted)+count(row.again))));
    const y=value=>margin.t+plotHeight-value/max*plotHeight;
    const grid=[0,.5,1].map(fraction=>`<line x1="${margin.l}" x2="${width-margin.r}" y1="${y(max*fraction).toFixed(1)}" y2="${y(max*fraction).toFixed(1)}"/><text x="${margin.l-6}" y="${(y(max*fraction)+3).toFixed(1)}" text-anchor="end">${max*fraction}</text>`).join('');
    const every=Math.max(1,Math.ceil(total/Math.max(2,Math.floor(plotWidth/54))));
    const tick=row=>granularity==='month'?`${Number(String(row.period).slice(5,7))}月`:F.shortDay(row.period);
    const slots=list.map((row,index)=>{
      const left=margin.l+index*slot,x=left+(slot-barWidth)/2,centre=left+slot/2;
      let base=0;
      const parts=SERIES.map((item,seriesIndex)=>{
        const value=count(row[item.key]);if(!value)return '';
        const top=SERIES.slice(seriesIndex+1).every(next=>!count(row[next.key]));
        const y0=y(base),y1=y(base+value);base+=value;
        const barHeight=Math.max(1,y0-y1-(top?0:2));
        return top?`<path d="${topRounded(x,y1,barWidth,barHeight,Math.min(4,barWidth/2))}" fill="${item.color}"/>`:`<rect x="${x.toFixed(1)}" y="${(y1+2).toFixed(1)}" width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}" fill="${item.color}"/>`;
      }).join('');
      const manual=count(row.manual_changes)?`<rect x="${(centre-3.5).toFixed(1)}" y="${margin.t+plotHeight+7}" width="7" height="7" fill="var(--chart-manual)" transform="rotate(45 ${centre.toFixed(1)} ${margin.t+plotHeight+10.5})"/>`:'';
      const lastLabel=index===total-1&&total-1-Math.floor((total-1)/every)*every>every/2;
      const label=index%every===0||lastLabel?`<text x="${centre.toFixed(1)}" y="${height-12}" text-anchor="middle">${escapeHTML(tick(row))}</text>`:'';
      return `<g class="slot${row.estimated?' is-estimated':''}" data-i="${index}"><rect class="slot-band" x="${left.toFixed(1)}" y="${margin.t}" width="${slot.toFixed(1)}" height="${plotHeight}"/><g class="bars">${parts}</g>${manual}${label}<rect class="hit" x="${left.toFixed(1)}" y="${margin.t}" width="${slot.toFixed(1)}" height="${plotHeight+20}"/></g>`;
    }).join('');
    const sum=list.reduce((value,row)=>value+count(row.reviews),0);
    const best=list.reduce((top,row)=>count(row.reviews)>count(top.reviews)?row:top,list[0]||{reviews:0});
    const bestLabel=best.period?F.periodLabel(best.period,granularity):'—';
    host.innerHTML=`<svg width="${width}" height="${height}" tabindex="0" role="img" aria-label="复习趋势：共 ${sum} 次，最多的是${escapeHTML(bestLabel)}（${count(best.reviews)} 次）。方向键逐期查看。"><g class="grid">${grid}</g><line class="baseline" x1="${margin.l}" x2="${width-margin.r}" y1="${margin.t+plotHeight}" y2="${margin.t+plotHeight}"/>${slots}</svg>`;
    const svg=host.querySelector('svg');
    let active=total-1;
    const model=row=>{
      const sample=count(row.pass)+count(row.again);
      return {
        title:F.periodLabel(row.period,granularity),value:`${count(row.reviews)} 次复习`,
        rows:outcomeRows(row).concat(count(row.manual_changes)?[['手动调整',String(count(row.manual_changes)),'var(--chart-manual)']]:[]),
        foot:[sample?`答对率 ${Math.round(count(row.pass)/sample*100)}%`:'',count(row.first_mastered)?`新掌握 ${count(row.first_mastered)} 道`:'',row.estimated?'含估算历史':''].filter(Boolean).join(' · ')
      };
    };
    function setActive(index){
      if(!total)return;
      active=Math.max(0,Math.min(total-1,index));
      svg.querySelectorAll('.slot').forEach(group=>group.classList.toggle('is-active',Number(group.dataset.i)===active));
      const band=svg.querySelector(`.slot[data-i="${active}"] .slot-band`).getBoundingClientRect();
      showTip(model(list[active]),band.left+band.width/2,band.top+18);
      onActive?.(list[active].period);
    }
    function clear(){svg.querySelectorAll('.slot').forEach(group=>group.classList.remove('is-active'));hideTip();onActive?.(null)}
    svg.onpointerover=event=>{const group=event.target.closest?.('.slot');if(group)setActive(Number(group.dataset.i))};
    svg.onpointerleave=clear;svg.onfocus=()=>setActive(active);svg.onblur=clear;
    svg.onkeydown=event=>{
      if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();setActive(active+(event.key==='ArrowLeft'?-1:1))}
      else if(event.key==='Escape')clear();
    };
    return {
      link(day){const period=day?F.bucketOf(day,granularity):null;svg.querySelectorAll('.slot').forEach(group=>group.classList.toggle('is-linked',Boolean(period)&&String(list[Number(group.dataset.i)].period)===period))},
      table:()=>[['时期','复习','答对','提示后','再练','手动调整','新掌握'],...list.map(row=>[F.periodLabel(row.period,granularity),count(row.reviews),count(row.pass),count(row.assisted),count(row.again),count(row.manual_changes),count(row.first_mastered)])]
    };
  }

  function tableHTML(rows,caption){
    const [head,...body]=rows;
    return `<div class="stats-table-wrap"><table class="stats-table"><caption class="sr-only">${escapeHTML(caption)}</caption><thead><tr>${head.map(cell=>`<th scope="col">${escapeHTML(cell)}</th>`).join('')}</tr></thead><tbody>${body.map(row=>`<tr>${row.map((cell,index)=>index?`<td>${escapeHTML(cell)}</td>`:`<th scope="row">${escapeHTML(cell)}</th>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  }

  window.Cuoti.stats.charts=Object.freeze({showTip,hideTip,sparkline,calendar,trend,tableHTML});
})();
