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
  verify    Post-flight: walk the card and confirm captures landed.
  serve     Serve the HTML readiness page locally and proxy read-only camera
            routes with CORS headers, so the browser tool works live on site.
  init-config
            Write a template rededge.json.

Examples:
  python3 rededge.py check
  python3 rededge.py watch --interval 3
  python3 rededge.py offload ./flight_2026_06_01 --only tif
  python3 rededge.py verify
  python3 rededge.py capture --bands 31 --block
  python3 rededge.py serve --port 8000      # serves web/rededge-readiness.html
"""

import argparse
import json
import os
import re
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


# ----------------------------------------------------------------------------
# Values: what counts as a reading
# ----------------------------------------------------------------------------
def _num(v):
    """A finite number that was actually reported, or None.

    Anything else is treated as not reported: a string (even a numeric one,
    because text is not a measurement), a bool (a subclass of int in Python,
    so True would otherwise count as one satellite), NaN and infinities. The
    comparison operators would either raise on these or compare them in ways
    that skip the branch meant to catch a problem, and a skipped branch is a
    threshold that no longer exists."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _text(v):
    """A non-empty string that was reported, or None. A status that arrives as
    any other type, or as an empty string, is not a recognizable status and
    must not be compared as one."""
    return v if isinstance(v, str) and v else None


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
            loaded = json.load(f)
    except (OSError, ValueError) as e:
        sys.stderr.write("warning: could not read config %s: %s\n" % (path, e))
        return {}
    if not isinstance(loaded, dict):
        sys.stderr.write("warning: config %s is not a JSON object; ignoring it\n"
                         % path)
        return {}
    return loaded


# The numeric settings, and the two that are counts. The same rule lives in
# sanitizeCfg in web/app.js and sanitizeSettings in the iOS script: the three
# clients must refuse the same values, or a config that one tool rejects would
# quietly disable a threshold in another.
_NUMERIC_KEYS = ("timeout", "sd", "sats", "pacc", "volts", "cams")
_INTEGER_KEYS = ("sats", "cams")


def sanitize_settings(cfg, warn=None):
    """Return cfg with every threshold usable, falling back to the built-in
    default for anything that is not.

    A threshold that cannot be compared is not a loose threshold, it is the
    absence of one: in Python a string beside a number raises mid-readout, and
    a negative floor can never fire. Both are replaced by the default, and the
    replacement is said out loud through `warn` (stderr by default) so a typo
    in rededge.json is noticed rather than silently forgiven."""
    warn = warn or (lambda m: sys.stderr.write("warning: %s\n" % m))
    out = dict(cfg)
    for key in _NUMERIC_KEYS:
        raw = out.get(key)
        val = _num(raw)
        if val is None and isinstance(raw, str):
            try:
                val = _num(float(raw.strip()))
            except ValueError:
                val = None
        if val is None or val < 0 or (key == "timeout" and val <= 0):
            default = CONFIG_DEFAULTS[key]
            warn("config value %s=%r is not usable; using the default %g"
                 % (key, raw, default))
            val = default
        if key in _INTEGER_KEYS:
            val = int(val)
        out[key] = val
    fw = out.get("fw")
    out["fw"] = fw.strip() if isinstance(fw, str) else ""
    dls = out.get("dls")
    if not isinstance(dls, bool):
        dls = str(dls).strip().lower() in ("1", "true", "yes")
    out["dls"] = dls
    url = out.get("url")
    out["url"] = url.strip() if isinstance(url, str) and url.strip() else DEFAULT_URL
    return out


def resolve_settings(args):
    """Precedence: built-in defaults < config file < command-line flags.
    Returns the cfg dict the evaluator expects (internal key 'url'), with every
    threshold sanitized on the way through."""
    f = load_config(getattr(args, "config", None))
    pick = lambda key, val: val if val is not None else f.get(key, CONFIG_DEFAULTS[key])
    return sanitize_settings({
        "url": pick("cameraUrl", args.url),
        "timeout": pick("timeout", args.timeout),
        "sd": pick("sd", args.min_sd),
        "sats": pick("sats", args.min_sats),
        "pacc": pick("pacc", args.max_pacc),
        "volts": pick("volts", args.min_volts),
        "cams": pick("cams", args.cameras),
        "fw": pick("fw", args.firmware),
        "dls": True if args.require_dls else f.get("dls", CONFIG_DEFAULTS["dls"]),
    })

# Routes the local proxy is allowed to forward. Read-only by design: the
# browser tool can never trigger a capture, delete a file, or reformat a card.
# Every route here must return JSON, because the forwarder decodes JSON. A
# binary route (captures.kmz, image files) would always fail, so it is not
# listed rather than listed and permanently broken.
PROXY_ALLOW = ("status", "version", "networkstatus", "camera_info",
               "timesources", "files")

# The only allowlisted route that takes a sub-path (files/0000SET/000). The
# others are single names, and a sub-path under them is refused rather than
# forwarded, because the camera's own server decides what "status/../capture"
# means and the proxy must not let it decide that.
PROXY_SUBPATH = ("files",)
_SEGMENT_OK = re.compile(r"[A-Za-z0-9._-]+")

# The page the local server ships by default: the web client beside this
# script. Resolved against the script's own location so "python3 rededge.py
# serve" works from any working directory.
DEFAULT_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "web", "rededge-readiness.html")

# Response headers on the locally served page. They mirror the hosted page's
# web/_headers, minus HSTS (meaningless on plain HTTP) and the cross-origin
# opener policy (a LAN origin has no cross-origin windows to isolate), so a
# pilot running the tool locally gets the same page contract as the demo, and
# test_rededge.py holds the two files to each other.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'; object-src 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), usb=(), payment=()",
}

# Sibling files the page is allowed to pull from the same directory. Named
# explicitly rather than serving the directory, because a static server that
# will hand over "whatever is next to the page" is one path-traversal bug away
# from serving the rest of the disk. The page needs exactly these, and
# test_rededge.py reads the page's own markup to hold the two lists to each
# other: an asset the page names that is missing here is a red run.
STATIC_ALLOW = {
    "app.js": "application/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
    "apple-touch-icon.png": "image/png",
    "rededge-social.png": "image/png",
}
STATIC_HEADERS = {"X-Content-Type-Options": "nosniff"}


def proxy_route(path):
    """The camera route a /cam/ request may be forwarded to, or None.

    The request path is percent-decoded first, because the camera would decode
    it anyway and "%2e%2e" is ".." to the camera even when it is not to a naive
    string check. Every segment must then be a plain name: no dot segments, no
    empty segments, nothing outside [A-Za-z0-9._-]. The head must be an
    allowlisted route, and only the routes in PROXY_SUBPATH may carry more than
    one segment. A trailing slash (the files/ root listing) is the one shape
    that is allowed to end in an empty segment."""
    if not path.startswith("/cam/"):
        return None
    route = urllib.parse.unquote(path[len("/cam/"):])
    trailing = route.endswith("/")
    parts = route.rstrip("/").split("/") if route.rstrip("/") else []
    if not parts or parts[0] not in PROXY_ALLOW:
        return None
    for seg in parts:
        if seg in (".", "..") or not _SEGMENT_OK.fullmatch(seg):
            return None
    if len(parts) > 1 and parts[0] not in PROXY_SUBPATH:
        return None
    if trailing and parts[0] not in PROXY_SUBPATH:
        return None
    return "/".join(parts) + ("/" if trailing else "")


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
    """snapshot: {'ok':bool, 'status':..., 'version':..., 'network':...}.

    Every check reads its own field and nothing else. A field that is missing,
    or present with a type that is not a reading, is UNKNOWN for that check,
    which the overall verdict folds into CHECK. The web and iOS evaluators
    implement this same table row for row; parity_check.js holds all three to
    it, check by check."""
    if not snapshot.get("ok"):
        return {
            "overall": "NO-GO",
            "reason": "No link to the camera.",
            "checks": [("Camera link", "down", "NO-GO",
                        "no response from " + cfg["url"])],
        }

    s = snapshot.get("status")
    if not isinstance(s, dict):
        s = {}
    net = snapshot.get("network")
    ver = snapshot.get("version")
    if not isinstance(ver, dict):
        ver = {}
    checks = []

    # SD storage
    st, free = _text(s.get("sd_status")), _num(s.get("sd_gb_free"))
    state, note = "GO", "card present and writable"
    if st == "NotPresent":
        state, note = "NO-GO", "no SD card inserted"
    elif st == "Full":
        state, note = "NO-GO", "card full, offload before flight"
    elif s.get("sd_warn"):
        state, note = "CHECK", "low-space warning or unrecommended filesystem"
    elif free is not None and free < cfg["sd"]:
        state, note = "CHECK", "below %g GB headroom" % cfg["sd"]
    elif st != "Ok":
        state, note = "UNKNOWN", ("card status not reported" if st is None
                                  else "unrecognized card status")
    elif free is None:
        state, note = "UNKNOWN", "free space not reported"
    checks.append(("SD storage", ("%.1f GB" % free) if free is not None else "--",
                   state, note))

    # GPS fix: satellites and interference only. Position accuracy and clock
    # validity each have a row of their own below, so one cause flags one row.
    sats = _num(s.get("gps_used_sats"))
    state, note = "GO", "usable fix for geotagging"
    if sats is None:
        state, note = "UNKNOWN", "GPS not reported"
    elif s.get("gps_warn"):
        state, note = "CHECK", "receiver reports interference"
    elif sats < cfg["sats"]:
        state, note = "CHECK", "only %d sats, want %d+" % (sats, cfg["sats"])
    checks.append(("GPS fix", ("%d sats" % sats) if sats is not None else "--",
                   state, note))

    # Position accuracy. Reported separately from the fix itself because the two
    # fail independently: a receiver can hold plenty of satellites and still
    # report an error ellipse too wide for the survey.
    pacc = _num(s.get("p_acc"))
    state, note = "GO", "threshold %g m" % cfg["pacc"]
    if pacc is None:
        state, note = "UNKNOWN", "not reported"
    elif pacc > cfg["pacc"]:
        state = "CHECK"
    checks.append(("Position accuracy",
                   ("%.1f m" % pacc) if pacc is not None else "--",
                   state, note))

    # Light sensor (DLS)
    dls = _text(s.get("dls_status"))
    state, note = "GO", "irradiance sensor active"
    if dls == "Error":
        state, note = "NO-GO", "DLS error, reflectance data unreliable"
    elif dls == "NotPresent":
        state = "CHECK" if cfg["dls"] else "GO"
        note = ("no DLS, reflectance calibration limited" if cfg["dls"]
                else "no DLS (not required)")
    elif dls in ("Programming", "Initializing"):
        state, note = "CHECK", "DLS warming up, wait"
    elif dls != "Ok":
        state, note = "UNKNOWN", ("DLS state not reported" if dls is None
                                  else "unrecognized DLS state")
    checks.append(("Light sensor", dls or "--", state, note))

    # Supply voltage
    v = _num(s.get("bus_volts"))
    state, note = "GO", "supply within configured floor"
    if v is None:
        state, note = "UNKNOWN", "voltage not reported"
    elif v < cfg["volts"]:
        state, note = "CHECK", "below %g V floor, verify pack" % cfg["volts"]
    checks.append(("Supply voltage", ("%.2f V" % v) if v is not None else "--",
                   state, note))

    # Time source. Geotags and reflectance both depend on a valid clock, so a
    # camera that reports neither a source nor a validity flag is unconfirmed
    # rather than fine.
    ts, valid = _text(s.get("time_source")), s.get("utc_time_valid")
    state, note = "GO", (("%s time source" % ts) if ts else "time valid")
    if valid is False:
        state, note = "CHECK", "UTC time not yet valid"
    elif ts is None and valid is None:
        state, note = "UNKNOWN", "time source not reported"
    checks.append(("Time source", ts or ("valid" if valid else "--"),
                   state, note))

    # Camera rig. Only object entries count as devices; anything else in the
    # list is noise from a payload that is not what the tool expects.
    if not isinstance(net, dict) or not isinstance(net.get("network_map"), list):
        checks.append(("Camera rig", "--", "UNKNOWN", "network status unavailable"))
    else:
        devices = [x for x in net["network_map"] if isinstance(x, dict)]
        cams = [x for x in devices if x.get("device_type") == "Camera"]
        dlss = [x for x in devices
                if str(x.get("device_type", "")).startswith("DLS")]
        state = "GO"
        note = "%d camera%s%s" % (len(cams), "" if len(cams) == 1 else "s",
                                  ", DLS present" if dlss else "")
        fw_set = {x.get("sw_version") for x in cams
                  if isinstance(x.get("sw_version"), str) and x.get("sw_version")}
        card_issue = any(x.get("sd_status") and x.get("sd_status") != "Ok"
                         for x in cams)
        if cfg["cams"] > 0 and len(cams) < cfg["cams"]:
            state = "NO-GO"
            note = "only %d of %d cameras online" % (len(cams), cfg["cams"])
        elif not cams:
            # The camera answered /status, so at least one camera exists; a
            # map that lists none is not a rig of zero, it is a map that could
            # not be read.
            state, note = "UNKNOWN", "no cameras listed"
        elif card_issue:
            state, note = "CHECK", "a networked camera has a card issue"
        elif len(fw_set) > 1:
            state, note = "CHECK", "mixed firmware across cameras"
        elif cfg["dls"] and not dlss:
            state, note = "CHECK", "no DLS on the network"
        checks.append(("Camera rig", "%d online" % len(cams), state, note))

    # Firmware
    fw = _text(ver.get("sw_version"))
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
    a dead link reads NO-GO. Never returns a clear pass on missing data.

    /status is the critical read; if it fails or is malformed, that is a
    no-link NO-GO. /version and /networkstatus are best-effort, so a flaky
    secondary endpoint degrades only its own check (to CHECK) instead of
    failing the whole readout."""
    try:
        status = client.status()
    except RedEdgeError:
        return {"ok": False}
    if not isinstance(status, dict):
        return {"ok": False}
    try:
        version = client.version()
    except RedEdgeError:
        version = None
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


def render(result, use_color=True):
    c = _color(use_color)
    o = result["overall"]
    lines = []
    lines.append("%s%s%s  %s" % (c[o], o, c["_"], result["reason"]))
    for label, read, state, note in result["checks"]:
        # The state column is padded to the widest state (UNKNOWN, 7) so the
        # label, reading and note columns line up on every row.
        dot = "%s%-7s%s" % (c[state], state, c["_"])
        lines.append("  %s  %-18s %-12s %s%s%s"
                     % (dot, label, read, c["dim"], note, c["_"]))
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Card walk: offload and verify
# ----------------------------------------------------------------------------
def _card_entry_name(value):
    """A file or folder name from the card that is safe to join onto a local
    path, or None.

    The camera is an unauthenticated device on an open WiFi, and the README
    says to treat that network as untrusted. A listing is therefore input, not
    truth: a spoofed device could answer with a name like "../../.ssh/config"
    and, joined naively, offload would write outside the destination folder.
    A name is one path segment or it is nothing."""
    if not isinstance(value, str) or not value or value in (".", ".."):
        return None
    if "/" in value or "\\" in value or "\x00" in value:
        return None
    return value


def _card_listing(client, remote):
    """One /files listing as a (files, directories) pair of clean entries.
    Raises RedEdgeError if the camera answered with something that is not a
    listing, so a walk never silently reports an empty card off junk."""
    listing = client.list_files(remote)
    if not isinstance(listing, dict):
        raise RedEdgeError("files/%s: malformed listing" % remote.strip("/"))
    files = []
    raw_files = listing.get("files")
    for f in (raw_files if isinstance(raw_files, list) else []):
        name = _card_entry_name(f.get("name")) if isinstance(f, dict) else None
        if name is None:
            continue
        size = _num(f.get("size"))
        files.append((name, size))
    raw_dirs = listing.get("directories")
    dirs = [d for d in (raw_dirs if isinstance(raw_dirs, list) else [])
            if _card_entry_name(d) is not None]
    return files, dirs


def offload(client, dest, only=None):
    """Recursively download every file off the card into dest, preserving the
    SET/sub-folder layout. Skips files already present (resume-friendly)."""
    only = only.lower().lstrip(".") if only else None
    total_bytes = 0
    total_files = 0
    dest_root = os.path.abspath(dest)

    def walk(remote):
        nonlocal total_bytes, total_files
        files, dirs = _card_listing(client, remote)
        for name, size in files:
            if only and not name.lower().endswith("." + only):
                continue
            rpath = (remote.rstrip("/") + "/" + name).lstrip("/")
            local = os.path.join(dest_root, rpath)
            # Belt and suspenders: the segments are already vetted, and the
            # final path is still required to sit under the destination.
            if os.path.commonpath([dest_root, os.path.abspath(local)]) != dest_root:
                print("  skip  %s  (refused: escapes the destination)" % rpath)
                continue
            if (os.path.exists(local) and size is not None
                    and os.path.getsize(local) == size):
                print("  skip  %s" % rpath)
                continue
            n = client.download(rpath, local)
            total_bytes += n
            total_files += 1
            print("  pull  %s  (%.1f MB)" % (rpath, n / 1e6))
        for d in dirs:
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
        files, dirs = _card_listing(client, remote)
        for name, size in files:
            total_bytes += size or 0
            if name.upper().startswith("IMG_") and "_" in name:
                prefix = name.rsplit("_", 1)[0]   # IMG_0000_3.tif -> IMG_0000
                captures.add((remote, prefix))
        for d in dirs:
            if remote in ("", "/") and d.upper().endswith("SET"):
                sets += 1
            walk((remote.rstrip("/") + "/" + d).lstrip("/"))

    walk("/")
    return {"sets": sets, "captures": len(captures), "bytes": total_bytes}


# ----------------------------------------------------------------------------
# Local serve and read-only proxy
# ----------------------------------------------------------------------------
def make_handler(page_path, client):
    """Build the HTTP handler for `serve`.

    This exists because a browser cannot read the camera directly: the camera
    sends no CORS headers, and from an HTTPS page its plain-HTTP address is
    mixed content. Serving the page and the camera from one local origin
    removes both problems, which is the only way the web tool reads a real
    camera.

    The proxy is read-only by construction: only PROXY_ALLOW routes are
    forwarded, every segment of a forwarded route is vetted by proxy_route, and
    only GET and HEAD are implemented, so the browser can never trigger a
    capture, delete a file, or reformat a card.
    """
    page_bytes = b""
    page_dir = None
    if page_path and os.path.exists(page_path):
        with open(page_path, "rb") as f:
            page_bytes = f.read()
        page_dir = os.path.dirname(os.path.abspath(page_path)) or "."

    # The page's sibling assets come from STATIC_ALLOW at module level, so the
    # test suite can hold the list to the page's markup.
    # The proxy answers are the one place a cross-origin header belongs: they
    # exist so a browser can read camera JSON. The page and its assets are
    # same-origin documents and get no such grant.
    PROXY_HEADERS = {"Access-Control-Allow-Origin": "*",
                     "X-Content-Type-Options": "nosniff",
                     "Cache-Control": "no-store"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            # urlsplit, not urlparse: urlparse would peel a ";params" tail off
            # the last segment before the route is vetted, so "x;y" would be
            # forwarded as "x". The whole segment is judged, or none of it.
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", "/index.html"):
                if not page_bytes:
                    self._send(404, b"page not found; pass --page", "text/plain")
                else:
                    self._send(200, page_bytes, "text/html; charset=utf-8",
                               PAGE_HEADERS)
                return
            name = path.lstrip("/")
            if name in STATIC_ALLOW and page_dir:
                # os.path.basename strips any traversal before it reaches disk,
                # and the name is already known-good from the allowlist.
                fp = os.path.join(page_dir, os.path.basename(name))
                if os.path.exists(fp):
                    with open(fp, "rb") as f:
                        self._send(200, f.read(), STATIC_ALLOW[name],
                                   STATIC_HEADERS)
                else:
                    self._send(404, b"not found", "text/plain")
                return

            if path.startswith("/cam/"):
                route = proxy_route(path)
                if route is None:
                    self._send(403, b'{"error":"route not allowed"}',
                               "application/json", PROXY_HEADERS)
                    return
                try:
                    raw = client._get_json(route)
                    body = json.dumps(raw).encode("utf-8")
                    self._send(200, body, "application/json", PROXY_HEADERS)
                except RedEdgeError as e:
                    self._send(502,
                               json.dumps({"error": str(e)}).encode("utf-8"),
                               "application/json", PROXY_HEADERS)
                return
            self._send(404, b"not found", "text/plain")

        # HEAD answers with the same headers and no body; _send already skips
        # the body for it, so the two verbs cannot drift apart.
        do_HEAD = do_GET

    return Handler


def _lan_ip(camera_base=DEFAULT_URL):
    """Best-effort local address on the camera's network, used only to print a
    reachable URL. Routed against the configured camera host so an Ethernet
    setup (192.168.1.83) reports the right interface, not the WiFi one. No
    packets are sent; connect() on a UDP socket only selects a route."""
    host = urllib.parse.urlparse(camera_base).hostname or "192.168.10.254"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def serve(client, page_path, port):
    handler = make_handler(page_path, client)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    ip = _lan_ip(client.base)
    print("Serving the readiness page with a CORS proxy to %s" % client.base)
    if not (page_path and os.path.exists(page_path)):
        # Say so now, at the terminal, rather than as a 404 in a browser on
        # the other side of the room.
        print("  WARNING: page not found at %s; the proxy runs, the page will 404"
              % page_path)
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
    sv.add_argument("--page", default=DEFAULT_PAGE,
                    help="readiness page to serve (default: the web/ page "
                         "beside this script)")
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
            print(render(result, use_color))
            return {"GO": 0, "CHECK": 1, "NO-GO": 2}[result["overall"]]
        while True:
            result = evaluate(snapshot(client), cfg)
            os.system("clear" if os.name != "nt" else "cls")
            print(render(result, use_color))
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
        try:
            offload(client, args.dest, args.only)
        except RedEdgeError as e:
            # A dead link or a junk listing mid-walk is a message and an exit
            # code, not a traceback; the files already pulled stay on disk and
            # the next run resumes past them.
            print("Could not read the card: %s" % e)
            return 2
        return 0

    if args.cmd == "verify":
        try:
            info = count_captures(client)
        except RedEdgeError as e:
            print("Could not read the card: %s" % e)
            return 2
        st = {}
        try:
            raw = client.status()
            if isinstance(raw, dict):
                st = raw
        except RedEdgeError:
            pass   # the SD line is optional; the count above is the verdict
        free, total = _num(st.get("sd_gb_free")), _num(st.get("sd_gb_total"))
        c = _color(use_color)
        ok = info["captures"] > 0
        head = "%s%s%s" % (c["GO"] if ok else c["CHECK"],
                           "CAPTURES FOUND" if ok else "NO CAPTURES",
                           c["_"])
        print("%s  %d capture%s across %d SET folder%s, %.1f MB"
              % (head, info["captures"], "" if info["captures"] == 1 else "s",
                 info["sets"], "" if info["sets"] == 1 else "s",
                 info["bytes"] / 1e6))
        if free is not None and total is not None:
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
