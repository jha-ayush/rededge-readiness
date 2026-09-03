/*
 * app.js
 *
 * The readiness client for the web page.
 *
 * This lives in its own file so the page can forbid inline script outright.
 * With the code inline, the Content Security Policy had to allow
 * script-src 'unsafe-inline', which also permits any inline handler that ever
 * got injected into the document. Nothing injects one today, because camera
 * data reaches the DOM through textContent and URL parameters are sanitized,
 * but a policy that cannot be violated is worth more than a policy that
 * currently is not.
 *
 * Served by Cloudflare alongside the page, and by "rededge.py serve" in the
 * field. No build step, no bundler, no dependencies: it is the same file in
 * both places, readable as shipped.
 */
"use strict";

/* ---------- config (URL params, no storage) ---------- */
const DEFAULTS = {
  url:"http://192.168.10.254", sd:2, sats:6, pacc:5, volts:4.2,
  cams:0, poll:3, fw:"", dls:false
};

/* The camera is a local device by definition: it serves its API over the WiFi
   or Ethernet link you are physically joined to. So a camera URL arriving in a
   query string did not come from the pilot, it came from whoever wrote the
   link, and a link that quietly repointed this tool at a foreign host could
   show a fabricated GO for a camera nobody is holding. Query strings are
   therefore treated as untrusted and restricted to local addresses. Settings,
   typed by the pilot on purpose, is not restricted. */
function isLocalCameraUrl(u){
  if(!u) return false;
  // A leading "//" is not a path, it is a protocol-relative URL: "//evil.example"
  // resolves to a remote origin. Only a single leading slash is same-origin.
  if(u.startsWith("//")) return false;
  if(u.startsWith("/")) return true;                 // same-origin proxy, e.g. /cam
  let h;
  try{ h=new URL(u, location.href).hostname; }catch(_){ return false; }
  if(h==="localhost" || h.endsWith(".local")) return true;
  if(/^127\./.test(h)) return true;                  // loopback
  if(/^10\./.test(h)) return true;                   // RFC1918
  if(/^192\.168\./.test(h)) return true;             // RFC1918
  if(/^172\.(1[6-9]|2\d|3[01])\./.test(h)) return true;
  if(/^169\.254\./.test(h)) return true;             // link-local
  return false;
}

/* Both entry points into the config, a shared link and the Settings form, run
   through the same sanitizer. Keeping one copy is the point: the two paths
   previously clamped differently, so a value rejected from a link was accepted
   from the form. */
function sanitizeCfg(c){
  for(const k of Object.keys(DEFAULTS)){
    if(typeof DEFAULTS[k]!=="number") continue;
    const n=parseFloat(c[k]);
    c[k]=(isFinite(n) && n>=0) ? n : DEFAULTS[k];
  }
  // A zero or absent poll interval becomes setInterval(fn, 0), which polls the
  // camera as fast as the network allows and flattens a phone battery. The
  // ceiling keeps the "next in" countdown honest rather than effectively frozen.
  c.poll=Math.min(Math.max(c.poll,1),3600);
  return c;
}

function loadCfg(){
  const q=new URLSearchParams(location.search), c={...DEFAULTS};
  for(const k of Object.keys(DEFAULTS)){
    if(!q.has(k)) continue;
    const v=q.get(k);
    if(typeof DEFAULTS[k]==="number"){
      /* A malformed number must never silently disable a threshold. NaN
         compares false against everything, so a cfg.sd of NaN makes
         "free < cfg.sd" false, skips the low-space branch entirely, and a
         nearly full card reads GO. The same holds for satellites, position
         error and voltage. sanitizeCfg puts the default back. */
      c[k]=parseFloat(v);
    }
    else if(typeof DEFAULTS[k]==="boolean") c[k]=(v==="1"||v==="true");
    else c[k]=v;
  }
  sanitizeCfg(c);
  if(q.has("url") && !isLocalCameraUrl(c.url)){
    console.warn("Ignoring non-local camera URL from the query string:", c.url);
    c.url=DEFAULTS.url;
  }
  return c;
}
function saveCfg(c){
  const q=new URLSearchParams();
  for(const k of Object.keys(DEFAULTS)){
    const v=(typeof DEFAULTS[k]==="boolean")?(c[k]?"1":"0"):String(c[k]);
    q.set(k,v);
  }
  // Keep the chosen source in the URL so a shared/bookmarked link reopens in
  // the same state (e.g. ?source=demo-go for a presentable demo link).
  const srcEl=document.getElementById("source");
  if(srcEl && srcEl.value && srcEl.value!=="live") q.set("source",srcEl.value);
  // Keep the chosen theme so a shared link matches what you intended.
  const th=document.documentElement.getAttribute("data-theme");
  if(th) q.set("theme",th);
  // Persist to URL when running as a real file/page; ignored in the sandboxed
  // about:srcdoc preview where replaceState with a URL is blocked.
  try{ history.replaceState(null,"","?"+q.toString()); }catch(_){ /* in-memory only */ }
}
let cfg=loadCfg();

/* ---------- state ranking ---------- */
const RANK={"GO":1,"CHECK":2,"UNKNOWN":2,"NO-GO":3};
const worst=arr=>arr.reduce((a,b)=>RANK[b]>RANK[a]?b:a,"GO");

/* ---------- evaluation (pure) ---------- */
function evaluate(d, c){
  // d: {ok, status, network, version}  ok=false means no link
  if(!d.ok){
    return {overall:"NO-GO", reason:"No link to the camera.",
      sub:"Confirm you are joined to the camera WiFi and the base URL is correct.",
      checks:[{label:"Camera link",read:"down",state:"NO-GO",note:"No response from "+c.url}]};
  }
  const s=(d.status&&typeof d.status==="object")?d.status:{},
        net=d.network,
        ver=(d.version&&typeof d.version==="object")?d.version:{};
  const out=[];

  // SD card
  (function(){
    const st=s.sd_status, free=s.sd_gb_free;
    let state="GO", note="card present and writable";
    if(st==="NotPresent"){state="NO-GO";note="no SD card inserted";}
    else if(st==="Full"){state="NO-GO";note="card full, offload before flight";}
    else if(s.sd_warn){state="CHECK";note="low-space warning or unrecommended filesystem";}
    else if(typeof free==="number" && free<c.sd){state="CHECK";note="below "+c.sd+" GB headroom";}
    else if(st!=="Ok"){state="UNKNOWN";note=(st===undefined?"card status not reported":"unrecognized card status");}
    out.push({label:"SD storage",
      read:(typeof free==="number"?free.toFixed(1):"--"),unit:"GB free",state,note});
  })();

  // GPS fix
  (function(){
    const sats=s.gps_used_sats, pacc=s.p_acc, warn=s.gps_warn, tvalid=s.utc_time_valid;
    let state="GO", note="usable fix for geotagging";
    if(sats===undefined){state="UNKNOWN";note="GPS not reported";}
    else if(warn){state="CHECK";note="receiver reports interference";}
    else if(sats<c.sats){state="CHECK";note="only "+sats+" sats, want "+c.sats+"+";}
    else if(typeof pacc==="number" && pacc>c.pacc){state="CHECK";note="position error "+pacc.toFixed(1)+" m";}
    else if(tvalid===false){state="CHECK";note="time not yet valid";}
    out.push({label:"GPS fix",
      read:(sats!==undefined?String(sats):"--"),unit:"sats",state,note});
  })();

  // position accuracy (separate readout)
  (function(){
    const pacc=s.p_acc;
    let state="GO";
    if(pacc===undefined){state="UNKNOWN";}
    else if(pacc>c.pacc){state="CHECK";}
    out.push({label:"Position accuracy",
      read:(typeof pacc==="number"?pacc.toFixed(1):"--"),unit:"m (1\u03c3)",state,
      note:(typeof pacc==="number"?"threshold "+c.pacc+" m":"not reported")});
  })();

  // Light sensor (DLS)
  (function(){
    const dls=s.dls_status;
    let state="GO", note="irradiance sensor active";
    if(dls==="Error"){state="NO-GO";note="DLS error, reflectance data unreliable";}
    else if(dls==="NotPresent"){state=c.dls?"CHECK":"GO";note=c.dls?"no DLS, reflectance calibration limited":"no DLS (not required)";}
    else if(dls==="Programming"||dls==="Initializing"){state="CHECK";note="DLS warming up, wait";}
    else if(dls!=="Ok"){state="UNKNOWN";note=(dls===undefined?"DLS state not reported":"unrecognized DLS state");}
    out.push({label:"Light sensor",read:(dls||"--"),unit:"",state,note});
  })();

  // Power
  (function(){
    const v=s.bus_volts;
    let state="GO", note="supply within configured floor";
    if(v===undefined){state="UNKNOWN";note="voltage not reported";}
    else if(v<c.volts){state="CHECK";note="below "+c.volts+" V floor, verify pack";}
    out.push({label:"Supply voltage",
      read:(typeof v==="number"?v.toFixed(2):"--"),unit:"V",state,note});
  })();

  // Time source
  (function(){
    const ts=s.time_source, valid=s.utc_time_valid;
    let state="GO", note=(ts?ts+" time source":"time valid");
    if(valid===false){state="CHECK";note="UTC time not yet valid";}
    else if(ts===undefined && valid===undefined){state="UNKNOWN";note="time source not reported";}
    out.push({label:"Time source",read:(ts||(valid?"valid":"--")),unit:"",state,note});
  })();

  // Network rig
  (function(){
    if(!net||!Array.isArray(net.network_map)){
      out.push({label:"Camera rig",read:"--",unit:"",state:"UNKNOWN",note:"network status unavailable"});
      return;
    }
    const cams=net.network_map.filter(x=>x.device_type==="Camera");
    const dls=net.network_map.filter(x=>String(x.device_type).startsWith("DLS"));
    let state="GO", note=cams.length+" camera"+(cams.length===1?"":"s")+(dls.length?", DLS present":"");
    // per-device storage
    const cardIssue=cams.some(x=>x.sd_status&&x.sd_status!=="Ok");
    const fwSet=new Set(cams.map(x=>x.sw_version).filter(Boolean));
    if(c.cams>0 && cams.length<c.cams){state="NO-GO";note="only "+cams.length+" of "+c.cams+" cameras online";}
    else if(cardIssue){state="CHECK";note="a networked camera has a card issue";}
    else if(fwSet.size>1){state="CHECK";note="mixed firmware across cameras";}
    else if(c.dls && dls.length===0){state="CHECK";note="no DLS on the network";}
    out.push({label:"Camera rig",read:String(cams.length),unit:"online",state,note});
  })();

  // Firmware
  (function(){
    const v=ver.sw_version;
    let state="GO", note=(v?"running "+v:"version reported");
    if(v===undefined){state="UNKNOWN";note="version not reported";}
    else if(c.fw && v!==c.fw){state="CHECK";note="expected "+c.fw+", running "+v;}
    out.push({label:"Firmware",read:(v||"--"),unit:"",state,note});
  })();

  const overall=worst(out.map(x=>x.state==="UNKNOWN"?"CHECK":x.state));
  let reason, sub;
  const bad=out.filter(x=>x.state==="NO-GO");
  const warns=out.filter(x=>x.state==="CHECK"||x.state==="UNKNOWN");
  if(overall==="GO"){reason="Sensor ready to capture.";sub="All monitored systems within tolerance.";}
  else if(overall==="NO-GO"){reason=bad.map(x=>x.label+": "+x.note).join("; ")+".";sub="Resolve before flying.";}
  else{reason=warns.map(x=>x.label).join(", ")+" need attention.";sub=warns.map(x=>x.label+": "+x.note).join("; ")+".";}
  return {overall, reason, sub, checks:out};
}

/* ---------- data sources ---------- */
async function fetchJSON(base, path, signal){
  const r=await fetch(base.replace(/\/+$/,"")+path,{signal,cache:"no-store"});
  if(!r.ok) throw new Error(path+" "+r.status);
  return r.json();
}
async function readLive(c){
  const ctrl=new AbortController();
  const t=setTimeout(()=>ctrl.abort(),2500);
  try{
    // /status is the critical read. If it fails or is not an object, that is a
    // no-link NO-GO. /version and /networkstatus are best-effort: a flaky
    // secondary endpoint degrades only its own check, not the whole readout.
    const status=await fetchJSON(c.url,"/status",ctrl.signal);
    if(!status||typeof status!=="object") return {ok:false,error:"malformed status"};
    let version=null, network=null;
    try{ version=await fetchJSON(c.url,"/version",ctrl.signal); }catch(_){ }
    try{ network=await fetchJSON(c.url,"/networkstatus",ctrl.signal); }catch(_){ }
    return {ok:true,status,version,network};
  }catch(e){
    return {ok:false,error:String(e.message||e)};
  }finally{ clearTimeout(t); }
}

/* demo fixtures */
const DEMO={
  base:{
    status:{sd_status:"Ok",sd_gb_free:20.1,sd_warn:false,bus_volts:4.69,
      gps_used_sats:9,gps_vis_sats:14,gps_warn:false,p_acc:2.4,
      alt_agl:14.8,dls_status:"Ok",time_source:"GPS",utc_time_valid:true},
    version:{sw_version:"v7.1.0",serial:"RM02-1839163-SC"},
    network:{network_map:[
      {device_type:"Camera",sd_status:"Ok",sw_version:"v7.1.0",bands:[475,560,668,840,717]},
      {device_type:"DLS 2",sw_version:"v1.2.3"}
    ]}
  }
};
function demo(kind){
  if(kind==="demo-down") return {ok:false,error:"demo offline"};
  const d=JSON.parse(JSON.stringify(DEMO.base)); d.ok=true; const s=d.status;
  switch(kind){
    case "demo-sd": s.sd_gb_free=0.7; s.sd_warn=true; break;
    case "demo-nosd": s.sd_status="NotPresent"; break;
    case "demo-gps": s.gps_used_sats=4; break;
    case "demo-pos": s.p_acc=12.0; break;
    case "demo-time": s.utc_time_valid=false; break;
    case "demo-warmup": s.dls_status="Programming"; break;
    case "demo-dls": s.dls_status="Error"; break;
    case "demo-volts": s.bus_volts=3.9; break;
    case "demo-rig": d.network.network_map=[
      {device_type:"Camera",sd_status:"Ok",sw_version:"v7.1.0"},
      {device_type:"Camera",sd_status:"Ok",sw_version:"v7.0.0"},
      {device_type:"DLS 2",sw_version:"v1.2.3"}]; break;
    case "demo-warn": s.sd_gb_free=0.7; s.sd_warn=true; s.bus_volts=3.9; s.gps_used_sats=4; break;
    case "demo-nogo": s.sd_status="NotPresent"; s.dls_status="Error"; break;
  }
  return d;
}

/* ---------- render ---------- */
const el=id=>document.getElementById(id);

/* Plain-language explanation plus the active threshold for each check, so a
   row can show why a value passes or fails without opening Settings. */
function checkDetail(label, c){
  switch(label){
    case "SD storage": return "Card must be present, writable, and keep at least "+c.sd+" GB free.";
    case "GPS fix": return "Wants "+c.sats+" or more satellites with no interference for reliable geotagging.";
    case "Position accuracy": return "Reported position error should stay within "+c.pacc+" m.";
    case "Light sensor": return "DLS irradiance sensor, used for reflectance calibration."+(c.dls?" Required by your settings.":" Optional unless you require it.");
    case "Supply voltage": return "Camera supply should stay above "+c.volts+" V.";
    case "Time source": return "A valid clock is needed for correct image timestamps.";
    case "Camera rig": return "Expected cameras online"+(c.cams>0?" ("+c.cams+")":" (any)")+" with matching firmware.";
    case "Firmware": return c.fw?("Expecting firmware "+c.fw+"."):"Any firmware version is accepted.";
    case "Camera link": return "The tool reaches the camera at "+c.url+" over its WiFi.";
    default: return "";
  }
}

function render(res){
  const b=el("banner");
  if(b.dataset.s!==res.overall){
    // Brief emphasis only when the overall state actually changes.
    b.dataset.s=res.overall;
    el("stateWord").textContent=res.overall;
    b.classList.remove("flash"); void b.offsetWidth; b.classList.add("flash");
  }
  setText(el("reasonMain"), res.reason);
  const subEl=el("reasonSub");
  setText(subEl, res.sub||"");
  subEl.style.display = res.sub ? "" : "none";

  const box=el("checks");
  const rows=res.checks;
  // Reconcile node count without tearing the list down (no flicker on poll).
  while(box.children.length>rows.length) box.removeChild(box.lastChild);
  for(let i=box.children.length;i<rows.length;i++){
    const node=document.createElement("div");
    node.className="check enter";
    node.setAttribute("role","button");
    node.setAttribute("tabindex","0");
    node.setAttribute("aria-expanded","false");
    node.innerHTML='<span class="dot"></span><div class="meta"><div class="label">'
      +'<span class="lbl"></span> <span class="tag"></span><span class="caret">\u203a</span></div>'
      +'<div class="note"></div><div class="desc"></div></div><div class="read"><span class="rv"></span><span class="u"></span></div>';
    const toggle=()=>{ const open=node.classList.toggle("open"); node.setAttribute("aria-expanded", open?"true":"false"); };
    node.addEventListener("click",toggle);
    node.addEventListener("keydown",(e)=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); toggle(); } });
    box.appendChild(node);
    node.style.animationDelay=(i*45)+"ms";
  }
  for(let i=0;i<rows.length;i++){
    const ck=rows[i], node=box.children[i];
    if(node.dataset.s!==ck.state){
      node.dataset.s=ck.state;
      node.querySelector(".dot").className="dot "+ck.state;
      node.querySelector(".tag").className="tag "+ck.state;
      node.querySelector(".tag").textContent=ck.state;
    }
    setText(node.querySelector(".lbl"), ck.label);
    setText(node.querySelector(".note"), ck.note||"");
    setText(node.querySelector(".desc"), checkDetail(ck.label, cfg));
    setText(node.querySelector(".rv"), ck.read);
    setText(node.querySelector(".u"), ck.unit||"");
  }
}
function setText(node,t){ if(node.textContent!==t) node.textContent=t; }
function setLink(up){
  const p=el("linkPill");
  p.classList.remove("demo");
  p.classList.toggle("up",up); p.classList.toggle("down",!up);
  el("linkText").textContent=up?"linked":"no link";
}
function setLinkDemo(){
  const p=el("linkPill");
  p.classList.remove("up","down"); p.classList.add("demo");
  el("linkText").textContent="demo";
}
function stamp(){
  lastStampAt=Date.now();
  paintStatus();
}
let lastStampAt=0, nextAt=0;
/* A GO is the moment a pilot commits to flying, so the readout should say what
   it actually read. Previously the camera address appeared only when the link
   failed, which meant a green result and a green result from the wrong address
   looked identical. */
function fmtAge(ms){
  const sec=Math.floor(ms/1000);
  if(sec<90) return sec+"s";
  const min=Math.floor(sec/60);
  return min<60 ? min+" min" : Math.floor(min/60)+"h "+(min%60)+"m";
}
function sourceLabel(u){
  if(!u) return "unknown source";
  if(u.startsWith("/")) return "local proxy " + u;
  try{ return new URL(u, location.href).host; }catch(_){ return u; }
}
function paintStatus(){
  if(el("source").value!=="live"){
    el("updatedText").textContent="demo data \u00b7 not a live reading";
    return;
  }
  /* A verdict is evidence about the instant it was read and nothing more. If the
     poll loop stalls, a sleeping device, a hung request, a tab left in the
     background, the last green result keeps sitting on screen looking current.
     Past this age it is marked stale rather than left to imply currency. */
  const age=lastStampAt ? Date.now()-lastStampAt : 0;
  const stale=lastStampAt>0 && age>Math.max(30000, cfg.poll*4*1000);
  el("banner").classList.toggle("stale", stale);
  el("staleBadge").hidden=!stale;
  if(stale){
    el("updatedText").textContent=sourceLabel(cfg.url)+" \u00b7 last read "+fmtAge(age)+" ago, not current";
    return;
  }
  const t=new Date(lastStampAt||Date.now());
  let line=sourceLabel(cfg.url)+" \u00b7 updated "+t.toLocaleTimeString([], {hour12:false});
  if(nextAt){
    const secs=Math.max(0,Math.ceil((nextAt-Date.now())/1000));
    line+=" \u00b7 next in "+secs+"s";
  }
  el("updatedText").textContent=line;
}

/* ---------- theme ---------- */
function currentTheme(){ return document.documentElement.getAttribute("data-theme")||"dark"; }
function applyTheme(t){
  document.documentElement.setAttribute("data-theme", t);
  const btn=el("themeBtn");
  btn.innerHTML = (t==="dark") ? "&#9728;" : "&#9790;";   // sun in dark, moon in light
  btn.title = (t==="dark") ? "Switch to light" : "Switch to dark";
  const meta=document.querySelector('meta[name="theme-color"]');
  if(meta) meta.setAttribute("content", t==="dark" ? "#0b0e13" : "#e7ebf0");
}

/* ---------- loop ---------- */
let timer=null, countdownTimer=null, busy=false;
async function tick(){
  if(busy) return; busy=true;
  el("refreshBtn").classList.add("spin");
  const src=el("source").value;
  const isDemo = src!=="live";
  let d;
  if(src==="live"){ d=await readLive(cfg); }
  else { d=demo(src); }
  if(isDemo) setLinkDemo(); else setLink(!!d.ok);
  el("demoBadge").hidden = !isDemo;
  render(evaluate(d,cfg));
  // Contextual help only when trying Live and the camera is unreachable.
  const showHint = (src==="live" && !d.ok);
  el("hint").hidden = !showHint;
  if(showHint){
    const blocked = location.protocol==="https:" && /^http:\/\//i.test(cfg.url||"");
    el("hintLocal").hidden = blocked;
    el("hintHosted").hidden = !blocked;
  }
  nextAt=Date.now()+Math.max(1,cfg.poll)*1000;
  stamp(); busy=false;
  el("refreshBtn").classList.remove("spin");
}
function schedule(){
  if(timer) clearInterval(timer);
  if(countdownTimer) clearInterval(countdownTimer);
  const ms=Math.max(1,cfg.poll)*1000;
  nextAt=Date.now()+ms;
  timer=setInterval(tick,ms);
  countdownTimer=setInterval(paintStatus,1000);  // keeps the "next in" ticking
}

/* ---------- settings wiring ---------- */
function fillSettings(){
  el("cfgUrl").value=cfg.url; el("cfgSd").value=cfg.sd; el("cfgSats").value=cfg.sats;
  el("cfgPacc").value=cfg.pacc; el("cfgVolts").value=cfg.volts; el("cfgCams").value=cfg.cams;
  el("cfgPoll").value=cfg.poll; el("cfgFw").value=cfg.fw; el("cfgDls").checked=cfg.dls;
}
el("gearBtn").onclick=()=>{ const s=el("settings"); s.hidden=!s.hidden; if(!s.hidden){ el("help").hidden=true; el("prep").hidden=true; fillSettings(); } };
el("helpBtn").onclick=()=>{ const h=el("help"); h.hidden=!h.hidden; if(!h.hidden){ el("settings").hidden=true; el("prep").hidden=true; } };
el("prepBtn").onclick=()=>{ const p=el("prep"); p.hidden=!p.hidden; if(!p.hidden){ el("help").hidden=true; el("settings").hidden=true; } };
function updatePrepCount(){
  const items=el("prepList").querySelectorAll(".prep-item");
  const done=el("prepList").querySelectorAll(".prep-item.done").length;
  el("prepCount").textContent=done+" of "+items.length;
}
function togglePrep(it){ it.classList.toggle("done"); updatePrepCount(); }
el("prepList").addEventListener("click",e=>{ const it=e.target.closest(".prep-item"); if(it) togglePrep(it); });
el("prepList").addEventListener("keydown",e=>{ if(e.key==="Enter"||e.key===" "){ const it=e.target.closest(".prep-item"); if(it){ e.preventDefault(); togglePrep(it); } } });
updatePrepCount();
function openSettings(){ el("help").hidden=true; el("settings").hidden=false; fillSettings(); el("settings").scrollIntoView({behavior:"smooth",block:"nearest"}); }
el("hintSettings").onclick=openSettings;
el("helpToSettings").onclick=openSettings;
el("applyBtn").onclick=()=>{
  // Settings is the trusted path (the pilot typed it), so the camera URL is not
  // restricted to local addresses here as it is for a link. The numbers still
  // go through the shared sanitizer so a blank or nonsense field cannot leave a
  // threshold in a state that never fires.
  cfg=sanitizeCfg({
    url:el("cfgUrl").value.trim()||DEFAULTS.url,
    sd:parseFloat(el("cfgSd").value), sats:parseInt(el("cfgSats").value),
    pacc:parseFloat(el("cfgPacc").value), volts:parseFloat(el("cfgVolts").value),
    cams:parseInt(el("cfgCams").value), poll:parseFloat(el("cfgPoll").value),
    fw:el("cfgFw").value.trim(), dls:el("cfgDls").checked
  });
  saveCfg(cfg); schedule(); tick(); el("settings").hidden=true;
};
el("resetBtn").onclick=()=>{ cfg={...DEFAULTS}; saveCfg(cfg); fillSettings(); schedule(); tick(); };
el("refreshBtn").onclick=tick;
el("source").onchange=()=>{ saveCfg(cfg); tick(); schedule(); };
el("themeBtn").onclick=()=>{ applyTheme(currentTheme()==="dark"?"light":"dark"); saveCfg(cfg); };

/* ---------- boot ---------- */
const VALID_SOURCES=["live","demo-go","demo-sd","demo-nosd","demo-gps","demo-pos","demo-time","demo-warmup","demo-volts","demo-rig","demo-warn","demo-dls","demo-nogo","demo-down"];
const params=new URLSearchParams(location.search);
const wantTheme=params.get("theme");
const sysLight=window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
applyTheme((wantTheme==="light"||wantTheme==="dark") ? wantTheme : (sysLight?"light":"dark"));
const wantSource=params.get("source");
el("source").value=VALID_SOURCES.includes(wantSource)?wantSource:"live";
saveCfg(cfg);
fillSettings();
tick(); schedule();

// Pause the poll loop when the tab is backgrounded (saves battery and avoids
// a hidden tab hammering failed live reads); resume with a fresh read on return.
document.addEventListener("visibilitychange",()=>{
  if(document.hidden){
    if(timer){ clearInterval(timer); timer=null; }
    if(countdownTimer){ clearInterval(countdownTimer); countdownTimer=null; }
  } else {
    tick(); schedule();
  }
});
