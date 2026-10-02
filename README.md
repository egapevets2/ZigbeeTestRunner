# Zigbee HIL Test Runner

A Python test utility that replaces PuTTY on the PC host. It communicates with the **Zigbee Coordinator** (`Coordinator`) via USB-Serial to validate end-to-end wireless mesh commands down to the **`TXRXproto`** test fixture.

---

## Quick Start

### 1. Requirements
Ensure `pyserial` is installed (already installed on this system):
```powershell
pip install -r requirements.txt
```

### 2. Run Interactively
Launch the menu-driven runner:
```powershell
python test_runner.py
```
It will automatically scan for connected COM ports, let you choose the Coordinator port, and present an interactive test menu:
```text
===============================
  Zigbee HIL Test Runner Menu
===============================
  [1] Run 'blinkx' Verification Test
  [2] Request Network Report (GiveNetworkReport)
  [3] Ping Network (PingNetwork)
  [4] Interactive PuTTY Console Mode
  [5] Change Target Node Name (Current: Kitchen)
  [0] Exit
===============================
```

### 3. Run Directly from Command Line
Run an automated `blinkx` test targeting a specific device and COM port:
```powershell
# Test 'Kitchen' with 5 blinks on COM11
python test_runner.py --port COM11 --target Kitchen --count 5 --auto
```

---

## Features

- **Automated `blinkx` Verification:** Sends `<Target> blinkx <count>` over the Zigbee network, waits for execution, and logs operator confirmation.
- **Interactive PuTTY Mode:** Replaces PuTTY with a clean console for sending raw coordinator commands (`GiveNetworkReport`, `PingNetwork`, `ResetCoordinator`, etc.).
- **Live Output Streaming:** Background serial listener streams incoming coordinator acknowledgments (`< Kitchen: [ACK]`, `< Kitchen: [PONG ...]`) in real time.
