import argparse
from pythonosc.udp_client import SimpleUDPClient

# ==============================================================================
# CONFIG
# ==============================================================================
# Default to localhost if testing locally, or change to Pi's IP
DEFAULT_IP = "127.0.0.1" 
DEFAULT_PORT = 9001  # sender.py listens on this port

# ==============================================================================
# HELPER MAPPINGS (Shortcuts)
# ==============================================================================
COMMANDS = {
    "mix": "/mix",
    "follow": "/follow",
    "density": "/density",
    "temp": "/temperature",
    "shift": "/shift",
    "regen_a": "/regenerate_A",
    "regen_b": "/regenerate_B",
    "reset": "/reset",
    
    # Virtual "CC" command to simulate a knob turn
    # Usage: cc <number> <value>
    # Note: This sends a fake OSC message that sender.py interprets as a CC
    # sender.py expects: /midi [status=176, cc_num, value]
}

def print_help():
    print("\n🎛  GrooveTransformer CC Tester")
    print("---------------------------------------------------")
    print("Commands:")
    print("  mix <0.0-1.0>       -> Set Interpolation (A-B)")
    print("  follow <0.0-1.0>    -> Set Follow Amount")
    print("  density <0.0-1.0>   -> Set Density")
    print("  temp <val>          -> Set Temperature (e.g. 1.0)")
    print("  shift <-16 to 16>   -> Shift Pattern steps")
    print("  regen_a             -> Regenerate Pattern A")
    print("  regen_b             -> Regenerate Pattern B")
    print("  reset               -> Clear Groove")
    print("  cc <num> <val>      -> Send Raw CC (0-127)")
    print("  quit                -> Exit")
    print("---------------------------------------------------")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", default=DEFAULT_IP, help="Sender IP (Pi)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Sender Port")
    args = parser.parse_args()

    client = SimpleUDPClient(args.ip, args.port)
    
    print(f"🔌 Connected to Sender at {args.ip}:{args.port}")
    print_help()

    while True:
        try:
            user_input = input("\n> ").strip().split()
            if not user_input: continue
            
            cmd = user_input[0].lower()
            
            if cmd == "quit":
                break
                
            elif cmd == "help":
                print_help()

            # --- RAW CC SIMULATION ---
            elif cmd == "cc":
                if len(user_input) < 3:
                    print("Usage: cc <number> <value>")
                    continue
                try:
                    cc_num = int(user_input[1])
                    val = int(user_input[2])
                    # Construct MIDI CC Message: [Status=0xB0 (176), CC#, Value]
                    msg = bytes([0xB0, cc_num, val])
                    client.send_message("/midi", [msg])
                    print(f"📡 Sent CC {cc_num} = {val}")
                except ValueError:
                    print("❌ Invalid numbers")

            # --- SHORTCUT COMMANDS ---
            elif cmd in COMMANDS:
                osc_addr = COMMANDS[cmd]
                
                # Handling arguments (if any)
                if len(user_input) > 1:
                    try:
                        # Try parsing as float or int
                        arg_val = float(user_input[1])
                        client.send_message(osc_addr, arg_val)
                        print(f"📡 Sent {osc_addr} {arg_val}")
                    except ValueError:
                        print("❌ Invalid argument (number required)")
                else:
                    # Triggers (regen, reset) take no args usually, or dummy
                    client.send_message(osc_addr, 1.0)
                    print(f"📡 Sent {osc_addr}")
            
            else:
                print(f"❓ Unknown command: {cmd}")

        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except Exception as e:
            print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()