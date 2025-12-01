# GrooveTransformer (Python Standalone)

A real-time AI Drum Co-processor designed to run headless on a Raspberry Pi or locally on a computer. It processes your live MIDI drumming ("The Groove") through a Neural Network (GrooveTransformer VAE) and generates a complementary, evolving drum pattern.

The core technology uses the **GrooveTransformer** model developed by **Behance Labs**, adapted here as a standalone Python bridge.

The system uses Open Sound Control (OSC) to send pattern data and control messages over the network, providing low-latency communication with your Digital Audio Workstation (DAW).

## Architecture

The system consists of two main Python scripts communicating over UDP:

1. **`sender.py` (The Brain - Runs on Pi/Local)**

   * Runs the **PyTorch Neural Network** (`drumLoopVAE.pt`).

   * Listens for both **Hardware MIDI** (controller) and **OSC MIDI** (from the receiver).

   * Quantizes MIDI input to the internal 32-step grid.

   * Generates a new 2-bar pattern based on interpolated latent vectors.

   * Sends the final pattern as a binary blob on `/midi_bar`.

2. **`receiver.py` (The Bridge - Runs on DAW Host)**

   * Creates a host-side **Virtual MIDI In/Out** port pair (`Host_to_Pi`, `Pi_to_Host`).

   * **Captures** all MIDI/CC data from your DAW/Controller (via `Host_to_Pi`) and forwards it to the sender via OSC.

   * **Receives** the generated pattern blob from the sender and schedules playback on the `Pi_to_Host` virtual MIDI port.

## Installation

### Prerequisites

* Python 3.8+

* PyTorch (CPU version is recommended for Raspberry Pi)

### Setup Steps

1. **Install Dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

2. **Acquire Model:**

   * Ensure your TorchScript model file (`drumLoopVAE.pt`) is in the same directory as `sender.py`.

3. **Configure `settings.cfg`:**

   * Verify the `[Network]` section (especially `dest_ip`) points to the correct machine.

   * Review the `[Defaults]` section to set the starting levels for Velocity, Threshold, and Max Counts.

   * Adjust the `[CC_Map]` section if your MIDI controller uses different CC numbers for specific knobs/buttons.

## Usage

### 1. Start the Receiver (DAW Computer)

The receiver must be started first to establish the necessary virtual MIDI ports and the OSC listening endpoint.

```bash
# GUI Mode (Recommended for visual monitoring and debugging)
python receiver.py --gui

# Or Terminal Mode (for minimal output)
python receiver.py
```

**DAW Routing (Crucial):**

* **Input (Groove):** Route your MIDI Controller track's MIDI output to the virtual port **`Host_to_Pi`**.

* **Output (Beat):** Route a receiving MIDI track's input from the virtual port **`Pi_to_Host`**.

### 2. Start the Sender (Raspberry Pi / Local)

The sender will automatically try to connect to the receiver's OSC port (default 9000) and establish a MIDI input for your playing.

```bash
# GUI Mode (Recommended for RPi/Local monitoring)
python sender.py --gui

# Headless Mode (For RPi startup script)
python sender.py
```

**MIDI Input Selection:**

* If the `in_port` is set to `None` in `settings.cfg`, the script will prompt you in the terminal to select a connected MIDI hardware port or create a virtual one.

## Control Mapping (MIDI CC / OSC)

The sender uses the `settings.cfg` file to map MIDI Control Change (CC) messages to the neural network's parameters.

| **Parameter** | **Default CC #** | **Control Type** | **Range / Notes** |
| :--- | :--- | :--- | :--- |
| **Interpolate** | 1 | Continuous | Blend between Pattern A (0) and Pattern B (127). |
| **Follow** | 2 | Continuous | Blend between Generated Pattern (0) and Input Groove (127). |
| **Density** | 3 | Continuous | Global target density for the output pattern. |
| **Temperature** | 4 | Continuous | Randomness/Chaos (Low value = conservative, High value = experimental). |
| **Shift** | 5 | Continuous | Time-shift the entire pattern from -16 to +16 steps. |
| **Regenerate A** | 10 | Trigger | Creates a new random latent vector for Pattern A. (Trigger on value > 64) |
| **Regenerate B** | 11 | Trigger | Creates a new random latent vector for Pattern B (Trigger on value > 64) |
| **Reset** | 12 | Trigger | Clears all currently recorded groove data from memory. |

**Per-Voice Parameters:**
Velocity, Threshold, and Max Count for all 9 drum voices are controlled via CC 20-48, as defined in `[CC_Map]` and handled dynamically by `sender.py`.

## Troubleshooting

* **"No MIDI ports found"**: Ensure your MIDI interface is connected before starting the script. On Linux/Pi, ensure your user has permissions to access audio/midi groups.

* **"OSC Send failed"**: Check the `dest_ip` in `settings.cfg`. The Sender must point exactly to the Receiver's IP address.

* **MIDI/CC not received by Pi**: Verify that the MIDI track in your DAW is correctly outputting to the virtual port **`Host_to_Pi`** (created by `receiver.py`).

* **Latency/Jitter**: For best results, use a wired (Ethernet) connection between the Pi and the host machine.