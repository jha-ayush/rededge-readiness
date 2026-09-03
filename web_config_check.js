#!/usr/bin/env node
/*
 * web_config_check.js
 *
 * Guards the web page's configuration boundary.
 *
 * The page takes its settings from URL parameters, which means a shared link
 * carries them. Two defects lived there, both capable of producing a green
 * verdict that had nothing to do with the camera:
 *
 *   1. Numeric parameters were parsed with a bare parseFloat. Garbage produced
 *      NaN, and NaN compares false against everything, so "free < cfg.sd" with
 *      cfg.sd = NaN skipped the low-space branch entirely. A card holding
 *      0.2 GB read GO. So did zero satellites, and so did a pack at 3.1 V.
 *      A clean pass, produced by a link.
 *
 *   2. The camera URL was accepted verbatim, so a crafted link could aim the
 *      tool at any host and present its response as camera readings.
 *
 * Both are fixed. This file exists so they stay fixed: the fixes are a few
 * characters wide and would be easy to undo while refactoring, and nothing else
 * in the suite would notice. The Python tests cannot reach this code, because
 * it lives in the browser client.
 *
 * Zero dependencies: Node stdlib only, and Node is a development and CI tool
 * here, never a runtime requirement for the field tools.
 *
 * Run:
 *   node web_config_check.js
 * Exit code: 0 the boundary holds, 1 something got through.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEB = path.join(__dirname, "web", "rededge-readiness.html");

function fail(msg) {
  console.error("CONFIG GUARD FAIL: " + msg);
  process.exitCode = 1;
}

/* Load the page's pure functions with a query string of our choosing. The
 * shipped file is read directly, so this cannot pass against a stale copy. */
function withQuery(search) {
  const html = fs.readFileSync(WEB, "utf8");
  const open = html.indexOf("<script>");
  const close = html.lastIndexOf("</script>");
  if (open === -1 || close === -1) throw new Error("no script block in " + WEB);
  let js = html.slice(open + "<script>".length, close);
  const boot = js.indexOf("/* ---------- boot");
  if (boot === -1) throw new Error("boot marker missing; web file layout changed");
  js = js.slice(0, boot);

  const node = () => new Proxy({}, {
    get(t, k) {
      if (k === "classList") return { add() {}, remove() {}, toggle: () => false };
      if (k === "style" || k === "dataset") return {};
      if (k === "value") return "live";
      return () => node();
    },
    set() { return true; },
  });

  const ctx = {
    console: { log() {}, warn() {}, error() {} },
    Date, Math, JSON, Array, Set, Map, String, Number, Boolean,
    parseFloat, parseInt, isNaN, isFinite, URLSearchParams, URL, Promise,
    setTimeout: () => 0, setInterval: () => 0, clearTimeout() {}, clearInterval() {},
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    location: { search, href: "http://localhost/", protocol: "http:" },
    history: { replaceState() {} },
    window: { matchMedia: () => ({ matches: false }), addEventListener() {} },
    document: {
      getElementById: () => node(), querySelector: () => node(),
      querySelectorAll: () => [], createElement: () => node(),
      documentElement: { setAttribute() {}, getAttribute: () => "dark" },
      addEventListener() {},
    },
  };
  vm.createContext(ctx);
  vm.runInContext(js, ctx, { filename: "web" });
  if (typeof ctx.loadCfg !== "function") {
    throw new Error("loadCfg not found; the config entry point changed");
  }
  return ctx;
}

function main() {
  let defaults;
  try {
    defaults = withQuery("").loadCfg();
  } catch (e) {
    fail(e.message);
    return;
  }

  /* A threshold must never end up as NaN, because a NaN threshold is not a
   * loose threshold, it is no threshold at all: every comparison against it is
   * false, so the branch that would have flagged the problem never runs. */
  const junk = ["abc", "", "NaN", "null", "undefined", "-1", "1e", "0x10"];
  for (const bad of junk) {
    for (const key of ["sd", "sats", "pacc", "volts", "cams", "poll"]) {
      const cfg = withQuery("?" + key + "=" + encodeURIComponent(bad)).loadCfg();
      const v = cfg[key];
      if (typeof v !== "number" || !isFinite(v) || v < 0) {
        fail(`?${key}=${JSON.stringify(bad)} produced ${JSON.stringify(v)}, ` +
             "which is not a usable threshold");
      }
    }
  }

  /* The specific failures that shipped, stated as the readings a pilot would
   * have been shown. If a refactor reopens the hole, these name the cost. */
  const holes = [
    ["?sd=abc", "sd", 0.2, "a card holding 0.2 GB"],
    ["?sats=abc", "sats", 0, "zero satellites"],
    ["?volts=abc", "volts", 3.1, "a pack at 3.1 V"],
  ];
  for (const [query, key, reading, description] of holes) {
    const cfg = withQuery(query).loadCfg();
    // The page flags when the reading falls below the threshold. If the
    // comparison cannot fire, the check is silently gone.
    if (!(reading < cfg[key])) {
      fail(`${query}: ${description} no longer trips the ${key} threshold ` +
           `(threshold is ${JSON.stringify(cfg[key])})`);
    }
  }

  /* A poll interval of zero becomes setInterval(fn, 0), which polls as fast as
   * the network allows and flattens a phone battery in the field. */
  for (const [q, expect] of [["?poll=0", 1], ["?poll=-5", defaults.poll], ["?poll=1000000", 3600]]) {
    const got = withQuery(q).loadCfg().poll;
    if (got !== expect) fail(`${q} gave poll=${got}, expected ${expect}`);
  }

  /* The camera is a local device by definition. A URL arriving in a query
   * string was written by whoever sent the link, not by the pilot, so a
   * non-local address there is not a configuration, it is an attempt to show a
   * readout for a camera nobody is holding. */
  const remote = [
    "http://attacker.example",
    "https://example.com/status",
    "http://8.8.8.8",
    "//evil.example",
  ];
  for (const u of remote) {
    const got = withQuery("?url=" + encodeURIComponent(u)).loadCfg().url;
    if (got === u) fail(`a link supplying ${u} was accepted as the camera URL`);
  }

  /* Local addresses and the same-origin proxy must keep working, or the guard
   * would be protecting the pilot out of a functioning tool. */
  const local = [
    "http://192.168.10.254",   // camera over WiFi
    "http://192.168.1.83",     // camera over Ethernet
    "http://10.0.0.5",
    "http://172.16.4.9",
    "http://127.0.0.1:8000",
    "http://camera.local",
    "/cam",                    // rededge.py serve
  ];
  for (const u of local) {
    const got = withQuery("?url=" + encodeURIComponent(u)).loadCfg().url;
    if (got !== u) fail(`legitimate local address ${u} was rejected (got ${got})`);
  }

  if (process.exitCode === 1) {
    console.error("\nThe web configuration boundary has a hole.");
    return;
  }
  console.log("Web config guard OK: malformed thresholds fall back rather than");
  console.log("vanishing, the poll interval is clamped, and a link cannot point");
  console.log("the tool at a non-local host.");
}

main();
