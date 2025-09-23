# Host-side MIDI↔OSC bridge with terminal logs.
# - Capture local MIDI IN (virtual by default) → send as OSC '/midi' to the Pi.
# - Receive OSC '/midi' from the Pi → emit on local MIDI OUT (virtual) and log both directions.
# Works cross-platform; on Windows pass --in-port/--out-port to use real ports (virtuals not supported by WinMM).

import time, argparse, threading
from typing import Sequence

import rtmidi
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

import threading

MIDI_BAR_PLAYER = None
PENDING_BARS = []
PENDING_LOCK = threading.Lock()

def _unpack_bar_blob(blob: bytes):
    """
    Supports TWO formats:
      A) with offsets:  [off_lo][off_hi][len][data...]*   (off = uint16 ticks from bar start)
      B) legacy (no offsets): [len][data...]*             (falls back to even spacing)
    Returns: (has_offsets: bool, events: list[tuple[offset_ticks:int, list[int]]])
    """
    b = bytes(blob)
    ev = []
    # try A
    i = 0; ok = True
    while i < len(b):
        if i + 3 > len(b): ok = False; break
        off = b[i] | (b[i+1] << 8)
        ln = b[i+2]
        i += 3
        if i + ln > len(b): ok = False; break
        ev.append((off, list(b[i:i+ln])))
        i += ln
    if ok and i == len(b):
        return True, ev
    # fallback B
    ev.clear(); i = 0
    while i < len(b):
        ln = b[i]; i += 1
        ev.append((0, list(b[i:i+ln])))
        i += ln
    return False, ev

class MidiBarPlayer:
    def __init__(self, midi_out):
        self.midi_out = midi_out
        self.lock = threading.Lock()  # rtmidi isn't guaranteed thread-safe

    def _ticks_to_seconds(self, ticks: int, bpm100: int, ppq: int) -> float:
        bpm = bpm100 / 100.0
        return (60.0 / bpm) * (ticks / float(ppq))

    def play_bar(self, bar: int, bpm100: int, num: int, den: int, ppq: int, events):
        # events: list[(offset_ticks, byteslist)]
        # normalize: if no offsets, spread evenly across the bar
        has_offsets = any(off != 0 for off, _ in events)
        if not has_offsets:
            # even spacing across the bar (legacy blob); last evt lands near bar end
            total_ticks = int(num * (ppq * (4 / den)))
            n = max(1, len(events))
            step = total_ticks // n
            events = [(i * step, msg) for i, (_, msg) in enumerate(events)]

        # schedule in a background thread
        def runner():
            start = time.monotonic() + 0.010  # small lead-in to reduce jitter
            for off, msg in sorted(events, key=lambda x: (x[0], x[1])):
                when = start + self._ticks_to_seconds(off, bpm100, ppq)
                while True:
                    now = time.monotonic()
                    dt = when - now
                    if dt <= 0:
                        break
                    # sleep in small chunks to keep timing tight without busy-wait
                    time.sleep(min(0.002, dt))
                with self.lock:
                    self.midi_out.send_message(msg)
        threading.Thread(target=runner, daemon=True).start()

def on_midi_bar(_addr, bar, bpm100, num, den, ppq, blob):
    has_offs, ev = _unpack_bar_blob(blob)
    with PENDING_LOCK:
        if MIDI_BAR_PLAYER is None:
            PENDING_BARS.append((bar, bpm100, num, den, ppq, ev))
            return
        MIDI_BAR_PLAYER.play_bar(bar, bpm100, num, den, ppq, ev)

# ---------- utils ----------
t0 = time.monotonic()
def ts() -> str: return f"{time.monotonic() - t0:8.3f}s"
NOTE_NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
def note_name(n:int)->str: return f"{NOTE_NAMES[n%12]}{(n//12)-1}"
def fmt_hex(msg: Sequence[int]) -> str: return " ".join(f"{b:02X}" for b in msg)

def parse_msg(msg: Sequence[int], names: bool) -> str:
    if not msg: return "Empty"
    s = msg[0]; hi = s & 0xF0; ch = (s & 0x0F) + 1
    if s == 0xF0: return f"SysEx len={len(msg)}"
    if hi == 0x80 and len(msg)>=3: return f"NoteOff ch{ch} note={note_name(msg[1]) if names else msg[1]} vel={msg[2]}"
    if hi == 0x90 and len(msg)>=3:
        n,v = msg[1], msg[2]
        if v==0: return f"NoteOff ch{ch} note={note_name(n) if names else n} vel=0"
        return f"NoteOn  ch{ch} note={note_name(n) if names else n} vel={v}"
    if hi == 0xA0 and len(msg)>=3: return f"PolyAT ch{ch} note={msg[1]} val={msg[2]}"
    if hi == 0xB0 and len(msg)>=3: return f"CC     ch{ch} cc={msg[1]} val={msg[2]}"
    if hi == 0xC0 and len(msg)>=2: return f"ProgCh ch{ch} prog={msg[1]}"
    if hi == 0xD0 and len(msg)>=2: return f"ChAT   ch{ch} val={msg[1]}"
    if hi == 0xE0 and len(msg)>=3:
        val = (msg[2]<<7)|msg[1]; return f"PitchB ch{ch} val={val}"
    return f"Status 0x{s:02X} data={list(msg[1:])}"

def resolve_port_index(names, want):
    if want is None: return None
    if isinstance(want, int): return want
    # substring match (case-insensitive)
    lw = want.lower()
    for i, n in enumerate(names):
        if lw in n.lower(): return i
    raise RuntimeError(f"Port '{want}' not found. Available: {names}")

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="Host MIDI↔OSC bridge (terminal)")
    ap.add_argument("--pi-ip", default="192.168.178.82", help="Pi IP (default 192.168.1.10)")
    ap.add_argument("--pi-port", type=int, default=9001, help="Pi OSC listen port (default 9001)")
    ap.add_argument("--listen", default="0.0.0.0", help="Host IP to bind (default 0.0.0.0)")
    ap.add_argument("--listen-port", type=int, default=9000, help="Host OSC port to receive from Pi (default 9000)")
    ap.add_argument("--addr", default="/midi", help="OSC address (default /midi)")
    ap.add_argument("--names", action="store_true", default=True, help="Show note names (C4)")
    # MIDI port options
    ap.add_argument("--in-virtual", action="store_true", default=True, help=argparse.SUPPRESS)  # default on mac/linux
    ap.add_argument("--in-name", default="Host_to_Pi", help="Virtual/label for MIDI IN (default Host_to_Pi)")
    ap.add_argument("--in-port", default=None, help="Open existing MIDI IN by name substring or index (Windows: use this)")
    ap.add_argument("--out-virtual", action="store_true", default=True, help=argparse.SUPPRESS) # default on mac/linux
    ap.add_argument("--out-name", default="Pi_to_Host", help="Virtual/label for MIDI OUT (default Pi_to_Host)")
    ap.add_argument("--out-port", default=None, help="Open existing MIDI OUT by name substring or index (Windows: use this)")
    args = ap.parse_args()

    show_hex = False

    # OSC setup
    osc_client = SimpleUDPClient(args.pi_ip, args.pi_port)
    disp = Dispatcher()
    disp.map("/midi_bar", on_midi_bar)
    server = ThreadingOSCUDPServer((args.listen, args.listen_port), disp)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # MIDI OUT (from Pi → Host)
    mout = rtmidi.MidiOut()
    if args.out_port is not None:
        ports = mout.get_ports()
        idx = resolve_port_index(ports, int(args.out_port) if str(args.out_port).isdigit() else args.out_port)
        mout.open_port(idx)
        out_label = ports[idx]
    else:
        try:
            mout.open_virtual_port(args.out_name)
            out_label = f"(virtual) {args.out_name}"
        except (rtmidi.InvalidPortError, NotImplementedError):
            raise SystemExit("Virtual MIDI OUT not supported. Use --out-port to select a real port.")
        
    global MIDI_BAR_PLAYER
    MIDI_BAR_PLAYER = MidiBarPlayer(mout)
    with PENDING_LOCK:
        for args_ in PENDING_BARS:
            bar, bpm100, num, den, ppq, ev = args_
            MIDI_BAR_PLAYER.play_bar(bar, bpm100, num, den, ppq, ev)
        PENDING_BARS.clear()

    # MIDI IN (Host → Pi)
    def on_host_midi(event, _):
        msg, _t = event
        try:
            osc_client.send_message(args.addr, [bytes(msg)])
        except Exception:
            pass
        desc = parse_msg(msg, args.names)
        line = f"{ts()}  HOST→PI  {desc}"
        if show_hex: line += f"   [{fmt_hex(msg)}]"
        # print(line, flush=True)

    minput = rtmidi.MidiIn()
    if args.in_port is not None:
        ports = minput.get_ports()
        idx = resolve_port_index(ports, int(args.in_port) if str(args.in_port).isdigit() else args.in_port)
        minput.open_port(idx)
        in_label = ports[idx]
    else:
        try:
            minput.open_virtual_port(args.in_name)
            in_label = f"(virtual) {args.in_name}"
        except (rtmidi.InvalidPortError, NotImplementedError):
            raise SystemExit("Virtual MIDI IN not supported. Use --in-port to select a real port.")
    minput.set_callback(on_host_midi)

    # OSC handler (Pi → Host)
    def on_osc_midi(_addr, *args_):
        if not args_:
            print(f"{ts()}  WARN     /midi no args"); return
        payload = args_[0]
        if isinstance(payload, (bytes, bytearray)):
            msg = list(payload)
        elif isinstance(payload, (list, tuple)) and all(isinstance(x, int) for x in payload):
            msg = list(payload)
        else:
            print(f"{ts()}  WARN     Unsupported payload: {type(payload)}"); return
        try:
            mout.send_message(msg)
        except Exception:
            pass
        desc = parse_msg(msg, args.names)
        line = f"{ts()}  PI→HOST  {desc}"
        if show_hex: line += f"   [{fmt_hex(msg)}]"
        print(line, flush=True)

    disp.map(args.addr, on_osc_midi)

    print("Ready.")
    print(f"  MIDI IN  (host→pi): {in_label}")
    print(f"  MIDI OUT (pi→host): {out_label}")
    print(f"  OSC send → {args.pi_ip}:{args.pi_port} '{args.addr}'")
    print(f"  OSC recv ← {args.listen}:{args.listen_port} '{args.addr}'")
    print("Route your controller/DAW → MIDI IN, and listen to MIDI OUT for 'beats'. Ctrl+C to quit.")

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try: server.shutdown()
        except Exception: pass
        try: minput.close_port(); mout.close_port()
        except Exception: pass

if __name__ == "__main__":
    main()
