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


if __name__ == "__main__":
    unittest.main(verbosity=2)
