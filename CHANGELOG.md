<!-- CHANGELOG.md -->
# Changelog

Notable changes to RedEdge Readiness. Entries record what changed and, where it
matters, why, because the reason is usually the part worth keeping.

## Second audit pass, 19 September 2026

A second file-by-file pass, bottom up, under the same rule as the first: a
false all-clear must be structurally impossible, not merely unlikely. Every
fix below carries a test or a harness case that was mutated before it was
trusted, meaning the fix was reverted and the check confirmed to fail.

### Fixed: the hosted demo was dead, blocked by its own security policy

The hosted page at `rededge-readiness.sudokodes.workers.dev` rendered a
header, a source menu, the word "..." and nothing else. Loaded in a browser,
the console said why: `web/_headers` still sent
`script-src 'unsafe-inline'`, left from when the client script was inline, and
that directive does not include `'self'`, so the browser refused to load
`web/app.js`. The page could not run its own client.

The previous audit's changelog and the architecture notes both said the policy
had been tightened to `script-src 'self'` when the script moved into its own
file. The file was never changed. That is the finding worth recording plainly:
the note described the intended state, the review read the note, and nobody
loaded the page. A claim about a shipped file is not evidence about the file.

`web/_headers` now reads `script-src 'self'`, the dead Google Fonts grants are
gone (the page fetches no font), and two things hold it there. The Python
suite parses `web/_headers` and asserts that `script-src` contains `'self'`,
does not contain `'unsafe-inline'`, and grants no external origin. And the
local `rededge.py serve` now sends the same page headers from a constant in
the code, which the suite compares to `web/_headers` line by line, so the two
cannot drift apart again without the suite saying so.

### Fixed: the read-only proxy could be walked toward an action route

The proxy allowlist checked the first path segment and forwarded the rest
verbatim, so `/cam/status/../capture` passed as `status` and reached the
camera as `status/../capture`. Whether that triggered a capture depended on
how the camera's embedded server treats dot segments, which is not a question
a proxy whose whole claim is "cannot trigger a capture" should leave to the
other end.

Routes are now percent-decoded and vetted segment by segment: no empty or dot
segments, nothing outside `[A-Za-z0-9._-]`, an allowlisted head, and a
sub-path only under `files/`. The request path is split with `urlsplit` rather
than `urlparse`, because `urlparse` peels a `;params` tail off the last
segment before it can be judged. Tests cover the encoded and unencoded forms,
and the legitimate shapes (`files/`, `files/0000SET/000`) still forward.

### Fixed: a spoofed camera could write outside the offload folder

`offload` joined file and folder names from the card's listing straight onto
the destination path. The README already says to treat the camera network as
untrusted, and a device on that network answering with a name like
`../../escape.tif` would have been written two directories up. Names are now
required to be single path segments, and the resolved path is required to sit
under the destination as a second check. A test feeds a hostile listing and
asserts nothing lands beside or above the folder while the real files still
arrive. The iPhone post-flight walk applies the same vetting to folder names.

### Fixed: a reading with the wrong type could read GO beside a blank

A string where a number was expected slipped past every comparison in the two
JavaScript clients: `"abc" < 4.2` coerces to `NaN`, which compares false, so a
voltage of `"abc"` fell through to GO with `--` in the value column. Python
raised `TypeError` on the same input, mid-readout. A non-empty firmware string
was required for the Firmware row, but an empty one read GO.

All three clients now share one rule, written once per language (`_num` and
`_text` in Python, `num` and `text` in the two scripts): a number is a finite
number, a status is a non-empty string, and anything else is not a reading and
reads UNKNOWN. Entries in the device list that are not objects are ignored
rather than dereferenced. A card whose status is `Ok` but whose free space is
missing reads UNKNOWN rather than passing on the status alone. The parity
harness runs thirteen wrong-type probes through all three clients and requires
every one to agree and none to pass.

### Changed: the parity harness now covers all three clients

`parity_check.js` compared the web and iOS evaluators to each other; Python
was held only by its own tests, and only to the verdict per scenario. The
first audit recorded that verdict-only comparison is not enough, and then left
Python compared that way.

The harness now runs the Python evaluator in a child process, on snapshots
built from the mock camera's own payloads, and compares all three clients
check by check on the twelve scenarios, the no-link case, and every unknown,
wrong-type and agreement probe. It fails, rather than skips, if `python3` is
missing: a check that cannot run has not passed.

### Changed: one cause flags one row

The GPS row also folded in position accuracy and clock validity, both of
which have rows of their own. A wide error ellipse lit two rows and the reason
line named two problems for one cause. The GPS row now reports satellites and
interference only. Verdicts are unchanged, because the dedicated row still
flags; the readout just stops double-counting. Pinned in the harness and in
the Python suite.

### Changed: a device list with no cameras is unconfirmed, not a rig of zero

With "expected cameras" left at 0 (any), an empty or all-junk `network_map`
read GO with "0 cameras". The camera answered `/status`, so at least one
camera exists; a map that lists none is a map that could not be read. It now
reads UNKNOWN. The demo scenarios all list at least one camera, so nothing in
the shipped fixtures changes.

### Added: the Python and iPhone clients sanitize their settings

The web page validates its thresholds because a `NaN` threshold once disabled a
check from a link. The other two clients had no equivalent. A string in
`rededge.json` raised mid-readout; a negative floor could never fire; a
corrupted settings file on the phone was merged in as-is. `sanitize_settings`
in Python and `sanitizeSettings` in the iPhone script now apply the same rule
as the web page: a threshold that is not a finite, non-negative number falls
back to the default, counts are floored to whole numbers, a zero timeout is
not a timeout, and Python says on stderr what it replaced. The iPhone Settings
form runs its raw field text through the same function as the file it saves.

### Fixed: `serve` with no `--page` served a 404

The default page name was `rededge-readiness.html`, resolved against the
working directory, and no file by that name exists anywhere in the tree (the
page lives in `web/`). `python3 rededge.py serve` therefore answered every
request for the page with "page not found". The default is now the shipped
page beside the script, resolved against the script's own location, so it
works from any directory; `serve` also warns at the terminal if the page is
missing rather than leaving the 404 for a browser across the room.

### Fixed: an unknown demo name on the phone showed a healthy GO

`?source=<name>` on an NFC tag or Shortcut fell through to the healthy demo
fixture for any name that was not a real demo, so a typo produced a GO readout
(badged DEMO, but GO). The demo names are now one list, shared by the menu and
the URL handler, and an unknown name runs a live read instead.

### Fixed: the offload command answered a dead link with a traceback

`offload` was the one command that let a `RedEdgeError` escape to the shell.
It now prints "Could not read the card" and exits 2 like `verify` does, and
files already pulled stay on disk for the next run to resume past.

### Changed: the page is navigable without a pointer

- The banner's live region now wraps only the verdict and its reason. It used
  to wrap the whole banner, including the status line that re-renders every
  second ("next in 3s"), so a screen reader announced a countdown.
- The pre-flight prep rows are checkboxes to assistive technology
  (`role="checkbox"`, `aria-checked`), on the web page and in the iPhone
  readout, and the iPhone rows take the keyboard.
- Every Settings field has a label associated with it; the in-text "Settings"
  links in the hint and help panels are buttons, so they take focus.
- The theme toggle's accessible name follows its state.

### Changed: smaller items

- `web/index.html` is removed. `web/_redirects` already serves the page at
  the site root, so the file was never reached, and its inline redirect
  script would have been blocked by the policy above if it ever were.
- The page's icon is `web/favicon.svg`, referenced as a file, instead of the
  same drawing embedded twice as data URIs; the SVG `apple-touch-icon`, which
  iOS does not honor, is gone. The local server already allowlisted the file.
- The local server sends the cross-origin header on proxy answers only, not on
  the page and its script, and answers `HEAD` the same as `GET`; the mock does
  too.
- The iPhone post-flight card judges "SD free" against the configured floor
  and lets the worst row set the card's state, as pre-flight does.
- The iPhone post-flight walk skips entries that are not objects, names that
  are not strings and sizes that are not numbers, and treats a listing that is
  not an object as an error rather than an empty card. `rededge.py` does the
  same in `verify` and `offload`.
- Terminal columns line up: the state word was padded to three characters, so
  `NO-GO` and `UNKNOWN` pushed their rows out of alignment.
- The web client reads `/version` and `/networkstatus` together, as the phone
  already did and the architecture notes already said.
- Whole-number thresholds (satellites, cameras) are floored the same way in
  all three clients; the config guard pins it.
- The config guard also refuses `javascript:`, `data:` and `file:` camera
  URLs from a link.
- CI runs on Node 24 (Node 20 left support in April 2026) with the current
  action majors, and its token is read-only.
- The DLS default in the architecture table read "Present and Ok"; the default
  is that a DLS is optional unless required in settings.
- A no-op assignment in the mock's route handler is gone.
- American English throughout: "inquiries".

The test suite grew from 19 tests to 40. The parity harness grew from 12
scenarios and 5 probes across two clients to 12 scenarios and 29 probes
across three.

### Still open, deliberately

- **No hardware has been read.** Unchanged from the first pass, and still the
  largest gap: every fixture here is a model of the camera, not the camera.

## Hosted demo moved, 18 September 2026

The Cloudflare account subdomain changed from `write2ayushjha` to `sudokodes`, so
the hosted demo is now at `rededge-readiness.sudokodes.workers.dev`. The previous
address no longer resolves.

The reason is presentation rather than engineering: the old hostname carried a
personal email handle, which reads as a developer sandbox on a page that carries
the company's copyright line and a licensing contact. Nothing about the Worker
changed. It remains static assets with no script and no runtime code at the edge.

Updated in the same pass: the `canonical`, `og:url`, `og:image` and
`twitter:image` tags in `web/rededge-readiness.html`, the **Live** link in
README.md, and the hosted-demo link in ARCHITECTURE.md. The social preview image
is served from the same origin, so it would have 404'd on every share until the
meta tags moved with it.

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

### Fixed: the trust boundary had a bypass, found by its own guard

The check that restricts a link-supplied camera URL to local addresses treated
anything beginning with `/` as a same-origin path. But `//evil.example` also
begins with `/`, and it is not a path: a protocol-relative URL resolves to a
remote origin. So the control written to stop a link pointing the tool at a
foreign host could be walked past with one extra slash.

Worth recording plainly, because the fix had been reported as complete. It was
reviewed, tested against several remote URLs, and still wrong. What caught it was
`web_config_check.js`, written afterwards to guard that boundary: it failed on
its first clean run against the code it was written to protect. Reading a fix is
not the same as constraining it.

### Added: a guard on the web configuration boundary

`web_config_check.js` holds the boundary that URL parameters cross. It asserts
that no threshold can end up as `NaN`, since a `NaN` threshold is not a loose
threshold but the absence of one, that the specific failures which shipped still
trip (a card at 0.2 GB, zero satellites, a pack at 3.1 V), that the poll interval
stays clamped, that remote and protocol-relative URLs are refused, and that
legitimate local addresses and the `/cam` proxy still work, so the guard cannot
protect a pilot out of a working tool. It runs in CI.

### Changed: the page forbids inline script

The web client moved out of the HTML into `web/app.js`, which let the Content
Security Policy drop `script-src 'unsafe-inline'` for `script-src 'self'`.

Nothing injects script today: camera data reaches the DOM through `textContent`
and URL parameters are sanitized. The point is that the policy no longer depends
on that continuing to be true. `rededge.py serve` now serves the page's sibling
assets from an explicit three-entry allowlist rather than the directory, because
a server that hands over whatever sits next to the page is one traversal bug away
from serving the rest of the disk. Traversal and arbitrary paths are refused,
verified.

There is still no build step and no bundler: `app.js` is the file that ships,
readable as served.

### Still open, deliberately

- **No hardware has been read.** The mock encodes assumptions about what the
  camera reports. Until a physical RedEdge or Altum has been on the WiFi, those
  assumptions are unverified. The design accounts for this by failing toward
  caution on anything unmodeled, but that is mitigation, not proof.
