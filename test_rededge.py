#!/usr/bin/env python3
"""
test_rededge.py

Regression tests for the readiness logic shared across all the tools, plus the
offload walk and config precedence. Stdlib only (unittest), no dependencies.

Run:
  python3 -m unittest test_rededge -v
"""

import contextlib
import io
import json
import os
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import rededge
import rededge_mock

EXIT = {"GO": 0, "CHECK": 1, "NO-GO": 2}


def base_cfg(**over):
    c = {"url": "", "timeout": 2.0, "sd": 2, "sats": 6, "pacc": 5,
         "volts": 4.2, "cams": 0, "fw": "", "dls": False}
    c.update(over)
    return c


@contextlib.contextmanager
def mock_server(scenario):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                rededge_mock.make_handler(scenario, cors=False))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        httpd.shutdown()
        httpd.server_close()


# scenario -> (cfg overrides, expected overall). Mirrors the shared web/iOS
# demo set; this also cross-checks that the Python evaluation agrees with them.
CASES = {
    "go": ({}, "GO"),
    "sd": ({}, "CHECK"),
    "nosd": ({}, "NO-GO"),
    "gps": ({}, "CHECK"),
    "pos": ({}, "CHECK"),
    "time": ({}, "CHECK"),
    "warmup": ({}, "CHECK"),
    "volts": ({}, "CHECK"),
    "rig": ({}, "CHECK"),
    "warn": ({}, "CHECK"),
    "dls": ({}, "NO-GO"),
    "nogo": ({}, "NO-GO"),
}


class ReadinessOverLiveHTTP(unittest.TestCase):
    def test_scenarios_overall_and_exit(self):
        for scenario, (over, expected) in CASES.items():
            with self.subTest(scenario=scenario):
                with mock_server(scenario) as url:
                    cfg = base_cfg(url=url, **over)
                    client = rededge.RedEdgeClient(url, cfg["timeout"])
                    res = rededge.evaluate(rededge.snapshot(client), cfg)
                    self.assertEqual(res["overall"], expected)
                    self.assertIn(EXIT[res["overall"]], (0, 1, 2))

    def test_no_link_is_nogo(self):
        # Nothing listening on this port: must fail toward NO-GO, never a pass.
        cfg = base_cfg(url="http://127.0.0.1:1", timeout=1.0)
        client = rededge.RedEdgeClient(cfg["url"], cfg["timeout"])
        res = rededge.evaluate(rededge.snapshot(client), cfg)
        self.assertEqual(res["overall"], "NO-GO")
        self.assertEqual(EXIT[res["overall"]], 2)


class Offload(unittest.TestCase):
    def test_pull_then_resume(self):
        with mock_server("go") as url:
            client = rededge.RedEdgeClient(url, 2.0)
            with tempfile.TemporaryDirectory() as d:
                with contextlib.redirect_stdout(io.StringIO()):
                    rededge.offload(client, d, only="tif")
                tifs = []
                for root, _, files in os.walk(d):
                    tifs += [f for f in files if f.endswith(".tif")]
                self.assertEqual(len(tifs), 5)
                # second run should skip everything already present
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rededge.offload(client, d, only="tif")
                self.assertIn("0 files", buf.getvalue())


class ConfigPrecedence(unittest.TestCase):
    def _args(self, **over):
        b = dict(config=None, url=None, timeout=None, min_sd=None, min_sats=None,
                 max_pacc=None, min_volts=None, cameras=None, firmware=None,
                 require_dls=None)
        b.update(over)
        return types.SimpleNamespace(**b)

    def test_defaults_then_flag(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)  # no rededge.json present -> defaults
            try:
                self.assertEqual(rededge.resolve_settings(self._args())["sats"], 6)
                self.assertEqual(rededge.resolve_settings(self._args(min_sats=12))["sats"], 12)
                self.assertFalse(rededge.resolve_settings(self._args())["dls"])
                self.assertTrue(rededge.resolve_settings(self._args(require_dls=True))["dls"])
            finally:
                os.chdir(cwd)

    def test_file_then_flag_over_file(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                with open("rededge.json", "w") as f:
                    json.dump({"sats": 10, "cams": 3, "dls": True}, f)
                self.assertEqual(rededge.resolve_settings(self._args())["sats"], 10)
                self.assertEqual(rededge.resolve_settings(self._args())["cams"], 3)
                self.assertTrue(rededge.resolve_settings(self._args())["dls"])
                # flag beats file
                self.assertEqual(rededge.resolve_settings(self._args(cameras=0))["cams"], 0)
            finally:
                os.chdir(cwd)


class Verify(unittest.TestCase):
    def test_counts_against_mock(self):
        with mock_server("go") as url:
            client = rededge.RedEdgeClient(url, 2.0)
            info = rededge.count_captures(client)
            self.assertEqual(info["captures"], 1)
            self.assertEqual(info["sets"], 1)
            self.assertGreater(info["bytes"], 0)

    def test_empty_card_zero_captures(self):
        class EmptyClient:
            def list_files(self, path="/"):
                return {"files": [], "directories": []}
        info = rededge.count_captures(EmptyClient())
        self.assertEqual(info["captures"], 0)
        self.assertEqual(info["sets"], 0)


class Robustness(unittest.TestCase):
    """Malformed, partial, and wrong-type camera responses must never crash and
    must never produce a false GO. Anything unconfirmable fails toward caution."""

    def _overall(self, snap):
        return rededge.evaluate(snap, base_cfg())["overall"]

    def test_malformed_does_not_crash_and_never_false_go(self):
        good = {"sd_status": "Ok", "sd_gb_free": 20.0, "bus_volts": 4.7,
                "gps_used_sats": 9, "p_acc": 2.0, "dls_status": "Ok",
                "utc_time_valid": True}
        bad_snapshots = [
            {"ok": True, "status": "not-a-dict", "version": {}, "network": None},
            {"ok": True, "status": ["list"], "version": None, "network": None},
            {"ok": True, "status": None, "version": None, "network": None},
            {"ok": True, "status": {}, "version": {}, "network": None},
            {"ok": True, "status": {**good, "sd_status": "Garbled"}},
            {"ok": True, "status": {**good, "dls_status": "Weird"}},
            {"ok": True, "status": {**good, "sd_gb_free": "twenty"}},
            {"ok": True, "status": {**good, "bus_volts": None}},
            {"ok": True, "status": {**good, "gps_used_sats": None}},
            {"ok": True, "status": good, "version": "not-a-dict", "network": "nope"},
            {"ok": True, "status": good, "network": {"network_map": "not-a-list"}},
        ]
        for snap in bad_snapshots:
            with self.subTest(snap=snap):
                res = rededge.evaluate(snap, base_cfg())   # must not raise
                self.assertIn(res["overall"], ("GO", "CHECK", "NO-GO"))

    def test_unrecognized_status_is_not_go(self):
        good = {"sd_status": "Ok", "sd_gb_free": 20.0, "bus_volts": 4.7,
                "gps_used_sats": 9, "p_acc": 2.0, "dls_status": "Ok",
                "utc_time_valid": True}
        # An unrecognized SD or DLS state must not read GO.
        self.assertNotEqual(
            self._overall({"ok": True, "status": {**good, "sd_status": "Garbled"}}), "GO")
        self.assertNotEqual(
            self._overall({"ok": True, "status": {**good, "dls_status": "Weird"}}), "GO")
        # Empty/missing status cannot be a clear pass.
        self.assertNotEqual(self._overall({"ok": True, "status": {}}), "GO")

    def test_missing_version_degrades_to_check_not_nogo(self):
        # A reachable camera with a good /status but absent /version should still
        # evaluate (firmware unconfirmed -> CHECK), not collapse to no-link NO-GO.
        good = {"sd_status": "Ok", "sd_gb_free": 20.0, "bus_volts": 4.7,
                "gps_used_sats": 9, "p_acc": 2.0, "dls_status": "Ok",
                "utc_time_valid": True}
        res = rededge.evaluate({"ok": True, "status": good, "version": None,
                                "network": None}, base_cfg())
        self.assertEqual(res["overall"], "CHECK")

    def test_snapshot_requires_valid_status(self):
        # status read succeeds but returns junk -> treated as no link (NO-GO).
        class JunkStatusClient:
            def status(self): return "totally not json"
            def version(self): return {"sw_version": "v7.1.0"}
            def networkstatus(self): return {"network_map": []}
        snap = rededge.snapshot(JunkStatusClient())
        self.assertFalse(snap.get("ok"))
        self.assertEqual(rededge.evaluate(snap, base_cfg())["overall"], "NO-GO")

    def test_snapshot_tolerates_secondary_endpoint_failure(self):
        # /status fine, /version and /networkstatus raise -> still ok, version/net None.
        good = {"sd_status": "Ok", "sd_gb_free": 20.0, "bus_volts": 4.7,
                "gps_used_sats": 9, "p_acc": 2.0, "dls_status": "Ok",
                "utc_time_valid": True}

        class FlakyClient:
            def status(self): return good
            def version(self): raise rededge.RedEdgeError("boom")
            def networkstatus(self): raise rededge.RedEdgeError("boom")
        snap = rededge.snapshot(FlakyClient())
        self.assertTrue(snap.get("ok"))
        self.assertIsNone(snap.get("version"))
        self.assertEqual(rededge.evaluate(snap, base_cfg())["overall"], "CHECK")


class LocalServeProxy(unittest.TestCase):
    """The local serve/proxy bridge had no coverage at all, and that is exactly
    how it came to ship broken: the definition of make_handler was lost, its
    body became unreachable code inside another function, and `serve` raised
    NameError on every invocation. Everything still compiled and every test
    still passed, because nothing here exercised it.

    These tests hold two things: the bridge works at all, and it stays
    read-only. The proxy is the only way the browser tool reads a real camera,
    so a silent break costs a pilot the web client in the field."""

    @contextlib.contextmanager
    def _bridge(self, camera_url, page_path):
        client = rededge.RedEdgeClient(camera_url, 2.0)
        handler = rededge.make_handler(page_path, client)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            yield "http://127.0.0.1:%d" % httpd.server_address[1]
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_make_handler_exists(self):
        # Guards the exact regression above: a missing definition here means
        # `serve` cannot run, and no other test would notice.
        self.assertTrue(callable(getattr(rededge, "make_handler", None)))

    def test_serves_page_and_forwards_allowed_route(self):
        with tempfile.TemporaryDirectory() as d:
            page = os.path.join(d, "page.html")
            with open(page, "w") as f:
                f.write("<!doctype html><title>readiness</title>")
            with mock_server("go") as cam:
                with self._bridge(cam, page) as base:
                    body = urllib.request.urlopen(base + "/", timeout=5).read()
                    self.assertIn(b"readiness", body)
                    raw = urllib.request.urlopen(base + "/cam/status", timeout=5).read()
                    self.assertEqual(json.loads(raw)["sd_status"], "Ok")

    def test_proxy_is_read_only(self):
        # Actions must never be reachable through the browser bridge. If this
        # ever passes a route, a web page could trigger a capture or worse.
        with mock_server("go") as cam:
            with self._bridge(cam, None) as base:
                for route in ("capture", "capture/mock123", "captures.kmz",
                              "reformat", "../status"):
                    with self.subTest(route=route):
                        try:
                            urllib.request.urlopen(base + "/cam/" + route, timeout=5)
                            self.fail("route %s was allowed" % route)
                        except urllib.error.HTTPError as e:
                            self.assertIn(e.code, (403, 404))

    def test_unreachable_camera_reports_upstream_failure(self):
        # A dead camera behind the proxy must surface as an error, never as an
        # empty success the page could misread.
        with self._bridge("http://127.0.0.1:1", None) as base:
            try:
                urllib.request.urlopen(base + "/cam/status", timeout=5)
                self.fail("expected an upstream error")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 502)


if __name__ == "__main__":
    unittest.main(verbosity=2)
