#!/usr/bin/env python3
"""
HF Operating Dashboard — local radio bridge.

Listens for WSJT-X / JS8Call (and GridTracker2, which can rebroadcast the
same protocol) UDP "Status" packets to learn the rig's current dial
frequency + mode, and serves that over a local WebSocket so the dashboard
(running in a normal browser tab) can display it live.

Optionally forwards a "set frequency" command from the dashboard to a
running `rigctld` (Hamlib) daemon, so you can tune the rig from the page.

Run:
    pip install -r requirements.txt
    python hf_bridge.py

Then in WSJT-X:  Settings -> Reporting -> enable "UDP Server", host 127.0.0.1
port 2237 (default). In JS8Call: File -> Settings -> Reporting -> enable UDP,
port 2442 (default). GridTracker2 can be pointed at another port below.

The dashboard connects to ws://127.0.0.1:8765 and shows a "Connect Radio"
button — click it once this script is running.
"""
import asyncio
import json
import struct
import time

WSJTX_PORT = 2237     # WSJT-X default UDP port
JS8CALL_PORT = 2442   # JS8Call default UDP port
GRIDTRACKER_PORT = 2243  # optional — point GridTracker2's UDP passthrough here
WS_PORT = 8765

RIGCTLD_HOST = "127.0.0.1"
RIGCTLD_PORT = 4532   # standard Hamlib rigctld port; run rigctld separately

BANDS = [
    (1800, 2000, "160m"), (3500, 4000, "80m"), (5330, 5410, "60m"),
    (7000, 7300, "40m"), (10100, 10150, "30m"), (14000, 14350, "20m"),
    (18068, 18168, "17m"), (21000, 21450, "15m"), (24890, 24990, "12m"),
    (28000, 29700, "10m"), (50000, 54000, "6m"), (144000, 148000, "2m"),
    (420000, 450000, "70cm"),
]

def band_for_hz(hz):
    khz = hz / 1000.0
    for lo, hi, name in BANDS:
        if lo <= khz <= hi:
            return name
    return None

def read_qstring(buf, off):
    (length,) = struct.unpack_from(">i", buf, off)
    off += 4
    if length <= 0:
        return "", off
    s = buf[off:off + length].decode("utf-8", errors="replace")
    return s, off + length

def parse_status_packet(buf, source_label):
    """Parse a WSJT-X/JS8Call-protocol UDP datagram; return dict or None."""
    try:
        magic, schema, msg_type = struct.unpack_from(">III", buf, 0)
        if magic != 0xADBCCBDA:
            return None
        off = 12
        app_id, off = read_qstring(buf, off)
        if msg_type != 1:  # only care about "Status" messages
            return None
        (freq_hz,) = struct.unpack_from(">Q", buf, off)
        off += 8
        mode, off = read_qstring(buf, off)
        return {
            "source": app_id or source_label,
            "freqHz": freq_hz,
            "mode": mode,
            "band": band_for_hz(freq_hz),
            "updatedAt": time.time(),
        }
    except Exception:
        return None

class Bridge:
    def __init__(self):
        self.current = None
        self.clients = set()

    async def broadcast(self):
        if not self.clients or self.current is None:
            return
        msg = json.dumps({"type": "state", **self.current})
        dead = set()
        for ws in self.clients:
            try:
                await ws.send(msg)
            except Exception:
                dead.add(ws)
        self.clients -= dead

    async def udp_listener(self, port, label):
        loop = asyncio.get_event_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UDPProto(self, label), local_addr=("0.0.0.0", port)
        )
        return transport

    async def handle_ws(self, websocket):
        self.clients.add(websocket)
        try:
            if self.current:
                await websocket.send(json.dumps({"type": "state", **self.current}))
            async for raw in websocket:
                try:
                    cmd = json.loads(raw)
                except Exception:
                    continue
                if cmd.get("cmd") == "setFreq":
                    await self.set_freq(int(cmd["hz"]))
        finally:
            self.clients.discard(websocket)

    async def set_freq(self, hz):
        """Forward a frequency change to rigctld (Hamlib). Requires rigctld
        running separately and pointed at your rig — this does NOT talk to
        the rig's serial port directly."""
        try:
            reader, writer = await asyncio.open_connection(RIGCTLD_HOST, RIGCTLD_PORT)
            writer.write(f"F {hz}\n".encode())
            await writer.drain()
            await reader.readline()
            writer.close()
        except Exception as e:
            print(f"[rigctld] set freq failed: {e}")

class _UDPProto(asyncio.DatagramProtocol):
    def __init__(self, bridge, label):
        self.bridge = bridge
        self.label = label

    def datagram_received(self, data, addr):
        parsed = parse_status_packet(data, self.label)
        if parsed:
            self.bridge.current = parsed
            asyncio.ensure_future(self.bridge.broadcast())

async def main():
    import websockets
    bridge = Bridge()
    await bridge.udp_listener(WSJTX_PORT, "WSJT-X")
    await bridge.udp_listener(JS8CALL_PORT, "JS8Call")
    await bridge.udp_listener(GRIDTRACKER_PORT, "GridTracker2")
    print(f"Listening: WSJT-X UDP :{WSJTX_PORT}, JS8Call UDP :{JS8CALL_PORT}, "
          f"GridTracker2 UDP :{GRIDTRACKER_PORT}")
    async with websockets.serve(bridge.handle_ws, "127.0.0.1", WS_PORT):
        print(f"Dashboard bridge ready at ws://127.0.0.1:{WS_PORT}")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
