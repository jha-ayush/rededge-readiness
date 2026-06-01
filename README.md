# RedEdge Field Tools

Field readiness and capture tooling for the MicaSense RedEdge and Altum
multispectral cameras, built around one question a pilot needs answered before
launch: is this sensor ready to capture good data, right now, and why.

Every check fails toward caution. Anything that cannot be confirmed reads as
CHECK, a lost link reads as NO-GO, and the tools never return a clear pass on
missing data. They report sensor readiness, not flight legality.

## The constraint that shapes everything

The camera is a local device. It serves plain HTTP JSON at `192.168.10.254`
over its own WiFi access point (or `192.168.1.83` over Ethernet), on port 80,
with no internet and no CORS headers.

Two consequences:

- A web page in any phone browser is blocked by CORS from reading the camera's
  JSON, no matter where the page is hosted. A cloud-hosted page can show the UI
  anywhere but can never talk to the camera in the field.
- Anything that reads the camera must run on a device joined to the camera WiFi.
  Native code (Python, or Scriptable on iOS) has no CORS restriction; a browser
  does.

That is why the everyday field tool is the native iOS script, not a website.

## Files

| File | What it is |
| --- | --- |
| `rededge-readiness.scriptable.js` | iPhone field tool. Paste into the Scriptable app. Reads the camera natively, shows a full-screen go/no-go readout, and can run as a Home Screen widget. This is the everyday tool. |
| `rededge-readiness.html` | Responsive web version. Useful for demo and review on any device. Live camera use requires the local proxy in `rededge.py serve`, so it is a computer tool, not a phone tool. |
| `rededge.py` | Zero-dependency Python client and CLI for a computer or Raspberry Pi joined to the camera WiFi. Commands: `check`, `watch`, `status`, `offload`, `capture`, `serve`. |

## iPhone (everyday use)

1. Install Scriptable from the App Store (free).
2. New script, paste in `rededge-readiness.scriptable.js`, name it.
3. Join the camera WiFi. On first run, allow Local Network access when prompted
   (or enable it in Settings > Scriptable > Local Network).
4. Run from the app, a Home Screen icon, or add a Scriptable widget for a
   glanceable state. Re-run to refresh.

Set `DEMO` near the top of the script to `go`, `sd`, `gps`, `dls`, or `down` to
review states without a camera. Thresholds live in the `CFG` block at the top.

## Computer (offload, capture, web UI)

Python 3, no dependencies:

    python3 rededge.py check                 # one-shot readiness, exit 0/1/2
    python3 rededge.py watch --interval 3     # live terminal readout
    python3 rededge.py offload ./flight --only tif   # pull captures off the card
    python3 rededge.py capture --bands 31 --block    # trigger one capture
    python3 rededge.py serve --page rededge-readiness.html   # serve the web UI + CORS proxy

`serve` is what makes the web UI work live: it serves the page locally and
proxies the camera's read-only routes with CORS added, then prints a link to
open on the same WiFi. The proxy is read-only by design and cannot trigger a
capture, delete a file, or reformat the card.

## Readiness checks

SD storage, GPS fix, position accuracy, light sensor (DLS), supply voltage,
time source, the multi-camera rig, and firmware. The worst check sets the
overall state. All three tools share the same logic and thresholds.

## Thresholds

Defaults are sensible starting points, not vendor spec. The minimum supply
voltage in particular is a placeholder; set it against your own power setup
before trusting it.
