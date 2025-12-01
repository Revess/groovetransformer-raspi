import time
import torch
import rtmidi
import threading
import argparse
import configparser
import sys
import os
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

# --- Optional GUI Imports ---
GUI_AVAILABLE = False
try:
    from textual.app import App, ComposeResult
    from textual.containers import Container, Horizontal, Vertical
    from textual.widgets import Header, Footer, Log, Static, Label, Digits
    from textual.reactive import reactive
    from textual import work
    GUI_AVAILABLE = True
except ImportError:
    pass

# ==============================================================================
# 0. GLOBALS & LOGGING
# ==============================================================================
GUI_APP = None  # Reference to the running Textual App

def log_info(msg):
    """Unified logging: Prints to terminal OR writes to GUI log"""
    if GUI_APP:
        GUI_APP.write_log(str(msg))
    else:
        print(msg)

def load_config(filename="settings.cfg"):
    config = configparser.ConfigParser()
    if not os.path.exists(filename):
        log_info(f"⚠️ Config file '{filename}' not found. Using defaults.")
        return None
    config.read(filename)
    return config

CFG = load_config()

# ==============================================================================
# 1. THE BRAIN: Neural Network Wrapper
# ==============================================================================
class GrooveTransformerModel:
    def __init__(self, model_path="drumLoopVAE.pt"):
        self.device = torch.device("cpu")
        try:
            self.model = torch.jit.load(model_path, map_location=self.device)
            self.model.eval()
            log_info(f"✅ [Brain] Model loaded from {model_path}")
        except Exception as e:
            log_info(f"❌ [Brain] Error loading model: {e}")
            self.model = None

        # --- State Tensors ---
        self.latent_A = torch.randn(1, 128)
        self.latent_B = torch.randn(1, 128)
        self.latent_g = torch.randn(1, 128)
        self.latent_vector = torch.randn(1, 128)

        # --- Global Parameters ---
        self.interpolate = 0.0
        self.follow = 1.0
        self.temperature = 1.0
        self.density = 0.5
        self.shift_steps = 0

        # --- Voice Parameters ---
        self.voice_thresholds = torch.ones(9) * 0.5
        self.max_counts = torch.ones(9) * 32
        self.velocity_scale = torch.ones(9) * 1.0

        # --- Groove Input State ---
        self.groove_hits = torch.zeros(1, 32, 1)
        self.groove_vels = torch.zeros(1, 32, 1)
        self.groove_offs = torch.zeros(1, 32, 1)
        
        # Load Voice Map
        self.voice_names = ["Kick", "Snare", "ClHat", "OpHat", "LoTom", "MidTom", "HiTom", "Crash", "Ride"]
        self.voice_map = [36, 38, 42, 46, 41, 48, 45, 49, 51]
        
        if CFG and 'VoiceMap' in CFG:
            try:
                self.voice_map = [int(CFG['VoiceMap'].get(v, default)) for v, default in zip(self.voice_names, self.voice_map)]
            except: pass

        # --- Apply Defaults from Config ---
        if CFG and 'Defaults' in CFG:
            log_info("⚙️ Applying Defaults from settings.cfg...")
            d = CFG['Defaults']
            try:
                if 'interpolate' in d: self.interpolate = float(d['interpolate'])
                if 'follow' in d: self.follow = float(d['follow'])
                if 'density' in d: self.density = float(d['density'])
                if 'temperature' in d: self.temperature = float(d['temperature'])
                if 'shift' in d: self.shift_steps = int(d['shift'])

                for i, name in enumerate(self.voice_names):
                    v_key = name.lower()
                    if f'vel_{v_key}' in d: self.velocity_scale[i] = float(d[f'vel_{v_key}'])
                    if f'thresh_{v_key}' in d: self.voice_thresholds[i] = float(d[f'thresh_{v_key}'])
                    if f'max_{v_key}' in d: self.max_counts[i] = float(d[f'max_{v_key}'])
                        
                self.update_latents()
            except Exception as e:
                log_info(f"⚠️ Error applying defaults: {e}")

    def update_latents(self):
        mixed = (1.0 - self.interpolate) * self.latent_A + (self.interpolate) * self.latent_B
        self.latent_vector = (1.0 - self.follow) * mixed + (self.follow) * self.latent_g
        # Update GUI if active
        if GUI_APP: GUI_APP.update_params()

    def register_groove_event(self, step_idx, velocity, offset):
        if 0 <= step_idx < 32:
            self.groove_hits[0, step_idx, 0] = 1.0
            self.groove_vels[0, step_idx, 0] = velocity
            self.groove_offs[0, step_idx, 0] = offset

    def encode_groove(self):
        if self.model is None: return
        
        groove_tensor = torch.zeros(1, 32, 27)
        groove_tensor[:, :, 2] = self.groove_hits.squeeze(-1)
        groove_tensor[:, :, 11] = self.groove_vels.squeeze(-1)
        groove_tensor[:, :, 20] = self.groove_offs.squeeze(-1)
        
        density_tensor = torch.tensor([self.density], dtype=torch.float32)
        
        try:
            with torch.no_grad():
                outputs = self.model.encode(groove_tensor, density_tensor)
                self.latent_g = outputs[2] 
                self.update_latents()
        except Exception:
            pass

    def generate(self):
        if self.model is None: return []
        with torch.no_grad():
            outputs = self.model.sample(
                self.latent_vector,
                self.voice_thresholds,
                self.max_counts,
                0,                 
                self.temperature   
            )
        hits, vels, offs = outputs[0], outputs[1], outputs[2]
        
        if self.shift_steps != 0:
            hits = torch.roll(hits, shifts=int(self.shift_steps), dims=1)
            vels = torch.roll(vels, shifts=int(self.shift_steps), dims=1)
            offs = torch.roll(offs, shifts=int(self.shift_steps), dims=1)

        notes = []
        for step in range(32):
            for v_idx in range(9):
                if hits[0, step, v_idx] > 0.5:
                    raw_vel = vels[0, step, v_idx].item()
                    offset = offs[0, step, v_idx].item()
                    scaled_vel = raw_vel * self.velocity_scale[v_idx].item()
                    ppq = (step + offset) * 0.25
                    note_num = self.voice_map[v_idx]
                    
                    notes.append({
                        "step": step,
                        "ppq": ppq,
                        "note": note_num,
                        "vel": int(min(127, max(1, scaled_vel * 127)))
                    })
        notes.sort(key=lambda x: x["ppq"])
        return notes, hits # Return raw hits for GUI visualization

    def resolve_voice_index(self, v_id):
        if isinstance(v_id, int): return v_id if 0 <= v_id < 9 else None
        if isinstance(v_id, str): return {n.lower():i for i,n in enumerate(self.voice_names)}.get(v_id.lower())
        return None

# ==============================================================================
# 2. HELPERS
# ==============================================================================
def pack_bar_blob(notes, ppq_per_beat=480):
    out = bytearray()
    for n in notes:
        ticks = int(n["ppq"] * ppq_per_beat)
        ticks = max(0, min(65535, ticks))
        msg = bytes([0x90, n["note"], n["vel"]])
        out.append(ticks & 0xFF)
        out.append((ticks >> 8) & 0xFF)
        out.append(len(msg))
        out.extend(msg)
    return bytes(out)

# ==============================================================================
# 3. TRANSPORT
# ==============================================================================
class Transport:
    def __init__(self, osc_client, brain, bpm=120.0):
        self.osc_client = osc_client
        self.brain = brain
        self.bpm = bpm
        self.running = True
        self.bar_count = 0
        self.start_time = time.time()
        
    def get_current_step(self):
        now = time.time()
        elapsed = now - self.start_time
        beats = elapsed * (self.bpm / 60.0)
        total_steps = beats * 4 
        wrapped_step = int(total_steps) % 32
        offset = total_steps - int(total_steps)
        return wrapped_step, offset

    def run(self):
        log_info(f"🚀 [Transport] Loop running at {self.bpm} BPM")
        self.start_time = time.time()
        
        while self.running:
            notes, raw_hits = self.brain.generate()
            blob = pack_bar_blob(notes)
            
            log_info(f"   -> Sending Bar {self.bar_count} ({len(notes)} notes)")
            
            # Update GUI Pattern
            if GUI_APP: GUI_APP.update_grid(raw_hits)

            try:
                self.osc_client.send_message("/midi_bar", [
                    self.bar_count, int(self.bpm * 100), 4, 4, 480, blob
                ])
            except Exception as e:
                log_info(f"   ⚠️ Send failed: {e}")

            seconds_per_loop = (60.0 / self.bpm) * 8.0
            time.sleep(seconds_per_loop)
            self.bar_count += 1
            self.start_time = time.time() 

# ==============================================================================
# 4. HANDLERS
# ==============================================================================
brain_ref = None 
transport_ref = None
cc_mapping = {}

if CFG and 'CC_Map' in CFG:
    for key, val in CFG['CC_Map'].items():
        try: cc_mapping[int(key)] = val
        except ValueError: pass

def process_cc(cc_num, value):
    global brain_ref
    if not brain_ref: return
    
    log_info(f"🎛 CC IN: {cc_num} | Val {value}")
    param_name = cc_mapping.get(cc_num)
    if not param_name: return

    norm_val = value / 127.0
    log_info(f"   ↳ {param_name}: {norm_val:.2f}")

    if param_name == 'interpolate':
        brain_ref.interpolate = norm_val
        brain_ref.update_latents()
    elif param_name == 'follow':
        brain_ref.follow = norm_val
        brain_ref.update_latents()
    elif param_name == 'density':
        brain_ref.density = norm_val
        brain_ref.encode_groove()
    elif param_name == 'temperature':
        brain_ref.temperature = 0.1 + (norm_val * 2.0)
    elif param_name == 'shift':
        brain_ref.shift_steps = int((norm_val * 32) - 16)
    
    # Triggers
    elif param_name == 'regenerate_A' and value > 64:
        brain_ref.latent_A = torch.randn(1, 128)
        brain_ref.update_latents()
        log_info("🎲 Regen A")
    elif param_name == 'regenerate_B' and value > 64:
        brain_ref.latent_B = torch.randn(1, 128)
        brain_ref.update_latents()
        log_info("🎲 Regen B")
    elif param_name == 'reset' and value > 64:
        brain_ref.groove_hits.zero_()
        brain_ref.groove_vels.zero_()
        brain_ref.groove_offs.zero_()
        brain_ref.encode_groove()
        log_info("🧹 Groove Reset")

    elif param_name.startswith('vel_'):
        voice = param_name.split('_')[1]
        for i, name in enumerate(brain_ref.voice_names):
            if name.lower() == voice.lower(): brain_ref.velocity_scale[i] = norm_val
    elif param_name.startswith('thresh_'):
        voice = param_name.split('_')[1]
        for i, name in enumerate(brain_ref.voice_names):
            if name.lower() == voice.lower(): brain_ref.voice_thresholds[i] = norm_val
    elif param_name.startswith('max_'):
        voice = param_name.split('_')[1]
        for i, name in enumerate(brain_ref.voice_names):
            if name.lower() == voice.lower(): brain_ref.max_counts[i] = norm_val * 32.0

def process_input_note(source_label, note, velocity):
    global brain_ref, transport_ref
    if not brain_ref or not transport_ref: return

    step, offset = transport_ref.get_current_step()
    log_info(f"🎹 {source_label}: Note {note} Vel {velocity} -> Step {step}")
    
    vel_norm = velocity / 127.0
    brain_ref.register_groove_event(step, vel_norm, 0.0)
    brain_ref.encode_groove()

def osc_handler(address, *args):
    # Pass through for direct OSC control logic if needed
    # (Simplified to use process_cc logic pattern or existing code)
    pass

def osc_midi_handler(address, *args):
    if len(args) == 1 and isinstance(args[0], bytes):
        msg = list(args[0])
        if len(msg) >= 3:
            status = msg[0] & 0xF0
            if status == 0x90 and msg[2] > 0:
                process_input_note("OSC (Blob)", msg[1], msg[2])
            elif status == 0xB0:
                process_cc(msg[1], msg[2])

def midi_callback(event, data):
    msg, _ = event
    status = msg[0] & 0xF0
    if status == 0x90 and msg[2] > 0:
        process_input_note("MIDI PORT", msg[1], msg[2])
    elif status == 0xB0:
        process_cc(msg[1], msg[2])

# ==============================================================================
# 5. TEXTUAL GUI
# ==============================================================================
if GUI_AVAILABLE:
    class PatternGrid(Static):
        """Visualizes the 9x32 drum grid"""
        grid_data = reactive("Waiting for data...")

        def update_hits(self, hit_tensor):
            # hit_tensor shape: [1, 32, 9]
            lines = []
            voice_names = ["KICK ", "SNRE ", "CLHT ", "OPHT ", "LTOM ", "MTOM ", "HTOM ", "CRSH ", "RIDE "]
            
            # Top Border (Step markers)
            lines.append("STEP: " + "".join([f"{i%4}" for i in range(32)]))
            lines.append("      " + "-"*32)

            for v_idx in range(9):
                row = voice_names[v_idx] + "|"
                for step in range(32):
                    val = hit_tensor[0, step, v_idx].item()
                    row += "█" if val > 0.5 else "·"
                lines.append(row)
            
            self.update("\n".join(lines))

    class GrooveApp(App):
        CSS = """
        Screen { layout: grid; grid-size: 2; grid-columns: 1fr 3fr; }
        .box { border: solid green; padding: 1; }
        #log { height: 100%; border: solid yellow; }
        #status { height: auto; border: solid blue; }
        #grid { height: 20; border: solid magenta; }
        """
        
        # Reactive State
        mix_val = reactive(0.0)
        follow_val = reactive(1.0)
        density_val = reactive(0.5)
        temp_val = reactive(1.0)

        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical(classes="box"):
                yield Label("PARAMETERS", id="title")
                yield Label(f"Mix (A-B): {self.mix_val:.2f}", id="lbl_mix")
                yield Label(f"Follow:    {self.follow_val:.2f}", id="lbl_follow")
                yield Label(f"Density:   {self.density_val:.2f}", id="lbl_dens")
                yield Label(f"Temp:      {self.temp_val:.2f}", id="lbl_temp")
                yield Static("\nSTATUS", classes="status_header")
                yield Label(f"IP: {CFG['Network']['dest_ip']}:{CFG['Network']['dest_port']}")
                yield Label(f"Listen: {CFG['Network']['osc_port']}")
            
            with Vertical():
                yield PatternGrid(id="patterngrid")
                yield Log(id="log")
            
            yield Footer()

        def on_mount(self):
            self.log_widget = self.query_one("#log", Log)
            self.grid_widget = self.query_one("#patterngrid", PatternGrid)
            
            # --- FIX: Call internal method directly (we are already on UI thread) ---
            self._update_reactive_vars() 
            # -----------------------------------------------------------------------

        def write_log(self, msg):
            """Thread-safe log writer"""
            self.call_from_thread(self.log_widget.write_line, msg)

        def update_grid(self, hits):
            """Thread-safe grid update"""
            self.call_from_thread(self.grid_widget.update_hits, hits)

        def update_params(self):
            """Update labels from brain state (For Background Threads)"""
            if not brain_ref: return
            self.call_from_thread(self._update_reactive_vars)

        def _update_reactive_vars(self):
            """Internal update logic (Must run on UI Thread)"""
            self.mix_val = brain_ref.interpolate
            self.follow_val = brain_ref.follow
            self.density_val = brain_ref.density
            self.temp_val = brain_ref.temperature
            
            # Force refresh labels
            self.query_one("#lbl_mix", Label).update(f"Mix (A-B): {self.mix_val:.2f}")
            self.query_one("#lbl_follow", Label).update(f"Follow:    {self.follow_val:.2f}")
            self.query_one("#lbl_dens", Label).update(f"Density:   {self.density_val:.2f}")
            self.query_one("#lbl_temp", Label).update(f"Temp:      {self.temp_val:.2f}")
# ==============================================================================
# 6. MAIN
# ==============================================================================
def main():
    global brain_ref, transport_ref, GUI_APP
    
    # --- Arguments ---
    def_dest_ip = CFG['Network']['dest_ip'] if CFG else "127.0.0.1"
    def_dest_port = int(CFG['Network']['dest_port']) if CFG else 9000
    def_osc_port = int(CFG['Network']['osc_port']) if CFG else 9001
    def_in_port = CFG['MIDI'].get('in_port', None) if CFG and 'MIDI' in CFG else None

    parser = argparse.ArgumentParser()
    parser.add_argument("--dest-ip", default=def_dest_ip, help="Receiver IP")
    parser.add_argument("--dest-port", type=int, default=def_dest_port, help="Receiver Port")
    parser.add_argument("--osc-port", type=int, default=def_osc_port, help="Listen for OSC port")
    parser.add_argument("--in-port", default=def_in_port, help="MIDI Input Name")
    parser.add_argument("--gui", action="store_true", help="Enable TUI Mode")
    args = parser.parse_args()

    if args.in_port == "None": args.in_port = None

    # --- Setup System ---
    brain = GrooveTransformerModel("drumLoopVAE.pt")
    brain_ref = brain
    
    client = SimpleUDPClient(args.dest_ip, args.dest_port)
    transport = Transport(client, brain)
    transport_ref = transport

    dispatcher = Dispatcher()
    dispatcher.map("/midi", osc_midi_handler)
    dispatcher.map("/*", osc_handler)
    server = ThreadingOSCUDPServer(("0.0.0.0", args.osc_port), dispatcher)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log_info(f"👂 Listening for OSC on port {args.osc_port}")

    # --- MIDI ---
    midi_in = rtmidi.MidiIn()
    available_ports = midi_in.get_ports()
    
    # Auto-select or Ask (CLI mode only for asking)
    if args.in_port is not None:
        try:
            midi_in.open_virtual_port(args.in_port)
            log_info(f"✅ Virtual Port: '{args.in_port}'")
        except:
            # Try find hardware match
            found = False
            for i, name in enumerate(available_ports):
                if args.in_port.lower() in name.lower():
                    midi_in.open_port(i)
                    log_info(f"✅ Hardware Port: '{name}'")
                    found = True; break
            if not found: log_info(f"⚠️ Port '{args.in_port}' not found.")

    elif not args.gui: # Only ask interactively if NOT in GUI mode
        print("\n🎹 MIDI Inputs:")
        for i, p in enumerate(available_ports): print(f"[{i}] {p}")
        print("[v] Virtual")
        try:
            u = input("Select: ").strip()
            if u == 'v' or u == '': midi_in.open_virtual_port("GrooveSender_Input")
            elif u.isdigit(): midi_in.open_port(int(u))
        except: pass
    else:
        # Default for GUI mode without specific arg: Virtual
        try: midi_in.open_virtual_port("GrooveSender_Input")
        except: pass

    midi_in.set_callback(midi_callback)

    # --- RUN ---
    if args.gui and GUI_AVAILABLE:
        # Start Transport in Background Thread
        t_thread = threading.Thread(target=transport.run, daemon=True)
        t_thread.start()
        
        # Start GUI in Main Thread
        GUI_APP = GrooveApp()
        GUI_APP.run()
    else:
        # Blocking Transport (CLI Mode)
        if args.gui and not GUI_AVAILABLE:
            print("⚠️ 'textual' lib not found. Running in CLI mode.")
        try:
            transport.run()
        except KeyboardInterrupt:
            print("Stopping...")
            midi_in.close_port()

if __name__ == "__main__":
    main()