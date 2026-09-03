# Changelog

Notable changes to RedEdge Readiness. Entries record what changed and, where it
matters, why, because the reason is usually the part worth keeping.

## Audit pass, September 2026

A file-by-file audit anchored on one question: a recreational or Part 107 pilot
is standing at a launch site under time pressure, deciding whether to fly. Every
finding below was judged against a single rule, that a false all-clear must be
structurally impossible rather than merely unlikely.

Four of the findings were live defects that the existing tests could not see.
None were style issues.

### Fixed: `serve` was broken and could not run

The definition of `make_handler` had been lost in an earlier iteration. Its body
survived as unreachable code inside `count_captures`, after that function's
return statement, so everything still compiled and every test still passed while
`python3 rededge.py serve` raised `NameError` on every invocation.

The local proxy is the only way the browser tool reads a real camera, and it is
documented in the module docstring, the README and the architecture notes, so
the project was promising a capability that crashed on use. Restored, and now
covered by tests that assert the handler exists, serves the page, forwards an
allowed route, and refuses everything else.

### Fixed: the Python client could return GO where the phone returned CHECK

The documented contract is eight named checks. The phone and the web page
implemented eight. Python implemented six, folding position accuracy and time
validity into the GPS row.

On the happy path all three agreed, which is why nothing failed. On a camera
that simply omits `p_acc` and the time fields, Python read **GO** while the
other two read **CHECK** on the identical payload. That is the command line
being optimistic about data it never received.

Python now reports the same eight checks in the same order. `test_rededge.py`
pins the label list and the omitted-field cases, so a client cannot silently
drop a named check again.

### Fixed: a URL parameter could disable every safety threshold

The web page parsed numeric parameters with a bare `parseFloat`. Garbage
produced `NaN`, and `NaN` compares false against everything, so each threshold
comparison silently evaporated. With `?sd=abc` a card holding 0.2 GB read GO.
With `?sats=xyz` zero satellites read GO. With `?volts=nope` a pack at 3.1 V
read GO. A clean green pass, produced by a link.

`?poll=0` or `?poll=abc` also produced `setInterval(fn, NaN)`, which behaves as
zero and polls as fast as the network allows.

Values are now validated, fall back to the built-in default, and the poll
interval is clamped. The link path and the Settings form share one sanitizer so
they cannot diverge again.

### Fixed: a link could repoint the tool at a foreign host

`?url=` was accepted verbatim, so a crafted link could aim the tool at any
server and present its response as camera readings.

The camera is a local device by definition, so a camera URL arriving in a query
string is now restricted to local addresses (RFC1918, loopback, link-local,
`.local`, or a same-origin path). Settings, typed by the pilot, stays
unrestricted. The trust boundary is the point: a link is written by whoever
sends it, a setting is entered by the person flying.

### Fixed: documentation that instructed users to run commands that fail

The operating guide told users to test with `--scenario healthy` and listed
`lowsd`, `nogps`, `dlserror`, `badfw`, `multicam`. None of those scenario names
exist, and the mock rejects them, so the one place a new user is told to verify
the system without hardware was a dead end. Corrected to the twelve real
scenarios with a working example.

### Added: the cross-client parity harness that was already being claimed

The architecture notes described a parity harness three times, including as a
mechanism enforcing "never a false pass". It did not exist.

`parity_check.js` now loads the web and iOS evaluators out of the shipped files
and asserts they agree on every canonical scenario, on every individual check,
and on the no-link and unknown-value cases. It runs in CI.

Verdict-only comparison would not have been enough, which the Python divergence
above proves: three clients can agree on every tested scenario and still differ
on real payloads.

### Added: staleness, and a readout that names its source

Two gaps where the tool stated something true in a way that implied something
false.

The camera address appeared only when the link failed, so a real GO and a GO
from the wrong address looked identical at the moment a pilot commits to
flying. Every live readout now names what it read.

A verdict is evidence about the instant it was read. Past `max(30s, 4 poll
intervals)` the web readout marks itself stale: the state word recedes, the
frame turns to caution, and the status line gives the age. The widget, whose
refresh cadence iOS controls, is now framed as a snapshot rather than carrying a
relative age that would freeze and become a lie.

### Fixed: smaller defects

- The iOS post-flight path crashed with a `TypeError` if the camera answered
  with a null or non-object body. Every other read path had been hardened
  against this; post-flight had been missed.
- The post-flight card showed `SET folders 0` and `Data on card 0.0 MB` in
  green beside a caution, hardcoded regardless of value. Green rows on a zero
  count are false reassurance at the moment a crew decides whether to pack up.
- `_lan_ip()` hardcoded the WiFi address when selecting an interface, so an
  Ethernet setup could be shown the wrong URL. It now follows the configured
  camera host.
- `captures.kmz` was in the proxy allowlist, but the forwarder decodes JSON, so
  that route could only ever fail. Removed, with the rule recorded.
- Removed dead code: an unused `cfg_from_args` wrapper and an unused parameter
  on `render`.
- Removed dead Cloudflare configuration: `nodejs_compat` applies to a Worker
  runtime, and this deployment has no Worker script.

### Changed: CI now covers all three clients

The pipeline ran Python only, so two of the three clients had no automated
checking and a syntax error in the iPhone script would have shipped undetected.
CI now compiles the Python, runs the suite, syntax-checks both JavaScript
clients, and runs the parity harness.

The test suite grew from 12 tests to 19.

### Changed: type chosen for reading in the field, and no webfont fetch

The display stack fell back to Arial Narrow, a condensed face. A pilot reads
this on camera WiFi with no internet, so the downloaded font never arrived and
the fallback is what the field actually got. That meant the giant
GO / CHECK / NO-GO word, the one element the tool exists to deliver, rendered at
its narrowest exactly where it is read fastest, at a glance, in bright sun.

The page now uses system faces only, which resolve to San Francisco, Segoe UI or
Roboto depending on platform, are built for screen legibility, and render
identically online and off. Figures a pilot acts on carry a slashed zero, so 0
cannot be misread as O, and tabular figures, so the reading column holds still
as values update on each poll instead of jittering.

Two consequences followed. The page now makes **zero external requests**, which
is what the zero-dependency, offline-first design had claimed all along. And
with no font fetch, the Google origins in the Content Security Policy became
dead grants, so `style-src` lost its external origin and `font-src` is now
`'none'`.

The tradeoff, stated plainly: the wordmark and the state word no longer use a
distinct display face, so the brand reads slightly plainer than the hosted demo
did. Legibility in the field won over identity on a desk. Self-hosting a display
font in `web/` would recover the identity without reintroducing an external
request, if that trade is ever worth revisiting.

### Still open, deliberately

- **`script-src 'unsafe-inline'`** remains in the Content Security Policy. The
  page is a single self-contained file with no build step, which is a property
  worth keeping; externalizing the script would tighten the policy and cost
  that.
- **No hardware has been read.** The mock encodes assumptions about what the
  camera reports. Until a physical RedEdge or Altum has been on the WiFi, those
  assumptions are unverified. The design accounts for this by failing toward
  caution on anything unmodeled, but that is mitigation, not proof.
