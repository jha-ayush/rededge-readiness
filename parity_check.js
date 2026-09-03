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
 * evaluators side by side, runs every canonical scenario through both, and
 * fails if they disagree on the overall state or on any individual check. The
 * Python implementation is held to the same scenarios by test_rededge.py, so
 * between the two, all three clients are pinned to one expected table.
 *
 * It reads the shipped files directly rather than a copy, so it cannot pass
 * against a stale duplicate of the logic.
 *
 * Zero dependencies: Node stdlib only. Node is a development and CI tool here,
 * never a runtime requirement for the field tools themselves.
 *
 * Run:
 *   node parity_check.js
 * Exit code: 0 all clients agree, 1 a disagreement was found.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

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

/* Reduce a result to the shape both clients must agree on: the overall verdict
 * and the state of every named check. Labels are compared too, because a check
 * silently renamed in one client is itself a drift worth catching. */
function shape(res) {
  const checks = {};
  for (const c of res.checks || []) {
    const label = Array.isArray(c) ? c[0] : c.label;
    const state = Array.isArray(c) ? c[2] : c.state;
    checks[label] = state;
  }
  return { overall: res.overall, checks };
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

  let compared = 0;

  for (const kind of Object.keys(EXPECTED)) {
    const expected = EXPECTED[kind];

    const w = shape(web.evaluate(web.demo("demo-" + kind), CFG));
    const i = shape(ios.evaluate(ios.demoSnap(kind), CFG));

    if (w.overall !== expected) {
      fail(`${kind}: web says ${w.overall}, contract says ${expected}`);
    }
    if (i.overall !== expected) {
      fail(`${kind}: iOS says ${i.overall}, contract says ${expected}`);
    }

    const labels = new Set([...Object.keys(w.checks), ...Object.keys(i.checks)]);
    for (const label of labels) {
      if (w.checks[label] !== i.checks[label]) {
        fail(`${kind}: check "${label}" is ${w.checks[label]} on web but ` +
             `${i.checks[label]} on iOS`);
      }
    }
    compared++;
  }

  /* A dead link must read NO-GO in both clients. This is the single most
   * important agreement in the project: it is the case where a disagreement
   * would mean one tool showing a pass while the camera is unreachable. */
  const downWeb = web.evaluate({ ok: false }, CFG).overall;
  const downIos = ios.evaluate({ ok: false }, CFG).overall;
  if (downWeb !== "NO-GO" || downIos !== "NO-GO") {
    fail(`no-link must be NO-GO everywhere, got web=${downWeb} iOS=${downIos}`);
  }

  /* An unrecognized status must never read GO. Each field is probed on its own,
   * with everything else healthy. An earlier draft of this harness set several
   * fields to junk at once, and the first unknown masked the rest: a client that
   * started treating an unrecognized DLS state as a pass still went green here
   * because the SD value was also unknown. A probe that can be masked is not a
   * probe, so each one is now isolated. */
  const healthy = {
    sd_status: "Ok", sd_gb_free: 20, sd_warn: false, bus_volts: 4.7,
    gps_used_sats: 9, gps_warn: false, p_acc: 2, dls_status: "Ok",
    time_source: "GPS", utc_time_valid: true,
  };
  const cams = [{ device_type: "Camera", sw_version: "v7.1.0", sd_status: "Ok" }];
  const unknownProbes = {
    "unrecognized SD status": { sd_status: "Garbled" },
    "unrecognized DLS state": { dls_status: "Weird" },
    "missing SD status": { sd_status: undefined },
    "missing DLS state": { dls_status: undefined },
    "missing voltage": { bus_volts: undefined },
  };
  for (const [name, patch] of Object.entries(unknownProbes)) {
    const snap = {
      ok: true,
      status: Object.assign({}, healthy, patch),
      version: { sw_version: "v7.1.0" },
      network: { network_map: cams },
    };
    const oddWeb = web.evaluate(snap, CFG).overall;
    const oddIos = ios.evaluate(snap, CFG).overall;
    if (oddWeb === "GO") fail(`${name}: web read GO, unknown values must never pass`);
    if (oddIos === "GO") fail(`${name}: iOS read GO, unknown values must never pass`);
    if (oddWeb !== oddIos) {
      fail(`${name}: web says ${oddWeb} but iOS says ${oddIos}`);
    }
  }

  if (process.exitCode === 1) {
    console.error(`\nChecked ${compared} scenarios. Clients disagree.`);
    return;
  }
  console.log(`Parity OK: web and iOS agree on ${compared} canonical scenarios,`);
  console.log("on every individual check, on no-link, and on unknown values.");
}

main();
