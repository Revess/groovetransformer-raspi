#!/usr/bin/env python3
"""
Pi OSCâMIDI bridge with per-bar batching.
- From host â /midi â plugin: real-time (unchanged).
- From plugin â host: collected per bar and sent as /midi_bar(bar,bpm100,num,den,ppq,blob).

Bar segmentation:
  1) Preferred: SysEx bar marker (see header below) emitted by plugin at each bar start.
  2) Fallback: time-based bars using --bpm/--num/--den if no marker seen yet.

Blob format (simple, length-prefixed events):
  [len][byte0..byteN] repeated for all MIDI events in that bar.
  'len' is 1 byte (0..255). SysEx inside the bar is forwarded too (len may be >3).

Wire your plugin:
  aconnect "GrooveWrap_to_Plugin:0" "<Plugin MIDI IN>"
  aconnect "<Plugin MIDI OUT>" "Plugin_to_GrooveWrap:0"
"""

import argparse
import time
from typing import List, Sequence, Optional

import rtmidi
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

# ---------- SysEx bar marker spec ----------
SYSEX_HDR = bytes([0xF0, 0x7D, 0x47, 0x54, 0x01])  # F0 7D 'G' 'T' 01
SYSEX_END = 0xF7

def parse_bar_marker(msg: Sequence[int]):
    """Return (bar:int, bpm100:int, num:int, den:int, ppq:int) if msg is our marker, else None."""
    if len(msg) < 5 or msg[0] != 0xF0 or msg[-1] != SYSEX_END:
        return None
    b = bytes(msg)
    if not b.startswith(SYSEX_HDR):
        return None
    payload = b[len(SYSEX_HDR):-1]  # strip hdr and trailing F7
    if len(payload) != (4 + 2 + 1 + 1 + 2):
        return None
    # Little-endian unpacking
    bar = int.from_bytes(payload[0:4], "little", signed=False)
    bpm100 = int.from_bytes(payload[4:6], "little", signed=False)
    num = payload[6]
    den = payload[7]
    ppq = int.from_bytes(payload[8:10], "little", signed=False)
    return bar, bpm100, num, den, ppq

# ---------- helpers ----------
t0 = time.monotonic()
def ts() -> str: return f"{time.monotonic() - t0:8.3f}s"

def pack_events(events: List[List[int]]) -> bytes:
    """Pack events into [len][bytes...]* for OSC blob."""
    out = bytearray()
    for ev in events:
        if not ev:  # skip empties
            continue
        if len(ev) > 255:
            # unlikely in live MIDI, but truncate to be safe
            ev = ev[:255]
        out.append(len(ev))
        out.extend(ev)
    return bytes(out)

def fmt_hex(msg: Sequence[int]) -> str:
    return " ".join(f"{b:02X}" for b in msg)

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="Pi per-bar MIDI batcher â OSC")
    # OSC peer (host)
    ap.add_argument("--host-ip", default="192.168.178.113", help="Host IP to send /midi_bar to")
    ap.add_argument("--host-port", type=int, default=9000, help="Host OSC port")
    ap.add_argument("--addr-batch", default="/midi_bar", help="OSC address for bar batches")
    ap.add_argument("--addr-rt", default="/midi", help="OSC address for real-time (hostâpi)")
    # OSC server (receive from host)
    ap.add_argument("--listen", default="0.0.0.0", help="Bind IP")
    ap.add_argument("--listen-port", type=int, default=9001, help="OSC port to receive hostâpi")
    # Virtual MIDI names
    ap.add_argument("--out-name", default="GrooveWrap_to_Plugin", help="Virtual OUT â plugin")
    ap.add_argument("--in-name", default="Plugin_to_GrooveWrap", help="Virtual IN  â plugin")
    # Fallback bar timing
    ap.add_argument("--bpm", type=float, default=120.0, help="Fallback BPM (if no marker yet)")
    ap.add_argument("--num", type=int, default=4, help="Fallback time sig numerator")
    ap.add_argument("--den", type=int, default=4, help="Fallback time sig denominator")
    ap.add_argument("--ppq", type=int, default=480, help="PPQ metadata in batches")
    ap.add_argument("--verbose", action="store_true", help="Verbose logs")
    args = ap.parse_args()

    # OSC setup
    osc_client = SimpleUDPClient(args.host_ip, args.host_port)
    dispatcher = Dispatcher()

    # MIDI OUT â plugin
    mout = rtmidi.MidiOut()
    mout.open_virtual_port(args.out_name)

    # State for bar collection
    current_bar: Optional[int] = None
    bpm100 = int(round(args.bpm * 100))
    num, den, ppq = args.num, args.den, args.ppq
    collected: List[List[int]] = []
    have_seen_marker = False

    # Fallback timing
    def bar_seconds(bpm: float, num: int, den: int) -> float:
        # quarter note = 60/bpm; bar = num * (4/den) quarter notes
        return (60.0 / bpm) * num * (4.0 / den)

    next_bar_deadline = time.monotonic() + bar_seconds(args.bpm, num, den)

    def flush(label="FLUSH"):
        nonlocal collected, current_bar, bpm100, num, den, ppq
        if not collected:
            return
        blob = pack_events(collected)
        try:
            osc_client.send_message(args.addr_batch, [int(current_bar or 0),
                                                      int(bpm100), int(num), int(den), int(ppq),
                                                      blob])
        except Exception:
            pass
        if args.verbose:
            print(f"{ts()}  {label} â host  bar={current_bar} evts={len(collected)} size={len(blob)} bytes", flush=True)
        collected = []

    # MIDI IN â plugin (collect)
    def on_plugin_midi(event, _):
        nonlocal current_bar, bpm100, num, den, ppq, have_seen_marker, next_bar_deadline
        msg, _t = event
        # Marker?
        marker = parse_bar_marker(msg)
        if marker is not None:
            have_seen_marker = True
            # flush previous bar before switching, if any events
            flush("BAR-MARK")
            current_bar, bpm100, num, den, ppq = marker
            if args.verbose:
                print(f"{ts()}  MARK  bar={current_bar} bpm={bpm100/100:.2f} {num}/{den} ppq={ppq}", flush=True)
            return

        # Normal MIDI -> collect
        collected.append(list(msg))

        # If we haven't seen a marker yet, time-slice by fallback BPM
        if not have_seen_marker:
            now = time.monotonic()
            if now >= next_bar_deadline:
                # flush as unspecified bar (None)
                flush("TIME")
                next_bar_deadline = now + bar_seconds(args.bpm, num, den)

    min_plugin = rtmidi.MidiIn()
    min_plugin.open_virtual_port(args.in_name)
    min_plugin.set_callback(on_plugin_midi)

    # OSC handler (host â plugin real-time)
    def on_osc_midi(_addr, *args_):
        if not args_:
            return
        payload = args_[0]
        if isinstance(payload, (bytes, bytearray)):
            msg = list(payload)
        elif isinstance(payload, (list, tuple)) and all(isinstance(x, int) for x in payload):
            msg = list(payload)
        else:
            return
        try:
            mout.send_message(msg)
        except Exception:
            pass
        if args.verbose:
            print(f"{ts()}  HOSTâPLUGIN  [{fmt_hex(msg)}]", flush=True)

    dispatcher.map(args.addr_rt, on_osc_midi)

    server = ThreadingOSCUDPServer((args.listen, args.listen_port), dispatcher)
    print("Pi per-bar batcher ready.")
    print(f"  MIDI OUT â plugin: {args.out_name}")
    print(f"  MIDI IN  â plugin: {args.in_name}")
    print(f"  OSC send â host: {args.host_ip}:{args.host_port} '{args.addr_batch}'")
    print(f"  OSC recv â host: {args.listen}:{args.listen_port} '{args.addr_rt}'")
    print("Tip: emit the SysEx bar marker at bar start for exact segmentation. Ctrl+C to quit.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            # final flush in case we exit mid-bar
            flush("EXIT")
        except Exception:
            pass
        try:
            min_plugin.close_port(); mout.close_port()
        except Exception:
            pass

if __name__ == "__main__":
    main()
