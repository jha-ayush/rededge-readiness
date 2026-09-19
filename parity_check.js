#!/usr/bin/env node
/*
 * parity_check.js
 *
 * Cross-client parity harness.
 *
 * The readiness logic is implemented three times, once per client, because
 * there is no shared runtime across a Scriptable script, a browser page and a
 * Python process, and adding a build step to unify them would cost more than it
 * buys. The price of that choice is drift: three copies of one contract can
 * quietly disagree, and the disagreement would surface in the field as two
 * tools giving a pilot different answers about the same camera.
 *
 * This harness is what makes that price payable. It loads the web and iOS
 * evaluators side by side, runs the Python evaluator in a child process on the
 * mock camera's payloads, and fails if any client disagrees with another on
 * the overall state or on any individual check. Python used to be held only
 * by test_rededge.py, and only to the verdict per scenario; that is how it
 * once read GO on a payload where the other two read CHECK. Now all three
 * are compared check by check on the same inputs.
 *
 * It reads the shipped files directly rather than a copy, so it cannot pass
 * against a stale duplicate of the logic.
 *
 * Zero dependencies: Node stdlib only, plus python3 on the PATH for the
 * Python side. Both are development and CI tools here, never a runtime
 * requirement for the field tools themselves. If python3 is missing the
 * harness fails rather than skipping: a check that cannot run has not passed.
 *
 * Run:
 *   node parity_check.js
 * Exit code: 0 all clients agree, 1 a disagreement was found.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { spawnSync } = require("child_process");

const WEB = path.join(__dirname, "web", "app.js");
const IOS = path.join(__dirname, "ios", "rededge-readiness.scriptable.js");

// The expected state for every canonical scenario. This table is the contract.
// test_rededge.py asserts the identical mapping against the Python evaluator
// and the mock camera, so all three clients answer to one source of truth.
const EXPECTED = {
  go: "GO",
  sd: "CHECK",
  nosd: "NO-GO",
  gps: "CHECK",
  pos: "CHECK",
  time: "CHECK",
  warmup: "CHECK",
  volts: "CHECK",
  rig: "CHECK",
  warn: "CHECK",
  dls: "NO-GO",
  nogo: "NO-GO",
};

// Thresholds identical to CONFIG_DEFAULTS in rededge.py and DEFAULTS in the two
// clients. A drift here would make the harness lie, so it is stated once.
const CFG = {
  url: "", cameraUrl: "", timeout: 2.5,
  sd: 2, sats: 6, pacc: 5, volts: 4.2,
  cams: 0, fw: "", dls: false, theme: "dark",
};

function fail(msg) {
  console.error("PARITY FAIL: " + msg);
  process.exitCode = 1;
}

/* Load the web evaluator out of the shipped page. The boot section is dropped
 * because it wires up DOM handlers and timers that have no meaning here; every
 * pure function above that marker is kept exactly as it ships. */
function loadWeb() {
  let js = fs.readFileSync(WEB, "utf8");
  const boot = js.indexOf("/* ---------- boot");
  if (boot === -1) throw new Error("boot marker missing; app.js layout changed");
  js = js.slice(0, boot);

  /* The page wires some DOM handlers above the boot marker. Returning a
   * permissive stub (rather than null) lets that wiring run harmlessly so the
   * pure evaluation functions below it can be reached without editing the
   * shipped file to suit the harness. */
  const node = () => new Proxy({}, {
    get(t, k) {
      if (k === "classList") return { add() {}, remove() {}, toggle: () => false, contains: () => false };
      if (k === "style" || k === "dataset") return {};
      if (k === "value" || k === "textContent" || k === "innerHTML") return "";
      if (k === "hidden") return false;
      if (k === "children") return [];
      if (k in t) return t[k];
      return () => node();
    },
    set() { return true; },
  });

  const ctx = {
    console, Date, Math, JSON, Array, Set, Map, String, Number, Boolean,
    parseFloat, parseInt, isNaN, isFinite, URLSearchParams, Promise,
    setTimeout: () => 0, setInterval: () => 0,
    clearTimeout() {}, clearInterval() {},
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    location: { search: "", protocol: "https:" },
    history: { replaceState() {} },
    window: { matchMedia: () => ({ matches: false }), addEventListener() {} },
    document: {
      getElementById: () => node(),
      querySelector: () => node(),
      querySelectorAll: () => [],
      createElement: () => node(),
      documentElement: { setAttribute() {}, getAttribute: () => "dark" },
      addEventListener() {},
    },
  };
  vm.createContext(ctx);
  vm.runInContext(js, ctx, { filename: "web" });
  if (typeof ctx.evaluate !== "function" || typeof ctx.demo !== "function") {
    throw new Error("web evaluate/demo not found; entry points changed");
  }
  return ctx;
}

/* Load the iOS evaluator out of the shipped Scriptable file. The trailing
 * main() call is dropped so importing does not try to render a UI; the
 * Scriptable globals are stubbed because none of them are reached by the pure
 * evaluation path. */
function loadIos() {
  let js = fs.readFileSync(IOS, "utf8").replace(/\nmain\(\);\s*$/, "\n");
  const ctx = {
    console, Date, Math, JSON, Array, Set, Map, String, Number, Boolean,
    parseFloat, parseInt, isNaN, isFinite, Promise, setTimeout: () => 0,
    FileManager: {
      local: () => ({
        documentsDirectory: () => "/tmp",
        joinPath: (a, b) => a + "/" + b,
        fileExists: () => false,
        readString: () => "{}",
        writeString() {},
      }),
    },
    Request: class { async loadJSON() { throw new Error("offline"); } },
    WebView: class { async loadHTML() {} async present() {} },
    ListWidget: class {},
    Color: class {},
    Font: new Proxy({}, { get: () => () => ({}) }),
    Script: { setWidget() {}, complete() {} },
    Device: { isUsingDarkAppearance: () => true },
    Alert: class {},
    args: { queryParameters: {} },
    config: { runsInApp: false, runsFromHomeScreen: false },
  };
  vm.createContext(ctx);
  vm.runInContext(js, ctx, { filename: "ios" });
  if (typeof ctx.evaluate !== "function" || typeof ctx.demoSnap !== "function") {
    throw new Error("iOS evaluate/demoSnap not found; entry points changed");
  }
  return ctx;
}

/* Reduce a result to the shape every client must agree on: the overall verdict
 * and the state of every named check. Labels are compared too, because a check
 * silently renamed in one client is itself a drift worth catching. Python
 * reports each check as a (label, read, state, note) tuple, which arrives here
 * as an array; the two JS clients report objects. */
function shape(res) {
  const checks = {};
  for (const c of res.checks || []) {
    const label = Array.isArray(c) ? c[0] : c.label;
    const state = Array.isArray(c) ? c[2] : c.state;
    checks[label] = state;
  }
  return { overall: res.overall, checks };
}

/* The Python evaluator, run once in a child process on a batch of named
 * snapshots. The canonical scenarios are built from the mock camera's own
 * payloads (rededge_mock.payloads), so the fixture the Python tests use and the
 * fixtures the JS clients carry are compared against each other, not merely
 * against the same table. Any other snapshot is passed through as given. */
const PY_SHIM = `
import json, sys
import rededge, rededge_mock
req = json.load(sys.stdin)
cfg = req["cfg"]
out = {}
for name, spec in req["cases"].items():
    if "scenario" in spec:
        p = rededge_mock.payloads(spec["scenario"])
        snap = {"ok": True, "status": p["status"], "version": p["version"],
                "network": p["networkstatus"]}
    else:
        snap = spec["snapshot"]
    out[name] = rededge.evaluate(snap, cfg)
json.dump(out, sys.stdout)
`;

function runPython(cases) {
  const r = spawnSync("python3", ["-c", PY_SHIM], {
    cwd: __dirname,
    input: JSON.stringify({ cfg: CFG, cases }),
    encoding: "utf8",
    timeout: 60000,
  });
  if (r.error) throw new Error("python3 could not be run: " + r.error.message);
  if (r.status !== 0) throw new Error("python evaluator failed:\n" + r.stderr);
  return JSON.parse(r.stdout);
}

/* Compare the three shaped results for one case: every verdict against the
 * others, and every check label against the others. Any difference is a
 * failure that names the case, the check and both readings. */
function compareAll(name, results) {
  const names = Object.keys(results);
  const labels = new Set();
  for (const n of names) for (const l of Object.keys(results[n].checks)) labels.add(l);
  for (let a = 0; a < names.length; a++) {
    for (let b = a + 1; b < names.length; b++) {
      const ra = results[names[a]], rb = results[names[b]];
      if (ra.overall !== rb.overall) {
        fail(`${name}: ${names[a]} says ${ra.overall} but ${names[b]} says ${rb.overall}`);
      }
      for (const label of labels) {
        if (ra.checks[label] !== rb.checks[label]) {
          fail(`${name}: check "${label}" is ${ra.checks[label]} on ${names[a]} ` +
               `but ${rb.checks[label]} on ${names[b]}`);
        }
      }
    }
  }
}

function main() {
  let web, ios;
  try {
    web = loadWeb();
    ios = loadIos();
  } catch (e) {
    fail(e.message);
    return;
  }

  /* Every case the harness runs, named. Scenarios come from each client's own
   * fixtures (and the mock, for Python); probes are explicit snapshots that all
   * three evaluate as given. */
  const healthy = {
    sd_status: "Ok", sd_gb_free: 20, sd_warn: false, bus_volts: 4.7,
    gps_used_sats: 9, gps_warn: false, p_acc: 2, dls_status: "Ok",
    time_source: "GPS", utc_time_valid: true,
  };
  const cams = [{ device_type: "Camera", sw_version: "v7.1.0", sd_status: "Ok" }];
  const probe = (statusPatch, extra) => ({
    ok: true,
    status: Object.assign({}, healthy, statusPatch || {}),
    version: { sw_version: "v7.1.0" },
    network: { network_map: cams },
    ...(extra || {}),
  });

  /* An unrecognized or missing status must never read GO. Each field is probed
   * on its own, with everything else healthy. An earlier draft of this harness
   * set several fields to junk at once, and the first unknown masked the rest: a
   * client that started treating an unrecognized DLS state as a pass still went
   * green here because the SD value was also unknown. A probe that can be masked
   * is not a probe, so each one is isolated. */
  const unknownProbes = {
    "unrecognized SD status": probe({ sd_status: "Garbled" }),
    "unrecognized DLS state": probe({ dls_status: "Weird" }),
    "missing SD status": probe({ sd_status: undefined }),
    "missing DLS state": probe({ dls_status: undefined }),
    "missing voltage": probe({ bus_volts: undefined }),
    "missing free space": probe({ sd_gb_free: undefined }),
    "missing satellites": probe({ gps_used_sats: undefined }),
    "missing position accuracy": probe({ p_acc: undefined }),
    "missing time fields": probe({ time_source: undefined, utc_time_valid: undefined }),
    "missing version": probe({}, { version: {} }),
    "missing network": probe({}, { network: null }),
  };

  /* A reading with the wrong type is not a reading. JavaScript coerces a string
   * beside a number to NaN, which compares false against everything, so "4.7"
   * volts or "abc" satellites used to fall through every branch and read GO
   * with "--" in the value column; Python raised TypeError on the same input.
   * Each client must treat these as unconfirmed, and all three must agree. */
  const wrongTypeProbes = {
    "string satellites": probe({ gps_used_sats: "9" }),
    "boolean satellites": probe({ gps_used_sats: true }),
    "string voltage": probe({ bus_volts: "4.7" }),
    "garbage voltage": probe({ bus_volts: "abc" }),
    "string position accuracy": probe({ p_acc: "2.0" }),
    "string free space": probe({ sd_gb_free: "20" }),
    "numeric SD status": probe({ sd_status: 1 }),
    "numeric DLS state": probe({ dls_status: 0 }),
    "object time source": probe({ time_source: {}, utc_time_valid: undefined }),
    "empty version string": probe({}, { version: { sw_version: "" } }),
    "numeric version": probe({}, { version: { sw_version: 7 } }),
    "only junk in the device list": probe({}, { network: { network_map: [null, "x", 3, []] } }),
    "empty device list": probe({}, { network: { network_map: [] } }),
  };

  /* Cases where the clients must agree with each other and the outcome is
   * pinned only by agreement plus the fail-toward-caution rule above. */
  const agreementProbes = {
    "junk beside a real camera in the device list": probe({}, { network: { network_map: [null, "x", 3, [], cams[0]] } }),
    "list firmware beside a real camera": probe({}, { network: { network_map: [
      { device_type: "Camera", sw_version: ["v7"] }, cams[0]] } }),
    "two cameras on mixed firmware behind junk": probe({}, { network: { network_map: [
      null, { device_type: "Camera", sw_version: "v7.1.0" },
      { device_type: "Camera", sw_version: "v7.0.0" }] } }),
    "wide error ellipse flags one row": probe({ p_acc: 12 }),
    "invalid clock flags one row": probe({ utc_time_valid: false }),
  };

  const pyCases = {};
  for (const kind of Object.keys(EXPECTED)) pyCases["scenario:" + kind] = { scenario: kind };
  for (const [n, s] of Object.entries(unknownProbes)) pyCases["unknown:" + n] = { snapshot: s };
  for (const [n, s] of Object.entries(wrongTypeProbes)) pyCases["type:" + n] = { snapshot: s };
  for (const [n, s] of Object.entries(agreementProbes)) pyCases["agree:" + n] = { snapshot: s };
  pyCases["no-link"] = { snapshot: { ok: false } };

  let py;
  try {
    py = runPython(pyCases);
  } catch (e) {
    fail(e.message);
    return;
  }

  let compared = 0;

  for (const kind of Object.keys(EXPECTED)) {
    const expected = EXPECTED[kind];
    const results = {
      web: shape(web.evaluate(web.demo("demo-" + kind), CFG)),
      iOS: shape(ios.evaluate(ios.demoSnap(kind), CFG)),
      python: shape(py["scenario:" + kind]),
    };
    for (const [client, r] of Object.entries(results)) {
      if (r.overall !== expected) {
        fail(`${kind}: ${client} says ${r.overall}, contract says ${expected}`);
      }
    }
    compareAll(kind, results);
    compared++;
  }

  /* A dead link must read NO-GO in every client. This is the single most
   * important agreement in the project: it is the case where a disagreement
   * would mean one tool showing a pass while the camera is unreachable. */
  const down = {
    web: web.evaluate({ ok: false }, CFG).overall,
    iOS: ios.evaluate({ ok: false }, CFG).overall,
    python: py["no-link"].overall,
  };
  for (const [client, v] of Object.entries(down)) {
    if (v !== "NO-GO") fail(`no-link must be NO-GO everywhere, ${client} says ${v}`);
  }

  const cautionProbes = Object.assign({}, unknownProbes, wrongTypeProbes);
  for (const [name, snap] of Object.entries(cautionProbes)) {
    const key = (name in unknownProbes ? "unknown:" : "type:") + name;
    const results = {
      web: shape(web.evaluate(snap, CFG)),
      iOS: shape(ios.evaluate(snap, CFG)),
      python: shape(py[key]),
    };
    for (const [client, r] of Object.entries(results)) {
      if (r.overall === "GO") fail(`${name}: ${client} read GO, unconfirmed values must never pass`);
    }
    compareAll(name, results);
  }

  for (const [name, snap] of Object.entries(agreementProbes)) {
    const results = {
      web: shape(web.evaluate(snap, CFG)),
      iOS: shape(ios.evaluate(snap, CFG)),
      python: shape(py["agree:" + name]),
    };
    compareAll(name, results);
  }

  /* One cause, one row: a wide error ellipse flags Position accuracy and an
   * invalid clock flags Time source, and neither also lights the GPS row. The
   * verdict is unchanged either way; this pins the readout, not the verdict. */
  const wide = shape(web.evaluate(agreementProbes["wide error ellipse flags one row"], CFG)).checks;
  if (wide["Position accuracy"] !== "CHECK" || wide["GPS fix"] !== "GO") {
    fail(`wide error ellipse: expected Position accuracy CHECK and GPS fix GO, got ${JSON.stringify(wide)}`);
  }
  const clock = shape(web.evaluate(agreementProbes["invalid clock flags one row"], CFG)).checks;
  if (clock["Time source"] !== "CHECK" || clock["GPS fix"] !== "GO") {
    fail(`invalid clock: expected Time source CHECK and GPS fix GO, got ${JSON.stringify(clock)}`);
  }

  if (process.exitCode === 1) {
    console.error(`\nChecked ${compared} scenarios. Clients disagree.`);
    return;
  }
  const probes = Object.keys(cautionProbes).length + Object.keys(agreementProbes).length;
  console.log(`Parity OK: web, iOS and Python agree on ${compared} canonical scenarios`);
  console.log(`and ${probes} probes, on every individual check, on no-link, and on`);
  console.log("unknown and wrong-typed values.");
}

main();
