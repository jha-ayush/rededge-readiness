// rededge-readiness.scriptable.js
//
// MicaSense RedEdge / Altum field readiness for iPhone, via Scriptable.
//
// Why Scriptable: iOS browsers block cross-origin reads of the camera's
// plain-HTTP JSON (CORS), and iOS suspends a backgrounded local server the
// moment you switch to Safari. Scriptable uses native networking that has no
// CORS restriction, needs no server and no internet, and stays in foreground.
//
// Setup:
//   1. Install Scriptable from the App Store (free).
//   2. New script, paste this in, name it "RedEdge Readiness".
//   3. Join the camera WiFi. On first run, allow Local Network access
//      (Settings > Scriptable > Local Network if not prompted).
//   4. Run from the app, the Share Sheet, a Home Screen icon, or add it as a
//      Home Screen widget for a glanceable state.
//
// Re-run to refresh. Open this script inside the Scriptable app to get a menu
// with Check now, Settings (edit thresholds and camera URL on the device), and
// demo states. A Home Screen icon or widget skips the menu and checks directly.

// ----------------------------------------------------------------------------
// Config
// ----------------------------------------------------------------------------
// Defaults. Editable on the device: open this script inside the Scriptable app
// and choose Settings. Field use (Home Screen icon or widget) skips the menu
// and runs the check directly. Settings persist in a local file.
const DEMO = "";   // top-level demo for the widget: "" live, or go|sd|gps|dls|net|down

const DEFAULTS = {
  cameraUrl: "http://192.168.10.254", // WiFi default; Ethernet 192.168.1.83
  timeout: 2.5,   // seconds per request
  sd: 2,          // min SD free (GB)
  sats: 6,        // min GPS sats
  pacc: 5,        // max position error (m)
  volts: 4.2,     // min supply (V); placeholder, verify against your power setup
  cams: 0,        // expected cameras, 0 = any
  fw: "",         // expected firmware, "" = any
  dls: false,     // require light sensor for reflectance work
  theme: "auto",  // "auto" follows the phone, or "dark" / "light"
};

function settingsPath() {
  const fm = FileManager.local();
  return fm.joinPath(fm.documentsDirectory(), "rededge-settings.json");
}

function loadSettings() {
  const s = { ...DEFAULTS };
  try {
    const fm = FileManager.local();
    const p = settingsPath();
    if (fm.fileExists(p)) Object.assign(s, JSON.parse(fm.readString(p)));
  } catch (e) { /* fall back to defaults */ }
  return s;
}

function saveSettings(s) {
  try { FileManager.local().writeString(settingsPath(), JSON.stringify(s)); }
  catch (e) { /* non-fatal */ }
}

async function editSettings(s) {
  const a = new Alert();
  a.title = "RedEdge Settings";
  a.message = "Blank fields fall back to the default.";
  a.addTextField("Camera URL", s.cameraUrl);
  a.addTextField("Min SD free (GB)", String(s.sd));
  a.addTextField("Min GPS sats", String(s.sats));
  a.addTextField("Max position error (m)", String(s.pacc));
  a.addTextField("Min supply (V)", String(s.volts));
  a.addTextField("Expected cameras (0 = any)", String(s.cams));
  a.addTextField("Expected firmware", s.fw);
  a.addTextField("Require DLS (yes/no)", s.dls ? "yes" : "no");
  a.addTextField("Theme (auto/dark/light)", s.theme || "auto");
  a.addAction("Save");
  a.addCancelAction("Cancel");
  const idx = await a.presentAlert();
  if (idx === -1) return null;
  const num = (v, d) => { const n = parseFloat(v); return isNaN(n) ? d : n; };
  const th = (a.textFieldValue(8) || "auto").trim().toLowerCase();
  const ns = {
    cameraUrl: (a.textFieldValue(0) || DEFAULTS.cameraUrl).trim(),
    timeout: s.timeout,
    sd: num(a.textFieldValue(1), DEFAULTS.sd),
    sats: Math.round(num(a.textFieldValue(2), DEFAULTS.sats)),
    pacc: num(a.textFieldValue(3), DEFAULTS.pacc),
    volts: num(a.textFieldValue(4), DEFAULTS.volts),
    cams: Math.round(num(a.textFieldValue(5), DEFAULTS.cams)),
    fw: (a.textFieldValue(6) || "").trim(),
    dls: /^y/i.test((a.textFieldValue(7) || "").trim()),
    theme: (th === "dark" || th === "light") ? th : "auto",
  };
  saveSettings(ns);
  return ns;
}

// ----------------------------------------------------------------------------
// Readiness evaluation (parity with the web and Python tools)
// ----------------------------------------------------------------------------
const RANK = { "GO": 1, "CHECK": 2, "UNKNOWN": 2, "NO-GO": 3 };
const worst = (arr) => arr.reduce((a, b) => (RANK[b] > RANK[a] ? b : a), "GO");

function evaluate(d, c) {
  if (!d.ok) {
    return {
      overall: "NO-GO",
      reason: "No link to the camera.",
      sub: "Confirm you are on the camera WiFi, that Local Network access is allowed, and the base URL is correct.",
      checks: [{ label: "Camera link", read: "down", state: "NO-GO", note: "no response from " + (c.cameraUrl || "camera") }],
    };
  }
  const s = d.status || {}, net = d.network, ver = d.version || {};
  const out = [];

  // SD storage
  (function () {
    const st = s.sd_status, free = s.sd_gb_free;
    let state = "GO", note = "card present and writable";
    if (st === "NotPresent") { state = "NO-GO"; note = "no SD card inserted"; }
    else if (st === "Full") { state = "NO-GO"; note = "card full, offload before flight"; }
    else if (s.sd_warn) { state = "CHECK"; note = "low-space warning or unrecommended filesystem"; }
    else if (typeof free === "number" && free < c.sd) { state = "CHECK"; note = "below " + c.sd + " GB headroom"; }
    else if (st === undefined) { state = "UNKNOWN"; note = "card status not reported"; }
    out.push({ label: "SD storage", read: (typeof free === "number" ? free.toFixed(1) : "--"), unit: "GB free", state, note });
  })();

  // GPS fix
  (function () {
    const sats = s.gps_used_sats, pacc = s.p_acc;
    let state = "GO", note = "usable fix for geotagging";
    if (sats === undefined) { state = "UNKNOWN"; note = "GPS not reported"; }
    else if (s.gps_warn) { state = "CHECK"; note = "receiver reports interference"; }
    else if (sats < c.sats) { state = "CHECK"; note = "only " + sats + " sats, want " + c.sats + "+"; }
    else if (typeof pacc === "number" && pacc > c.pacc) { state = "CHECK"; note = "position error " + pacc.toFixed(1) + " m"; }
    else if (s.utc_time_valid === false) { state = "CHECK"; note = "time not yet valid"; }
    out.push({ label: "GPS fix", read: (sats !== undefined ? String(sats) : "--"), unit: "sats", state, note });
  })();

  // Position accuracy
  (function () {
    const pacc = s.p_acc;
    let state = "GO";
    if (pacc === undefined) state = "UNKNOWN";
    else if (pacc > c.pacc) state = "CHECK";
    out.push({ label: "Position accuracy", read: (typeof pacc === "number" ? pacc.toFixed(1) : "--"), unit: "m (1\u03c3)", state, note: (typeof pacc === "number" ? "threshold " + c.pacc + " m" : "not reported") });
  })();

  // Light sensor (DLS)
  (function () {
    const dls = s.dls_status;
    let state = "GO", note = "irradiance sensor active";
    if (dls === "Error") { state = "NO-GO"; note = "DLS error, reflectance data unreliable"; }
    else if (dls === "NotPresent") { state = c.dls ? "CHECK" : "GO"; note = c.dls ? "no DLS, reflectance calibration limited" : "no DLS (not required)"; }
    else if (dls === "Programming" || dls === "Initializing") { state = "CHECK"; note = "DLS warming up, wait"; }
    else if (dls === undefined) { state = "UNKNOWN"; note = "DLS state not reported"; }
    out.push({ label: "Light sensor", read: (dls || "--"), unit: "", state, note });
  })();

  // Supply voltage
  (function () {
    const v = s.bus_volts;
    let state = "GO", note = "supply within configured floor";
    if (v === undefined) { state = "UNKNOWN"; note = "voltage not reported"; }
    else if (v < c.volts) { state = "CHECK"; note = "below " + c.volts + " V floor, verify pack"; }
    out.push({ label: "Supply voltage", read: (typeof v === "number" ? v.toFixed(2) : "--"), unit: "V", state, note });
  })();

  // Time source
  (function () {
    const ts = s.time_source, valid = s.utc_time_valid;
    let state = "GO", note = (ts ? ts + " time source" : "time valid");
    if (valid === false) { state = "CHECK"; note = "UTC time not yet valid"; }
    else if (ts === undefined && valid === undefined) { state = "UNKNOWN"; note = "time source not reported"; }
    out.push({ label: "Time source", read: (ts || (valid ? "valid" : "--")), unit: "", state, note });
  })();

  // Camera rig
  (function () {
    if (!net || !Array.isArray(net.network_map)) {
      out.push({ label: "Camera rig", read: "--", unit: "", state: "UNKNOWN", note: "network status unavailable" });
      return;
    }
    const cams = net.network_map.filter((x) => x.device_type === "Camera");
    const dlss = net.network_map.filter((x) => String(x.device_type).startsWith("DLS"));
    let state = "GO";
    let note = cams.length + " camera" + (cams.length === 1 ? "" : "s") + (dlss.length ? ", DLS present" : "");
    const fwSet = new Set(cams.map((x) => x.sw_version).filter(Boolean));
    const cardIssue = cams.some((x) => x.sd_status && x.sd_status !== "Ok");
    if (c.cams > 0 && cams.length < c.cams) { state = "NO-GO"; note = "only " + cams.length + " of " + c.cams + " cameras online"; }
    else if (cardIssue) { state = "CHECK"; note = "a networked camera has a card issue"; }
    else if (fwSet.size > 1) { state = "CHECK"; note = "mixed firmware across cameras"; }
    else if (c.dls && dlss.length === 0) { state = "CHECK"; note = "no DLS on the network"; }
    out.push({ label: "Camera rig", read: String(cams.length), unit: "online", state, note });
  })();

  // Firmware
  (function () {
    const v = ver.sw_version;
    let state = "GO", note = (v ? "running " + v : "version reported");
    if (v === undefined) { state = "UNKNOWN"; note = "version not reported"; }
    else if (c.fw && v !== c.fw) { state = "CHECK"; note = "expected " + c.fw + ", running " + v; }
    out.push({ label: "Firmware", read: (v || "--"), unit: "", state, note });
  })();

  const overall = worst(out.map((x) => (x.state === "UNKNOWN" ? "CHECK" : x.state)));
  let reason, sub;
  const bad = out.filter((x) => x.state === "NO-GO");
  const warns = out.filter((x) => x.state === "CHECK" || x.state === "UNKNOWN");
  if (overall === "GO") { reason = "Sensor ready to capture."; sub = "All monitored systems within tolerance."; }
  else if (overall === "NO-GO") { reason = bad.map((x) => x.label + ": " + x.note).join("; ") + "."; sub = "Resolve before flying."; }
  else { reason = warns.map((x) => x.label).join(", ") + " need attention."; sub = warns.map((x) => x.label + ": " + x.note).join("; ") + "."; }
  return { overall, reason, sub, checks: out };
}

// ----------------------------------------------------------------------------
// Camera read (native Request: no CORS, no server)
// ----------------------------------------------------------------------------
async function getJSON(s, path) {
  const r = new Request(s.cameraUrl.replace(/\/+$/, "") + path);
  r.timeoutInterval = s.timeout;
  return await r.loadJSON();
}

function demoSnap(kind) {
  const base = {
    ok: true,
    status: { sd_status: "Ok", sd_gb_free: 20.1, sd_warn: false, bus_volts: 4.69, gps_used_sats: 9, gps_warn: false, p_acc: 2.4, dls_status: "Ok", time_source: "GPS", utc_time_valid: true },
    version: { sw_version: "v7.1.0", serial: "RM02-1839163-SC" },
    network: { network_map: [{ device_type: "Camera", sd_status: "Ok", sw_version: "v7.1.0" }, { device_type: "DLS 2", sw_version: "v1.2.3" }] },
  };
  const d = JSON.parse(JSON.stringify(base));
  if (kind === "sd") { d.status.sd_gb_free = 0.7; d.status.sd_warn = true; }
  if (kind === "gps") { d.status.gps_used_sats = 2; d.status.p_acc = 18.3; d.status.utc_time_valid = false; d.status.time_source = "None"; }
  if (kind === "dls") { d.status.dls_status = "Error"; }
  if (kind === "net") { d.network.network_map = [{ device_type: "Camera", sd_status: "Ok", sw_version: "v7.1.0" }]; }
  if (kind === "down") return { ok: false };
  return d;
}

async function snapshot(s, demoKind) {
  if (demoKind) return demoSnap(demoKind);
  try {
    const [status, version] = await Promise.all([getJSON(s, "/status"), getJSON(s, "/version")]);
    let network = null;
    try { network = await getJSON(s, "/networkstatus"); } catch (e) { /* optional */ }
    return { ok: true, status, version, network };
  } catch (e) {
    return { ok: false };
  }
}

// Post-flight: walk the card and count captures (IMG_NNNN sets).
async function countCaptures(s) {
  let sets = 0, bytes = 0;
  const caps = new Set();
  async function walk(remote) {
    const sub = remote.replace(/^\/+/, "");
    const listing = await getJSON(s, "/files/" + sub);
    for (const f of (listing.files || [])) {
      bytes += (f.size || 0);
      const name = f.name || "";
      if (name.toUpperCase().startsWith("IMG_") && name.includes("_")) {
        caps.add(remote + "|" + name.substring(0, name.lastIndexOf("_")));
      }
    }
    for (const d of (listing.directories || [])) {
      if ((remote === "" || remote === "/") && d.toUpperCase().endsWith("SET")) sets++;
      await walk((remote.replace(/\/+$/, "") + "/" + d).replace(/^\/+/, ""));
    }
  }
  await walk("");
  return { sets, captures: caps.size, bytes };
}

async function runPostflight(s) {
  let info;
  try { info = await countCaptures(s); }
  catch (e) { return evaluate({ ok: false }, s); }  // reuse the no-link readout
  let st = {};
  try { st = await getJSON(s, "/status"); } catch (e) { /* SD line optional */ }
  const ok = info.captures > 0;
  const checks = [
    { label: "Captures", read: String(info.captures), unit: "", state: ok ? "GO" : "CHECK", note: ok ? "image sets on the card" : "card has no images" },
    { label: "SET folders", read: String(info.sets), unit: "", state: "GO", note: "capture folders" },
    { label: "Data on card", read: (info.bytes / 1e6).toFixed(1), unit: "MB", state: "GO", note: "total image bytes" },
  ];
  if (typeof st.sd_gb_free === "number") {
    checks.push({ label: "SD free", read: st.sd_gb_free.toFixed(1), unit: "GB", state: "GO", note: "remaining space" });
  }
  return {
    overall: ok ? "GO" : "CHECK",
    reason: ok ? "Post-flight: captures found." : "Post-flight: no captures found.",
    sub: ok ? "Confirm coverage before leaving the site." : "Do not pack up before re-checking the card.",
    checks,
  };
}


// ----------------------------------------------------------------------------
// Rendering
// ----------------------------------------------------------------------------
const PALETTES = {
  dark: {
    bg: "#0a0c0e", panel: "#13171b", line: "#232a31", text: "#e7eef4",
    muted: "#74828e", faint: "#4a555f", tagbg: "rgba(255,255,255,.05)",
    GO: "#2fe39a", CHECK: "#f6b13e", "NO-GO": "#ff5a5a", UNKNOWN: "#5b6772",
  },
  light: {
    bg: "#eef2f6", panel: "#ffffff", line: "#dde4ea", text: "#16212c",
    muted: "#566370", faint: "#97a4af", tagbg: "rgba(20,40,60,.05)",
    GO: "#0e9b69", CHECK: "#bd7a10", "NO-GO": "#d62f2f", UNKNOWN: "#8a97a3",
  },
};

function resolveTheme(s) {
  const t = (s && s.theme) || "auto";
  if (t === "dark" || t === "light") return t;
  try { return Device.isUsingDarkAppearance() ? "dark" : "light"; }
  catch (e) { return "dark"; }
}

function buildHTML(res, theme) {
  const p = PALETTES[theme] || PALETTES.dark;
  const stamp = new Date().toLocaleTimeString([], { hour12: false });
  const rows = res.checks.map((ck) => {
    const sc = p[ck.state] || p.UNKNOWN;
    return `<div class="check">
       <span class="dot" style="--c:${sc}"></span>
       <div class="meta"><div class="label">${ck.label} <span class="tag" style="color:${sc}">${ck.state}</span></div><div class="note">${ck.note}</div></div>
       <div class="read">${ck.read}${ck.unit ? ` <span class="u">${ck.unit}</span>` : ""}</div>
     </div>`;
  }).join("");
  const c = p[res.overall] || p.text;
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>
  :root{--bg:${p.bg};--panel:${p.panel};--line:${p.line};--text:${p.text};--muted:${p.muted};--faint:${p.faint};
    --tagbg:${p.tagbg};
    --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
    --body:-apple-system,system-ui,sans-serif;--state:${c}}
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{background:var(--bg);color:var(--text);font-family:var(--body);-webkit-font-smoothing:antialiased}
  body{padding:max(18px,env(safe-area-inset-top)) 16px max(24px,env(safe-area-inset-bottom))}
  .wrap{max-width:760px;margin:0 auto}
  .head{display:flex;align-items:center;gap:10px;margin-bottom:14px}
  .brand{font-weight:800;letter-spacing:.04em;font-size:14px;text-transform:uppercase}
  .brand .r{color:${p["NO-GO"]}}
  .stamp{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--faint)}
  .banner{position:relative;border-radius:16px;padding:24px 20px;border:1px solid var(--state);
    background:var(--panel);overflow:hidden}
  .banner::before{content:"";position:absolute;inset:0;opacity:.12;
    background:radial-gradient(120% 140% at 0% 0%,var(--state),transparent 55%)}
  .state{position:relative;font-weight:900;font-size:clamp(44px,14vw,72px);line-height:.9;color:var(--state)}
  .reason{position:relative;margin-top:12px;font-size:15px;line-height:1.45}
  .sub{color:var(--muted);font-size:12.5px;margin-top:4px;line-height:1.4}
  .checks{margin-top:14px;display:grid;grid-template-columns:1fr;gap:8px}
  @media(min-width:680px){.checks{grid-template-columns:1fr 1fr}}
  .check{display:flex;align-items:center;gap:13px;background:var(--panel);
    border:1px solid var(--line);border-radius:12px;padding:14px}
  .dot{width:9px;height:9px;border-radius:50%;flex:none;background:var(--c);box-shadow:0 0 7px var(--c)}
  .meta{min-width:0;flex:1}.label{font-size:14px;font-weight:500;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .tag{font-family:var(--mono);font-size:9.5px;font-weight:600;letter-spacing:.05em;padding:2px 6px;border-radius:5px;background:var(--tagbg)}
  .note{font-size:11.5px;color:var(--muted);margin-top:3px;line-height:1.35}
  .read{font-family:var(--mono);font-size:14px;text-align:right;white-space:nowrap}
  .read .u{color:var(--faint);font-size:11px}
  .foot{margin-top:16px;font-size:11.5px;color:var(--faint);line-height:1.5}
</style></head><body><div class="wrap">
  <div class="head"><div class="brand"><span class="r">RED</span>EDGE READINESS</div>
    <div class="stamp">checked ${stamp}</div></div>
  <div class="banner"><div class="state">${res.overall}</div>
    <div class="reason">${res.reason}<div class="sub">${res.sub || ""}</div></div></div>
  <div class="checks">${rows}</div>
  <div class="foot">Fail toward caution. Anything unconfirmed counts as CHECK; a lost link is NO-GO. Reports sensor readiness, not flight legality. Re-run to refresh.</div>
</div></body></html>`;
}

function buildWidget(res, theme) {
  const p = PALETTES[theme] || PALETTES.dark;
  const w = new ListWidget();
  const c = new Color(p[res.overall] || p.UNKNOWN);
  w.backgroundColor = new Color(p.bg);
  const bar = w.addStack(); bar.layoutHorizontally();
  const tag = bar.addText("REDEDGE"); tag.font = Font.semiboldSystemFont(9);
  tag.textColor = new Color(p.muted); bar.addSpacer();
  w.addSpacer(6);
  const big = w.addText(res.overall); big.font = Font.heavySystemFont(34); big.textColor = c;
  w.addSpacer(2);
  const r = w.addText(res.reason); r.font = Font.systemFont(11);
  r.textColor = new Color(p.text); r.lineLimit = 3;
  w.addSpacer();
  const t = w.addText("checked " + new Date().toLocaleTimeString([], { hour12: false }));
  t.font = Font.regularSystemFont(9); t.textColor = new Color(p.faint);
  return w;
}

// ----------------------------------------------------------------------------
// Entry
// ----------------------------------------------------------------------------
async function main() {
  let s = loadSettings();

  // Widget: render compact state, no menu.
  if (config.runsInWidget) {
    const res = evaluate(await snapshot(s, DEMO), s);
    Script.setWidget(buildWidget(res, resolveTheme(s)));
    Script.complete();
    return;
  }

  // Field use (Home Screen icon, Share Sheet, Siri): go straight to the check.
  // Opened inside the Scriptable app: show a menu so settings are reachable.
  const inApp = config.runsInApp && !config.runsFromHomeScreen;
  let demoKind = DEMO;
  let result = null;  // set directly by branches that build their own result

  if (inApp) {
    const m = new Alert();
    m.title = "RedEdge Readiness";
    m.message = "Camera " + s.cameraUrl;
    m.addAction("Check now");          // 0
    m.addAction("Post-flight check");  // 1
    m.addAction("Settings");           // 2
    m.addAction("Demo: all clear");    // 3
    m.addAction("Demo: low SD");       // 4
    m.addAction("Demo: no GPS");       // 5
    m.addAction("Demo: DLS error");    // 6
    m.addAction("Demo: no link");      // 7
    m.addCancelAction("Cancel");
    const i = await m.presentSheet();
    if (i === -1) { Script.complete(); return; }
    if (i === 1) { result = await runPostflight(s); }
    else if (i === 2) { const ns = await editSettings(s); if (ns) s = ns; demoKind = ""; }
    else if (i === 3) demoKind = "go";
    else if (i === 4) demoKind = "sd";
    else if (i === 5) demoKind = "gps";
    else if (i === 6) demoKind = "dls";
    else if (i === 7) demoKind = "down";
    else demoKind = "";
  }

  if (!result) result = evaluate(await snapshot(s, demoKind), s);
  const wv = new WebView();
  await wv.loadHTML(buildHTML(result, resolveTheme(s)));
  await wv.present(true);
  Script.complete();
}

main();
