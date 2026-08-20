# HF Dashboard — local radio bridge

Lets the HF Operating Dashboard show your rig's live dial frequency/mode
from WSJT-X, JS8Call, or GridTracker2, and optionally tune the rig from
the page. Runs on your own PC, next to those apps — the dashboard (a
browser tab) can't read UDP/serial directly, so this small script relays
it over a local WebSocket.

## Setup

```
cd bridge
pip install -r requirements.txt
python hf_bridge.py
```

Leave it running in a terminal while you operate. It listens on:
- UDP 2237 — WSJT-X's default UDP report port
- UDP 2442 — JS8Call's default UDP report port
- UDP 2243 — free port for GridTracker2, if you want it forwarding too
- ws://127.0.0.1:8765 — what the dashboard connects to

## Point your apps at it

**WSJT-X**: Settings → Reporting → check "UDP Server", host `127.0.0.1`,
port `2237` (leave "Accept UDP requests" on if you want the Tune button
in the dashboard to work later).

**JS8Call**: File → Settings → Reporting → enable UDP, host `127.0.0.1`,
port `2442`.

**GridTracker2**: only needed if you're not already running WSJT-X/JS8Call
directly — point its UDP forwarding at `127.0.0.1:2243`.

If more than one app is sending status at once, the dashboard shows
whichever reported most recently.

## Tuning the rig from the dashboard

Reading frequency needs nothing extra. *Setting* it forwards to a
`rigctld` (Hamlib) daemon on `127.0.0.1:4532` — run that separately,
pointed at your actual rig:

```
rigctld -m <your rig's Hamlib model number> -r <serial port> -s <baud>
```

Both WSJT-X and this bridge can then talk to the same `rigctld` without
fighting over the serial port. If you don't run `rigctld`, frequency
*display* still works — only the "Tune" button needs it.

## Dashboard side

Open the dashboard and click "Connect Radio" — it dials
`ws://127.0.0.1:8765`. No connection needed on this end; just keep the
script running.
