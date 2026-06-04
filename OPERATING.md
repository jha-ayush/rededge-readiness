# RedEdge Readiness, Operating Guide

One page covering the whole system: what each piece is for, and the day-to-day
flow from pre-flight to post-flight. For repo and install detail see `README.md`.

## The one idea

Before you fly a MicaSense RedEdge or Altum, you want a single honest answer:
is the sensor ready to capture good data, right now, and why. After you land,
you want to confirm the data is actually on the card before you leave the site.
These tools answer exactly those two questions, and they fail toward caution:
anything they cannot confirm is flagged, never passed.

## What each piece is for

- **iPhone (Scriptable script)** is the everyday field tool. It reads the
  camera over WiFi natively and shows a GO / CHECK / NO-GO readout, plus a
  Post-flight check that counts captures on the card. Works with no computer.
- **Web page** (live at the Cloudflare link) is for demo, review, and showing
  the tool on any device. It reads a live camera only through the local proxy,
  so in the field the phone is the real tool, not the website.
- **`rededge.py`** is the computer tool: readiness `check` and `watch`,
  `offload` to pull imagery, `verify` to confirm captures, `capture` to trigger
  a shot, and `serve` to run the web UI locally against a real camera.
- **`rededge_mock.py`** emulates the camera so any tool can be exercised
  end to end without hardware.

## Day-to-day flow

### Before the flight (phone)
1. Power the camera, join its WiFi.
2. Open the RedEdge Readiness script (Home Screen icon or widget).
3. Read the result. GO means clear to capture. CHECK or NO-GO: tap into the
   reason, fix it, re-run. Common ones: low SD space, weak GPS, DLS warming up.

### After the flight (phone)
1. Still on the camera WiFi, open the script and choose Post-flight check.
2. Confirm it reports captures found with a sensible count before you pack up.
   No captures means something went wrong; do not leave the site yet.

### Back at the computer
1. `python3 rededge.py verify` to re-confirm the card from a keyboard.
2. `python3 rededge.py offload ./flight_<date> --only tif` to pull imagery.
   It preserves the SET folder layout and resumes if interrupted.

## Reading the result

- **GO** every monitored system is within tolerance.
- **CHECK** something needs attention or could not be confirmed.
- **NO-GO** a blocking problem, or no link to the camera.

The checks: SD storage, GPS fix, position accuracy, light sensor (DLS), supply
voltage, time source, the multi-camera rig, and firmware. The worst single
check sets the overall state.

## Setting your thresholds

Defaults are starting points, not vendor spec, especially minimum supply
voltage. Set them to your rig once:

- **iPhone**: open the script in the Scriptable app, choose Settings.
- **Computer**: `python3 rededge.py init-config`, edit `rededge.json`.

Both use the same fields, so the two tools judge readiness identically.

## When the readout says NO-GO with "no link"

That is the tool being honest that it cannot reach the camera, not a fault in
the camera. Check, in order: you are joined to the camera WiFi; the camera is
powered and booted; the address in Settings matches the camera
(`192.168.10.254` on WiFi, `192.168.1.83` on Ethernet).

## Testing without a camera

Run `python3 rededge_mock.py --scenario healthy` and point any tool at it. Use
the other scenarios (`lowsd`, `nogps`, `dlserror`, `badfw`, `multicam`) to see
each readout. The web page also has built-in Demo sources in its Source menu.
