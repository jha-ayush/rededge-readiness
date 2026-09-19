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
import re
import struct
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

    def test_names_that_escape_the_destination_are_refused(self):
        """The camera is an unauthenticated device on an open WiFi, so a
        listing is untrusted input. A spoofed device answering with a name
        like "../../escape.tif" must not be able to write outside the offload
        folder, and must not stop the walk from pulling the real files."""
        pulled = []

        class HostileClient:
            def list_files(self, path="/"):
                if path.strip("/") == "":
                    return {"files": [{"name": "../../escape.tif", "size": 4},
                                      {"name": "sub/../../escape2.tif", "size": 4},
                                      {"name": "..", "size": 4},
                                      {"name": "IMG_0000_1.tif", "size": 4}],
                            "directories": ["..", "../up", "0000SET"]}
                return {"files": [{"name": "IMG_0001_1.tif", "size": 4}],
                        "directories": []}

            def download(self, remote, dest):
                pulled.append(remote)
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(b"II*\x00")
                return 4

        with tempfile.TemporaryDirectory() as outer:
            dest = os.path.join(outer, "flight")
            with contextlib.redirect_stdout(io.StringIO()):
                rededge.offload(HostileClient(), dest, only="tif")
            self.assertEqual(sorted(pulled),
                             ["0000SET/IMG_0001_1.tif", "IMG_0000_1.tif"])
            # Nothing landed beside or above the destination.
            self.assertEqual(sorted(os.listdir(outer)), ["flight"])
            self.assertFalse(os.path.exists(os.path.join(outer, "escape.tif")))

    def test_dead_link_is_an_exit_code_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = rededge.main(["--url", "http://127.0.0.1:1", "--timeout",
                                     "1", "offload", d])
            self.assertEqual(code, 2)
            self.assertIn("Could not read the card", buf.getvalue())


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

    def test_junk_listing_is_an_error_not_an_empty_card(self):
        """A card that answers with something other than a listing must not
        count as an empty card: "no captures" is a verdict a crew acts on, and
        it has to come from a listing, not from a parse failure."""
        for junk in ("text", ["list"], None, 7):
            with self.subTest(junk=junk):
                class JunkClient:
                    def list_files(self, path="/", _j=junk):
                        return _j
                with self.assertRaises(rededge.RedEdgeError):
                    rededge.count_captures(JunkClient())

    def test_malformed_entries_are_skipped_not_fatal(self):
        class OddClient:
            def list_files(self, path="/"):
                if path.strip("/") == "":
                    return {"files": [None, "x", {"name": 5}, {"name": "IMG_0000_1.tif",
                                                               "size": "big"},
                                      {"name": "IMG_0000_2.tif", "size": 10}],
                            "directories": [None, 3, "0000SET"]}
                return {"files": "nope", "directories": None}
        info = rededge.count_captures(OddClient())
        self.assertEqual(info["captures"], 1)
        self.assertEqual(info["sets"], 1)
        self.assertEqual(info["bytes"], 10)

    def test_verify_survives_a_non_object_status(self):
        # The SD line is optional; a status that is not an object must not
        # crash the verdict the crew is waiting on.
        class Client:
            def __init__(self, *a, **k): pass
            def list_files(self, path="/"):
                return {"files": [{"name": "IMG_0000_1.tif", "size": 4}],
                        "directories": []}
            def status(self): return ["not", "a", "dict"]
        original = rededge.RedEdgeClient
        rededge.RedEdgeClient = Client
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = rededge.main(["--no-color", "verify"])
        finally:
            rededge.RedEdgeClient = original
        self.assertEqual(code, 0)
        self.assertIn("CAPTURES FOUND", buf.getvalue())


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

    def test_wrong_typed_readings_never_crash_and_never_pass(self):
        """A reading that arrives with the wrong type is not a reading. Before
        this held, a string satellite count raised TypeError mid-readout, and a
        string voltage or position error slipped past its comparison and read
        GO with "--" in the value column: a pass shown beside a blank."""
        good = {"sd_status": "Ok", "sd_gb_free": 20.0, "bus_volts": 4.7,
                "gps_used_sats": 9, "p_acc": 2.0, "dls_status": "Ok",
                "time_source": "GPS", "utc_time_valid": True}
        ver = {"sw_version": "v7.1.0"}
        net = {"network_map": [{"device_type": "Camera", "sw_version": "v7.1.0",
                                "sd_status": "Ok"}]}
        nan = float("nan")
        probes = {
            "string sats": {"status": {**good, "gps_used_sats": "9"}},
            "bool sats": {"status": {**good, "gps_used_sats": True}},
            "nan sats": {"status": {**good, "gps_used_sats": nan}},
            "string volts": {"status": {**good, "bus_volts": "4.7"}},
            "string pacc": {"status": {**good, "p_acc": "2.0"}},
            "string free": {"status": {**good, "sd_gb_free": "20"}},
            "missing free": {"status": {k: v for k, v in good.items()
                                        if k != "sd_gb_free"}},
            "numeric sd status": {"status": {**good, "sd_status": 1}},
            "numeric dls status": {"status": {**good, "dls_status": 0}},
            "object time source": {"status": {**good, "time_source": {},
                                              "utc_time_valid": None}},
            "empty version": {"status": good, "version": {"sw_version": ""}},
            "numeric version": {"status": good, "version": {"sw_version": 7}},
            "list version": {"status": good, "version": {"sw_version": ["v7"]}},
        }
        for name, patch in probes.items():
            with self.subTest(probe=name):
                snap = {"ok": True, "version": ver, "network": net, **patch}
                res = rededge.evaluate(snap, base_cfg())   # must not raise
                self.assertNotEqual(res["overall"], "GO")

    def test_junk_in_the_device_list_never_crashes(self):
        """Entries in network_map that are not objects are ignored, and a
        firmware field that is not a string is not compared. The rig row must
        still answer, and a list of two real cameras on different firmware must
        still be caught with junk sitting between them."""
        good = {"sd_status": "Ok", "sd_gb_free": 20.0, "bus_volts": 4.7,
                "gps_used_sats": 9, "p_acc": 2.0, "dls_status": "Ok",
                "time_source": "GPS", "utc_time_valid": True}
        junk = [None, "text", 3, [], {"device_type": "Camera", "sw_version": ["x"]}]
        snap = {"ok": True, "status": good, "version": {"sw_version": "v7.1.0"},
                "network": {"network_map": junk + [
                    {"device_type": "Camera", "sw_version": "v7.1.0"},
                    {"device_type": "Camera", "sw_version": "v7.0.0"}]}}
        res = rededge.evaluate(snap, base_cfg())        # must not raise
        rig = dict((c[0], c) for c in res["checks"])["Camera rig"]
        self.assertEqual(rig[2], "CHECK")
        self.assertIn("mixed firmware", rig[3])


class CheckContract(unittest.TestCase):
    """All three clients must report the same named checks, not merely the same
    verdict. Python previously folded position accuracy and time validity into
    the GPS row, so a camera that omitted p_acc and the time fields read GO here
    while the phone and the web page read CHECK on the identical payload. Equal
    verdicts on the happy path hid a real divergence on the unhappy one."""

    EXPECTED_LABELS = [
        "SD storage", "GPS fix", "Position accuracy", "Light sensor",
        "Supply voltage", "Time source", "Camera rig", "Firmware",
    ]

    def _healthy(self, **over):
        st = {"sd_status": "Ok", "sd_gb_free": 20.1, "bus_volts": 4.69,
              "gps_used_sats": 9, "p_acc": 2.4, "dls_status": "Ok",
              "time_source": "GPS", "utc_time_valid": True}
        st.update(over)
        return {"ok": True, "status": st, "version": {"sw_version": "v7.1.0"},
                "network": {"network_map": [{"device_type": "Camera",
                                             "sw_version": "v7.1.0",
                                             "sd_status": "Ok"}]}}

    def test_reports_every_contracted_check_in_order(self):
        res = rededge.evaluate(self._healthy(), base_cfg())
        self.assertEqual([c[0] for c in res["checks"]], self.EXPECTED_LABELS)

    def test_missing_position_accuracy_is_not_a_pass(self):
        snap = self._healthy()
        del snap["status"]["p_acc"]
        self.assertNotEqual(rededge.evaluate(snap, base_cfg())["overall"], "GO")

    def test_missing_time_fields_are_not_a_pass(self):
        snap = self._healthy()
        del snap["status"]["time_source"]
        del snap["status"]["utc_time_valid"]
        self.assertNotEqual(rededge.evaluate(snap, base_cfg())["overall"], "GO")

    def _states(self, snap):
        return {c[0]: c[2] for c in rededge.evaluate(snap, base_cfg())["checks"]}

    def test_one_cause_flags_one_row(self):
        """Position accuracy and clock validity each own a row. The GPS row
        used to fold both in as well, so a wide error ellipse lit two rows and
        the reason line named two problems for one cause. Verdict unchanged,
        because the dedicated row still flags; the readout just stops
        double-counting."""
        wide = self._states(self._healthy(p_acc=12.0))
        self.assertEqual(wide["Position accuracy"], "CHECK")
        self.assertEqual(wide["GPS fix"], "GO")
        clock = self._states(self._healthy(utc_time_valid=False))
        self.assertEqual(clock["Time source"], "CHECK")
        self.assertEqual(clock["GPS fix"], "GO")
        # The row that owns the cause still drives the verdict.
        for snap in (self._healthy(p_acc=12.0), self._healthy(utc_time_valid=False)):
            self.assertEqual(rededge.evaluate(snap, base_cfg())["overall"], "CHECK")

    def test_render_columns_line_up(self):
        # Every state word is padded to the same width, so the label column
        # starts at one offset on every row. NO-GO and UNKNOWN used to overrun.
        snap = self._healthy(sd_status="NotPresent", dls_status="Weird",
                             bus_volts=3.0)
        text = rededge.render(rededge.evaluate(snap, base_cfg()), use_color=False)
        rows = text.splitlines()[1:]
        offsets = {row.index(label) for row, label in
                   zip(rows, self.EXPECTED_LABELS)}
        self.assertEqual(len(offsets), 1, text)


class ConfigSanitizer(unittest.TestCase):
    """The web client sanitizes its thresholds because a NaN threshold once
    disabled a check from a link. The Python client had no equivalent: a
    string in rededge.json raised TypeError mid-readout, and a negative floor
    could never fire. The three clients now refuse the same values."""

    def _clean(self, **over):
        warnings = []
        cfg = base_cfg(**over)
        out = rededge.sanitize_settings(cfg, warn=warnings.append)
        return out, warnings

    def test_unusable_numbers_fall_back_and_are_reported(self):
        for key in ("sd", "sats", "pacc", "volts", "cams", "timeout"):
            for bad in ("abc", "", None, -1, float("nan"), float("inf"), [], True):
                with self.subTest(key=key, bad=bad):
                    out, warnings = self._clean(**{key: bad})
                    self.assertEqual(out[key], rededge.CONFIG_DEFAULTS[key])
                    self.assertTrue(warnings and key in warnings[0])

    def test_numeric_strings_are_accepted_and_integers_floored(self):
        out, warnings = self._clean(sd="3.5", sats="7", cams="2", volts=" 4.4 ")
        self.assertEqual((out["sd"], out["sats"], out["cams"], out["volts"]),
                         (3.5, 7, 2, 4.4))
        self.assertEqual(warnings, [])
        out, _ = self._clean(sats=6.7, cams=1.9)
        self.assertEqual((out["sats"], out["cams"]), (6, 1))
        self.assertIsInstance(out["sats"], int)

    def test_zero_timeout_is_not_a_timeout(self):
        out, warnings = self._clean(timeout=0)
        self.assertEqual(out["timeout"], rededge.CONFIG_DEFAULTS["timeout"])
        self.assertTrue(warnings)
        out, warnings = self._clean(sd=0, cams=0)      # zero is a valid floor
        self.assertEqual((out["sd"], out["cams"]), (0, 0))
        self.assertEqual(warnings, [])

    def test_text_and_flag_fields_are_normalized(self):
        out, _ = self._clean(fw=None, dls="yes", url="   ")
        self.assertEqual(out["fw"], "")
        self.assertIs(out["dls"], True)
        self.assertEqual(out["url"], rededge.DEFAULT_URL)
        out, _ = self._clean(fw="  v7.1.0 ", dls="no", url="http://10.0.0.5/")
        self.assertEqual(out["fw"], "v7.1.0")
        self.assertIs(out["dls"], False)
        self.assertEqual(out["url"], "http://10.0.0.5/")

    def test_config_file_that_is_not_an_object_is_ignored(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                with open("rededge.json", "w") as f:
                    json.dump(["not", "an", "object"], f)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(rededge.load_config(None), {})
                self.assertIn("not a JSON object", err.getvalue())
            finally:
                os.chdir(cwd)

    def test_resolve_settings_runs_the_sanitizer(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                with open("rededge.json", "w") as f:
                    json.dump({"sats": "lots", "volts": -3}, f)
                args = types.SimpleNamespace(
                    config=None, url=None, timeout=None, min_sd=None,
                    min_sats=None, max_pacc=None, min_volts=None, cameras=None,
                    firmware=None, require_dls=None)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    cfg = rededge.resolve_settings(args)
                self.assertEqual(cfg["sats"], rededge.CONFIG_DEFAULTS["sats"])
                self.assertEqual(cfg["volts"], rededge.CONFIG_DEFAULTS["volts"])
                self.assertIn("sats", err.getvalue())
                self.assertIn("volts", err.getvalue())
            finally:
                os.chdir(cwd)


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

    def test_proxy_refuses_dot_segments_and_stray_subpaths(self):
        """The allowlist used to check only the first segment, so
        "status/../capture" passed as "status" and was forwarded verbatim. What
        that meant on the wire was the camera's business: an embedded server
        that normalizes dot segments would have received /capture, an action,
        through a proxy whose whole claim is that it cannot trigger one. The
        route is now vetted segment by segment after percent-decoding, and only
        files/ may carry a sub-path at all."""
        refused = ("status/../capture", "status%2F..%2Fcapture",
                   "%2e%2e/capture", "files/../capture", "files/..%2Fcapture",
                   "files/%2e%2e/capture", "status/extra", "version/",
                   "files//0000SET", "files/a%00b", "files/0000SET/../../capture",
                   "files/0000SET/x%20y", "files/0000SET/x;y", "")
        with mock_server("go") as cam:
            with self._bridge(cam, None) as base:
                for route in refused:
                    with self.subTest(route=route):
                        try:
                            urllib.request.urlopen(base + "/cam/" + route, timeout=5)
                            self.fail("route %r was forwarded" % route)
                        except urllib.error.HTTPError as e:
                            self.assertEqual(e.code, 403)
                # The legitimate shapes still work, root listing included.
                for route in ("files/", "files", "files/0000SET", "files/0000SET/000"):
                    with self.subTest(route=route):
                        raw = urllib.request.urlopen(base + "/cam/" + route,
                                                     timeout=5).read()
                        self.assertIn("directories", json.loads(raw))

    def test_proxy_route_is_pure_and_pinned(self):
        # The vetting function itself, so a regression is named precisely.
        self.assertEqual(rededge.proxy_route("/cam/status"), "status")
        self.assertEqual(rededge.proxy_route("/cam/files/"), "files/")
        self.assertEqual(rededge.proxy_route("/cam/files/0000SET/000"),
                         "files/0000SET/000")
        for bad in ("/cam/status/../capture", "/cam/status%2F..%2Fcapture",
                    "/cam/capture", "/cam/", "/cam/files/../x", "/status"):
            self.assertIsNone(rededge.proxy_route(bad), bad)

    def _hosted_headers(self):
        """The header block Cloudflare applies to the hosted page, parsed from
        web/_headers into a name -> value map."""
        here = os.path.dirname(os.path.abspath(__file__))
        out = {}
        with open(os.path.join(here, "web", "_headers")) as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("  ") and ":" in line:
                    name, value = line.strip().split(":", 1)
                    out[name.strip()] = value.strip()
        return out

    def test_hosted_page_forbids_inline_script_and_allows_its_own(self):
        """The hosted demo shipped for a while with app.js blocked by its own
        Content Security Policy: the script had moved out of the page and the
        notes said the policy now read script-src 'self', but web/_headers was
        never changed and still said 'unsafe-inline' without 'self'. Every
        load of the demo rendered a dead page. This pins the directive the
        page cannot run without."""
        csp = self._hosted_headers().get("Content-Security-Policy", "")
        directives = {d.strip().split(" ")[0]: d.strip().split(" ")[1:]
                      for d in csp.split(";") if d.strip()}
        self.assertIn("'self'", directives.get("script-src", []))
        self.assertNotIn("'unsafe-inline'", directives.get("script-src", []))
        self.assertEqual(directives.get("default-src"), ["'none'"])
        # No external origins anywhere: the page makes zero external requests.
        for name, sources in directives.items():
            for src in sources:
                self.assertFalse(src.startswith("http"),
                                 "%s grants an external origin: %s" % (name, src))

    def test_local_page_carries_the_hosted_headers(self):
        """The local server sends the same page headers as the hosted page,
        so a pilot running the tool on a laptop gets the same contract as the
        demo. The two are held to each other here: a change to one without
        the other is a drift, and drift is exactly how the hosted policy came
        to block its own script."""
        hosted = self._hosted_headers()
        for name, value in rededge.PAGE_HEADERS.items():
            self.assertEqual(hosted.get(name), value, name)
        with mock_server("go") as cam:
            with self._bridge(cam, rededge.DEFAULT_PAGE) as base:
                page = urllib.request.urlopen(base + "/", timeout=5)
                for name, value in rededge.PAGE_HEADERS.items():
                    self.assertEqual(page.headers.get(name), value, name)
                self.assertIsNone(page.headers.get("Access-Control-Allow-Origin"))
                js = urllib.request.urlopen(base + "/app.js", timeout=5)
                self.assertIsNone(js.headers.get("Access-Control-Allow-Origin"))
                self.assertEqual(js.headers.get("X-Content-Type-Options"), "nosniff")
                cam_r = urllib.request.urlopen(base + "/cam/status", timeout=5)
                self.assertEqual(cam_r.headers.get("Access-Control-Allow-Origin"), "*")

    def test_default_page_is_the_shipped_web_client(self):
        # "python3 rededge.py serve" with no --page used to look for the page
        # in the working directory under a name that exists nowhere in the
        # tree, and answered every request with a 404.
        self.assertTrue(os.path.exists(rededge.DEFAULT_PAGE), rededge.DEFAULT_PAGE)
        self.assertEqual(os.path.basename(rededge.DEFAULT_PAGE), "rededge-readiness.html")
        with mock_server("go") as cam:
            with self._bridge(cam, rededge.DEFAULT_PAGE) as base:
                body = urllib.request.urlopen(base + "/", timeout=5).read()
                self.assertIn(b'src="app.js"', body)
                js = urllib.request.urlopen(base + "/app.js", timeout=5).read()
                self.assertIn(b"function evaluate", js)

    SITE = "https://rededge-readiness.sudokodes.workers.dev/"

    def _page_assets(self):
        """Every sibling file the shipped page asks a browser for, read out
        of the page's own markup: link hrefs (icons), script srcs, and the
        image the og and twitter tags name by absolute URL. The canonical
        link points at the site root, which is the page itself, and drops
        out as an empty name. Anything left with a scheme is an off-site
        request, which the page must never make; the caller asserts that."""
        with open(rededge.DEFAULT_PAGE, encoding="utf-8") as f:
            page = f.read()
        found = re.findall(r'<link\b[^>]*\bhref="([^"]+)"', page)
        found += re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', page)
        found += re.findall(r'<meta\b[^>]*\b(?:property|name)="(?:og|twitter):image"'
                            r'[^>]*\bcontent="([^"]+)"', page)
        names = set()
        for value in found:
            if value.startswith(self.SITE):
                value = value[len(self.SITE):]
            if value:
                names.add(value)
        return names

    def _png_size(self, data):
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        return struct.unpack(">II", data[16:24])

    # The type an asset must be served as, by what the file is. Held apart
    # from STATIC_ALLOW on purpose: checking the served type against the same
    # dict that produced it would pass a wrong entry.
    ASSET_TYPES = {".js": "application/javascript; charset=utf-8",
                   ".svg": "image/svg+xml",
                   ".png": "image/png"}

    def test_every_asset_the_page_names_is_allowlisted_and_served(self):
        """The serve allowlist and the page drifted once already: the page
        gained a Home Screen icon and the local server did not know the name,
        so the hosted demo showed the icon and a laptop serving the same page
        answered 404 for it. This reads the assets out of the markup rather
        than repeating the list here, so a new href fails the run until the
        allowlist and the file beside the page both exist. The names are also
        held to be relative: the page makes zero off-site requests, and the
        Content Security Policy test says the same from the header side."""
        names = self._page_assets()
        self.assertIn("app.js", names)
        self.assertIn("favicon.svg", names)
        self.assertIn("apple-touch-icon.png", names)
        self.assertIn("rededge-social.png", names)
        for name in names:
            with self.subTest(asset=name):
                self.assertNotIn("://", name, "off-site asset: %s" % name)
                self.assertNotIn("/", name, "asset outside the page dir: %s" % name)
                self.assertIn(name, rededge.STATIC_ALLOW)
                self.assertTrue(os.path.exists(
                    os.path.join(os.path.dirname(rededge.DEFAULT_PAGE), name)), name)
        with mock_server("go") as cam:
            with self._bridge(cam, rededge.DEFAULT_PAGE) as base:
                for name in names:
                    with self.subTest(asset=name):
                        ext = os.path.splitext(name)[1]
                        self.assertIn(ext, self.ASSET_TYPES, "unknown asset kind")
                        r = urllib.request.urlopen(base + "/" + name, timeout=5)
                        self.assertEqual(r.status, 200)
                        self.assertEqual(r.headers.get("Content-Type"),
                                         self.ASSET_TYPES[ext])
                        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
                        self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))
                        body = r.read()
                        self.assertTrue(body, "%s served empty" % name)
                        if ext == ".png":
                            self._png_size(body)
                        elif ext == ".svg":
                            self.assertIn(b"<svg", body)
                        else:
                            self.assertIn(b"function evaluate", body)
                # The allowlist is an allowlist: the hosted config files sit
                # beside the page and are not page assets, so they do not come
                # out of the server by name.
                for name in ("_headers", "_redirects", "..%2F..%2Frededge.py"):
                    with self.subTest(refused=name):
                        try:
                            urllib.request.urlopen(base + "/" + name, timeout=5)
                            self.fail("%s was served" % name)
                        except urllib.error.HTTPError as e:
                            self.assertEqual(e.code, 404)

    def test_icons_are_the_size_their_tags_claim(self):
        """A Home Screen icon that is not 180 by 180 is scaled by iOS and
        looks soft; a social card whose real size differs from the og tags
        is cropped by the previewer. Both numbers live in the markup, so both
        are read from there and held to the file bytes."""
        with open(rededge.DEFAULT_PAGE, encoding="utf-8") as f:
            page = f.read()
        here = os.path.dirname(rededge.DEFAULT_PAGE)
        m = re.search(r'<link\b[^>]*rel="apple-touch-icon"[^>]*\bsizes="(\d+)x(\d+)"'
                      r'[^>]*\bhref="([^"]+)"', page)
        self.assertIsNotNone(m, "apple-touch-icon link with sizes and href")
        with open(os.path.join(here, m.group(3)), "rb") as f:
            self.assertEqual(self._png_size(f.read()), (int(m.group(1)), int(m.group(2))))
        self.assertEqual((int(m.group(1)), int(m.group(2))), (180, 180))
        w = re.search(r'property="og:image:width" content="(\d+)"', page)
        h = re.search(r'property="og:image:height" content="(\d+)"', page)
        img = re.search(r'property="og:image" content="([^"]+)"', page)
        self.assertTrue(w and h and img, "og:image tags")
        self.assertTrue(img.group(1).startswith(self.SITE))
        with open(os.path.join(here, img.group(1)[len(self.SITE):]), "rb") as f:
            self.assertEqual(self._png_size(f.read()), (int(w.group(1)), int(h.group(1))))

    def test_head_answers_like_get_without_a_body(self):
        with mock_server("go") as cam:
            with self._bridge(cam, rededge.DEFAULT_PAGE) as base:
                req = urllib.request.Request(base + "/cam/status", method="HEAD")
                r = urllib.request.urlopen(req, timeout=5)
                self.assertEqual(r.status, 200)
                self.assertEqual(r.headers.get("Content-Type"), "application/json")
                self.assertEqual(r.read(), b"")


if __name__ == "__main__":
    unittest.main(verbosity=2)
