#!/usr/bin/env python3
"""
rededge_mock.py

A faithful, stdlib-only mock of the MicaSense RedEdge HTTP API, for exercising
the field tools end to end without a camera. It serves the read routes the
tools use (status, version, networkstatus, camera_info, timesources), a small
fake SD card tree (files and downloads, so offload works), capture stubs, and
a captures.kmz, with payloads shaped to the documented schemas.

Like the real camera, it does NOT send CORS headers by default, so the browser
path must go through "rededge.py serve". Pass --cors to relax that for quick
direct testing of the web page.

Usage:
  python3 rededge_mock.py --port 8080 --scenario go
  python3 rededge.py --url http://127.0.0.1:8080 check
  python3 rededge.py --url http://127.0.0.1:8080 offload ./_mock_pull

Scenarios (identical to the web and iOS demo set): go, sd, nosd, gps, pos, time,
warmup, volts, rig, warn, dls, nogo. The "no link" state is simulated by simply
not running the server.
Point a phone's Scriptable script at http://<this-machine-ip>:8080 to test it
against this mock over the WiFi (set --host 0.0.0.0, the default).
"""

import argparse
import io
import json
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BANDS = [475, 560, 668, 840, 717]
BANDWIDTHS = [20, 20, 10, 40, 10]

# Fake SD card layout: nodes have files {name: size} and dirs {name: node}.
SD = {
    "files": {},
    "dirs": {
        "0000SET": {
            "files": {"paramlog.dat": 512},
            "dirs": {
                "000": {
                    "files": {("IMG_0000_%d.tif" % b): 2048 for b in range(1, 6)},
                    "dirs": {},
                }
            },
        }
    },
}


def healthy_status():
    return {
        "sd_status": "Ok", "sd_gb_free": 20.1, "sd_gb_total": 29.7, "sd_warn": False,
        "sd_type": "FAT32", "bus_volts": 4.69,
        "gps_used_sats": 9, "gps_vis_sats": 14, "gps_warn": False, "p_acc": 2.4,
        "gps_lat": 0.6327, "gps_lon": -1.8278, "gps_type": "Ublox",
        "alt_agl": 14.8, "alt_msl": 283.5, "course": 169.4, "vel_2d": 0.08,
        "dls_status": "Ok", "auto_cap_active": False,
        "time_source": "GPS", "utc_time": "2026-06-01T18:34:00.000Z",
        "utc_time_valid": True, "sv_info": [],
    }


def payloads(scenario):
    status = healthy_status()
    cam_fw = "v7.1.0"
    cams = [{"bands": BANDS, "bandwidths": BANDWIDTHS, "device_type": "Camera",
             "gps_source": "", "mode": "main", "sd_gb_free": 20.1,
             "sd_gb_total": 29.7, "sd_status": "Ok", "sd_type": "FAT32",
             "sd_warn": False, "serial": "RM02-1839163-SC", "sw_version": cam_fw}]
    dls = [{"bands": [450, 500, 550, 570, 600, 610, 650, 680, 730, 760, 810, 860],
            "bandwidths": [], "device_type": "DLS 2", "gps_source": "direct",
            "mode": "auxiliary", "serial": "DA03-1921711-OB", "sw_version": "v1.2.3"}]

    # Canonical scenarios, identical to the web and iOS demo set. The same name
    # produces the same readiness state across all three layers. "down" (no
    # link) is simulated by simply not running the server.
    if scenario == "go":
        pass
    elif scenario == "sd":
        status["sd_gb_free"] = 0.7
        status["sd_warn"] = True
    elif scenario == "nosd":
        status["sd_status"] = "NotPresent"
    elif scenario == "gps":
        status["gps_used_sats"] = 4
    elif scenario == "pos":
        status["p_acc"] = 12.0
    elif scenario == "time":
        status["utc_time_valid"] = False
    elif scenario == "warmup":
        status["dls_status"] = "Programming"
    elif scenario == "volts":
        status["bus_volts"] = 3.9
    elif scenario == "rig":
        aux = dict(cams[0])
        aux["sw_version"] = "v7.0.0"
        aux["serial"] = "RM02-1839202-SC"
        cams = [cams[0], aux]
    elif scenario == "warn":
        status["sd_gb_free"] = 0.7
        status["sd_warn"] = True
        status["bus_volts"] = 3.9
        status["gps_used_sats"] = 4
    elif scenario == "dls":
        status["dls_status"] = "Error"
    elif scenario == "nogo":
        status["sd_status"] = "NotPresent"
        status["dls_status"] = "Error"

    version = {"sw_version": cam_fw, "serial": "RM02-1839163-SC"}
    network = {"network_map": cams + dls}
    camera_info = {str(i + 1): {"type": "bandpass", "center_nm": BANDS[i],
                                "bandwidth_nm": BANDWIDTHS[i], "focal_length_px": 1100.0,
                                "image_width": 1280, "image_height": 960}
                   for i in range(len(BANDS))}
    timesources = {"time_sources": [
        {"active": True, "delay": 5.0e-09, "type": "GPS"}]}
    return {"status": status, "version": version, "networkstatus": network,
            "camera_info": camera_info, "timesources": timesources}


def resolve(parts):
    """Walk SD by path parts. Return ('dir', node) or ('file', size) or None."""
    node = SD
    for i, p in enumerate(parts):
        if p in node["dirs"]:
            node = node["dirs"][p]
        elif p in node["files"] and i == len(parts) - 1:
            return ("file", node["files"][p])
        else:
            return None
    return ("dir", node)


def listing(node):
    return {"files": [{"name": n, "size": s} for n, s in node["files"].items()],
            "directories": list(node["dirs"].keys())}


def fake_tiff(size):
    body = bytearray(b"II*\x00")           # little-endian TIFF magic
    body += bytes(max(0, size - len(body)))
    return bytes(body[:size])


def make_kmz():
    kml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder>'
           '<name>0000SET</name><Placemark><name>0</name><Point>'
           '<coordinates>-104.7104612,36.2709446,283.5</coordinates>'
           '</Point></Placemark></Folder></Document></kml>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
    return buf.getvalue()


def make_handler(scenario, cors):
    data = payloads(scenario)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            print("  mock <- %s" % (self.path))

        def _hdr(self, code, ctype, length):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self._hdr(code, "application/json", len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _bytes(self, body, ctype):
            self._hdr(200, ctype, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0].strip("/")
            parts = path.split("/") if path else []
            head = parts[0] if parts else ""

            if head in ("status", "version", "networkstatus", "camera_info",
                        "timesources"):
                key = "networkstatus" if head == "networkstatus" else head
                self._json(data[key])
                return
            if path == "captures.kmz":
                self._bytes(make_kmz(), "application/vnd.google-earth.kmz")
                return
            if head == "capture" and len(parts) == 1:        # command capture
                self._json({"status": "complete", "id": "mock123",
                            "time": "2026-06-01T18:34:30.000Z",
                            "raw_storage_path": {str(b): "/files/0000SET/000/IMG_0000_%d.tif" % b
                                                 for b in range(1, 6)}})
                return
            if head == "capture" and len(parts) == 2:        # capture status
                self._json({"status": "complete", "id": parts[1]})
                return
            if head == "files":
                sub = parts[1:]
                r = resolve(sub)
                if r is None:
                    self._json({"error": "not found"}, 404)
                elif r[0] == "dir":
                    self._json(listing(r[1]))
                else:
                    self._bytes(fake_tiff(r[1]), "image/tiff")
                return
            self._json({"error": "route not mocked: /%s" % path}, 404)

    return Handler


def main():
    p = argparse.ArgumentParser(description="Mock RedEdge HTTP API for testing.")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--scenario", default="go",
                   choices=["go", "sd", "nosd", "gps", "pos", "time", "warmup",
                            "volts", "rig", "warn", "dls", "nogo"])
    p.add_argument("--cors", action="store_true",
                   help="send CORS headers (the real camera does not)")
    a = p.parse_args()
    httpd = ThreadingHTTPServer((a.host, a.port), make_handler(a.scenario, a.cors))
    print("Mock RedEdge (%s) on http://%s:%d  cors=%s"
          % (a.scenario, a.host, a.port, a.cors))
    print("Point a tool at it:  python3 rededge.py --url http://127.0.0.1:%d check" % a.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
