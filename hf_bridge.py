#!/usr/bin/env python3
"""
HF Operating Dashboard - local radio bridge.

Listens for WSJT-X / JS8Call (and GridTracker2) UDP status traffic to learn the
rig's current dial frequency + mode, and serves that over a local WebSocket so
the dashboard (a normal browser tab) can display it live.

Optionally forwards a "set frequency" command from the dashboard to a running
`rigctld` (Hamlib) daemon, so you can tune the rig from the page.

Run:
    pip install -r requirements.txt
    python hf_bridge.py

--------------------------------------------------------------------------
WHAT CHANGED FROM THE ORIGINAL, AND WHY
--------------------------------------------------------------------------
1. MULTICAST. This station runs WSJT-X in multicast mode (224.0.0.73:2237) so
   that Log4OM, GridTracker and this bridge can all listen at once. A plain
   bind() on 0.0.0.0 does NOT receive multicast - the socket must join the
   group with IP_ADD_MEMBERSHIP. Without that join this bridge sees nothing.
   Set WSJTX_MCAST = None to go back to plain unicast.

2. SO_REUSEADDR / SO_REUSEPORT. Required so several programs can share the
   same multicast port. Without it, whoever starts second fails to bind.

3. JS8CALL. Its UDP API defaults to port 2242 (2442 is the TCP API port, which
   JS8Spotter already uses here). JS8Call also speaks JSON, not the WSJT-X
   binary protocol, so it needs its own parser.

4. asyncio.get_running_loop() instead of get_event_loop() - the latter is
   deprecated and is a hard error in newer Python.
--------------------------------------------------------------------------
"""
import asyncio
import json
import socket
import struct
import time

WSJTX_PORT = 2237            # WSJT-X UDP port
WSJTX_MCAST = "224.0.0.73"   # multicast group; set to None for plain unicast
JS8CALL_PORT = 2242          # JS8Call UDP API (NOT 2442 - that is its TCP API)
JS8CALL_LEGACY_PORT = 2442   # kept in case anything still sends here
GRIDTRACKER_PORT = 2243      # optional - GridTracker2 UDP passthrough
WS_PORT = 8765

BIND_ADDR = "0.0.0.0"        # interface to bind / join the group on

RIGCTLD_HOST = "127.0.0.1"
RIGCTLD_PORT = 4532          # standard Hamlib rigctld port; run rigctld separately

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
    """Parse a WSJT-X-protocol UDP datagram; return dict or None.

    Layout: magic(4) schema(4) type(4) then, for Status (type 1):
    Id(utf8) DialFrequency(quint64) Mode(utf8) ... - all big-endian (Qt).
    """
    try:
        magic, schema, msg_type = struct.unpack_from(">III", buf, 0)
        if magic != 0xADBCCBDA:
            return None
        if msg_type != 1:                       # only "Status"
            return None
        off = 12
        app_id, off = read_qstring(buf, off)
        (freq_hz,) = struct.unpack_from(">Q", buf, off)
        off += 8
        mode, off = read_qstring(buf, off)
        if not freq_hz:
            return None
        return {
            "source": app_id or source_label,
            "freqHz": freq_hz,
            "mode": mode,
            "band": band_for_hz(freq_hz),
            "updatedAt": time.time(),
        }
    except Exception:
        return None


def parse_js8call_packet(buf, source_label):
    """JS8Call's UDP API emits JSON, not the WSJT-X binary protocol.

    Best-effort: pull a dial frequency out of whatever message arrives. JS8Call
    puts it in params as DIAL (and FREQ/OFFSET alongside). Tolerant on purpose -
    verify against your own JS8Call before relying on it.
    """
    try:
        txt = buf.decode("utf-8", errors="replace").strip()
        if not txt.startswith("{"):
            return None
        msg = json.loads(txt)
    except Exception:
        return None

    params = msg.get("params") or {}
    dial = None
    for key in ("DIAL", "Dial", "dial", "FREQ", "Frequency"):
        v = params.get(key, msg.get(key))
        if isinstance(v, (int, float)) and v > 100000:
            dial = int(v)
            break
    if not dial:
        return None
    # don't fall back to msg["type"] - that is the API message name (RIG.FREQ),
    # not a modulation mode
    mode = params.get("MODE") or params.get("SPEED") or "JS8"
    return {
        "source": source_label,
        "freqHz": dial,
        "mode": str(mode),
        "band": band_for_hz(dial),
        "updatedAt": time.time(),
    }


def make_udp_socket(port, mcast_group=None, bind_addr=BIND_ADDR):
    """UDP socket that can share a port and, when asked, join a multicast group."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):          # not present on Windows
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    # bind to all interfaces; for multicast the group join selects the traffic
    s.bind(("", port))
    if mcast_group:
        mreq = struct.pack("4s4s",
                           socket.inet_aton(mcast_group),
                           socket.inet_aton(bind_addr))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.setblocking(False)
    return s


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

    async def udp_listener(self, port, label, parser, mcast_group=None):
        loop = asyncio.get_running_loop()
        sock = make_udp_socket(port, mcast_group)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UDPProto(self, label, parser), sock=sock
        )
        where = f"{mcast_group}:{port}" if mcast_group else f":{port}"
        print(f"  {label:<13} UDP {where}")
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
        running separately and pointed at your rig - this does NOT talk to
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
    def __init__(self, bridge, label, parser):
        self.bridge = bridge
        self.label = label
        self.parser = parser

    def datagram_received(self, data, addr):
        parsed = self.parser(data, self.label)
        if parsed:
            self.bridge.current = parsed
            asyncio.ensure_future(self.bridge.broadcast())


async def main():
    import websockets
    bridge = Bridge()
    print("HF bridge listening:")
    await bridge.udp_listener(WSJTX_PORT, "WSJT-X", parse_status_packet, WSJTX_MCAST)
    await bridge.udp_listener(JS8CALL_PORT, "JS8Call", parse_js8call_packet)
    await bridge.udp_listener(JS8CALL_LEGACY_PORT, "JS8Call-tcpapi", parse_js8call_packet)
    await bridge.udp_listener(GRIDTRACKER_PORT, "GridTracker2", parse_status_packet)
    async with websockets.serve(bridge.handle_ws, "127.0.0.1", WS_PORT):
        print(f"Dashboard bridge ready at ws://127.0.0.1:{WS_PORT}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
