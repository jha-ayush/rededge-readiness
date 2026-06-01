#!/usr/bin/env python3
"""
rededge.py

Zero-dependency client, readiness check, image offload, and a local
serve-and-proxy bridge for MicaSense RedEdge / Altum cameras.

Why stdlib only: this runs on a field laptop or a Raspberry Pi joined to the
camera WiFi, where "pip install" may not be available. urllib and http.server
cover everything we need.

The camera serves plain HTTP JSON at 192.168.10.254 (WiFi) or 192.168.1.83
(Ethernet), port 80. CORS is a browser-only policy, so Python reaches the
camera directly with no CORS or mixed-content trouble.

Subcommands:
  check     One-shot readiness readout. Exit code: 0 GO, 1 CHECK, 2 NO-GO.
  watch     Repeat the readiness readout on an interval.
  status    Print the raw /status, /version and /networkstatus payloads.
  offload   Download every capture off the SD card, preserving folders.
  capture   Trigger a single capture (action; not exposed via the proxy).
  serve     Serve the HTML readiness page locally and proxy read-only camera
            routes with CORS headers, so the browser tool works live on site.

Examples:
  python3 rededge.py check
  python3 rededge.py watch --interval 3
  python3 rededge.py offload ./flight_2026_06_01 --only tif
  python3 rededge.py capture --bands 31 --block
  python3 rededge.py serve --page rededge-readiness.html --port 8000
"""

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_URL = os.environ.get("REDEDGE_URL", "http://192.168.10.254")
DEFAULT_TIMEOUT = 2.5

# Built-in defaults. The config file (rededge.json) and the iOS Scriptable
# settings share this key schema so all the tools speak the same format.
CONFIG_DEFAULTS = {
    "cameraUrl": DEFAULT_URL, "timeout": DEFAULT_TIMEOUT,
    "sd": 2, "sats": 6, "pacc": 5, "volts": 4.2,
    "cams": 0, "fw": "", "dls": False,
}


def config_path(explicit):
    """Resolve which config file to use: explicit flag, then REDEDGE_CONFIG,
    then rededge.json in the working directory if present, else None."""
    if explicit:
        return explicit
    env = os.environ.get("REDEDGE_CONFIG")
    if env:
        return env
    return "rededge.json" if os.path.exists("rededge.json") else None


def load_config(explicit):
    path = config_path(explicit)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        sys.stderr.write("warning: could not read config %s: %s\n" % (path, e))
        return {}


def resolve_settings(args):
    """Precedence: built-in defaults < config file < command-line flags.
    Returns the cfg dict the evaluator expects (internal key 'url')."""
    f = load_config(getattr(args, "config", None))
    pick = lambda key, val: val if val is not None else f.get(key, CONFIG_DEFAULTS[key])
    return {
        "url": pick("cameraUrl", args.url),
        "timeout": pick("timeout", args.timeout),
        "sd": pick("sd", args.min_sd),
        "sats": pick("sats", args.min_sats),
        "pacc": pick("pacc", args.max_pacc),
        "volts": pick("volts", args.min_volts),
        "cams": pick("cams", args.cameras),
        "fw": pick("fw", args.firmware),
        "dls": True if args.require_dls else f.get("dls", CONFIG_DEFAULTS["dls"]),
    }

# Routes the local proxy is allowed to forward. Read-only by design: the
# browser tool can never trigger a capture, delete a file, or reformat a card.
PROXY_ALLOW = ("status", "version", "networkstatus", "camera_info",
               "timesources", "captures.kmz", "files")


# ----------------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------------
class RedEdgeError(Exception):
    pass


class RedEdgeClient:
    """Thin wrapper over the RedEdge HTTP API. All paths are relative."""

    def __init__(self, base=DEFAULT_URL, timeout=DEFAULT_TIMEOUT):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _url(self, path, params=None):
        url = self.base + "/" + path.lstrip("/")
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def _get_json(self, path, params=None):
        url = self._url(path, params)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, socket.timeout, ValueError) as e:
            raise RedEdgeError("%s: %s" % (path, e))

    # read-only routes
    def status(self):
        return self._get_json("status")

    def version(self):
        return self._get_json("version")

    def networkstatus(self):
        return self._get_json("networkstatus")

    def camera_info(self):
        return self._get_json("camera_info")

    def list_files(self, path="/"):
        sub = path.strip("/")
        return self._get_json("files/" + sub if sub else "files/")

    # actions
    def capture(self, bands=None, block=False, preview=False, store=True):
        params = {
            "block": "true" if block else "false",
            "preview": "true" if preview else None,
            "store_capture": "true" if store else "false",
        }
        if bands is not None:
            params["cache_raw"] = bands
        return self._get_json("capture", params)

    def capture_status(self, capture_id):
        return self._get_json("capture/" + capture_id)

    def download(self, remote_path, dest_path):
        url = self._url("files/" + remote_path.lstrip("/"))
        try:
            with urllib.request.urlopen(url, timeout=max(self.timeout, 30)) as r:
                data = r.read()
        except (urllib.error.URLError, socket.timeout) as e:
            raise RedEdgeError("download %s: %s" % (remote_path, e))
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return len(data)


# ----------------------------------------------------------------------------
# Readiness evaluation (parity with the HTML tool)
# ----------------------------------------------------------------------------
RANK = {"GO": 1, "CHECK": 2, "UNKNOWN": 2, "NO-GO": 3}


def _worst(states):
    out = "GO"
    for s in states:
        if RANK[s] > RANK[out]:
            out = s
    return out


def evaluate(snapshot, cfg):
    """snapshot: {'ok':bool, 'status':..., 'version':..., 'network':...}."""
    if not snapshot.get("ok"):
        return {
            "overall": "NO-GO",
            "reason": "No link to the camera.",
            "checks": [("Camera link", "down", "NO-GO",
                        "no response from " + cfg["url"])],
        }

    s = snapshot.get("status") or {}
    net = snapshot.get("network")
    ver = snapshot.get("version") or {}
    checks = []

    # SD storage
    st, free = s.get("sd_status"), s.get("sd_gb_free")
    state, note = "GO", "card present and writable"
    if st == "NotPresent":
        state, note = "NO-GO", "no SD card inserted"
    elif st == "Full":
        state, note = "NO-GO", "card full, offload before flight"
    elif s.get("sd_warn"):
        state, note = "CHECK", "low-space warning or unrecommended filesystem"
    elif isinstance(free, (int, float)) and free < cfg["sd"]:
        state, note = "CHECK", "below %g GB headroom" % cfg["sd"]
    elif st is None:
        state, note = "UNKNOWN", "card status not reported"
    checks.append(("SD storage",
                   ("%.1f GB" % free) if isinstance(free, (int, float)) else "--",
                   state, note))

    # GPS fix
    sats, pacc = s.get("gps_used_sats"), s.get("p_acc")
    state, note = "GO", "usable fix for geotagging"
    if sats is None:
        state, note = "UNKNOWN", "GPS not reported"
    elif s.get("gps_warn"):
        state, note = "CHECK", "receiver reports interference"
    elif sats < cfg["sats"]:
        state, note = "CHECK", "only %d sats, want %d+" % (sats, cfg["sats"])
    elif isinstance(pacc, (int, float)) and pacc > cfg["pacc"]:
        state, note = "CHECK", "position error %.1f m" % pacc
    elif s.get("utc_time_valid") is False:
        state, note = "CHECK", "time not yet valid"
    checks.append(("GPS fix", ("%d sats" % sats) if sats is not None else "--",
                   state, note))

    # Light sensor (DLS)
    dls = s.get("dls_status")
    state, note = "GO", "irradiance sensor active"
    if dls == "Error":
        state, note = "NO-GO", "DLS error, reflectance data unreliable"
    elif dls == "NotPresent":
        state = "CHECK" if cfg["dls"] else "GO"
        note = "no DLS, reflectance limited" if cfg["dls"] else "no DLS (not required)"
    elif dls in ("Programming", "Initializing"):
        state, note = "CHECK", "DLS warming up, wait"
    elif dls is None:
        state, note = "UNKNOWN", "DLS state not reported"
    checks.append(("Light sensor", dls or "--", state, note))

    # Supply voltage
    v = s.get("bus_volts")
    state, note = "GO", "supply within configured floor"
    if v is None:
        state, note = "UNKNOWN", "voltage not reported"
    elif v < cfg["volts"]:
        state, note = "CHECK", "below %g V floor, verify pack" % cfg["volts"]
    checks.append(("Supply voltage",
                   ("%.2f V" % v) if isinstance(v, (int, float)) else "--",
                   state, note))

    # Camera rig
    if not net or not isinstance(net.get("network_map"), list):
        checks.append(("Camera rig", "--", "UNKNOWN", "network status unavailable"))
    else:
        cams = [x for x in net["network_map"] if x.get("device_type") == "Camera"]
        dlss = [x for x in net["network_map"]
                if str(x.get("device_type", "")).startswith("DLS")]
        state = "GO"
        note = "%d camera%s%s" % (len(cams), "" if len(cams) == 1 else "s",
                                  ", DLS present" if dlss else "")
        fw_set = {x.get("sw_version") for x in cams if x.get("sw_version")}
        card_issue = any(x.get("sd_status") and x.get("sd_status") != "Ok"
                         for x in cams)
        if cfg["cams"] > 0 and len(cams) < cfg["cams"]:
            state = "NO-GO"
            note = "only %d of %d cameras online" % (len(cams), cfg["cams"])
        elif card_issue:
            state, note = "CHECK", "a networked camera has a card issue"
        elif len(fw_set) > 1:
            state, note = "CHECK", "mixed firmware across cameras"
        elif cfg["dls"] and not dlss:
            state, note = "CHECK", "no DLS on the network"
        checks.append(("Camera rig", "%d online" % len(cams), state, note))

    # Firmware
    fw = ver.get("sw_version")
    state, note = "GO", ("running " + fw) if fw else "version reported"
    if fw is None:
        state, note = "UNKNOWN", "version not reported"
    elif cfg["fw"] and fw != cfg["fw"]:
        state, note = "CHECK", "expected %s, running %s" % (cfg["fw"], fw)
    checks.append(("Firmware", fw or "--", state, note))

    overall = _worst(["CHECK" if c[2] == "UNKNOWN" else c[2] for c in checks])
    if overall == "GO":
        reason = "Sensor ready to capture."
    elif overall == "NO-GO":
        reason = "; ".join("%s: %s" % (c[0], c[3])
                           for c in checks if c[2] == "NO-GO") + "."
    else:
        flagged = [c for c in checks if c[2] in ("CHECK", "UNKNOWN")]
        reason = ", ".join(c[0] for c in flagged) + " need attention."
    return {"overall": overall, "reason": reason, "checks": checks}


def snapshot(client):
    """Read the camera. Fail toward caution: any read failure flags the check,
    a dead link reads NO-GO. Never returns a clear pass on missing data."""
    try:
        status = client.status()
        version = client.version()
    except RedEdgeError:
        return {"ok": False}
    try:
        network = client.networkstatus()
    except RedEdgeError:
        network = None
    return {"ok": True, "status": status, "version": version, "network": network}


# ----------------------------------------------------------------------------
# Terminal rendering
# ----------------------------------------------------------------------------
_C = {"GO": "\033[92m", "CHECK": "\033[93m", "NO-GO": "\033[91m",
      "UNKNOWN": "\033[90m", "_": "\033[0m", "dim": "\033[90m"}


def _color(use):
    return _C if use else {k: "" for k in _C}


def render(result, cfg, use_color=True):
    c = _color(use_color)
    o = result["overall"]
    lines = []
    lines.append("%s%s%s  %s" % (c[o], o, c["_"], result["reason"]))
    for label, read, state, note in result["checks"]:
        dot = "%s%s%s" % (c[state], "GO " if state == "GO" else state.ljust(3),
                          c["_"])
        lines.append("  %s  %-18s %-12s %s%s%s"
                     % (dot, label, read, c["dim"], note, c["_"]))
    return "\n".join(lines)


def cfg_from_args(a):
    return resolve_settings(a)


# ----------------------------------------------------------------------------
# Offload
# ----------------------------------------------------------------------------
def offload(client, dest, only=None):
    """Recursively download every file off the card into dest, preserving the
    SET/sub-folder layout. Skips files already present (resume-friendly)."""
    only = only.lower().lstrip(".") if only else None
    total_bytes = 0
    total_files = 0

    def walk(remote):
        nonlocal total_bytes, total_files
        listing = client.list_files(remote)
        for f in listing.get("files", []):
            name = f.get("name", "")
            if only and not name.lower().endswith("." + only):
                continue
            rpath = (remote.rstrip("/") + "/" + name).lstrip("/")
            local = os.path.join(dest, rpath)
            if os.path.exists(local) and os.path.getsize(local) == f.get("size", -1):
                print("  skip  %s" % rpath)
                continue
            n = client.download(rpath, local)
            total_bytes += n
            total_files += 1
            print("  pull  %s  (%.1f MB)" % (rpath, n / 1e6))
        for d in listing.get("directories", []):
            walk((remote.rstrip("/") + "/" + d).lstrip("/"))

    walk("/")
    print("\nDone. %d files, %.1f MB into %s"
          % (total_files, total_bytes / 1e6, dest))


def count_captures(client):
    """Walk the card and count captures. A capture is one IMG_NNNN set of band
    images within a folder; bands of the same capture share the IMG_NNNN prefix.
    Returns counts of capture SET folders, distinct captures, and total bytes."""
    sets = 0
    captures = set()
    total_bytes = 0

    def walk(remote):
        nonlocal sets, total_bytes
        listing = client.list_files(remote)
        for f in listing.get("files", []):
            name = f.get("name", "")
            total_bytes += f.get("size", 0)
            if name.upper().startswith("IMG_") and "_" in name:
                prefix = name.rsplit("_", 1)[0]   # IMG_0000_3.tif -> IMG_0000
                captures.add((remote, prefix))
        for d in listing.get("directories", []):
            if remote in ("", "/") and d.upper().endswith("SET"):
                sets += 1
            walk((remote.rstrip("/") + "/" + d).lstrip("/"))

    walk("/")
    return {"sets": sets, "captures": len(captures), "bytes": total_bytes}



    page_bytes = b""
    if page_path and os.path.exists(page_path):
        with open(page_path, "rb") as f:
            page_bytes = f.read()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                if not page_bytes:
                    self._send(404, b"page not found; pass --page", "text/plain")
                else:
                    self._send(200, page_bytes, "text/html; charset=utf-8")
                return
            if path.startswith("/cam/"):
                route = path[len("/cam/"):]
                head = route.split("/", 1)[0]
                if head not in PROXY_ALLOW:
                    self._send(403, b'{"error":"route not allowed"}',
                               "application/json")
                    return
                try:
                    raw = client._get_json(route)
                    body = json.dumps(raw).encode("utf-8")
                    self._send(200, body, "application/json")
                except RedEdgeError as e:
                    self._send(502,
                               json.dumps({"error": str(e)}).encode("utf-8"),
                               "application/json")
                return
            self._send(404, b"not found", "text/plain")

    return Handler


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.10.254", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def serve(client, page_path, port):
    handler = make_handler(page_path, client)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    ip = _lan_ip()
    print("Serving the readiness page with a CORS proxy to %s" % client.base)
    print("  This machine : http://localhost:%d/?url=%%2Fcam" % port)
    print("  On the WiFi  : http://%s:%d/?url=%%2Fcam" % (ip, port))
    print("  In the page, Camera base URL is /cam (set by the link above).")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="RedEdge field tool (stdlib only).")
    # Defaults are None so we can tell an explicit flag from an unset one; the
    # real defaults live in CONFIG_DEFAULTS and are applied in resolve_settings.
    p.add_argument("--config", help="path to config JSON (default: rededge.json)")
    p.add_argument("--url", default=None, help="camera base URL (overrides config)")
    p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--min-sd", type=float, default=None, help="min SD free (GB)")
    p.add_argument("--min-sats", type=int, default=None, help="min GPS sats")
    p.add_argument("--max-pacc", type=float, default=None, help="max pos error (m)")
    p.add_argument("--min-volts", type=float, default=None, help="min supply (V)")
    p.add_argument("--cameras", type=int, default=None, help="expected cameras (0=any)")
    p.add_argument("--firmware", default=None, help="expected firmware (blank=any)")
    p.add_argument("--require-dls", action="store_const", const=True, default=None,
                   help="DLS required")
    p.add_argument("--no-color", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="one-shot readiness, exit code reflects state")
    w = sub.add_parser("watch", help="repeat readiness on an interval")
    w.add_argument("--interval", type=float, default=3.0)
    sub.add_parser("status", help="dump raw status/version/networkstatus")
    o = sub.add_parser("offload", help="download all captures off the card")
    o.add_argument("dest", help="destination directory")
    o.add_argument("--only", help="extension filter, e.g. tif or jpg")
    cap = sub.add_parser("capture", help="trigger one capture")
    cap.add_argument("--bands", type=int, default=31, help="band bitmask (31=all 5)")
    cap.add_argument("--block", action="store_true")
    cap.add_argument("--preview", action="store_true")
    sub.add_parser("verify", help="post-flight: confirm captures landed on the card")
    sv = sub.add_parser("serve", help="serve the page + CORS proxy for live use")
    sv.add_argument("--page", default="rededge-readiness.html")
    sv.add_argument("--port", type=int, default=8000)
    ic = sub.add_parser("init-config", help="write a template rededge.json")
    ic.add_argument("--path", default="rededge.json")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.cmd == "init-config":
        if os.path.exists(args.path):
            sys.stderr.write("refusing to overwrite existing %s\n" % args.path)
            return 1
        with open(args.path, "w") as f:
            json.dump(CONFIG_DEFAULTS, f, indent=2)
            f.write("\n")
        print("wrote template config to %s" % args.path)
        return 0

    cfg = resolve_settings(args)
    client = RedEdgeClient(cfg["url"], cfg["timeout"])
    use_color = (not args.no_color) and sys.stdout.isatty()

    if args.cmd in ("check", "watch"):
        if args.cmd == "check":
            result = evaluate(snapshot(client), cfg)
            print(render(result, cfg, use_color))
            return {"GO": 0, "CHECK": 1, "NO-GO": 2}[result["overall"]]
        while True:
            result = evaluate(snapshot(client), cfg)
            os.system("clear" if os.name != "nt" else "cls")
            print(render(result, cfg, use_color))
            print("\n%supdated %s, every %gs, Ctrl-C to stop%s"
                  % (_color(use_color)["dim"], time.strftime("%H:%M:%S"),
                     args.interval, _color(use_color)["_"]))
            try:
                time.sleep(args.interval)
            except KeyboardInterrupt:
                return 0

    if args.cmd == "status":
        snap = snapshot(client)
        if not snap.get("ok"):
            print("No link to camera at %s" % cfg["url"])
            return 2
        print(json.dumps({k: snap[k] for k in ("status", "version", "network")},
                         indent=2))
        return 0

    if args.cmd == "offload":
        offload(client, args.dest, args.only)
        return 0

    if args.cmd == "verify":
        try:
            info = count_captures(client)
        except RedEdgeError as e:
            print("Could not read the card: %s" % e)
            return 2
        st = {}
        try:
            st = client.status()
        except RedEdgeError:
            pass
        free, total = st.get("sd_gb_free"), st.get("sd_gb_total")
        c = _color(use_color)
        ok = info["captures"] > 0
        head = "%s%s%s" % (c["GO"] if ok else c["CHECK"],
                           "CAPTURES FOUND" if ok else "NO CAPTURES",
                           c["_"])
        print("%s  %d capture%s across %d SET folder%s, %.1f MB"
              % (head, info["captures"], "" if info["captures"] == 1 else "s",
                 info["sets"], "" if info["sets"] == 1 else "s",
                 info["bytes"] / 1e6))
        if isinstance(free, (int, float)) and isinstance(total, (int, float)):
            print("  SD: %.1f of %.1f GB free" % (free, total))
        if not ok:
            print("  Card has no images. Do not pack up before re-checking.")
        return 0 if ok else 1

    if args.cmd == "capture":
        try:
            print(json.dumps(client.capture(bands=args.bands, block=args.block,
                                             preview=args.preview), indent=2))
            return 0
        except RedEdgeError as e:
            print("capture failed: %s" % e)
            return 2

    if args.cmd == "serve":
        serve(client, args.page, args.port)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
