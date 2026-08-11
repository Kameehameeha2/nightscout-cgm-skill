#!/usr/bin/env python3
"""Generate a self-contained interactive CGM therapy-analysis HTML report."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
bundle = json.loads((ROOT / "report_bundle.json").read_text())
DATA = json.dumps(bundle, separators=(",", ":"))

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CGM Therapy Analysis — full history (Dec 2025 – Jul 2026)</title>
<style>
:root, .viz-root{
  --surface-1:#fcfcfb; --page:#f9f9f7;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --series-1:#2a78d6; --band-inner:#9ec5f4; --band-outer:#cde2fb;
  --good:#0ca30c; --low:#d03b3b; --vlow:#a31212; --high:#eda100; --vhigh:#eb6834;
  --basal:#1baf7a; --isf:#4a3aa7; --icr:#eb6834;
  --target-band:rgba(12,163,12,.09);
  --tile:#ffffff;
}
@media (prefers-color-scheme: dark){
  :root, .viz-root{
    --surface-1:#1a1a19; --page:#0d0d0d;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --series-1:#3987e5; --band-inner:#1c5cab; --band-outer:#184f95;
    --good:#0ca30c; --low:#e05555; --vlow:#c23a3a; --high:#c98500; --vhigh:#d95926;
    --basal:#199e70; --isf:#9085e9; --icr:#d95926;
    --target-band:rgba(12,163,12,.13);
    --tile:#232322;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:18px;margin:40px 0 6px;letter-spacing:-.01em}
.sub{color:var(--ink-2);font-size:14px;margin:0 0 4px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:14px;
  padding:20px 22px;margin-top:14px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:16px}
.tile{background:var(--tile);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.tile .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.tile .v{font-size:30px;font-weight:600;letter-spacing:-.02em;margin-top:2px}
.tile .u{font-size:13px;color:var(--ink-2);font-weight:400}
.tile .note{font-size:12px;margin-top:4px}
.good{color:var(--good)} .warn{color:var(--vhigh)} .bad{color:var(--low)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--ink-2);margin:8px 0 2px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:14px;height:10px;border-radius:3px;display:inline-block}
.sw.line{height:0;border-top:3px solid;width:16px;border-radius:0}
svg{display:block;width:100%;height:auto;overflow:visible}
.tick{fill:var(--muted);font-size:11px}
.axlab{fill:var(--ink-2);font-size:12px}
.dlabel{fill:var(--ink);font-size:11px;font-weight:600}
.tip{position:fixed;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);
  border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 6px 24px rgba(0,0,0,.16);
  opacity:0;transition:opacity .08s;z-index:20;max-width:230px}
.tip b{font-weight:600}
.finding{border-left:3px solid var(--axis);padding:2px 0 2px 14px;margin:14px 0}
.finding.hi{border-color:var(--vhigh)} .finding.lo{border-color:var(--low)}
.finding.ok{border-color:var(--good)}
.finding h3{margin:0 0 3px;font-size:15px}
.finding p{margin:4px 0;font-size:14px;color:var(--ink-2)}
.finding .rx{color:var(--ink);font-size:14px}
.tag{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;
  background:var(--tile);border:1px solid var(--border);margin-right:6px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}
th,td{text-align:right;padding:6px 10px;border-bottom:1px solid var(--grid);font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.disc{font-size:12.5px;color:var(--ink-2);background:var(--tile);border:1px solid var(--border);
  border-radius:10px;padding:12px 14px;margin-top:14px}
.muted{color:var(--muted)}
details summary{cursor:pointer;color:var(--ink-2);font-size:13px;margin-top:8px}
</style>
</head>
<body>
<div class="wrap viz-root" data-palette="#2a78d6,#1baf7a,#eda100,#008300,#4a3aa7,#e34948,#e87ba4,#eb6834">
  <h1>CGM Therapy Analysis</h1>
  <p class="sub" id="meta"></p>

  <div class="tiles" id="tiles"></div>

  <h2>Time in range</h2>
  <p class="sub">Share of the full 210-day history spent in each glucose band (target 3.9–10.0 mmol/L).</p>
  <div class="card"><div id="tirbar"></div></div>

  <h2>Ambulatory Glucose Profile (AGP)</h2>
  <p class="sub">All 210 days overlaid onto a single 24-hour day. Median (line) with the 25–75% (dark band) and 5–95% (light band) spread. Hover for the hourly distribution.</p>
  <div class="card">
    <div class="legend">
      <span><i class="sw line" style="border-color:var(--series-1)"></i>Median</span>
      <span><i class="sw" style="background:var(--band-inner)"></i>25–75%</span>
      <span><i class="sw" style="background:var(--band-outer)"></i>5–95%</span>
      <span><i class="sw" style="background:var(--target-band)"></i>Target 3.9–10</span>
    </div>
    <div id="agp"></div>
  </div>

  <h2>Time out of range, by hour</h2>
  <p class="sub">Highs above the line, lows below. This is the clearest view of <em>when</em> control breaks down across the day.</p>
  <div class="card">
    <div class="legend">
      <span><i class="sw" style="background:var(--high)"></i>High (&gt;10)</span>
      <span><i class="sw" style="background:var(--low)"></i>Low (&lt;3.9)</span>
    </div>
    <div id="hourbars"></div>
  </div>

  <h2>Where the trouble clusters — day × hour</h2>
  <p class="sub">Percent of time <em>out of range</em> for each weekday-hour cell. Darker = worse. Hover for detail.</p>
  <div class="card"><div id="heat"></div></div>

  <h2>Current pump settings vs. observed glucose</h2>
  <p class="sub">Your active profile (basal, insulin sensitivity, carb ratio) plotted against the day, so settings line up with the patterns above.</p>
  <div class="card"><div id="basal"></div></div>
  <div class="card"><div id="isf"></div></div>
  <div class="card"><div id="icr"></div></div>

  <h2>Findings &amp; adjustment ideas to discuss with your care team</h2>
  <div id="findings"></div>

  <div class="disc">
    <b>Important:</b> This is a data-pattern analysis of past CGM readings, not medical advice.
    It does not know your meals, activity, illness, insulin timing, or set changes, and it
    cannot see boluses in the readings themselves. Basal, ISF and carb-ratio changes should be
    made only with your diabetes care team, one variable at a time, and re-assessed. Treat any
    low first.
  </div>
</div>

<div class="tip" id="tip"></div>
<script>
const D = __DATA__;
const NS="http://www.w3.org/2000/svg";
const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
function el(tag, attrs, parent){const e=document.createElementNS(NS,tag);
  for(const k in attrs) e.setAttribute(k, attrs[k]); if(parent) parent.appendChild(e); return e;}
const tip=document.getElementById('tip');
function showTip(html,x,y){tip.innerHTML=html;tip.style.opacity=1;
  const pad=14; let tx=x+pad, ty=y+pad;
  const r=tip.getBoundingClientRect();
  if(tx+r.width>innerWidth) tx=x-r.width-pad;
  if(ty+r.height>innerHeight) ty=y-r.height-pad;
  tip.style.left=tx+'px';tip.style.top=ty+'px';}
function hideTip(){tip.style.opacity=0;}

const agp = Array.from({length:24},(_,h)=>D.agp[h]);

// ---------- meta + tiles ----------
document.getElementById('meta').textContent =
  `Full history: 16 Dec 2025 – 14 Jul 2026 (210 days) · ${D.stats.n.toLocaleString()} sensor readings · units mmol/L`;
const tirTotal = D.bands.inr;
const tiles=[
  {k:'Time in range',v:tirTotal,u:'%',note:tirTotal>=70?'meets ≥70% goal':'below 70% goal',cls:tirTotal>=70?'good':'warn'},
  {k:'GMI (est. A1c)',v:D.stats.gmi,u:'%',note:D.stats.gmi<7?'below 7.0% goal':'above 7.0%',cls:D.stats.gmi<7?'good':'warn'},
  {k:'Mean glucose',v:D.stats.mean,u:'mmol/L',note:'SD '+D.stats.sd,cls:''},
  {k:'Variability (CV)',v:D.stats.cv,u:'%',note:D.stats.cv<=36?'stable (≤36%)':'unstable',cls:D.stats.cv<=36?'good':'warn'},
  {k:'Time low',v:(D.bands.vlow+D.bands.low).toFixed(1),u:'%',note:(D.bands.vlow+D.bands.low)>4?'above 4% limit':'within 4% limit',cls:(D.bands.vlow+D.bands.low)>4?'bad':'good'},
  {k:'Daily basal',v:D.profile.tdb,u:'U/day',note:D.profile.basal.length+' rate segments',cls:''},
];
document.getElementById('tiles').innerHTML = tiles.map(t=>
  `<div class="tile"><div class="k">${t.k}</div>
   <div class="v">${t.v}<span class="u"> ${t.u}</span></div>
   <div class="note ${t.cls}">${t.note}</div></div>`).join('');

// ---------- TIR stacked bar ----------
(function(){
  const W=900,H=90,x0=10,x1=W-10,y=18,bh=34;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`},document.getElementById('tirbar'));
  const segs=[
    {k:'Very low',v:D.bands.vlow,c:css('--vlow'),r:'<3.0'},
    {k:'Low',v:D.bands.low,c:css('--low'),r:'3.0–3.9'},
    {k:'In range',v:D.bands.inr,c:css('--good'),r:'3.9–10'},
    {k:'High',v:D.bands.high,c:css('--high'),r:'10–13.9'},
    {k:'Very high',v:D.bands.vhigh,c:css('--vhigh'),r:'>13.9'},
  ];
  const total=segs.reduce((s,d)=>s+d.v,0), scale=(x1-x0)/total;
  let cx=x0;
  segs.forEach(d=>{
    const w=d.v*scale;
    if(w<=0){return;}
    el('rect',{x:cx+ (cx>x0?1:0),y,width:Math.max(w-1,0.5),height:bh,fill:d.c,rx:3},svg);
    if(w>46){
      const t=el('text',{x:cx+w/2,y:y+bh/2+1,'text-anchor':'middle','dominant-baseline':'middle',
        fill:'#fff','font-size':13,'font-weight':600},svg);
      t.textContent=d.v+'%';
    }
    cx+=w;
  });
  // labels below
  cx=x0;
  segs.forEach(d=>{const w=d.v*scale; const c=cx+w/2; cx+=w;});
  let lx=x0, ly=y+bh+20;
  const lg=el('g',{},svg);
  const legItems=segs.map(d=>`${d.k} (${d.r}): ${d.v}%`);
  segs.forEach((d,i)=>{
    el('rect',{x:lx,y:ly-9,width:11,height:11,rx:2,fill:d.c},lg);
    const t=el('text',{x:lx+16,y:ly,'font-size':12,fill:css('--ink-2')},lg);
    t.textContent=`${d.k} ${d.v}%`;
    lx += 22 + (d.k.length*6.6)+34;
  });
})();

// ---------- AGP ----------
(function(){
  const W=900,H=340,mL=42,mR=14,mT=14,mB=28;
  const iw=W-mL-mR, ih=H-mT-mB;
  const ymax=15;
  const X=h=>mL+(h/24)*iw;
  const Xc=h=>mL+((h+0.5)/24)*iw;
  const Y=v=>mT+ih-(Math.min(v,ymax)/ymax)*ih;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`},document.getElementById('agp'));
  // target band
  el('rect',{x:mL,y:Y(10),width:iw,height:Y(3.9)-Y(10),fill:css('--target-band')},svg);
  el('line',{x1:mL,x2:W-mR,y1:Y(3.9),y2:Y(3.9),stroke:css('--good'),'stroke-width':1,'stroke-dasharray':'4 4',opacity:.5},svg);
  el('line',{x1:mL,x2:W-mR,y1:Y(10),y2:Y(10),stroke:css('--good'),'stroke-width':1,'stroke-dasharray':'4 4',opacity:.5},svg);
  // gridlines + y ticks
  [0,3,6,9,12,15].forEach(v=>{
    el('line',{x1:mL,x2:W-mR,y1:Y(v),y2:Y(v),stroke:css('--grid'),'stroke-width':1},svg);
    const t=el('text',{x:mL-8,y:Y(v)+4,'text-anchor':'end',class:'tick'},svg);t.textContent=v;
  });
  // x ticks every 3h
  for(let h=0;h<=24;h+=3){
    const t=el('text',{x:X(h),y:H-8,'text-anchor':'middle',class:'tick'},svg);
    t.textContent=String(h).padStart(2,'0');
  }
  const area=(lo,hi,fill)=>{
    let d='M';
    for(let h=0;h<24;h++){d+=`${Xc(h)},${Y(agp[h][hi])} `;}
    // close along low reversed, extend to edges
    for(let h=23;h>=0;h--){d+=`${Xc(h)},${Y(agp[h][lo])} `;}
    d+='Z';
    el('path',{d,fill,stroke:'none'},svg);
  };
  area('p05','p95',css('--band-outer'));
  area('p25','p75',css('--band-inner'));
  // median line
  let dl='M'+agp.map((a,h)=>`${Xc(h)},${Y(a.p50)}`).join(' L');
  el('path',{d:dl,fill:'none',stroke:css('--series-1'),'stroke-width':2.5,'stroke-linejoin':'round'},svg);
  // crosshair + hover
  const cross=el('line',{x1:0,x2:0,y1:mT,y2:mT+ih,stroke:css('--axis'),'stroke-width':1,opacity:0},svg);
  const dot=el('circle',{r:4.5,fill:css('--series-1'),stroke:css('--surface-1'),'stroke-width':2,opacity:0},svg);
  const hit=el('rect',{x:mL,y:mT,width:iw,height:ih,fill:'transparent'},svg);
  hit.addEventListener('mousemove',ev=>{
    const pt=svg.getBoundingClientRect();
    const rel=(ev.clientX-pt.left)/pt.width*W;
    let h=Math.floor((rel-mL)/iw*24); h=Math.max(0,Math.min(23,h));
    const a=agp[h];
    cross.setAttribute('x1',Xc(h));cross.setAttribute('x2',Xc(h));cross.setAttribute('opacity',.6);
    dot.setAttribute('cx',Xc(h));dot.setAttribute('cy',Y(a.p50));dot.setAttribute('opacity',1);
    showTip(`<b>${String(h).padStart(2,'0')}:00</b><br>Median ${a.p50} · IQR ${a.p25}–${a.p75}<br>`+
      `5–95%: ${a.p05}–${a.p95}<br>TIR ${a.tir}% · low ${a.low}% · high ${a.high}%`,ev.clientX,ev.clientY);
  });
  hit.addEventListener('mouseleave',()=>{hideTip();cross.setAttribute('opacity',0);dot.setAttribute('opacity',0);});
})();

// ---------- hourly diverging out-of-range bars ----------
(function(){
  const W=900,H=260,mL=42,mR=14,mT=14,mB=26;
  const iw=W-mL-mR, ih=H-mT-mB;
  const maxUp=30, maxDn=15; // high% up, low% down
  const zero=mT+ih*(maxUp/(maxUp+maxDn));
  const Xc=h=>mL+((h+0.5)/24)*iw;
  const bw=iw/24*0.62;
  const Yup=v=>zero-(v/maxUp)*(zero-mT);
  const Ydn=v=>zero+(v/maxDn)*(mT+ih-zero);
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`},document.getElementById('hourbars'));
  // gridlines
  [10,20,30].forEach(v=>{el('line',{x1:mL,x2:W-mR,y1:Yup(v),y2:Yup(v),stroke:css('--grid')},svg);
    const t=el('text',{x:mL-6,y:Yup(v)+3,'text-anchor':'end',class:'tick'},svg);t.textContent=v+'%';});
  [5,10,15].forEach(v=>{el('line',{x1:mL,x2:W-mR,y1:Ydn(v),y2:Ydn(v),stroke:css('--grid')},svg);
    const t=el('text',{x:mL-6,y:Ydn(v)+3,'text-anchor':'end',class:'tick'},svg);t.textContent=v+'%';});
  el('line',{x1:mL,x2:W-mR,y1:zero,y2:zero,stroke:css('--axis'),'stroke-width':1.5},svg);
  for(let h=0;h<24;h++){
    const a=agp[h];
    // high (up)
    const hu=zero-Yup(a.high);
    const ru=el('rect',{x:Xc(h)-bw/2,y:Yup(a.high),width:bw,height:Math.max(hu,0),rx:3,fill:css('--high')},svg);
    // low (down)
    const hd=Ydn(a.low)-zero;
    const rd=el('rect',{x:Xc(h)-bw/2,y:zero,width:bw,height:Math.max(hd,0),rx:3,fill:css('--low')},svg);
    [ru,rd].forEach(r=>{
      r.style.cursor='pointer';
      r.addEventListener('mousemove',ev=>showTip(
        `<b>${String(h).padStart(2,'0')}:00</b><br>High &gt;10: ${a.high}%<br>Low &lt;3.9: ${a.low}%<br>TIR ${a.tir}%`,ev.clientX,ev.clientY));
      r.addEventListener('mouseleave',hideTip);
    });
  }
  for(let h=0;h<=24;h+=3){const t=el('text',{x:mL+(h/24)*iw,y:H-6,'text-anchor':'middle',class:'tick'},svg);t.textContent=String(h).padStart(2,'0');}
  const tu=el('text',{x:mL+2,y:mT+10,class:'axlab',fill:css('--high')},svg);tu.textContent='▲ time high';
  const td=el('text',{x:mL+2,y:mT+ih-2,class:'axlab',fill:css('--low')},svg);td.textContent='▼ time low';
})();

// ---------- heatmap day x hour ----------
(function(){
  const days=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  const W=900,mL=40,mT=20,mR=10,cellH=30,gap=2;
  const iw=W-mL-mR, cw=iw/24;
  const H=mT+7*cellH+24;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`},document.getElementById('heat'));
  // color: % out of range 0..40 -> sequential red
  const ramp=['#fcfcfb','#fbe3df','#f6b8ac','#ec8a78','#dd5a46','#b8321f','#8a1e10'];
  function col(oor){
    const t=Math.max(0,Math.min(1,oor/40));
    const idx=t*(ramp.length-1); const i=Math.floor(idx); const f=idx-i;
    const a=hex(ramp[i]),b=hex(ramp[Math.min(i+1,ramp.length-1)]);
    const m=a.map((v,k)=>Math.round(v+(b[k]-v)*f));
    return `rgb(${m[0]},${m[1]},${m[2]})`;
  }
  function hex(h){return [1,3,5].map(i=>parseInt(h.slice(i,i+2),16));}
  for(let h=0;h<=24;h+=3){const t=el('text',{x:mL+h*cw,y:mT-6,'text-anchor':'middle',class:'tick'},svg);t.textContent=String(h).padStart(2,'0');}
  for(let d=0;d<7;d++){
    const t=el('text',{x:mL-8,y:mT+d*cellH+cellH/2+4,'text-anchor':'end',class:'tick'},svg);t.textContent=days[d];
    for(let h=0;h<24;h++){
      const cell=D.heat[`${d}_${h}`]; if(!cell)continue;
      const oor=+(100-cell.tir).toFixed(1);
      const r=el('rect',{x:mL+h*cw+gap/2,y:mT+d*cellH+gap/2,width:cw-gap,height:cellH-gap,rx:3,
        fill:col(oor)},svg);
      r.style.cursor='pointer';
      r.addEventListener('mousemove',ev=>showTip(
        `<b>${days[d]} ${String(h).padStart(2,'0')}:00</b><br>Out of range: ${oor}%<br>TIR ${cell.tir}% · mean ${cell.mean}`,ev.clientX,ev.clientY));
      r.addEventListener('mouseleave',hideTip);
    }
  }
  // legend
  const ly=mT+7*cellH+16, lx=mL;
  const lgw=140;
  const grad=el('linearGradient',{id:'hg'},svg);
  ramp.forEach((c,i)=>el('stop',{offset:(i/(ramp.length-1)*100)+'%','stop-color':c},grad));
  const cap=el('text',{x:lx,y:ly+2,class:'tick'},svg);cap.textContent='% time out of range:';
  const gx=lx+130;
  el('rect',{x:gx,y:ly-9,width:lgw,height:11,rx:3,fill:'url(#hg)',stroke:css('--border')},svg);
  const a=el('text',{x:gx+4,y:ly+16,class:'tick'},svg);a.textContent='0 (good)';
  const b=el('text',{x:gx+lgw,y:ly+16,'text-anchor':'end',class:'tick'},svg);b.textContent='40+ (poor)';
})();

// ---------- profile step charts ----------
function stepChart(elId,segs,opts){
  const W=900,H=opts.h||150,mL=42,mR=90,mT=16,mB=26;
  const iw=W-mL-mR, ih=H-mT-mB;
  const X=h=>mL+(h/24)*iw;
  const vmax=opts.vmax, vmin=opts.vmin||0;
  const Y=v=>mT+ih-((v-vmin)/(vmax-vmin))*ih;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`},document.getElementById(elId));
  // shaded problem windows
  (opts.shade||[]).forEach(s=>{
    el('rect',{x:X(s[0]),y:mT,width:X(s[1])-X(s[0]),height:ih,fill:s[2]||'rgba(235,104,52,.08)'},svg);
  });
  opts.ticks.forEach(v=>{el('line',{x1:mL,x2:mL+iw,y1:Y(v),y2:Y(v),stroke:css('--grid')},svg);
    const t=el('text',{x:mL-8,y:Y(v)+4,'text-anchor':'end',class:'tick'},svg);t.textContent=v;});
  for(let h=0;h<=24;h+=3){const t=el('text',{x:X(h),y:H-6,'text-anchor':'middle',class:'tick'},svg);t.textContent=String(h).padStart(2,'0');}
  // build step points
  const pts=[];
  segs.forEach((s,i)=>{
    const start=s.t, end=(i<segs.length-1)?segs[i+1].t:24;
    pts.push([start,s.v],[end,s.v]);
  });
  let d='M'+pts.map(p=>`${X(p[0])},${Y(p[1])}`).join(' L');
  // area fill under
  const areaD=d+` L${X(24)},${Y(vmin)} L${X(0)},${Y(vmin)} Z`;
  el('path',{d:areaD,fill:opts.color,opacity:.12},svg);
  el('path',{d,fill:'none',stroke:opts.color,'stroke-width':2.5,'stroke-linejoin':'round'},svg);
  // value labels at each segment
  segs.forEach((s,i)=>{
    const end=(i<segs.length-1)?segs[i+1].t:24; const mid=(s.t+end)/2;
    const t=el('text',{x:X(mid),y:Y(s.v)-7,'text-anchor':'middle',class:'dlabel'},svg);t.textContent=s.v;
  });
  const lab=el('text',{x:mL+iw+10,y:mT+12,class:'axlab',fill:opts.color,'font-weight':600},svg);
  lab.textContent=opts.label;
  const lab2=el('text',{x:mL+iw+10,y:mT+28,class:'tick'},svg);lab2.textContent=opts.unit;
}
const toH=t=>{const[a,b]=t.split(':').map(Number);return a+b/60;};
stepChart('basal',D.profile.basal.map(s=>({t:toH(s.time),v:s.rate})),
  {vmax:1.0,ticks:[0,0.25,0.5,0.75,1.0],color:css('--basal'),label:'Basal',unit:'U/hr',h:150,
   shade:[[0,3,'rgba(208,59,59,.09)'],[19,22,'rgba(235,104,52,.09)']]});
stepChart('isf',D.profile.isf.map(s=>({t:toH(s.time),v:s.value})),
  {vmax:3.4,vmin:2.4,ticks:[2.5,2.8,3.1,3.4],color:css('--isf'),label:'ISF',unit:'mmol/L per U',h:140});
stepChart('icr',D.profile.icr.map(s=>({t:toH(s.time),v:s.value})),
  {vmax:11,vmin:6,ticks:[6,8,10],color:css('--icr'),label:'Carb ratio',unit:'g per U',h:140,
   shade:[[17,22,'rgba(235,104,52,.09)']]});

// ---------- findings ----------
const findings=[
 {cls:'ok',tag:'Overall',tags:['TIR 89.5%','GMI 6.3%','CV 28.3%','210 days'],h:'Excellent, stable long-term control',
  p:'Across the full 7-month record (59,954 readings) time-in-range averages 89.5% — well clear of the 70% goal — with GMI 6.3% and variability (CV 28.3%) comfortably under the 36% stability threshold. Control has also improved over time: mean glucose fell from ~7.3 mmol/L in Dec–Jan to ~6.8 by summer, and monthly TIR rose from 86.8% to 90–92%. Overnight and morning (03:00–10:00) are near-perfect (TIR 96–99%+). These are fine-tuning opportunities in specific windows, not a system that needs overhaul.',
  rx:''},
 {cls:'hi',tag:'Biggest issue',tags:['18:00–21:00','~20% high','worst window'],h:'Post-dinner evening highs (UAM reacting late)',
  p:'The evening is the weakest window by a wide margin. From 18:00 to 21:00 roughly 19–21% of readings sit above 10 mmol/L and TIR drops to 77–80%. Mean glucose plateaus at ~8.2 across 19:00–21:00. Friday evening is the single worst cell (21:00 TIR 53.9%). Because dinners are almost always unannounced (only ~283 carb entries in 210 days), UAM can only react to the rise after it starts — so the loop is always chasing dinner.',
  rx:'A dinner-announcement check (see Setup card) shows the evening highs are mostly meal-size-driven and already well-capped — no clean settings lever cuts them without pushing overnight lows up. Best treated as near the practical floor for a reactive loop; the overnight low is the more actionable target.'},
 {cls:'lo',tag:'Safety',tags:['23:00–01:00','9% low at 00:00','the binding limit'],h:'Late-evening / early-overnight lows',
  p:'Lows cluster at 23:00–01:00 (00:00 is the most common low hour, ~9% low; 23:00 ~7%), 1,306 low events over 210 nights, and rose as the highs came down. Notably these lows are WORSE on lighter-dinner nights than big-dinner nights — so they are NOT dinner-correction stacking; they look like a background/UAM effect on normal-intake evenings, largely independent of the evening highs.',
  rx:'This is the binding safety constraint (time-low touched 3.9% in May vs a 4% ceiling). The defensible experiment with your care team is to scrutinise the late-evening (23:00) basal block (0.7 U/hr) and UAM behaviour on small evening rises — NOT to raise evening basal, which would worsen exactly these light-night lows.'},
 {cls:'hi',tag:'Secondary',tags:['12:00–14:00','~13% high'],h:'Midday / post-lunch rise',
  p:'A secondary rise peaks around noon–14:00 (mean ~8.0, ~13% high at 12:00), easing back by mid-afternoon with a small rise in lows into 14:00–16:00 (4–5% low). The high-then-dip shape suggests lunch coverage that is slightly late relative to the carbs, sometimes followed by a mild over-correction.',
  rx:'Discuss: lunch pre-bolus timing; the midday carb ratio (9 g/U) and whether ISF (≈3.0 mmol/L/U) leaves post-lunch corrections a touch strong.'},
 {cls:'ok',tag:'Setup',tags:['Dynamic ISF','UAM','AF 0.8','DIA 10 h (correct)'],h:'Full closed loop — the levers that fit it',
  p:'You run Trio (iPhone) fully automated, no manual boluses: Dynamic ISF (sigmoid, AF 0.8) + UAM, Lyumjev with Insulin Peak Time 50 min and DIA 10 h — both correct, not levers. An announced-vs-unannounced dinner check was decisive: announced (bigger-meal) evenings ran HIGHER not lower (confounded — announcing does not demonstrably help), and overnight lows were WORSE on lighter-dinner nights. So evening highs (meal-size-driven, well-capped by UAM) and overnight lows (background/UAM effect on light nights) are largely INDEPENDENT, and there is no clean settings lever that fixes one without worsening the other.',
  rx:'This is a loop near its practical ceiling, not a mistuned one. Advance-notice and evening-basal ideas are downgraded (unproven / raise the light-night lows). The one defensible experiment, with your care team, is to target the overnight lows directly — scrutinise the late-evening (23:00) basal block and why UAM fires on small evening rises — since hypoglycemia, not the highs, is the binding constraint.'},
];
document.getElementById('findings').innerHTML = findings.map(f=>
  `<div class="finding ${f.cls}">
     <h3>${f.h}</h3>
     <div>${f.tags.map(t=>`<span class="tag">${t}</span>`).join('')}</div>
     <p>${f.p}</p>
     ${f.rx?`<p class="rx"><b>Idea:</b> ${f.rx}</p>`:''}
   </div>`).join('');
</script>
</body>
</html>
"""

out = ROOT / "cgm_therapy_report.html"
out.write_text(HTML.replace("__DATA__", DATA), encoding="utf-8")
print("WROTE", out)
