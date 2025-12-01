# Host-side MIDI↔OSC bridge with terminal logs & GUI.
# - Capture local MIDI IN (virtual by default) → send as OSC '/midi' to the Pi.
# - Receive OSC '/midi' from the Pi → emit on local MIDI OUT (virtual) and log both directions.

import time, argparse, threading, sys
from typing import Sequence

import rtmidi
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

# --- Optional GUI Imports ---
GUI_AVAILABLE = False
try:
    from textual.app import App, ComposeResult
    from textual.containers import Container, Vertical
    from textual.widgets import Header, Footer, Log, Static, Label
    from textual.reactive import reactive
    GUI_AVAILABLE = True
except ImportError:
    pass

# ==============================================================================
# GLOBALS & LOGGING
# ==============================================================================
GUI_APP = None
MIDI_BAR_PLAYER = None
PENDING_BARS = []
PENDING_LOCK = threading.Lock()
t0 = time.monotonic()

def ts() -> str: return f"{time.monotonic() - t0:8.3f}s"

def log_info(msg):
    """Unified logging: Prints to terminal OR writes to GUI log"""
    if GUI_APP:
        GUI_APP.write_log(str(msg))
    else:
        print(msg, flush=True)

def update_gui_grid(blob):
    """Parses blob and sends 9-track hit data to GUI"""
    if not GUI_APP: return
    
    # We need to unpack this blob into a simple 9x32 grid for display
    # Blob format: [off_lo, off_hi, len, 0x90, note, vel]...
    
    # Map MIDI notes back to rows (0-8)
    # Kick=36, Snare=38, ClHat=42, OpHat=46, LoTom=41, MidTom=48, HiTom=45, Crash=49, Ride=51
    note_to_row = {36:0, 38:1, 42:2, 46:3, 41:4, 48:5, 45:6, 49:7, 51:8}
    
    grid_state = [[0]*32 for _ in range(9)] # 9 rows x 32 steps
    
    b = bytes(blob)
    i = 0
    while i < len(b):
        if i + 3 > len(b): break
        # unpack offset
        off = b[i] | (b[i+1] << 8) # ticks
        length = b[i+2]
        i += 3
        
        if i + length > len(b): break
        msg = list(b[i:i+length])
        i += length
        
        # Check if Note On
        if len(msg) >= 3 and (msg[0] & 0xF0) == 0x90 and msg[2] > 0:
            note = msg[1]
            if note in note_to_row:
                row_idx = note_to_row[note]
                
                # Convert ticks to step (assuming 480 PPQ, 16th note = 120 ticks)
                # This is an approximation for visualization
                # 2 bars = 8 beats = 32 steps
                # Total ticks = 480 * 8 = 3840 ticks
                # Ticks per step = 120
                
                step_idx = int(off / 120)
                if 0 <= step_idx < 32:
                    grid_state[row_idx][step_idx] = 1

    GUI_APP.update_grid(grid_state)

# ==============================================================================
# MIDI LOGIC
# ==============================================================================
def _unpack_bar_blob(blob: bytes):
    b = bytes(blob)
    ev = []
    # try A (with offsets)
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
    
    # fallback B (legacy)
    ev.clear(); i = 0
    while i < len(b):
        ln = b[i]; i += 1
        ev.append((0, list(b[i:i+ln])))
        i += ln
    return False, ev

class MidiBarPlayer:
    def __init__(self, midi_out):
        self.midi_out = midi_out
        self.lock = threading.Lock()

    def _ticks_to_seconds(self, ticks: int, bpm100: int, ppq: int) -> float:
        bpm = bpm100 / 100.0
        return (60.0 / bpm) * (ticks / float(ppq))

    def play_bar(self, bar: int, bpm100: int, num: int, den: int, ppq: int, events):
        has_offsets = any(off != 0 for off, _ in events)
        if not has_offsets:
            total_ticks = int(num * (ppq * (4 / den)))
            n = max(1, len(events))
            step = total_ticks // n
            events = [(i * step, msg) for i, (_, msg) in enumerate(events)]

        def runner():
            start = time.monotonic() + 0.010 
            for off, msg in sorted(events, key=lambda x: (x[0], x[1])):
                when = start + self._ticks_to_seconds(off, bpm100, ppq)
                while True:
                    now = time.monotonic()
                    dt = when - now
                    if dt <= 0: break
                    time.sleep(min(0.002, dt))
                with self.lock:
                    self.midi_out.send_message(msg)
        threading.Thread(target=runner, daemon=True).start()

def on_midi_bar(_addr, bar, bpm100, num, den, ppq, blob):
    log_info(f"{ts()}  RECV BAR {bar} (len={len(blob)} bytes)")
    
    # Update Visualization
    update_gui_grid(blob)
    
    has_offs, ev = _unpack_bar_blob(blob)
    with PENDING_LOCK:
        if MIDI_BAR_PLAYER is None:
            PENDING_BARS.append((bar, bpm100, num, den, ppq, ev))
            return
        MIDI_BAR_PLAYER.play_bar(bar, bpm100, num, den, ppq, ev)

# ---------- MIDI Utils ----------
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
    if hi == 0xB0 and len(msg)>=3: return f"CC     ch{ch} cc={msg[1]} val={msg[2]}"
    return f"Status 0x{s:02X} data={list(msg[1:])}"

def resolve_port_index(names, want):
    if want is None: return None
    if isinstance(want, int): return want
    lw = want.lower()
    for i, n in enumerate(names):
        if lw in n.lower(): return i
    raise RuntimeError(f"Port '{want}' not found. Available: {names}")

# ==============================================================================
# GUI APP
# ==============================================================================
if GUI_AVAILABLE:
    class ReceiverGrid(Static):
        """Visualizes the received drum pattern"""
        def update_hits(self, grid_state):
            # grid_state: list of 9 rows, each 32 ints (0 or 1)
            lines = []
            voice_names = ["KICK ", "SNRE ", "CLHT ", "OPHT ", "LTOM ", "MTOM ", "HTOM ", "CRSH ", "RIDE "]
            
            # Header
            lines.append("STEP: " + "".join([f"{i%4}" for i in range(32)]))
            lines.append("      " + "-"*32)

            for r, row_data in enumerate(grid_state):
                line = voice_names[r] + "|"
                for val in row_data:
                    line += "█" if val > 0 else "·"
                lines.append(line)
            
            self.update("\n".join(lines))

    class ReceiverApp(App):
        CSS = """
        Screen { layout: grid; grid-size: 2; grid-columns: 1fr 3fr; }
        .box { border: solid green; padding: 1; }
        #log { height: 100%; border: solid yellow; }
        #grid { height: 20; border: solid magenta; }
        #status { height: auto; border: solid blue; }
        """

        def __init__(self, cli_args, in_label, out_label, **kwargs):
            super().__init__(**kwargs)
            self.cli_args = cli_args
            self.in_label = in_label
            self.out_label = out_label

        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical(classes="box"):
                yield Label("CONFIGURATION", id="title")
                yield Label(f"Sender IP:   {self.cli_args.pi_ip}:{self.cli_args.pi_port}")
                yield Label(f"Listening:   {self.cli_args.listen}:{self.cli_args.listen_port}")
                yield Static("\nMIDI PORTS", classes="status_header")
                yield Label(f"IN (Host→Pi): {self.in_label}")
                yield Label(f"OUT (Pi→Host): {self.out_label}")
            
            with Vertical():
                yield ReceiverGrid(id="grid")
                yield Log(id="log")
            
            yield Footer()

        def on_mount(self):
            self.log_widget = self.query_one("#log", Log)
            self.grid_widget = self.query_one("#grid", ReceiverGrid)

        def write_log(self, msg):
            """Thread-safe log writer"""
            self.call_from_thread(self.log_widget.write_line, msg)

        def update_grid(self, grid_state):
            """Thread-safe grid update"""
            self.call_from_thread(self.grid_widget.update_hits, grid_state)

# ==============================================================================
# MAIN
# ==============================================================================
def main():
    global GUI_APP, MIDI_BAR_PLAYER

    ap = argparse.ArgumentParser(description="Host MIDI↔OSC bridge")
    ap.add_argument("--pi-ip", default="127.0.0.1", help="Pi IP")
    ap.add_argument("--pi-port", type=int, default=9001, help="Pi OSC Port")
    ap.add_argument("--listen", default="127.0.0.1", help="Bind IP")
    ap.add_argument("--listen-port", type=int, default=9000, help="Listen Port")
    ap.add_argument("--addr", default="/midi", help="OSC address")
    ap.add_argument("--names", action="store_true", default=True, help="Show note names")
    
    # MIDI
    ap.add_argument("--in-name", default="Host_to_Pi", help="Virtual IN Name")
    ap.add_argument("--in-port", default=None, help="Hardware IN Port")
    ap.add_argument("--out-name", default="Pi_to_Host", help="Virtual OUT Name")
    ap.add_argument("--out-port", default=None, help="Hardware OUT Port")
    
    ap.add_argument("--gui", action="store_true", help="Enable TUI Mode")
    args = ap.parse_args()

    show_hex = False

    # 1. OSC Setup
    osc_client = SimpleUDPClient(args.pi_ip, args.pi_port)
    disp = Dispatcher()
    disp.map("/midi_bar", on_midi_bar)
    
    # OSC Handler (Pi -> Host)
    def on_osc_midi(_addr, *args_):
        if not args_: return
        payload = args_[0]
        if isinstance(payload, (bytes, bytearray)): msg = list(payload)
        elif isinstance(payload, (list, tuple)): msg = list(payload)
        else: return
        
        try: mout.send_message(msg)
        except: pass
        
        desc = parse_msg(msg, args.names)
        line = f"{ts()}  PI→HOST  {desc}"
        if show_hex: line += f"   [{fmt_hex(msg)}]"
        log_info(line)

    disp.map(args.addr, on_osc_midi)

    server = ThreadingOSCUDPServer((args.listen, args.listen_port), disp)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 2. MIDI OUT Setup
    mout = rtmidi.MidiOut()
    out_label = "Unknown"
    if args.out_port is not None:
        ports = mout.get_ports()
        idx = resolve_port_index(ports, args.out_port) # simplified int/str handling
        mout.open_port(idx)
        out_label = ports[idx]
    else:
        try:
            mout.open_virtual_port(args.out_name)
            out_label = f"(virtual) {args.out_name}"
        except:
            log_info("⚠️ Virtual MIDI OUT not supported. Use --out-port.")
            sys.exit(1)
            
    MIDI_BAR_PLAYER = MidiBarPlayer(mout)
    
    # 3. MIDI IN Setup
    def on_host_midi(event, _):
        msg, _t = event
        try: osc_client.send_message(args.addr, [bytes(msg)])
        except: pass
        
        desc = parse_msg(msg, args.names)
        line = f"{ts()}  HOST→PI  {desc}"
        if show_hex: line += f"   [{fmt_hex(msg)}]"
        log_info(line)

    minput = rtmidi.MidiIn()
    in_label = "Unknown"
    if args.in_port is not None:
        ports = minput.get_ports()
        idx = resolve_port_index(ports, args.in_port)
        minput.open_port(idx)
        in_label = ports[idx]
    else:
        try:
            minput.open_virtual_port(args.in_name)
            in_label = f"(virtual) {args.in_name}"
        except:
            log_info("⚠️ Virtual MIDI IN not supported. Use --in-port.")
            sys.exit(1)
            
    minput.set_callback(on_host_midi)

    # 4. Run
    log_info("Ready.")
    log_info(f"  MIDI IN  (host→pi): {in_label}")
    log_info(f"  MIDI OUT (pi→host): {out_label}")
    log_info(f"  OSC send → {args.pi_ip}:{args.pi_port}")
    log_info(f"  OSC recv ← {args.listen}:{args.listen_port}")

    if args.gui and GUI_AVAILABLE:
        GUI_APP = ReceiverApp(args, in_label, out_label)
        GUI_APP.run()
    else:
        if args.gui and not GUI_AVAILABLE:
            print("⚠️ 'textual' not found. Running in CLI mode.")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            minput.close_port()
            mout.close_port()

if __name__ == "__main__":
    main()