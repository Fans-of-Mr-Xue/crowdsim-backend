"use strict";
// Self-contained, offline: no fetch, CDN, WebSocket or simulation controls.
const DATA = JSON.parse(document.getElementById("viewer-data").textContent);
const $ = id => document.getElementById(id);
const safe = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const valid = value => typeof value === "number" && Number.isFinite(value);
const fmt = (value, digits = 3) => valid(value) ? value.toLocaleString("zh-CN", {maximumFractionDigits: digits}) : "—";
const COLOR = {blue:"#3975d7",teal:"#14a08c",orange:"#edac4f",red:"#db6375",gray:"#9caaba",purple:"#9673c3"};
const STATUS = {
  recorded:"已记录",complete:"已完成",interrupted:"提前结束",error:"运行异常",running:"记录中",
  not_implemented:"旧记录未实现",not_selected:"未选择",not_recorded:"旧记录未采集",not_started:"尚未开始",
  in_progress:"进行中",incomplete:"未完成",no_valid_samples:"无有效样本",no_baseline:"缺少初始基线",
  partial:"部分可用",partial_reconstructed:"旧记录补算",zero_duration:"策略后时间为零",
  invalid_population_accounting:"人口账本异常",invalid_density_baseline:"密度基线无效",
  no_target_population:"无目标人群",uncommitted_completion:"结束快照未提交",unknown:"未记录状态",
};
const BEHAVIOR = [
  ["walking","行走",COLOR.blue],["waiting","停留",COLOR.teal],["blocked","受阻",COLOR.orange],
  ["avoiding","避让",COLOR.purple],["unknown","未知",COLOR.gray],
];
const PSYCHOLOGY = [["calm","平静",COLOR.teal],["tense","紧张",COLOR.orange],["panic","恐慌",COLOR.red],["unknown","未知",COLOR.gray]];
const DESCRIPTION = {
  A1:"区域内人数 ÷ 所选区域面积。曲线使用保存的实际面积；区域外人数另列。",
  A2:"固定均匀网格按区域边界裁剪，密度 = 片区人数 ÷ 裁剪后的片区面积。点击片区查看时间曲线；颜色范围在本轮固定。",
  A3:"区域内有效速度的平均值，包含实际停留人员。无人或没有有效速度时留空；零速度是真实观测。",
  A4:"与 A2 使用同一套固定片区，统计片区内有效平均速度。灰色表示无人或无有效样本；可点击片区查看曲线。",
  A5:"共享边界两侧各取采样带，显示 A 侧密度减 B 侧密度，或其绝对值。正值表示 A 侧更密；灰色外边界没有区域外观测，不能视为零。",
  B1:"从首次成功应用疏散策略 ta 到本轮所有行人正常完成 SUMO 行程 te 的仿真时长。首次运行为事件开始 t0；暂停、离开页面和时间上限不代表疏散结束。",
  B2:"疏散效率 = (策略应用时区域密度 − 疏散结束时区域密度) ÷ 疏散时间。缺少策略、正常结束或有效密度基线时不显示终值。",
  B3:"按同一固定片区，分别计算初始与运行时、运行时与结束、初始与结束密度的绝对差。后三阶段图例使用统一范围；尚未正常结束时，涉及结束密度的两组差留空。",
  C1:"行走、停留、受阻、避让、未知互斥统计；拥挤是附加标签。实际状态转换不包含首次建立标签，区域出入单独统计。",
  C2:"模型压力按已保存的工程阈值、滞回及持续确认规则分类为平静、紧张、恐慌或未知。人数按区域汇总；压力是模型值，查看器不修改模型。",
};
const state = {run:DATA.runs[0],metric:DATA.metrics[0],frame:0,cell:0,boundary:0,absolute:false,percent:false,showReference:true,zoom:1,pan:[0,0],timer:null};
const current = () => state.run.samples[state.frame];
const nearestMap = () => {
  const indices = state.run.maps.indices;
  let best = 0;
  indices.forEach((i,k) => { if (Math.abs(i-state.frame) < Math.abs(indices[best]-state.frame)) best = k; });
  return best;
};

function option(select, value, title) {
  const el = document.createElement("option"); el.value = value; el.textContent = title; select.append(el);
}
function pill(el, status) {
  el.textContent = STATUS[status] || status || "未记录";
  el.className = "pill " + (["complete","recorded"].includes(status) ? "success" : "warning");
}
function stat(label, value, unit, detail) {
  return `<div class="stat"><div class="stat-label">${safe(label)}</div><div class="stat-value">${safe(value)}<small>${safe(unit)}</small></div><div class="stat-detail">${safe(detail)}</div></div>`;
}
function stop() { clearInterval(state.timer); state.timer = null; $("play").textContent = "播放"; }
function runChanged(id) {
  stop(); state.run = DATA.runs.find(run => run.id === id); state.frame = 0; state.zoom=1; state.pan=[0,0];
  const densities=state.run.maps.density[0] || [];
  state.cell=Math.max(0,densities.indexOf(Math.max(0,...densities.filter(valid))));
  state.boundary=state.run.geometry.boundaries.findIndex(boundary=>boundary.b);
  if(state.boundary<0)state.boundary=0;
  $("time-slider").max=state.run.samples.length-1;
  $("play").disabled=state.run.samples.length<2;
  pill($("run-status"),state.run.status);
  $("run-info").textContent=`run_id: ${state.run.id}  ·  requirement_id: ${state.run.requirement_id || "未记录"}  ·  统计范围: ${state.run.scope_name}  ·  热点: ${state.run.hotspot || "未记录"}`;
  renderQuality(); renderNav(); refresh(); renderScopePanel();
}
function sceneChanged(id) {
  $("run-select").replaceChildren();
  const runs=DATA.runs.filter(run=>run.scene_id===id);
  runs.forEach(run=>option($("run-select"),run.id,`${run.id} · ${run.samples.length} 帧 · ${fmt(run.area,1)} m² · ${run.scope_info?.status==="matching"?"预设范围一致":run.scope_info?.status==="changed"?"范围不同":""} · ${STATUS[run.status] || run.status}`));
  runChanged(runs[0].id);
}
function renderNav() {
  $("metric-nav").replaceChildren();
  DATA.metrics.forEach(metric=>{
    const button=document.createElement("button"); button.className="nav-btn"+(state.metric.id===metric.id?" active":"");
    button.dataset.metric=metric.code;
    const status=state.run.metrics[metric.id]?.status;
    button.innerHTML=`<b>${safe(metric.code)}</b>${safe(metric.name)}<span class="nav-mark ${["recorded","complete","partial","partial_reconstructed"].includes(status)?"":"missing"}"></span>`;
    button.setAttribute("aria-current",state.metric.id===metric.id?"page":"false");
    button.onclick=()=>{state.metric=metric; renderNav(); renderMetric();}; $("metric-nav").append(button);
  });
}
function renderQuality() {
  const run=state.run;
  $("quality-count").textContent=`记录格式 v${run.schema_version} · ${run.quality.length} 类完整性提示`;
  const notes=[...run.notes,run.geometry.roads.note];
  if(run.end_reason)notes.push(`记录结束原因：${run.end_reason}。记录结束与疏散完成分别判断。`);
  if(run.error)notes.push(`运行错误：${typeof run.error==="string"?run.error:JSON.stringify(run.error)}`);
  if(DATA.skipped.length)notes.push(`本次导出跳过 ${DATA.skipped.length} 份缺少有效观测的记录：${DATA.skipped.map(s=>`${s.run_id} (${s.reason})`).join("；")}`);
  $("quality-content").innerHTML=`<ul>${notes.map(note=>`<li>${safe(note)}</li>`).join("")}${run.quality.map(q=>`<li>${safe(q.message)}：${q.count}</li>`).join("")}</ul><div>数据源：${safe(run.source)}</div><div>保存的路网 SHA-256：${safe(run.network_sha256)}</div><div>仅使用 observation_samples.jsonl 已提交的快照；轨迹大文件不参与加载。空间图采用本轮保存的几何，灰色代表未知或缺失。</div>`;
}
function renderScopePanel() {
  const run=state.run,info=run.scope_info,panel=$("scope-panel");
  panel.hidden=!info?.reference;
  if(!info?.reference){panel.replaceChildren();return;}
  const ref=info.reference,matching=info.status==="matching";
  panel.className="scope-panel"+(matching?" matching":"");
  const title=matching?"本轮范围与已同步外滩预设一致":info.status==="changed"?"本轮记录范围与已同步预设不同":"缺少原始边界，无法比较范围版本";
  panel.innerHTML=`<strong>${safe(title)}</strong><div class="scope-versions"><span>本轮统计：${fmt(run.area,2)} m² · ${fmt(info.vertex_count,0)} 点 · <code>${safe(info.version || "未记录")}</code></span><span>对照预设：${fmt(ref.area_m2,2)} m² · ${fmt(ref.vertex_count,0)} 点 · <code>${safe(ref.version)}</code></span></div><p>${matching?"指标使用本轮保存的区域和网格，已与同步预设核对。":"所有指标仍使用本轮原始范围、面积和网格；预设边界只作对照，未按新范围重算旧数据。"} ${!matching&&valid(info.area_delta_m2)?`预设面积比本轮 ${info.area_delta_m2>=0?"增加":"减少"} ${fmt(Math.abs(info.area_delta_m2),2)} m²。`:""}</p>${info.overlay_geometry&&info.status==="changed"?'<details class="scope-comparison"><summary>查看新旧范围边界对照</summary><canvas id="scope-comparison-map" class="map-canvas scope-map" role="img" aria-label="本轮统计范围与预设范围对照"></canvas><div id="scope-comparison-map-scale" class="map-scope-legend"></div></details>':""}`;
  const details=panel.querySelector("details");
  if(details)details.ontoggle=()=>{if(details.open)map("scope-comparison-map",[],0,{outlineOnly:true});};
}
function refresh() {
  const run=state.run,sample=current();
  $("time-slider").value=state.frame;
  $("current-time").textContent=`${fmt(sample.time_seconds,1)} s`;
  $("time-end").textContent=`/ ${fmt(run.samples.at(-1).time_seconds,1)} s`;
  const mapSample=run.samples[run.maps.indices[nearestMap()]];
  $("map-time").textContent=run.maps.indices.length<run.samples.length?`空间快照 ${fmt(mapSample.time_seconds,1)} s（已抽样）`:`第 ${state.frame+1} / ${run.samples.length} 帧`;
  $("stats").innerHTML=stat("当前区域内人数",fmt(sample.person_count,0),"人",`全路网 ${fmt(sample.network_person_count,0)} · 区域外 ${fmt(sample.outside_scope_person_count,0)}`)+stat("统计区域面积",fmt(run.area,1),"m²",`${run.geometry.cells.length} 个片区 · ${run.geometry.grid_size || "—"} m 均匀网格`)+stat("当前区域平均速度",fmt(sample.avg_speed_mps),"m/s",`有效速度 ${fmt(sample.speed_sample_count,0)} · 无效 ${fmt(sample.invalid_speed_count,0)}`)+stat("已提交观测快照",fmt(run.samples.length,0),"帧",`时间 ${fmt(run.samples[0].time_seconds,1)} – ${fmt(run.samples.at(-1).time_seconds,1)} s`);
  renderMetric();
}
function plotBox(id,title,sub="",short=false) {
  return `<div class="chart-box"><div class="chart-title">${safe(title)}<small>${safe(sub)}</small></div><canvas id="${id}" class="chart-canvas${short?" short":""}" role="img" aria-label="${safe(title)}"></canvas><div id="${id}-legend" class="legend"></div></div>`;
}
function context(canvas) {
  const width=Math.max(220,canvas.clientWidth),height=canvas.clientHeight||240,dpr=window.devicePixelRatio||1;
  canvas.width=Math.round(width*dpr); canvas.height=Math.round(height*dpr);
  const ctx=canvas.getContext("2d"); ctx.scale(dpr,dpr); ctx.fillStyle="#fff"; ctx.fillRect(0,0,width,height);
  ctx.font='11px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif';
  return {ctx,width,height};
}
function chart(id,series,{stacked=false,percent=false,max=null,times=null}={}) {
  const canvas=$(id); if(!canvas)return;
  const {ctx,width,height}=context(canvas),pad={l:53,r:14,t:18,b:35};
  times=times || state.run.samples.map(sample=>sample.time_seconds);
  const minT=times[0] || 0,maxT=times.at(-1) || 0;
  let maximum=max ?? 0;
  for(let i=0;i<times.length;i++) {
    if(stacked)maximum=Math.max(maximum,series.reduce((sum,line)=>sum+(valid(line.values[i])?line.values[i]:0),0));
    else series.forEach(line=>{if(valid(line.values[i]))maximum=Math.max(maximum,line.values[i]);});
  }
  if(percent)maximum=100;
  maximum=maximum>0?maximum:1;
  if(max===null&&!percent)maximum*=1.08;
  const x=t=>pad.l+(maxT===minT?.5:(t-minT)/(maxT-minT))*(width-pad.l-pad.r);
  const y=v=>height-pad.b-v/maximum*(height-pad.t-pad.b);
  ctx.lineWidth=1;ctx.textAlign="right";
  for(let k=0;k<=4;k++) {
    const value=maximum*k/4,Y=y(value);ctx.strokeStyle="#e9eef5";ctx.beginPath();ctx.moveTo(pad.l,Y);ctx.lineTo(width-pad.r,Y);ctx.stroke();
    ctx.fillStyle="#8694a6";ctx.fillText(fmt(value,value<.01?5:3),pad.l-8,Y+4);
  }
  ctx.textAlign="center";
  for(let k=0;k<=4;k++){const t=minT+(maxT-minT)*k/4; if(minT===maxT&&k!==2)continue;ctx.fillText(fmt(t,1),x(t),height-14);}
  ctx.textAlign="right";ctx.fillText("仿真秒",width-pad.r,height-1);
  const any=series.some(line=>line.values.some(valid));
  if(stacked){
    const sums=Array(times.length).fill(0);
    series.forEach(line=>{
      let segment=[];
      const draw=()=>{if(!segment.length)return;ctx.fillStyle=line.color;ctx.beginPath();segment.forEach((p,k)=>k?ctx.lineTo(x(p.t),y(p.hi)):ctx.moveTo(x(p.t),y(p.hi)));segment.slice().reverse().forEach(p=>ctx.lineTo(x(p.t),y(p.lo)));ctx.closePath();ctx.fill();
        if(segment.length===1){const p=segment[0];ctx.fillRect(x(p.t)-4,y(p.hi),8,y(p.lo)-y(p.hi));}segment=[];};
      times.forEach((t,i)=>{if(valid(line.values[i])){const lo=sums[i];sums[i]+=line.values[i];segment.push({t,lo,hi:sums[i]});}else draw();});draw();
    });
  }else{
    series.forEach(line=>{ctx.strokeStyle=line.color;ctx.lineWidth=2;ctx.beginPath();let started=false;
      line.values.forEach((value,i)=>{if(!valid(value)){started=false;return;}if(started)ctx.lineTo(x(times[i]),y(value));else ctx.moveTo(x(times[i]),y(value));started=true;});ctx.stroke();
      if(times.length===1&&valid(line.values[0])){ctx.beginPath();ctx.arc(x(times[0]),y(line.values[0]),4,0,Math.PI*2);ctx.fillStyle=line.color;ctx.fill();}
    });
  }
  if(!any){ctx.textAlign="center";ctx.fillStyle="#8e9cae";ctx.fillText("没有可用的观测数据",width/2,height/2);}
  const cursor=x(Math.max(minT,Math.min(maxT,current().time_seconds)));
  ctx.setLineDash([4,4]);ctx.strokeStyle="#617b9f";ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(cursor,pad.t);ctx.lineTo(cursor,height-pad.b);ctx.stroke();ctx.setLineDash([]);
  const legend=$(id+"-legend");
  if(legend)legend.innerHTML=series.map(line=>`<span><i style="background:${line.color}"></i>${safe(line.label)}</span>`).join("");
  canvas.onclick=event=>{const rect=canvas.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(event.clientX-rect.left-pad.l)/(width-pad.l-pad.r))),target=minT+(maxT-minT)*ratio;let best=0;state.run.samples.forEach((s,i)=>{if(Math.abs(s.time_seconds-target)<Math.abs(state.run.samples[best].time_seconds-target))best=i;});state.frame=best;refresh();};
  canvas.onmousemove=event=>{const rect=canvas.getBoundingClientRect(),target=minT+(maxT-minT)*Math.max(0,Math.min(1,(event.clientX-rect.left-pad.l)/(width-pad.l-pad.r)));let best=0;times.forEach((t,i)=>{if(Math.abs(t-target)<Math.abs(times[best]-target))best=i;});canvas.title=`${fmt(times[best],1)} s\n`+series.map(line=>`${line.label}: ${fmt(line.values[best],5)}`).join("\n");};
}
const series=(field,label,color)=>({label,color,values:state.run.samples.map(sample=>sample[field])});

function polygonRings(geometry) {
  if(!geometry)return [];
  if(geometry.type==="Polygon")return [geometry.coordinates];
  if(geometry.type==="MultiPolygon")return geometry.coordinates;
  if(geometry.type==="GeometryCollection")return geometry.geometries.flatMap(polygonRings);
  return [];
}
function lineStrings(geometry) {
  if(!geometry)return [];
  if(geometry.type==="LineString")return [geometry.coordinates];
  if(geometry.type==="MultiLineString")return geometry.coordinates;
  if(geometry.type==="GeometryCollection")return geometry.geometries.flatMap(lineStrings);
  return [];
}
function tint(value,maximum,diverging=false) {
  if(!valid(value))return "#e5e9ee";
  const mix=(a,b,k)=>`rgb(${a.map((v,i)=>Math.round(v+(b[i]-v)*k)).join(",")})`;
  const t=maximum>0?Math.min(1,Math.abs(value)/maximum):0;
  if(diverging)return mix([243,245,248],value>=0?[214,87,81]:[38,95,187],t);
  const stops=[[239,246,253],[107,188,211],[37,125,162],[22,59,115]],position=t*3,j=Math.min(2,Math.floor(position));
  return mix(stops[j],stops[j+1],position-j);
}
function mapBox(id,title,compact=false) {
  return `<div class="chart-box"><div class="chart-title">${safe(title)}</div><canvas id="${id}" class="map-canvas${compact?" compact":""}" role="img" aria-label="${safe(title)}"></canvas><div id="${id}-scale" class="map-legend"></div><div id="${id}-scope-legend" class="map-scope-legend"></div></div>`;
}
function map(id,values,maximum,{boundary=false,diverging=false,outlineOnly=false}={}) {
  const canvas=$(id);if(!canvas)return;
  const {ctx,width,height}=context(canvas),geometry=state.run.geometry;
  const reference=state.run.scope_info?.status==="changed"?state.run.scope_info.overlay_geometry:null;
  const showReference=!!reference&&(outlineOnly||state.showReference);
  const points=[...polygonRings(geometry.scope),...(showReference?polygonRings(reference):[])].flat(2);
  if(!points.length){ctx.fillStyle="#718196";ctx.fillText("缺少保存的区域几何",20,30);return;}
  let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
  points.forEach(([x,y])=>{minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);});
  const scale=Math.min((width-70)/(maxX-minX||1),(height-65)/(maxY-minY||1))*(outlineOnly?1:state.zoom);
  const X=x=>width/2+(x-(minX+maxX)/2)*scale+(outlineOnly?0:state.pan[0]),Y=y=>height/2-8-(y-(minY+maxY)/2)*scale+(outlineOnly?0:state.pan[1]);
  const path=geo=>{const p=new Path2D();polygonRings(geo).forEach(rings=>rings.forEach(ring=>{ring.forEach(([x,y],i)=>i?p.lineTo(X(x),Y(y)):p.moveTo(X(x),Y(y)));p.closePath();}));return p;};
  const paths=geometry.cells.map(cell=>path(cell.geometry));
  ctx.fillStyle="#f8fafc";ctx.fill(path(geometry.scope),"evenodd");
  ctx.strokeStyle="#dbe4ed";ctx.lineWidth=1;
  for(const line of geometry.roads.lines){ctx.beginPath();line.forEach(([x,y],i)=>i?ctx.lineTo(X(x),Y(y)):ctx.moveTo(X(x),Y(y)));ctx.stroke();}
  if(!outlineOnly)paths.forEach((p,i)=>{ctx.fillStyle=boundary?"rgba(235,241,247,.65)":tint(values[i],maximum,diverging);ctx.fill(p,"evenodd");ctx.strokeStyle="#c2cfdd";ctx.lineWidth=.55;ctx.stroke(p);});
  // Matching road shapes remain visible over filled cells as spatial context.
  ctx.strokeStyle="rgba(75,104,135,.35)";ctx.lineWidth=.85;
  for(const line of geometry.roads.lines){ctx.beginPath();line.forEach(([x,y],i)=>i?ctx.lineTo(X(x),Y(y)):ctx.moveTo(X(x),Y(y)));ctx.stroke();}
  const boundarySegments=[];
  if(boundary&&!outlineOnly){geometry.boundaries.forEach((item,i)=>{
    ctx.strokeStyle=tint(values[i],maximum,diverging);ctx.lineWidth=i===state.boundary?5.5:2.5;
    lineStrings(item.geometry).forEach(line=>{const segment=line.map(([x,y])=>[X(x),Y(y)]);boundarySegments.push({index:i,points:segment});ctx.beginPath();segment.forEach(([x,y],j)=>j?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.stroke();});
    if(i===state.boundary){ctx.setLineDash([3,3]);ctx.strokeStyle="#183853";ctx.lineWidth=1;lineStrings(item.geometry).forEach(line=>{ctx.beginPath();line.forEach(([x,y],j)=>j?ctx.lineTo(X(x),Y(y)):ctx.moveTo(X(x),Y(y)));ctx.stroke();});ctx.setLineDash([]);}
  });}else if(!outlineOnly&&paths[state.cell]){ctx.strokeStyle="#e69229";ctx.lineWidth=2.3;ctx.stroke(paths[state.cell]);}
  ctx.strokeStyle="#56708d";ctx.lineWidth=1.2;ctx.stroke(path(geometry.scope));
  if(showReference){ctx.strokeStyle="#dc8c27";ctx.lineWidth=1.7;ctx.setLineDash([6,4]);ctx.stroke(path(reference));ctx.setLineDash([]);}
  ctx.fillStyle="#718196";ctx.textAlign="right";ctx.fillText("N ↑",width-12,20);
  const distance=scale>0?Math.pow(10,Math.floor(Math.log10(70/scale))):1;
  ctx.strokeStyle="#64788d";ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(15,height-20);ctx.lineTo(15+distance*scale,height-20);ctx.stroke();ctx.textAlign="left";ctx.fillText(`${fmt(distance,2)} m`,15,height-27);
  const sample=state.run.samples[state.run.maps.indices[nearestMap()]];
  ctx.textAlign="right";ctx.fillText(`${fmt(sample.time_seconds,1)} s`,width-12,height-12);
  if(!outlineOnly&&!values.some(valid)){ctx.fillStyle="rgba(255,255,255,.88)";ctx.fillRect(20,height/2-20,width-40,40);ctx.fillStyle="#788a9f";ctx.textAlign="center";ctx.fillText("本阶段暂无有效观测",width/2,height/2+4);}
  if(outlineOnly){$(id+"-scale").innerHTML='<span><i></i>本轮实际统计边界</span><span><i class="reference"></i>已同步预设边界（仅对照）</span>';return;}
  $(id+"-scale").innerHTML=`<span>${diverging?"−"+fmt(maximum,4):"0"}</span><span class="gradient ${diverging?"diverging":""}"></span><span>${fmt(maximum,4)} ${state.metric.code==="A4"?"m/s":"人/m²"}</span><span>灰：无数据</span>`;
  const scopeLegend=$(id+"-scope-legend");
  if(scopeLegend)scopeLegend.innerHTML=showReference?'<span><i></i>本轮实际统计边界</span><span><i class="reference"></i>已同步预设边界（仅对照）</span>':"";
  const point=event=>{const rect=canvas.getBoundingClientRect();return [event.clientX-rect.left,event.clientY-rect.top];};
  const pick=event=>{const [x,y]=point(event);
    if(boundary){let best=-1,distance=9;boundarySegments.forEach(segment=>{for(let j=1;j<segment.points.length;j++){const [ax,ay]=segment.points[j-1],[bx,by]=segment.points[j],dx=bx-ax,dy=by-ay,t=Math.max(0,Math.min(1,((x-ax)*dx+(y-ay)*dy)/(dx*dx+dy*dy||1))),d=Math.hypot(x-ax-t*dx,y-ay-t*dy);if(d<distance){distance=d;best=segment.index;}}});return best;}
    // Coordinates use the canvas's physical pixels because the context is scaled for retina.
    const dpr=window.devicePixelRatio||1;
    return paths.findIndex(p=>ctx.isPointInPath(p,x*dpr,y*dpr,"evenodd"));
  };
  canvas.onclick=event=>{const index=pick(event);if(index>=0){if(boundary)state.boundary=index;else state.cell=index;renderMetric();}};
  canvas.onmousemove=event=>{const index=pick(event),item=(boundary?geometry.boundaries:geometry.cells)[index];canvas.title=item?`${item.id}\n${fmt(values[index],5)} ${state.metric.code==="A4"?"m/s":"人/m²"}`:"点击片区或边界查看详情；滚轮可缩放";};
  canvas.onwheel=event=>{event.preventDefault();const old=state.zoom,[px,py]=point(event);state.zoom=Math.max(.5,Math.min(8,state.zoom*(event.deltaY<0?1.2:1/1.2)));const k=state.zoom/old;state.pan=[(state.pan[0]-(px-width/2))*k+px-width/2,(state.pan[1]-(py-height/2+8))*k+py-height/2-8];renderMetric();};
}

function mapTools(boundary=false) {
  const items=boundary?state.run.geometry.boundaries:state.run.geometry.cells,index=boundary?state.boundary:state.cell;
  return `<div class="map-tools"><label>${boundary?"边界":"片区"}<select id="element-select">${items.map((item,i)=>`<option value="${i}" ${i===index?"selected":""}>${safe(item.id)}</option>`).join("")}</select></label>${boundary?`<label>差值<select id="boundary-mode"><option value="signed" ${state.absolute?"":"selected"}>有符号：A − B</option><option value="absolute" ${state.absolute?"selected":""}>绝对值</option></select></label>`:""}<button id="zoom-in" aria-label="地图放大">＋</button><button id="zoom-out" aria-label="地图缩小">−</button><button id="zoom-reset">还原视野</button>${state.run.scope_info?.overlay_geometry&&state.run.scope_info.status==="changed"?`<label><input type="checkbox" id="reference-toggle" ${state.showReference?"checked":""}>显示预设对照边界</label>`:""}<span class="muted">点击选取 · 滚轮缩放</span></div>`;
}
function bindMapTools(boundary=false) {
  $("element-select").onchange=event=>{if(boundary)state.boundary=+event.target.value;else state.cell=+event.target.value;renderMetric();};
  if(boundary)$("boundary-mode").onchange=event=>{state.absolute=event.target.value==="absolute";renderMetric();};
  if($("reference-toggle"))$("reference-toggle").onchange=event=>{state.showReference=event.target.checked;state.zoom=1;state.pan=[0,0];renderMetric();};
  $("zoom-in").onclick=()=>{state.zoom=Math.min(8,state.zoom*1.3);renderMetric();};
  $("zoom-out").onclick=()=>{state.zoom=Math.max(.5,state.zoom/1.3);renderMetric();};
  $("zoom-reset").onclick=()=>{state.zoom=1;state.pan=[0,0];renderMetric();};
}
function selectedDetail(boundary=false,field="density") {
  const run=state.run,k=nearestMap(),sample=run.samples[run.maps.indices[k]];
  if(boundary){const item=run.geometry.boundaries[state.boundary];if(!item)return '<div class="message">未保存边界几何</div>';
    return `<h3>选中边界</h3><div>${safe(item.id)}</div><dl><dt>A 侧片区</dt><dd>${safe(item.a)}</dd><dt>B 侧片区</dt><dd>${safe(item.b || "区域外（未观测）")}</dd><dt>A → B 方向</dt><dd>${safe(item.direction)}</dd><dt>A − B</dt><dd>${fmt(run.maps.boundary[k][state.boundary],5)} 人/m²</dd><dt>快照时间</dt><dd>${fmt(sample.time_seconds,1)} s</dd></dl>`;
  }
  const cell=run.geometry.cells[state.cell];if(!cell)return '<div class="message">未保存片区几何</div>';
  return `<h3>选中片区</h3><div>${safe(cell.id)}</div><dl><dt>实际面积</dt><dd>${fmt(cell.area,3)} m²</dd><dt>人数</dt><dd>${fmt(run.maps.count[k][state.cell],0)} 人</dd><dt>${field==="speed"?"平均速度":"密度"}</dt><dd>${fmt(run.maps[field][k][state.cell],5)} ${field==="speed"?"m/s":"人/m²"}</dd><dt>快照时间</dt><dd>${fmt(sample.time_seconds,1)} s</dd></dl>`;
}
function spatialMetric() {
  const code=state.metric.code,boundary=code==="A5",field=code==="A4"?"speed":boundary?"boundary":"density";
  const run=state.run,k=nearestMap(),index=boundary?state.boundary:state.cell,range=run.ranges[field];
  $("metric-content").innerHTML=mapTools(boundary)+`<div class="map-layout">${mapBox("spatial-map",boundary?"边界两侧密度差":code==="A4"?"片区平均速度":"片区密度")}<div><div class="map-detail">${selectedDetail(boundary,field)}</div><div class="spacer"></div>${plotBox("local-chart","选中位置的时间曲线",state.metric.unit,true)}</div></div><p class="map-caption">${safe(run.geometry.roads.note)} 空间图例在本轮全部快照间固定。</p>`;
  const values=run.maps[field][k].map(v=>boundary&&state.absolute&&valid(v)?Math.abs(v):v);
  map("spatial-map",values,range,{boundary,diverging:boundary&&!state.absolute});
  const curves=[{label:boundary?"A − B":code==="A4"?"平均速度":"密度",color:COLOR.blue,values:run.maps[field].map(row=>boundary&&state.absolute&&valid(row[index])?Math.abs(row[index]):row[index])}];
  // Signed boundary curves need a genuine signed vertical axis.
  if(boundary&&!state.absolute)signedChart("local-chart",curves[0],run.maps.indices.map(i=>run.samples[i].time_seconds));
  else chart("local-chart",curves,{times:run.maps.indices.map(i=>run.samples[i].time_seconds)});
  bindMapTools(boundary);
}
function signedChart(id,line,times) {
  const canvas=$(id),{ctx,width,height}=context(canvas),pad={l:60,r:15,t:18,b:35};
  const maximum=Math.max(.00001,...line.values.filter(valid).map(Math.abs))*1.1,minT=times[0],maxT=times.at(-1);
  const x=t=>pad.l+(maxT===minT?.5:(t-minT)/(maxT-minT))*(width-pad.l-pad.r);
  const y=v=>pad.t+(1-v/maximum)/2*(height-pad.t-pad.b);
  ctx.textAlign="right";
  for(let k=-2;k<=2;k++){const v=maximum*k/2,Y=y(v);ctx.strokeStyle=k===0?"#b9cadd":"#e9eef5";ctx.beginPath();ctx.moveTo(pad.l,Y);ctx.lineTo(width-pad.r,Y);ctx.stroke();ctx.fillStyle="#8493a6";ctx.fillText(fmt(v,5),pad.l-7,Y+4);}
  ctx.textAlign="center";for(let k=0;k<=4;k++){if(minT===maxT&&k!==2)continue;const t=minT+(maxT-minT)*k/4;ctx.fillText(fmt(t,1),x(t),height-14);}
  ctx.strokeStyle=line.color;ctx.lineWidth=2;ctx.beginPath();let active=false;
  line.values.forEach((v,i)=>{if(!valid(v)){active=false;return;}if(active)ctx.lineTo(x(times[i]),y(v));else ctx.moveTo(x(times[i]),y(v));active=true;});ctx.stroke();
  if(times.length===1&&valid(line.values[0])){ctx.fillStyle=line.color;ctx.beginPath();ctx.arc(x(times[0]),y(line.values[0]),4,0,Math.PI*2);ctx.fill();}
  const cursor=x(current().time_seconds);ctx.strokeStyle="#617b9f";ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(cursor,pad.t);ctx.lineTo(cursor,height-pad.b);ctx.stroke();ctx.setLineDash([]);
  if(!line.values.some(valid)){ctx.fillStyle="#8694a6";ctx.textAlign="center";ctx.fillText("本边界没有可用的两侧观测",width/2,height/2);}
  $(id+"-legend").innerHTML=`<span><i style="background:${line.color}"></i>A 侧 − B 侧（人/m²）</span>`;
  canvas.onclick=event=>{const rect=canvas.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(event.clientX-rect.left-pad.l)/(width-pad.l-pad.r))),target=minT+(maxT-minT)*ratio;let best=0;state.run.samples.forEach((s,i)=>{if(Math.abs(s.time_seconds-target)<Math.abs(state.run.samples[best].time_seconds-target))best=i;});state.frame=best;refresh();};
  canvas.onmousemove=event=>{const rect=canvas.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(event.clientX-rect.left-pad.l)/(width-pad.l-pad.r))),target=minT+(maxT-minT)*ratio;let best=0;times.forEach((t,i)=>{if(Math.abs(t-target)<Math.abs(times[best]-target))best=i;});canvas.title=`${fmt(times[best],1)} s\nA − B: ${fmt(line.values[best],5)} 人/m²`;};
}
function valueCard(label,value,unit,detail="") {
  return `<div class="value-card"><div class="stat-label">${safe(label)}</div><div class="stat-value">${fmt(value,6)}<small>${safe(unit)}</small></div><div class="stat-detail">${safe(detail)}</div></div>`;
}
function evacuationMetric() {
  const run=state.run,e=run.evacuation,code=state.metric.code;
  const status=run.metrics[state.metric.id].status;
  const hint=e.source==="legacy"?"这份旧版记录未采集策略应用与疏散结束，不能用最后一个快照补作结束。":
    e.ta===null?"本轮尚未记录有效疏散策略的成功应用。":e.te===null?"本轮尚未达到正常行程全部完成的疏散结束条件；策略后已记录时长不是最终疏散时间。":"已有完整正常到达与快照提交证据。";
  const cards=code==="B1"?valueCard("最终疏散时间",e.duration,"仿真秒",STATUS[status] || status)+valueCard("策略后已记录时长",e.te===null?e.elapsed_since_strategy:e.duration,"仿真秒",e.te===null?"进行中或提前终止的过程量":"已正常结束")+valueCard("事件全过程时长",e.te!==null&&e.t0!==null?e.te-e.t0:null,"仿真秒","te − t0"):
    valueCard("最终疏散效率",e.efficiency,"人/(m²·仿真秒)",STATUS[status] || status)+valueCard("策略应用时密度",e.strategy_global_density,"人/m²","ρa")+valueCard("正常结束时密度",e.final_global_density,"人/m²","ρe");
  const timeline=`<div class="timeline">${[["t0 · 首次运行",e.t0],["ta · 首次有效疏散策略",e.ta],["te · 全部正常到达",e.te]].map(([label,t])=>`<div class="phase ${t===null?"missing":""}"><strong>${safe(label)}</strong><span>${valid(t)?fmt(t,1)+" s":"未记录 / 未达到"}</span></div>`).join("")}</div>`;
  $("metric-content").innerHTML=`<div class="inline-note">${safe(hint)} 完成口径：${safe(e.basis)}。</div><div class="value-grid">${cards}</div>${timeline}<div class="charts-grid">${plotBox("evac-progress","本轮目标人群的完成进度","人")}${plotBox("evac-density","区域密度过程","人/m²")}</div>`;
  chart("evac-progress",[
    {label:"剩余目标人数",color:COLOR.orange,values:e.progress_series.map(p=>p?.remaining_person_count??null)},
    {label:"正常到达人数",color:COLOR.teal,values:e.progress_series.map(p=>p?.normally_arrived_person_count??null)},
  ]);
  chart("evac-density",[series("density_person_per_m2","区域密度",COLOR.blue)]);
  const p=e.progress;
  if(Object.keys(p).length)$("metric-content").insertAdjacentHTML("beforeend",`<div class="table-wrap"><table><thead><tr><th>目标人数</th><th>剩余人数</th><th>正常到达</th><th>显式移除</th><th>未知消失</th><th>账本误差</th></tr></thead><tbody><tr>${["target_person_count","remaining_person_count","normally_arrived_person_count","explicitly_removed_person_count","unknown_disappearance_count","conservation_error"].map(key=>`<td>${fmt(p[key],0)}</td>`).join("")}</tr></tbody></table></div>`);
  if(e.policies.length)$("metric-content").insertAdjacentHTML("beforeend",`<div class="table-wrap"><table><thead><tr><th>策略类型</th><th>应用时间 / 仿真秒</th><th>建立疏散基线</th><th>策略编号</th></tr></thead><tbody>${e.policies.map(p=>`<tr><td>${safe(p.name)}</td><td>${fmt(p.applied_at,1)}</td><td>${p.starts_evacuation?"是":"否"}</td><td>${safe(p.strategy_id || "—")}</td></tr>`).join("")}</tbody></table></div>`);
}
function densityDifferenceMetric() {
  const run=state.run,k=nearestMap(),index=state.cell,e=run.evacuation;
  const hint=e.source==="legacy"?"由旧记录的首次成功 start 命令和对应完整快照补算初始—运行时差；旧版没有记录正常结束，另外两组差不可恢复。":
    e.te===null?"尚未正常结束：初始—运行时差可查看，涉及结束分布的两组差暂时为空。":"三组差使用同一固定网格与同一颜色范围。";
  $("metric-content").innerHTML=`<div class="inline-note">${safe(hint)} 初始时刻：${fmt(e.t0,1)} s；结束时刻：${fmt(e.te,1)} s。</div>`+mapTools()+`<div class="three-maps">${mapBox("diff-initial-runtime","|初始 − 运行时|",true)}${mapBox("diff-runtime-final","|运行时 − 结束|",true)}${mapBox("diff-initial-final","|初始 − 结束|",true)}</div><div class="spacer"></div><div class="charts-grid">${plotBox("diff-mean","全区域面积加权的局部绝对差均值","人/m²")}${plotBox("diff-cell",`选中片区：${run.geometry.cells[index]?.id || "未保存"}`,"人/m²")}</div><p class="map-caption">曲线为先对每个片区取绝对差，再按面积加权；不等于全局平均密度之差的绝对值。</p>`;
  const keys=["initialRuntime","runtimeFinal","initialFinal"],labels=["初始—运行时","运行时—结束","初始—结束"],colors=[COLOR.blue,COLOR.teal,COLOR.orange];
  ["diff-initial-runtime","diff-runtime-final","diff-initial-final"].forEach((id,i)=>map(id,run.maps[keys[i]][k],run.ranges.difference));
  chart("diff-mean",keys.map((key,i)=>({label:labels[i],color:colors[i],values:run.density_difference_means.map(row=>row[i])})));
  chart("diff-cell",keys.map((key,i)=>({label:labels[i],color:colors[i],values:run.maps[key].map(row=>row[index])})),{times:run.maps.indices.map(i=>run.samples[i].time_seconds)});
  bindMapTools();
}
function stateSeries(definitions,prefix) {
  return definitions.map(([key,label,color])=>({label,color,values:state.run.samples.map(sample=>{
    const value=sample[`${prefix}_${key}_count`];return !state.percent?value:valid(value)&&sample.person_count>0?100*value/sample.person_count:null;
  })}));
}
function transitionTable(key,definitions) {
  const names=Object.fromEntries(definitions.map(([key,label])=>[key,label])),rows=state.run.transition_matrices[key];
  return `<div class="table-wrap"><table><thead><tr><th>实际转换 · 从</th><th>到</th><th>次数（全轮）</th></tr></thead><tbody>${rows.length?rows.map(([from,to,count])=>`<tr><td>${safe(names[from] || from)}</td><td>${safe(names[to] || to)}</td><td>${fmt(count,0)}</td></tr>`).join(""):'<tr><td colspan="3">没有可验证的实际状态转换，首次建立标签不计入。</td></tr>'}</tbody></table></div>`;
}
function stateMetric() {
  const psychology=state.metric.code==="C2",definitions=psychology?PSYCHOLOGY:BEHAVIOR,dimension=psychology?"psychological_state":"behavior_state",run=state.run;
  $("metric-content").innerHTML=`<div class="map-tools"><label>展示口径<select id="state-mode"><option value="count" ${state.percent?"":"selected"}>人数</option><option value="percent" ${state.percent?"selected":""}>区域内占比</option></select></label><span class="muted">无人时占比为空；未知人员单独列出</span></div><div class="charts-grid">${plotBox("state-count",psychology?"心理状态构成":"行为状态构成",state.percent?"%":"人")}${plotBox("state-extra",psychology?"模型心理变量均值":"逐快照实际转换与区域出入",psychology?"0–1":"事件次数")}</div><div class="spacer"></div>${plotBox("state-transitions",psychology?"逐快照心理状态实际转换":"附加拥挤标签","事件次数 / 人",true)}${transitionTable(dimension,definitions)}<p class="map-caption">转换次数表示相邻快照之间发生的事件数量；初始化标签、首次区域归属均已排除。非首次区域进入或离开单独显示，不作为行为转换。</p>`;
  chart("state-count",stateSeries(definitions,psychology?"psychology":"behavior"),{stacked:true,percent:state.percent});
  if(psychology){chart("state-extra",[series("stress_avg","压力",COLOR.red),series("fatigue_avg","疲劳",COLOR.orange),series("perceived_risk_avg","感知风险",COLOR.purple),series("perceived_crowding_avg","感知拥挤",COLOR.blue)],{max:1});
    chart("state-transitions",[{label:"心理状态转换",color:COLOR.purple,values:run.transitions.map(t=>t?t.psychological_state || 0:null)}]);
  }else{chart("state-extra",[
      {label:"行为转换",color:COLOR.purple,values:run.transitions.map(t=>t?t.behavior_state || 0:null)},
      {label:"进入区域",color:COLOR.teal,values:run.transitions.map(t=>t?t.scope_entry || 0:null)},
      {label:"离开区域",color:COLOR.orange,values:run.transitions.map(t=>t?t.scope_exit || 0:null)},
    ]);chart("state-transitions",[series("crowded_person_count","拥挤附加标签",COLOR.orange),series("crowding_unknown_count","拥挤状态未知",COLOR.gray)]);
  }
  $("state-mode").onchange=event=>{state.percent=event.target.value==="percent";renderMetric();};
}
function renderMetric() {
  const metric=state.metric,run=state.run;
  $("metric-code").textContent=`${metric.code} / ${metric.unit}`;
  $("metric-title").textContent=metric.name;
  $("metric-description").textContent=DESCRIPTION[metric.code];
  pill($("metric-status"),run.metrics[metric.id]?.status);
  $("export-csv").disabled=run.metrics[metric.id]?.status==="not_selected";
  $("export-png").disabled=run.metrics[metric.id]?.status==="not_selected";
  if(run.metrics[metric.id]?.status==="not_selected"){
    $("metric-content").innerHTML='<div class="message"><strong>本轮未选择此指标</strong>查看器不会将其他记录或依赖计算的数据冒作本轮指标。</div>';return;
  }
  if(metric.code==="A1"){
    $("metric-content").innerHTML=`<div class="charts-grid">${plotBox("global-density","区域平均密度","人/m²")}${plotBox("global-count","人数与观测范围","人")}</div>`;
    chart("global-density",[series("density_person_per_m2","全局密度",COLOR.blue)]);
    chart("global-count",[series("person_count","区域内",COLOR.blue),series("network_person_count","全路网",COLOR.teal),series("outside_scope_person_count","区域外",COLOR.orange),series("invalid_position_count","位置无效",COLOR.gray)]);
  }else if(metric.code==="A3"){
    $("metric-content").innerHTML=`<div class="charts-grid">${plotBox("global-speed","区域平均速度","m/s")}${plotBox("speed-samples","速度样本与运动人数","人")}</div>`;
    chart("global-speed",[series("avg_speed_mps","含停留的平均速度",COLOR.blue),series("moving_avg_speed_mps","仅运动人员平均速度",COLOR.teal)]);
    chart("speed-samples",[series("speed_sample_count","有效速度样本",COLOR.blue),series("moving_person_count","运动人员",COLOR.teal),series("invalid_speed_count","无效速度",COLOR.orange)]);
  }else if(["A2","A4","A5"].includes(metric.code))spatialMetric();
  else if(["B1","B2"].includes(metric.code))evacuationMetric();
  else if(metric.code==="B3")densityDifferenceMetric();
  else stateMetric();
}

function download(content,name,type) {
  const link=document.createElement("a"),url=URL.createObjectURL(new Blob([content],{type}));
  link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function csv() {
  const code=state.metric.code,run=state.run,rows=[],globals=run.samples;
  if(["A2","A4","A5","B3"].includes(code)){
    const boundary=code==="A5",items=boundary?run.geometry.boundaries:run.geometry.cells;
    const keys=code==="A2"?["count","density"]:code==="A4"?["count","speed"]:code==="A5"?["boundary"]:["initialRuntime","runtimeFinal","initialFinal"];
    const headers={count:"person_count",density:"density_person_per_m2",speed:"avg_speed_mps",boundary:"signed_difference_person_per_m2",initialRuntime:"absolute_initial_runtime_person_per_m2",runtimeFinal:"absolute_runtime_final_person_per_m2",initialFinal:"absolute_initial_final_person_per_m2"};
    rows.push(["run_id","snapshot_id","time_seconds",boundary?"boundary_id":"cell_id",...keys.map(key=>headers[key])]);
    run.maps.indices.forEach((index,k)=>items.forEach((item,j)=>rows.push([run.id,globals[index].snapshot_id,globals[index].time_seconds,item.id,...keys.map(key=>run.maps[key][k][j])])));
  }else if(["B1","B2"].includes(code)){
    const e=run.evacuation;rows.push(["run_id","status","event_start_seconds","strategy_applied_seconds","completion_seconds","evacuation_time_seconds","evacuation_efficiency_person_per_m2_per_s","strategy_density_person_per_m2","completion_density_person_per_m2"]);
    rows.push([run.id,run.metrics[state.metric.id].status,e.t0,e.ta,e.te,e.duration,e.efficiency,e.strategy_global_density,e.final_global_density]);
  }else{
    const fields=code==="A1"?["person_count","network_person_count","outside_scope_person_count","invalid_position_count","area_m2","density_person_per_m2"]:
      code==="A3"?["avg_speed_mps","moving_avg_speed_mps","speed_sample_count","invalid_speed_count","moving_person_count"]:
      code==="C1"?[...BEHAVIOR.map(([key])=>`behavior_${key}_count`),"crowded_person_count","crowding_unknown_count"]:
      [...PSYCHOLOGY.map(([key])=>`psychology_${key}_count`),"stress_avg","fatigue_avg","perceived_risk_avg","perceived_crowding_avg"];
    rows.push(["run_id","snapshot_id","time_seconds",...fields,...(code==="C1"?["behavior_transition_count","scope_entry_count","scope_exit_count"]:code==="C2"?["psychology_transition_count"]:[])]);
    globals.forEach((sample,i)=>{const t=run.transitions[i],extras=code==="C1"?[t?t.behavior_state || 0:null,t?t.scope_entry || 0:null,t?t.scope_exit || 0:null]:code==="C2"?[t?t.psychological_state || 0:null]:[];rows.push([run.id,sample.snapshot_id,sample.time_seconds,...fields.map(field=>sample[field]),...extras]);});
  }
  rows[0].splice(1,0,"scope_version","scope_area_m2");
  rows.slice(1).forEach(row=>row.splice(1,0,run.scope_info?.version || "",run.area));
  // Spreadsheet formula-like strings are escaped; actual numeric negatives stay numeric.
  const escape=value=>{let text=String(value??"");if(typeof value==="string"&&/^[=+@\-]/.test(text))text="'"+text;return '"'+text.replace(/"/g,'""')+'"';};
  download("\ufeff"+rows.map(row=>row.map(escape).join(",")).join("\r\n"),`${run.id}_${code}.csv`,"text/csv;charset=utf-8");
}
function png() {
  const canvases=[...$("metric-content").querySelectorAll("canvas")];if(!canvases.length)return;
  const scale=2,width=1000,heights=canvases.map(canvas=>Math.round(920*canvas.clientHeight/canvas.clientWidth));
  const evacuation=["B1","B2"].includes(state.metric.code);
  const headerHeight=evacuation?170:130;
  const result=document.createElement("canvas");result.width=width*scale;result.height=(headerHeight+heights.reduce((sum,h)=>sum+h+80,0))*scale;
  const ctx=result.getContext("2d");ctx.scale(scale,scale);ctx.fillStyle="#fff";ctx.fillRect(0,0,width,result.height/scale);
  ctx.fillStyle="#223348";ctx.font='22px -apple-system,"PingFang SC",sans-serif';ctx.fillText(`${state.metric.code} · ${state.metric.name} · ${state.run.scene_name}`,40,40);
  ctx.font='12px -apple-system,"PingFang SC",sans-serif';ctx.fillStyle="#6b7c8e";ctx.fillText(`${state.run.id} · 仿真 ${fmt(current().time_seconds,1)} s · ${STATUS[state.run.metrics[state.metric.id].status] || ""}`,40,67);
  ctx.fillText(`本轮统计范围: ${state.run.scope_info?.version || "未记录"} · ${fmt(state.run.area,2)} m²${state.run.scope_info?.status==="changed"?" · 与预设范围不同，指标保留原值":""}`,40,92);
  if(evacuation){const e=state.run.evacuation;ctx.fillText(`疏散时间: ${fmt(e.duration,4)} s · 疏散效率: ${fmt(e.efficiency,6)} 人/(m²·s) · t0: ${fmt(e.t0,1)} · ta: ${fmt(e.ta,1)} · te: ${fmt(e.te,1)}`,40,122);}
  let top=headerHeight;
  canvases.forEach((canvas,i)=>{ctx.fillStyle="#223348";ctx.font='14px -apple-system,"PingFang SC",sans-serif';ctx.fillText(canvas.getAttribute("aria-label") || state.metric.name,40,top);top+=10;ctx.drawImage(canvas,40,top,920,heights[i]);top+=heights[i]+18;ctx.font='11px -apple-system,"PingFang SC",sans-serif';ctx.fillStyle="#6b7c8e";const legend=canvas.parentElement.querySelector(".legend,.map-legend");if(legend)ctx.fillText(legend.textContent,40,top);const scopeLegend=canvas.parentElement.querySelector(".map-scope-legend");if(scopeLegend?.textContent)ctx.fillText(scopeLegend.textContent,40,top+18);top+=52;});
  result.toBlob(blob=>download(blob,`${state.run.id}_${state.metric.code}_${current().time_seconds}s.png`,"image/png"));
}

const scenes=new Map(DATA.runs.map(run=>[run.scene_id,run.scene_name]));
scenes.forEach((name,id)=>option($("scene-select"),id,name));
$("scene-select").value=state.run.scene_id;
$("scene-select").onchange=event=>sceneChanged(event.target.value);
$("run-select").onchange=event=>runChanged(event.target.value);
$("time-slider").oninput=event=>{state.frame=+event.target.value;refresh();};
$("play").onclick=()=>{if(state.timer){stop();return;}if(state.frame===state.run.samples.length-1)state.frame=0;$("play").textContent="暂停";state.timer=setInterval(()=>{state.frame=Math.min(state.frame+1,state.run.samples.length-1);refresh();if(state.frame===state.run.samples.length-1)stop();},200);};
$("export-json").onclick=()=>download(JSON.stringify(DATA),"experiment-viewer-data.json","application/json");
$("export-csv").onclick=csv;$("export-png").onclick=png;
$("build-info").textContent=`生成于 ${new Date(DATA.generated_at).toLocaleString("zh-CN")} · ${DATA.runs.length} 份实验`;
let resizeTimer;window.addEventListener("resize",()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{renderMetric();if($("scope-panel").querySelector("details[open]"))map("scope-comparison-map",[],0,{outlineOnly:true});},120);});
sceneChanged(state.run.scene_id);
