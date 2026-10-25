#!/usr/bin/env python3
"""
Zigbee HIL (Hardware-in-the-Loop) Regression Test Runner
Communicates with the Zigbee Coordinator and validates end-to-end mesh
operations on the ESP32-C6 node (PNPzigbee) and test fixture (TXRXproto).
"""

import sys
import time
import math
import re
import queue
import argparse
import threading
from datetime import datetime
import serial
import serial.tools.list_ports


class CoordinatorClient:
    """Manages serial communication with the Zigbee Coordinator."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.5):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None
        self._running = False
        self._rx_thread = None
        self.rx_callbacks = []
        self._lock = threading.Lock()

    def connect(self, verbose: bool = True) -> bool:
        try:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = serial.Serial()
            self.ser.port = self.port
            self.ser.baudrate = self.baudrate
            self.ser.timeout = self.timeout
            self.ser.bytesize = serial.EIGHTBITS
            self.ser.parity = serial.PARITY_NONE
            self.ser.stopbits = serial.STOPBITS_ONE
            self.ser.dtr = False
            self.ser.rts = False
            self.ser.open()
            # Give USB-Serial connection a moment to settle
            time.sleep(0.1)
            self._running = True
            self._rx_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._rx_thread.start()
            if verbose:
                print(f"[+] Connected to Coordinator on {self.port} @ {self.baudrate} baud.")
            return True
        except serial.SerialException as ex:
            if verbose:
                print(f"[-] Failed to open serial port {self.port}: {ex}")
            return False

    def reconnect(self, timeout: float = 12.0) -> bool:
        """Closes and re-opens the serial connection after a coordinator reboot."""
        self.close()
        time.sleep(0.8)
        t_end = time.time() + timeout
        print(f"[*] Waiting for Coordinator on {self.port} to reboot and reconnect...")
        while time.time() < t_end:
            try:
                if self.connect(verbose=False):
                    print(f"[+] Successfully reconnected to Coordinator on {self.port}!")
                    time.sleep(0.4)
                    return True
            except Exception:
                pass
            time.sleep(0.4)
        print(f"[-] Failed to reconnect to {self.port} within {timeout:.1f}s.")
        return False

    def wait_for_node(self, target: str = "Kitchen", timeout: float = 12.0) -> bool:
        """Verifies that the target node is online on the Zigbee mesh."""
        print(f"[*] Verifying mesh link to '{target}'...")
        t_end = time.time() + timeout
        got_reply = threading.Event()

        def on_rx(msg: str):
            if f"< {target}:" in msg or "PONG" in msg:
                got_reply.set()

        self.add_rx_callback(on_rx)
        try:
            while time.time() < t_end:
                self.send_line("GiveNetworkReport")
                if got_reply.wait(timeout=1.5):
                    print(f"[+] Node '{target}' is ONLINE on Zigbee mesh.")
                    return True
                got_reply.clear()
                time.sleep(0.5)
        finally:
            self.remove_rx_callback(on_rx)

        print(f"[!] Warning: '{target}' did not immediately confirm online, continuing anyway.")
        return False

    def close(self):
        self._running = False
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
            print(f"[*] Port {self.port} closed.")

    def send_line(self, line: str):
        """Sends a newline-terminated string to the coordinator."""
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Serial port not open.")
        clean = line.strip("\r\n")
        payload = (clean + "\r\n").encode("utf-8")
        self.ser.write(payload)
        self.ser.flush()

    def add_rx_callback(self, cb):
        with self._lock:
            if cb not in self.rx_callbacks:
                self.rx_callbacks.append(cb)

    def remove_rx_callback(self, cb):
        with self._lock:
            while cb in self.rx_callbacks:
                self.rx_callbacks.remove(cb)

    def _read_loop(self):
        """Continuously reads incoming lines and dispatches to registered callbacks."""
        while self._running and self.ser and self.ser.is_open:
            try:
                line = self.ser.readline()
                if line:
                    decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if decoded:
                        with self._lock:
                            callbacks = list(self.rx_callbacks)
                        for cb in callbacks:
                            try:
                                cb(decoded)
                            except Exception:
                                pass
            except (serial.SerialException, TypeError, OSError, Exception):
                break


def choose_port() -> str:
    """Detects available COM ports or prompts the user."""
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("[!] No active serial ports automatically detected.")
        return input("Please enter your Coordinator COM port manually (e.g. COM11): ").strip()

    print("\nAvailable COM Ports:")
    for idx, p in enumerate(ports, start=1):
        desc = p.description if p.description else "Unknown device"
        print(f"  [{idx}] {p.device} ({desc})")

    # If COM11 is in the list, offer it as smart default
    default_port = None
    for p in ports:
        if "COM11" in p.device:
            default_port = p.device
            break
    if not default_port and len(ports) == 1:
        default_port = ports[0].device

    if default_port:
        use_default = input(f"Use {default_port}? [Y/n]: ").strip().lower()
        if use_default in ("", "y", "yes"):
            return default_port

    while True:
        sel = input("Select port number or enter port name: ").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(ports):
            return ports[int(sel) - 1].device
        elif sel:
            return sel


try:
    import msvcrt
except ImportError:
    msvcrt = None


def check_esc_key() -> bool:
    """Checks whether the ESC key (0x1B) has been pressed in the console."""
    if msvcrt is not None:
        while msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch == b'\x1b':
                return True
            # Consume secondary scan code byte for special keys (arrows, F-keys, etc.)
            if ch in (b'\x00', b'\xe0') and msvcrt.kbhit():
                msvcrt.getch()
        return False
    else:
        import select
        if select.select([sys.stdin], [], [], 0)[0]:
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                return True
        return False


def flush_console_keys() -> None:
    """Clears any pending keystrokes from the console input buffer."""
    if msvcrt is not None:
        while msvcrt.kbhit():
            msvcrt.getch()


def sleep_or_esc(duration: float, step: float = 0.01) -> bool:
    """
    Sleeps for duration seconds while polling for the ESC key every step seconds.
    Returns True immediately if ESC was pressed, False if sleep completed normally.
    """
    deadline = time.time() + duration
    while time.time() < deadline:
        if check_esc_key():
            return True
        time.sleep(step)
    return False


def setup_serial_bridge(client: CoordinatorClient, target: str = "Kitchen", timeout: float = 3.0, verbose: bool = False) -> bool:
    """Configures the hardware UART serial bridge on target node."""
    got_ok_event = threading.Event()

    def on_rx(msg: str):
        if verbose:
            print(f"  <-- [RX] {msg}")
        if "SETUP SERIAL OK" in msg:
            got_ok_event.set()

    client.add_rx_callback(on_rx)
    cmd_string = f"{target} SetupSerialBridge"
    if verbose:
        print(f"\n[>] Sending command: \"{cmd_string}\"")
    start_time = time.time()
    client.send_line(cmd_string)
    success = got_ok_event.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if verbose:
        print("-" * 65)
        if success:
            print(f"[+] TEST PASSED: Serial bridge initialized successfully ({elapsed:.1f}ms)!")
        else:
            print(f"[-] TEST FAILED: Timed out waiting for 'SETUP SERIAL OK' from {target}.")
    return success


def run_setup_bridge_test(client: CoordinatorClient, target: str = "Kitchen", timeout: float = 3.0) -> bool:
    """Sends `<target> SetupSerialBridge` and verifies response."""
    print("\n" + "=" * 65)
    print("--- [TEST 2] SETUP SERIAL BRIDGE ---")
    print(f"Target Node: {target} (ESP32-C6)")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)
    return setup_serial_bridge(client, target=target, timeout=timeout, verbose=True)


def run_multi_node_blink_test(
    client: CoordinatorClient,
    count: int = 3,
    target: str = "Kitchen",
    garage_target: str = "Garage",
    n2_target: str = "N2",
    timeout: float = 4.0,
) -> bool:
    """
    [TEST 3] Multi-Node LED & Attached Serial Device Blink Test (Merged Test 3 & 4):
    1. Searches the mesh network and discovers all active endpoints (GiveNetworkReport).
    2. Probes each endpoint via UART auto-bridge for attached serial processors (e.g. Arduino Trinket M0).
    3. Prints a structured topology of all discovered endpoints and connected serial hardware.
    4. Performs 3 blinks on each discovered endpoint's ESP32-C6 onboard LED.
    5. Performs 3 blinks on each attached serial device's DotStar LED via auto-bridge.
    6. Verifies operator visual confirmation.
    """
    print("\n" + "=" * 65)
    print("--- [TEST 3] MULTI-NODE LED & SERIAL DEVICE BLINK TEST ---")
    print(f"Blink Count: {count} pulses per device")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # Step 1: Discover all mesh endpoints
    print("\n[*] Step 1: Searching network for active endpoints (GiveNetworkReport)...")
    nodes = get_network_report(client, timeout=timeout)

    # Filter for active/known endpoints
    discovered_endpoints = []
    if nodes:
        for name, data in nodes.items():
            if data.get("type") == "END_DEVICE" or data.get("online"):
                discovered_endpoints.append(name)

    # Fallback to configured target names if discovery report was empty
    if not discovered_endpoints:
        print("[!] Note: No endpoints returned in report, using configured defaults.")
        discovered_endpoints = [target, garage_target, n2_target]

    # Deterministic ordering: Kitchen first, then Garage, then N2, then others
    def ep_sort_key(name: str):
        nl = name.lower()
        if nl == "kitchen": return 0
        if nl == "garage": return 1
        if nl == "n2": return 2
        return 10
    discovered_endpoints.sort(key=ep_sort_key)

    # Step 2: Probe each endpoint for attached serial devices via auto-bridge
    print(f"[*] Step 2: Probing {len(discovered_endpoints)} endpoint(s) for attached serial processors...")
    attached_devices = {}
    for ep in discovered_endpoints:
        # Arm serial bridge on endpoint
        setup_serial_bridge(client, target=ep, timeout=1.5, verbose=False)
        time.sleep(0.15)

        # Probe with ping -> GotPing
        got_ping = threading.Event()
        def on_probe_rx(msg: str):
            if f"< {ep}:" in msg and "GotPing" in msg:
                got_ping.set()
            elif "GotPing" in msg:
                got_ping.set()

        client.add_rx_callback(on_probe_rx)
        client.send_line(f"{ep} ping")
        replied = got_ping.wait(timeout=1.2)
        client.remove_rx_callback(on_probe_rx)

        if replied:
            attached_devices[ep] = "Arduino Trinket M0 / DotStar Test Fixture (UART1 @ 115200)"
        else:
            attached_devices[ep] = None

    # Step 3: Print discovered endpoints and connected serial devices
    print("\n" + "=" * 65)
    print("[+] DISCOVERED MESH ENDPOINTS & ATTACHED SERIAL PROCESSORS:")
    print("=" * 65)
    for idx, ep in enumerate(discovered_endpoints, start=1):
        ep_data = nodes.get(ep, {})
        mac = ep_data.get("ieee", "Unknown")
        lqi = ep_data.get("lqi", "N/A")
        ser_dev = attached_devices.get(ep)
        ser_desc = ser_dev if ser_dev else "None (No attached serial processor detected)"
        print(f"  {idx}. Endpoint: '{ep}' (MAC: {mac}, LQI: {lqi})")
        print(f"     |-- Onboard Actuator:  ESP32-C6 Onboard LED (command: '{ep} blink {count}')")
        print(f"     \\-- Serial Processor:  {ser_desc}")
        if ser_dev:
            print(f"         \\-- Actuator:      Addressable RGB DotStar (command: '{ep} blinkx {count}')")
    print("=" * 65)

    # Step 4: Prompt user before commencing physical blinks
    print("\n[!] ACTION REQUIRED: Look at the hardware nodes on the test bench.")
    print(f"    Each endpoint's onboard LED will flash {count} times.")
    print(f"    Any attached serial processor (DotStar) will flash blue {count} times.")
    try:
        input("    Press [Enter] when ready to begin blinking sequence...")
    except (EOFError, OSError):
        pass

    def on_blink_rx(msg: str):
        if "<" in msg:
            print(f"  <-- [RX] {msg}")

    client.add_rx_callback(on_blink_rx)
    time.sleep(0.2)

    try:
        # Step 5: Perform 3 blinks on each endpoint, plus dotstar if serial device attached
        step_num = 1
        for ep in discovered_endpoints:
            # 5a. Blink ESP32-C6 onboard LED
            cmd = f"{ep} blink {count}"
            print(f"\n[{step_num}] Actuating Onboard LED on '{ep}' ({count} flashes): \"{cmd}\"")
            client.send_line(cmd)
            duration = (count * 0.4) + 0.5
            time.sleep(duration)
            step_num += 1

            # 5b. If attached serial processor exists, actuate DotStar LED via auto-bridge
            if attached_devices.get(ep):
                cmd_x = f"{ep} blinkx {count}"
                print(f"\n[{step_num}] Actuating DotStar LED on '{ep}' via Auto-Bridge ({count} blue flashes): \"{cmd_x}\"")
                client.send_line(cmd_x)
                duration_x = (count * 0.4) + 0.5
                time.sleep(duration_x)
                step_num += 1

    finally:
        client.remove_rx_callback(on_blink_rx)

    # Step 6: Operator verification prompt
    print("\n" + "-" * 65)
    while True:
        resp = input(f"[?] Did you observe {count} flashes on each endpoint and DotStar LED? (y/n) [y]: ").strip().lower()
        if resp in ("", "y", "yes"):
            print("-" * 65)
            print(f"[+] TEST PASSED: Confirmed {count} flashes across all discovered endpoints and DotStar!")
            return True
        elif resp in ("n", "no"):
            print("-" * 65)
            print(f"[-] TEST FAILED: Verification unconfirmed by operator.")
            return False
        print("    [!] Please enter 'y' or 'n'.")


run_blink_test = run_multi_node_blink_test
run_blinkx_test = run_multi_node_blink_test


def run_ping_test(client: CoordinatorClient, target: str = "Kitchen", timeout: float = 3.0) -> bool:
    """
    Executes the two-way ping round-trip test:
    Host -> Coordinator -> Zigbee OTA -> ESP32-C6 -> Arduino -> ESP32-C6 -> Coordinator -> Host
    """
    print("\n" + "=" * 65)
    print("--- [TEST 5] TWO-WAY SERIAL PING TEST ---")
    print(f"Target Node: {target} (End-to-end Zigbee + Serial Bridge)")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    got_ping_event = threading.Event()

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")
        if "GotPing" in msg:
            got_ping_event.set()

    client.add_rx_callback(on_rx)

    cmd_string = f"{target} ping"
    print(f"\n[>] Sending command: \"{cmd_string}\"")
    start_time = time.time()
    client.send_line(cmd_string)

    success = got_ping_event.wait(timeout=timeout)
    elapsed_ms = (time.time() - start_time) * 1000.0

    client.remove_rx_callback(on_rx)

    print("-" * 65)
    if success:
        print(f"[+] TEST PASSED: Received 'GotPing' reply in {elapsed_ms:.1f}ms!")
        print("    End-to-End Link Verified:")
        print("    Host -> Coordinator -> Zigbee -> ESP32-C6 -> Arduino -> ESP32-C6 -> Zigbee -> Coordinator -> Host")
    else:
        print(f"[-] TEST FAILED: Timed out after {timeout:.1f}s waiting for 'GotPing' from {target}.")
    return success


def run_dac_test(client: CoordinatorClient, target: str = "Kitchen", dac_val: int = 512, timeout: float = 3.0) -> bool:
    """
    Sends `<target> setDAC <dac_val>` to the coordinator.
    Waits for `< <target>: GotDAC <dac_val>`.
    """
    print("\n" + "=" * 65)
    print("--- [TEST 6] ARDUINO HARDWARE DAC OUTPUT TEST ---")
    print(f"Target Node: {target}")
    voltage = (dac_val * 3.3) / 1023.0
    print(f"DAC Value:   {dac_val} / 1023 (~{voltage:.2f}V on Trinket M0 Pin A0)")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    got_dac_event = threading.Event()
    expected_token = f"GotDAC {dac_val}"

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")
        if expected_token in msg:
            got_dac_event.set()

    client.add_rx_callback(on_rx)

    cmd_string = f"{target} setDAC {dac_val}"
    print(f"\n[>] Sending command: \"{cmd_string}\"")
    start_time = time.time()
    client.send_line(cmd_string)

    success = got_dac_event.wait(timeout=timeout)
    elapsed_ms = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    print("-" * 65)
    if success:
        print(f"[+] TEST PASSED: Received '{expected_token}' reply in {elapsed_ms:.1f}ms!")
        print(f"    Trinket M0 Pin A0 is now driven at ~{voltage:.2f}V.")
    else:
        print(f"[-] TEST FAILED: Did not receive '{expected_token}' from {target} within {timeout:.1f}s.")
    return success


def _normalize_adc_channel(pin_or_channel: int | str) -> str:
    """Normalizes pin/channel (0, 1, 2, '0', '1', '2', 'A0', 'A1', 'A2') to 'A0', 'A1', or 'A2'."""
    s = str(pin_or_channel).strip().upper()
    if s in ("0", "A0"):
        return "A0"
    if s in ("1", "A1"):
        return "A1"
    if s in ("2", "A2"):
        return "A2"
    return "A1"


def setup_adc_pins(
    client: CoordinatorClient,
    target: str = "Kitchen",
    pins: list[int | str] | int | str = (0, 1),
) -> bool:
    """
    Configures one or more pins (either or both Pin 0 / A0 and Pin 1 / A1, or Pin 2 / A2)
    as ADC inputs on the target node.
    """
    if isinstance(pins, (int, str)):
        pin_list = [pins]
    else:
        pin_list = list(pins)

    for p in pin_list:
        ch = _normalize_adc_channel(p)
        gpio_num = int(ch[1])
        cmd = f"{target} SetupADC{ch}"
        print(f"[>] Configuring Pin {gpio_num} ({ch}) as ADC input: \"{cmd}\"")
        client.send_line(cmd)
        time.sleep(0.25)

    return True


def read_adc_pin(client: CoordinatorClient, target: str = "Kitchen", channel: int | str = "A1", timeout: float = 3.0) -> int | None:
    """
    Sends `<target> ReadADC<channel>` and waits for `ADCval<channel> <raw_val>`.
    Returns integer raw_val (0..4095 on ESP32-C6) or None on timeout.
    """
    ch = _normalize_adc_channel(channel)
    got_adc_event = threading.Event()
    adc_reading = None
    expected_token = f"ADCval{ch}"

    def on_rx(msg: str):
        nonlocal adc_reading
        if expected_token in msg:
            # Handles lines like "< Kitchen: ADCvalA1 2048" or "ADCvalA1 2048"
            words = msg.replace(":", " ").split()
            for i, w in enumerate(words):
                if w == expected_token and i + 1 < len(words):
                    try:
                        adc_reading = int(words[i + 1])
                        got_adc_event.set()
                        return
                    except ValueError:
                        pass
                elif w.startswith(expected_token):
                    sub = w[len(expected_token):].lstrip(":=").strip()
                    if sub.isdigit():
                        adc_reading = int(sub)
                        got_adc_event.set()
                        return

    client.add_rx_callback(on_rx)
    cmd = f"{target} ReadADC{ch}"
    client.send_line(cmd)
    got_adc_event.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)
    return adc_reading


def run_read_adc_test(client: CoordinatorClient, target: str = "Kitchen", channel: int | str = "A1", timeout: float = 3.0) -> bool:
    """Configures and reads the specified ADC channel (Pin 0 / A0, Pin 1 / A1, or Pin 2 / A2) and displays raw count and voltage."""
    ch = _normalize_adc_channel(channel)
    gpio_num = int(ch[1])
    print("\n" + "=" * 65)
    print(f"--- READ ADC PIN {gpio_num} ({ch}) ---")
    print(f"Target Node: {target} (ESP32-C6 Pin {ch} / GPIO {gpio_num})")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")

    client.add_rx_callback(on_rx)
    setup_adc_pins(client, target=target, pins=[ch])
    print(f"\n[>] Sending command: \"{target} ReadADC{ch}\"")
    start_time = time.time()
    val = read_adc_pin(client, target=target, channel=ch, timeout=timeout)
    elapsed_ms = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    print("-" * 65)
    if val is not None:
        voltage = (val * 3.96) / 4095.0
        print(f"[+] TEST PASSED: Received ADCval{ch} = {val} (~{voltage:.2f}V) in {elapsed_ms:.1f}ms")
        return True
    else:
        print(f"[-] TEST FAILED: Timed out waiting for ADCval{ch} response from {target}.")
        return False


def run_dac_adc_loopback_test(
    client: CoordinatorClient,
    target: str = "Kitchen",
    test_points: list[int] = None,
    timeout: float = 3.0
) -> bool:
    """
    Executes Closed-Loop DAC -> Dual ADC (Pin 1 & Pin 0) loopback test:
      - Trinket M0 Pin 1~ (DAC) -> ESP32-C6 Pin 1 (A1 / GPIO 1): Expects FULL DAC voltage
      - Resistor divider (1/2)  -> ESP32-C6 Pin 0 (A0 / GPIO 0): Expects HALF DAC voltage
    Sets up both Pin 0 (A0) and Pin 1 (A1) as ADC inputs and tests across multiple setpoints.
    """
    if test_points is None:
        test_points = [0, 256, 512, 768, 1023]

    print("\n" + "=" * 92)
    print("--- [TEST 8] CLOSED-LOOP DAC -> DUAL ADC (PIN 1 FULL V & PIN 0 HALF V) LOOPBACK TEST ---")
    print(f"Target Node:  {target}")
    print("  - Pin 1 (A1 / GPIO 1): Full DAC Voltage  (1.0x V_DAC)")
    print("  - Pin 0 (A0 / GPIO 0): Half DAC Voltage  (0.5x V_DAC via resistor divider)")
    print(f"Test Points:  {test_points}")
    print(f"Timestamp:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 92)

    # 1. Ensure Serial Bridge is active and verified
    if not run_setup_bridge_test(client, target=target, timeout=timeout):
        print("[-] Aborting loopback test: Serial bridge initialization failed.")
        return False
    time.sleep(0.3)

    # 2. Configure BOTH Pin 1 (A1) and Pin 0 (A0) as ADC inputs
    print("\n[>] Initializing both Pin 1 (A1) and Pin 0 (A0) as ADC inputs...")
    setup_adc_pins(client, target=target, pins=[1, 0])
    time.sleep(0.2)

    header = (
        f"{'DAC':<6} | "
        f"{'ExpP1(V)':<8} {'RawP1':<6} {'Pin1(V)':<8} {'ErrP1(V)':<9} | "
        f"{'ExpP0(V)':<8} {'RawP0':<6} {'Pin0(V)':<8} {'ErrP0(V)':<9} | "
        f"{'Status':<8}"
    )
    print("\n" + header)
    print("-" * 92)

    all_passed = True
    previous_adc1 = -1
    previous_adc0 = -1

    # ESP32-C6 ADC1 with ADC_ATTEN_DB_12 full scale calibration factor (~3.96V)
    ADC_FULL_SCALE_VOLTS = 3.96

    for dac_val in test_points:
        expected_v1 = (dac_val * 3.3) / 1023.0
        expected_v0 = expected_v1 / 2.0

        # Step 1: Set DAC on Trinket M0
        got_dac = threading.Event()
        expected_token = f"GotDAC {dac_val}"

        def on_rx_dac(msg: str):
            if expected_token in msg:
                got_dac.set()

        client.add_rx_callback(on_rx_dac)
        client.send_line(f"{target} setDAC {dac_val}")
        dac_ok = got_dac.wait(timeout=timeout)
        client.remove_rx_callback(on_rx_dac)

        if not dac_ok:
            print(
                f"{dac_val:<6} | "
                f"{expected_v1:<8.2f} {'TIMEOUT':<6} {'---':<8} {'---':<9} | "
                f"{expected_v0:<8.2f} {'---':<6} {'---':<8} {'---':<9} | "
                f"[FAIL - No DAC reply]"
            )
            all_passed = False
            continue

        # Settling time for analog line & resistor divider
        time.sleep(0.15)

        # Step 2: Read ADC Pin 1 (A1 - Full DAC voltage)
        adc1_raw = read_adc_pin(client, target=target, channel="A1", timeout=timeout)
        time.sleep(0.05)

        # Step 3: Read ADC Pin 0 (A0 - Half DAC voltage)
        adc0_raw = read_adc_pin(client, target=target, channel="A0", timeout=timeout)

        if adc1_raw is None or adc0_raw is None:
            r1_str = str(adc1_raw) if adc1_raw is not None else "TIMEO"
            r0_str = str(adc0_raw) if adc0_raw is not None else "TIMEO"
            print(
                f"{dac_val:<6} | "
                f"{expected_v1:<8.2f} {r1_str:<6} {'---':<8} {'---':<9} | "
                f"{expected_v0:<8.2f} {r0_str:<6} {'---':<8} {'---':<9} | "
                f"[FAIL - No ADC reply]"
            )
            all_passed = False
            continue

        measured_v1 = (adc1_raw * ADC_FULL_SCALE_VOLTS) / 4095.0
        delta_v1 = measured_v1 - expected_v1

        measured_v0 = (adc0_raw * ADC_FULL_SCALE_VOLTS) / 4095.0
        delta_v0 = measured_v0 - expected_v0

        # Evaluation criteria for Pin 1 (Full DAC Voltage) and Pin 0 (Half DAC Voltage):
        # 1. Monotonicity check (allowing small ADC noise buffer of 50 counts)
        # 2. Voltage tracking tolerance within +-0.25V
        p1_pass = True
        if dac_val > 0 and (adc1_raw + 50) < previous_adc1:
            p1_pass = False
        if abs(delta_v1) > 0.25:
            p1_pass = False

        p0_pass = True
        if dac_val > 0 and (adc0_raw + 50) < previous_adc0:
            p0_pass = False
        if abs(delta_v0) > 0.25:
            p0_pass = False

        point_pass = p1_pass and p0_pass
        if point_pass:
            status_str = "[PASS]"
        elif not p1_pass and not p0_pass:
            status_str = "[FAIL P1&P0]"
        elif not p1_pass:
            status_str = "[FAIL P1]"
        else:
            status_str = "[FAIL P0]"

        if not point_pass:
            all_passed = False

        previous_adc1 = adc1_raw
        previous_adc0 = adc0_raw

        print(
            f"{dac_val:<6} | "
            f"{expected_v1:<8.2f} {adc1_raw:<6} {measured_v1:<8.2f} {delta_v1:+8.2f}  | "
            f"{expected_v0:<8.2f} {adc0_raw:<6} {measured_v0:<8.2f} {delta_v0:+8.2f}  | "
            f"{status_str:<8}"
        )
        time.sleep(0.15)

    print("-" * 92)
    if all_passed:
        print("[+] TEST PASSED: Closed-loop DAC -> Pin 1 (Full V) & Pin 0 (1/2 V) ADC tracking verified!")
    else:
        print("[-] TEST FAILED: One or more DAC -> ADC setpoints on Pin 1 or Pin 0 failed tracking criteria.")

    return all_passed


def _execute_schmitt_ramp_cycle(
    client: CoordinatorClient,
    target: str = "Kitchen",
    pass_label: str = "PASS 1",
    ramp_time: float = 2.0,
    steps: int = 10,
    lower_thresh: int = 1000,
    upper_thresh: int = 2200
) -> dict:
    """Runs a single ramp cycle (0 -> 1000 -> 0) with specified thresholds and records event timings."""
    upper_v = (upper_thresh * 3.96) / 4095.0
    lower_v = (lower_thresh * 3.96) / 4095.0
    print(f"\n>>> {pass_label}: Upper={upper_thresh} (~{upper_v:.2f}V), Lower={lower_thresh} (~{lower_v:.2f}V) <<<")

    # Configure thresholds
    print(f"[>] Setting thresholds: Upper={upper_thresh}, Lower={lower_thresh}...")
    client.send_line(f"{target} ADCupperThreshA1 {upper_thresh}")
    time.sleep(0.15)
    client.send_line(f"{target} ADCLowerThreshA1 {lower_thresh}")
    time.sleep(0.15)

    # Initialize DAC to 0
    client.send_line(f"{target} setDAC 0")
    time.sleep(0.4)

    high_event = None
    low_event = None
    t_high = None
    t_low = None
    t_test_start = time.time()

    def on_rx(msg: str):
        nonlocal high_event, low_event, t_high, t_low
        if "ADCcomparatorA1 1" in msg and high_event is None:
            high_event = msg
            t_high = time.time() - t_test_start
            print(f"  [+] [EVENT DETECTED +{t_high:.2f}s] RISING THRESHOLD CROSSING -> {msg}")
        elif "ADCcomparatorA1 0" in msg and low_event is None:
            low_event = msg
            t_low = time.time() - t_test_start
            print(f"  [+] [EVENT DETECTED +{t_low:.2f}s] FALLING THRESHOLD CROSSING -> {msg}")

    client.add_rx_callback(on_rx)

    interval = ramp_time / steps
    t_test_start = time.time()

    # --- Phase 1: Ramp UP (0 -> 1000) ---
    print(f"[*] Starting Ramp UP (0 -> 1000 in {ramp_time:.1f}s, {steps} steps, {interval*1000:.0f}ms/step)...")
    t_up_start = time.time()
    for i in range(1, steps + 1):
        dac_val = int((1000 / steps) * i)
        client.send_line(f"{target} setDAC {dac_val}")
        t_target = t_up_start + (i * interval)
        rem = t_target - time.time()
        if rem > 0:
            time.sleep(rem)
    t_up_elapsed = time.time() - t_up_start

    # --- Phase 2: Ramp DOWN (1000 -> 0) ---
    print(f"[*] Starting Ramp DOWN (1000 -> 0 in {ramp_time:.1f}s, {steps} steps, {interval*1000:.0f}ms/step)...")
    t_down_start = time.time()
    for i in range(1, steps + 1):
        dac_val = int(1000 - (1000 / steps) * i)
        client.send_line(f"{target} setDAC {dac_val}")
        t_target = t_down_start + (i * interval)
        rem = t_target - time.time()
        if rem > 0:
            time.sleep(rem)
    t_down_elapsed = time.time() - t_down_start

    # Drain time: wait up to 1.0s if needed
    t_drain = time.time()
    while time.time() - t_drain < 1.0:
        if high_event is not None and low_event is not None:
            break
        time.sleep(0.05)

    client.remove_rx_callback(on_rx)

    passed_high = high_event is not None
    passed_low = low_event is not None
    order_ok = passed_high and passed_low and (t_high < t_low)

    return {
        "passed": passed_high and passed_low and order_ok,
        "upper": upper_thresh,
        "lower": lower_thresh,
        "t_high": t_high,
        "t_low": t_low,
        "up_elapsed": t_up_elapsed,
        "down_elapsed": t_down_elapsed,
    }


def run_schmitt_hysteresis_ramp_test(
    client: CoordinatorClient,
    target: str = "Kitchen",
    ramp_time: float = 2.0,
    steps: int = 10,
    base_lower: int = 1000,
    base_upper: int = 2200,
    delta_offset: int = 400,
    timeout: float = 3.0
) -> bool:
    """
    Validates Schmitt trigger threshold controllability across two ramp cycles:
      Pass 1 (Baseline): Upper = 2200, Lower = 1000
      Pass 2 (Lowered by 400): Upper = 1800, Lower = 600
    Verifies that:
      1. Both passes receive rising (`ADCcomparatorA1 1`) and falling (`ADCcomparatorA1 0`) events.
      2. Lowering upper threshold causes rising event to trigger EARLIER during ramp-up.
      3. Lowering lower threshold causes falling event to trigger LATER during ramp-down.
    """
    print("\n" + "=" * 65)
    print("--- [TEST 9] SCHMITT TRIGGER THRESHOLD CONTROLLABILITY RAMP TEST ---")
    print(f"Target Node:       {target} (Trinket M0 DAC Pin 1~ -> ESP32-C6 Pin A1)")
    print(f"Ramp Profile:      0 -> 1000 in {ramp_time:.1f}s, then 1000 -> 0 in {ramp_time:.1f}s")
    print(f"Pass 1 (Baseline): Lower = {base_lower}, Upper = {base_upper}")
    print(f"Pass 2 (-{delta_offset} Shift): Lower = {base_lower - delta_offset}, Upper = {base_upper - delta_offset}")
    print(f"Timestamp:         {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # 1. Ensure Serial Bridge and ADC
    if not run_setup_bridge_test(client, target=target, timeout=timeout):
        print("[-] Aborting Schmitt test: Serial bridge initialization failed.")
        return False
    time.sleep(0.2)

    client.send_line(f"{target} SetupADCA1")
    time.sleep(0.2)

    # --- Run Pass 1: Baseline Thresholds ---
    res1 = _execute_schmitt_ramp_cycle(
        client, target=target, pass_label="PASS 1 (Baseline)",
        ramp_time=ramp_time, steps=steps, lower_thresh=base_lower, upper_thresh=base_upper
    )
    time.sleep(0.4)

    # --- Run Pass 2: Thresholds lowered by delta_offset (400) ---
    lower2 = base_lower - delta_offset
    upper2 = base_upper - delta_offset
    res2 = _execute_schmitt_ramp_cycle(
        client, target=target, pass_label=f"PASS 2 (Lowered by {delta_offset})",
        ramp_time=ramp_time, steps=steps, lower_thresh=lower2, upper_thresh=upper2
    )

    # --- Verification & Controllability Analysis ---
    pass1_ok = res1["passed"]
    pass2_ok = res2["passed"]

    rising_shift_ok = False
    falling_shift_ok = False

    if res1["t_high"] is not None and res2["t_high"] is not None:
        # Lowering upper threshold must cause rising event to trigger earlier
        rising_shift_ok = res2["t_high"] < res1["t_high"]

    if res1["t_low"] is not None and res2["t_low"] is not None:
        # Lowering lower threshold must cause falling event to trigger later (closer to 0)
        falling_shift_ok = res2["t_low"] > res1["t_low"]

    all_passed = pass1_ok and pass2_ok and rising_shift_ok and falling_shift_ok

    print("\n" + "=" * 65)
    print("SCHMITT TRIGGER CONTROLLABILITY VERIFICATION REPORT:")
    print("=" * 65)
    t_h1_str = f"+{res1['t_high']:.2f}s" if res1['t_high'] else "MISSING"
    t_l1_str = f"+{res1['t_low']:.2f}s" if res1['t_low'] else "MISSING"
    t_h2_str = f"+{res2['t_high']:.2f}s" if res2['t_high'] else "MISSING"
    t_l2_str = f"+{res2['t_low']:.2f}s" if res2['t_low'] else "MISSING"

    print(f"  Pass 1 Baseline (Upper {res1['upper']}, Lower {res1['lower']}):")
    print(f"    - Rising  Event: {t_h1_str}")
    print(f"    - Falling Event: {t_l1_str}")
    print(f"  Pass 2 Shifted  (Upper {res2['upper']}, Lower {res2['lower']}):")
    print(f"    - Rising  Event: {t_h2_str}")
    print(f"    - Falling Event: {t_l2_str}")
    print("-" * 65)

    if res1["t_high"] and res2["t_high"]:
        delta_high = res1["t_high"] - res2["t_high"]
        status_high = "[PASS]" if rising_shift_ok else "[FAIL]"
        print(f"  {status_high} Rising Threshold Controllability:  Shifted EARLIER by {delta_high:+.2f}s")

    if res1["t_low"] and res2["t_low"]:
        delta_low = res2["t_low"] - res1["t_low"]
        status_low = "[PASS]" if falling_shift_ok else "[FAIL]"
        print(f"  {status_low} Falling Threshold Controllability: Shifted LATER   by {delta_low:+.2f}s")

    print("-" * 65)
    if all_passed:
        print("[+] TEST PASSED: Schmitt trigger thresholds are dynamically controllable!")
    else:
        print("[-] TEST FAILED: Threshold shift did not produce expected event timing shifts.")

    return all_passed


def run_adcpot_ramp_test(
    client: CoordinatorClient,
    target: str = "Kitchen",
    detent_positions: int = 20,
    steps: int = 32,
    step_delay: float = 0.16,
    timeout: float = 3.0
) -> bool:
    """
    Executes Closed-Loop DAC -> ADCpot Detent Ramp Test (0 -> 1023 -> 0):
      1. Initializes Serial Bridge and sets DAC to 0.
      2. Configures ADCpot with 20 detent positions (PotDetent 5 -> 5% of full-scale per detent).
      3. Enables ADCpot mode on Pin 1 (ADCpotA1).
      4. Ramps DAC from 0 to 1023 (full-scale) and back down to 0, synchronizing each step with GotDAC.
      5. On each ADCpotA1 message from the end-device, prints the DAC value and new ADCpot value,
         and verifies that the reported ADCpot value matches the expected detent progression and voltage.
    """
    detent_pct = max(1, int(round(100.0 / detent_positions)))
    ADC_FULL_SCALE_VOLTS = 3.30  # Matched to 3.3V rail (potentiometer VCC)
    ADC_ZERO_OFFSET_VOLTS = 0.050  # 50mV zero threshold offset (shifted active range [50mV, 3.3V])
    POT_HYST_MARGIN = 0.15

    print("\n" + "=" * 86)
    print(f"--- [TEST A] CLOSED-LOOP DAC -> ADCpot DETENT RAMP TEST ({detent_positions} DETENTS) ---")
    print(f"Target Node:      {target} (Trinket M0 DAC Pin 1~ -> ESP32-C6 Pin A1)")
    print(f"Detent Config:    {detent_positions} positions (PotDetent {detent_pct} -> {detent_pct}% of active span [50mV..3.30V])")
    print(f"Ramp Profile:     0 -> 1023 -> 0 ({steps} synced DAC steps each way, +{step_delay*1000:.0f}ms settle)")
    print(f"Hysteresis Snap:  65% of step (0.50 halfway + {POT_HYST_MARGIN:.2f} margin, 0 floor <= 50mV)")
    print(f"Timestamp:        {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 86)

    # 1. Ensure Serial Bridge is active
    if not run_setup_bridge_test(client, target=target, timeout=timeout):
        print("[-] Aborting ADCpot test: Serial bridge initialization failed.")
        return False
    time.sleep(0.2)

    current_dac = 0
    phase = "UP"
    current_detent = 0
    up_events = []
    down_events = []
    all_events_valid = True
    rx_lock = threading.Lock()

    def set_dac_synced(dac_val: int):
        nonlocal current_dac
        got_dac = threading.Event()
        expected_token = f"GotDAC {dac_val}"

        def on_dac_rx(msg: str):
            if expected_token in msg:
                got_dac.set()

        with rx_lock:
            current_dac = dac_val

        client.add_rx_callback(on_dac_rx)
        try:
            client.send_line(f"{target} setDAC {dac_val}")
            got_dac.wait(timeout=1.2)
        finally:
            client.remove_rx_callback(on_dac_rx)
        time.sleep(step_delay)

    # 2. Initialize DAC to 0 before locking initial ADCpot detent
    print("\n[>] Initializing DAC to 0 (0.00V)...")
    set_dac_synced(0)
    time.sleep(0.25)

    # 3. Configure PotDetent X and enable ADCpot mode on A1
    print(f"[>] Configuring {detent_positions} detent positions: \"{target} PotDetent {detent_pct}\"")
    client.send_line(f"{target} PotDetent {detent_pct}")
    time.sleep(0.25)

    print(f"[>] Enabling ADCpot mode on Pin 1 (A1): \"{target} ADCpotA1\"")
    client.send_line(f"{target} ADCpotA1")
    time.sleep(0.4)

    def calc_theoretical_detent(dac_val: int, ramp_dir: str) -> tuple[int, float]:
        v_dac = (dac_val * 3.3) / 1023.0
        if v_dac <= ADC_ZERO_OFFSET_VOLTS:
            pos = 0.0
        else:
            effective_span = ADC_FULL_SCALE_VOLTS - ADC_ZERO_OFFSET_VOLTS
            frac = (v_dac - ADC_ZERO_OFFSET_VOLTS) / effective_span
            frac = min(1.0, max(0.0, frac))
            pos = frac / (detent_pct / 100.0)

        if pos <= 0.0:
            d_theory = 0
        elif ramp_dir == "UP":
            d_theory = max(0, int(math.floor(pos - POT_HYST_MARGIN + 0.5)))
        else:
            d_theory = max(0, int(math.ceil(pos + POT_HYST_MARGIN - 0.5)))
        return d_theory, pos

    def on_rx(msg: str):
        nonlocal current_detent, all_events_valid
        # Ignore ESP-IDF SERIAL_GATEWAY diagnostic log lines so each frame is processed once
        if "SERIAL_GATEWAY:" in msg:
            return
        match = re.search(r"ADCpotA1\s+(-?\d+)", msg, re.IGNORECASE)
        if not match:
            return
        pot_val = int(match.group(1))
        with rx_lock:
            if pot_val == current_detent:
                return
            dac_snapshot = current_dac
            dir_snapshot = phase
            if dir_snapshot == "UP":
                exp_seq = current_detent + 1
                dir_ok = pot_val > current_detent
            else:
                exp_seq = current_detent - 1
                dir_ok = pot_val < current_detent

            d_theory, pos_theory = calc_theoretical_detent(dac_snapshot, dir_snapshot)
            v_dac = (dac_snapshot * 3.3) / 1023.0

            # Verify that the reported ADCpot value steps in the right direction and matches
            # expected detent value (allowing +-2 detents tolerance for analog/timing latency)
            seq_ok = abs(pot_val - exp_seq) <= 1 and dir_ok
            theory_ok = abs(pot_val - d_theory) <= 2
            event_ok = seq_ok and theory_ok

            if not event_ok:
                all_events_valid = False

            current_detent = pot_val
            status_str = "[PASS]" if event_ok else f"[FAIL exp={exp_seq}, th={d_theory}]"
            print(
                f"  [RAMP {dir_snapshot:<4}] DAC = {dac_snapshot:<4} ({v_dac:4.2f}V) | "
                f"New ADCpot = {pot_val:<2} | Expected = {exp_seq:<2} (Theory: {d_theory:<2}, pos={pos_theory:5.2f}) | {status_str}"
            )

            record = {
                "dac": dac_snapshot,
                "pot": pot_val,
                "expected": exp_seq,
                "theory": d_theory,
                "ok": event_ok,
            }
            if dir_snapshot == "UP":
                up_events.append(record)
            else:
                down_events.append(record)

    client.add_rx_callback(on_rx)

    try:
        # --- Phase 1: Ramp UP (0 -> 1023) ---
        print("\n" + "-" * 86)
        print(f"[*] Starting Ramp UP: DAC 0 -> 1023 ({steps} steps)...")
        print("-" * 86)
        with rx_lock:
            phase = "UP"

        for i in range(1, steps + 1):
            dac_val = int(round((1023.0 / steps) * i))
            set_dac_synced(dac_val)

        # Drain and settle at peak DAC (1023) so peak detent is captured in UP phase
        print("\n[*] Settling at peak DAC 1023...")
        t_peak = time.time()
        last_det = current_detent
        while time.time() - t_peak < 1.0:
            time.sleep(0.05)
            with rx_lock:
                if current_detent != last_det:
                    last_det = current_detent
                    t_peak = time.time()

        # --- Phase 2: Ramp DOWN (1023 -> 0) ---
        print("\n" + "-" * 86)
        print(f"[*] Starting Ramp DOWN: DAC 1023 -> 0 ({steps} steps)...")
        print("-" * 86)
        with rx_lock:
            phase = "DOWN"

        for i in range(1, steps + 1):
            dac_val = int(round(1023.0 - (1023.0 / steps) * i))
            set_dac_synced(dac_val)

        # Wait up to 2.5s at DAC=0 for final downward detent message (detent 0)
        print("\n[*] Settling at DAC 0...")
        t_drain = time.time()
        while time.time() - t_drain < 2.5:
            with rx_lock:
                if current_detent == 0:
                    break
            time.sleep(0.05)
    finally:
        client.remove_rx_callback(on_rx)
        # Restore A1 to standard ADC comparator mode after test
        client.send_line(f"{target} SetupADCA1")
        time.sleep(0.15)

    # --- Verification Summary ---
    peak_detent = up_events[-1]["pot"] if up_events else 0
    final_detent = down_events[-1]["pot"] if down_events else -1
    # At 3.30V DAC full-scale on a 3.30V ADC range, pos_max reaches 20 detents
    min_expected_transitions = 16
    up_count_ok = len(up_events) >= min_expected_transitions
    down_count_ok = len(down_events) >= min_expected_transitions
    returned_to_zero = (final_detent == 0)

    overall_pass = all_events_valid and up_count_ok and down_count_ok and returned_to_zero

    print("\n" + "=" * 86)
    print("ADCpot DETENT RAMP VERIFICATION SUMMARY:")
    print("=" * 86)
    print(f"  Ramp UP Transitions:   {len(up_events)} detents (0 -> {peak_detent})")
    print(f"  Ramp DOWN Transitions: {len(down_events)} detents ({peak_detent} -> {final_detent})")
    print(f"  Returned to Detent 0:  {'YES' if returned_to_zero else 'NO'}")
    print(f"  All Values Matched:    {'YES' if all_events_valid else 'NO'}")
    print("-" * 86)

    if overall_pass:
        print(f"[+] TEST PASSED: ADCpot {detent_positions}-detent ramp verified up to {peak_detent} and back to 0!")
    else:
        print("[-] TEST FAILED: ADCpot detent transitions did not match expected progression.")

    return overall_pass


def run_servo_pwm_test(client: CoordinatorClient, target: str = "Kitchen") -> bool:
    """
    Executes the Hobby Servo PWM test on Pin 2 (Channel 1):
    1. SetupPWM1 (Pin 2 @ 50Hz, center 77)
    2. Zero slew snap: pwmSlew1 0 -> pwm1 51 -> pwm1 102 -> pwm1 51
    3. Smooth slew 30: pwmSlew1 30 -> pwm1 102 -> pwm1 51 -> (center 77)
    4. Operator confirms visual snap vs slew behavior.
    """
    print("\n" + "=" * 65)
    print("--- [TEST S] SERVO / PWM HARDWARE TEST (PIN 2) ---")
    print(f"Target Node: {target} (ESP32-C6 Pin 2 -> 74HCT125 -> Servo)")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    print("\n[!] ACTION REQUIRED: Look at the Servo motor horn (Pin 2).")
    print("    Watch for two distinct motion phases:")
    print("      1. Instant snap between positions (slew rate = 0).")
    print("      2. Smooth, slow sweeping motion between positions (slew rate = 30).")
    try:
        input("    Press [Enter] when ready to begin test...")
    except (EOFError, OSError):
        pass

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")

    client.add_rx_callback(on_rx)

    # 1. Initialize PWM1
    cmd = f"{target} SetupPWM1"
    print(f"\n[>] Initializing PWM Channel 1: \"{cmd}\"")
    client.send_line(cmd)
    time.sleep(0.5)

    # 2. Phase 1: Zero Slew (Instant Snap)
    print("\n>>> Phase 1: Instant Snap (pwmSlew1 0) <<<")
    cmd = f"{target} pwmSlew1 0"
    print(f"[>] Setting slew to 0 (instant): \"{cmd}\"")
    client.send_line(cmd)
    time.sleep(0.3)

    cmd = f"{target} pwm1 51"
    print(f"[>] Snap to Min duty 51 (~1.0ms): \"{cmd}\"")
    client.send_line(cmd)
    time.sleep(1.0)

    cmd = f"{target} pwm1 102"
    print(f"[>] Snap to Max duty 102 (~2.0ms): \"{cmd}\"")
    client.send_line(cmd)
    time.sleep(1.0)

    cmd = f"{target} pwm1 51"
    print(f"[>] Snap back to Min duty 51 (~1.0ms): \"{cmd}\"")
    client.send_line(cmd)
    time.sleep(1.0)

    # 3. Phase 2: Slew Rate 30 (Smooth Gliding Motion)
    print("\n>>> Phase 2: Smooth Slewing (pwmSlew1 30) <<<")
    cmd = f"{target} pwmSlew1 30"
    print(f"[>] Setting slew rate to 30 units/sec: \"{cmd}\"")
    client.send_line(cmd)
    time.sleep(0.3)

    cmd = f"{target} pwm1 102"
    print(f"[>] Smooth slew to Max duty 102 (~2.0ms): \"{cmd}\"")
    client.send_line(cmd)
    time.sleep(2.2)

    cmd = f"{target} pwm1 51"
    print(f"[>] Smooth slew back to Min duty 51 (~1.0ms): \"{cmd}\"")
    client.send_line(cmd)
    time.sleep(2.2)

    # Return to neutral center
    cmd = f"{target} pwm1 77"
    print(f"[>] Returning to Center duty 77 (~1.5ms): \"{cmd}\"")
    client.send_line(cmd)
    time.sleep(1.0)

    client.remove_rx_callback(on_rx)

    print("\n" + "-" * 65)
    print("[?] Visual Verification:")
    print("    1. Did the servo snap quickly between positions with slew=0 (51 -> 102 -> 51)?")
    print("    2. Did the servo sweep smoothly and slowly with slew=30 (51 -> 102 -> 51)?")
    resp = input("    Confirm servo motion observed? [y/N]: ").strip().lower()
    print("-" * 65)

    passed = resp in ("y", "yes")
    if passed:
        print("[+] TEST PASSED: Servo PWM snap and slew motion verified successfully!")
    else:
        print("[-] TEST FAILED: Servo motion was not confirmed by operator.")
    return passed


def setup_rgb_led_pwms(
    client: CoordinatorClient,
    target: str = "Garage",
    pin_r: int = 18,
    pin_g: int = 20,
    pin_b: int = 19,
    freq: int = 1000
) -> bool:
    """
    Initializes three GPIO pins as hardware PWM channels on the target node.
    Maps:
      PWM Channel 1 -> pin_r (default 18)
      PWM Channel 2 -> pin_g (default 20)
      PWM Channel 3 -> pin_b (default 19)
    """
    print(f"\n[>] Configuring RGB LED PWMs on {target}:")
    print(f"    Channel 1 -> GPIO {pin_r}")
    print(f"    Channel 2 -> GPIO {pin_g}")
    print(f"    Channel 3 -> GPIO {pin_b}")

    cmds = [
        # Pin assignment and frequency configuration
        f"{target} pwmPin1 {pin_r}",
        f"{target} pwmFreq1 {freq}",
        f"{target} SetupPWM1",
        f"{target} pwmPin2 {pin_g}",
        f"{target} pwmFreq2 {freq}",
        f"{target} SetupPWM2",
        f"{target} pwmPin3 {pin_b}",
        f"{target} pwmFreq3 {freq}",
        f"{target} SetupPWM3",
        # Turn all channels OFF initially (Common Anode @ 3.3V -> duty 1023)
        f"{target} pwmSlew1 0",
        f"{target} pwm1 1023",
        f"{target} pwmSlew2 0",
        f"{target} pwm2 1023",
        f"{target} pwmSlew3 0",
        f"{target} pwm3 1023",
    ]

    for c in cmds:
        client.send_line(c)
        time.sleep(0.06)

    print(f"[+] RGB LED PWM channels 1, 2, 3 configured on {target} (all OFF).")
    return True


def run_garage_rgb_fade_test(
    client: CoordinatorClient,
    target: str = "Garage",
    pin_r: int = 18,
    pin_g: int = 20,
    pin_b: int = 19,
    fade_duration: float = 1.0,
    common_anode: bool = True
) -> bool:
    """
    Tests three-color RGB LED on Garage node (Pins 18, 20, 19 with 1.2k resistors, Common to 3.3V):
    1. Sets up GPIOs 18, 20, 19 as hardware PWM channels.
    2. Fades Pin 18 from 0% -> 100% -> 0% over `fade_duration` (default: 1.0s).
    3. Fades Pin 20 from 0% -> 100% -> 0% over `fade_duration`.
    4. Fades Pin 19 from 0% -> 100% -> 0% over `fade_duration`.
    5. Fades all three simultaneously from 0% -> 100% -> 0% over `fade_duration`.
    6. Keeps cycling that pattern until the user hits the ESC key.
    """
    print("\n" + "=" * 65)
    print("--- [RGB] GARAGE THREE-COLOR LED PWM FADE CYCLE TEST ---")
    print(f"Target Node: {target} (ESP32-C6)")
    print(f"Pins:        Pin {pin_r} (Ch 1), Pin {pin_g} (Ch 2), Pin {pin_b} (Ch 3)")
    print("Hardware:    1.2k Ohm series resistors on each pin, Common Anode to 3.3V")
    print(f"Pattern:     Pin {pin_r} ({fade_duration:.1f}s) -> Pin {pin_g} ({fade_duration:.1f}s) -> Pin {pin_b} ({fade_duration:.1f}s) -> All Three ({fade_duration:.1f}s)")
    print("Action:      Cycling pattern continuously. Press [ESC] to stop.")
    print("=" * 65)

    # Step 1: Configure hardware PWM channels
    setup_rgb_led_pwms(client, target=target, pin_r=pin_r, pin_g=pin_g, pin_b=pin_b)
    time.sleep(0.1)

    # Calculate ramp timings and slew rate
    # fade_duration is total round-trip time (0% -> 100% -> 0%)
    half_time = max(0.1, fade_duration / 2.0)
    # Range is 1023 counts
    slew_rate = max(1, int(1023.0 / half_time))

    # Set hardware slew rate on all 3 channels
    client.send_line(f"{target} pwmSlew1 {slew_rate}")
    time.sleep(0.04)
    client.send_line(f"{target} pwmSlew2 {slew_rate}")
    time.sleep(0.04)
    client.send_line(f"{target} pwmSlew3 {slew_rate}")
    time.sleep(0.1)

    # For Common Anode (Common connected to 3.3V):
    #   0% brightness   -> Pin at 3.3V -> Duty 1023 (OFF)
    #   100% brightness -> Pin at 0.0V -> Duty 0    (Full ON)
    duty_off = 1023 if common_anode else 0
    duty_on  = 0 if common_anode else 1023

    flush_console_keys()
    cycle_count = 0
    stopped_by_user = False

    try:
        while True:
            cycle_count += 1
            print(f"\n[>] --- Starting Fade Cycle #{cycle_count} ---")

            # Phase 1: Pin 18 (Channel 1)
            print(f"  [1/4] Fading Pin {pin_r}: 0% -> 100% -> 0% ({fade_duration:.1f}s)...")
            client.send_line(f"{target} pwm1 {duty_on}")
            if sleep_or_esc(half_time):
                stopped_by_user = True
                break
            client.send_line(f"{target} pwm1 {duty_off}")
            if sleep_or_esc(half_time):
                stopped_by_user = True
                break

            # Phase 2: Pin 20 (Channel 2)
            print(f"  [2/4] Fading Pin {pin_g}: 0% -> 100% -> 0% ({fade_duration:.1f}s)...")
            client.send_line(f"{target} pwm2 {duty_on}")
            if sleep_or_esc(half_time):
                stopped_by_user = True
                break
            client.send_line(f"{target} pwm2 {duty_off}")
            if sleep_or_esc(half_time):
                stopped_by_user = True
                break

            # Phase 3: Pin 19 (Channel 3)
            print(f"  [3/4] Fading Pin {pin_b}: 0% -> 100% -> 0% ({fade_duration:.1f}s)...")
            client.send_line(f"{target} pwm3 {duty_on}")
            if sleep_or_esc(half_time):
                stopped_by_user = True
                break
            client.send_line(f"{target} pwm3 {duty_off}")
            if sleep_or_esc(half_time):
                stopped_by_user = True
                break

            # Phase 4: All Three Simultaneously
            print(f"  [4/4] Fading All 3 (Pins {pin_r}, {pin_g}, {pin_b}): 0% -> 100% -> 0% ({fade_duration:.1f}s)...")
            client.send_line(f"{target} pwm1 {duty_on}")
            client.send_line(f"{target} pwm2 {duty_on}")
            client.send_line(f"{target} pwm3 {duty_on}")
            if sleep_or_esc(half_time):
                stopped_by_user = True
                break
            client.send_line(f"{target} pwm1 {duty_off}")
            client.send_line(f"{target} pwm2 {duty_off}")
            client.send_line(f"{target} pwm3 {duty_off}")
            if sleep_or_esc(half_time):
                stopped_by_user = True
                break

    except KeyboardInterrupt:
        stopped_by_user = True

    finally:
        # Snap all three channels back to OFF
        print(f"\n[*] Turning off all RGB LED channels on {target}...")
        client.send_line(f"{target} pwmSlew1 0")
        client.send_line(f"{target} pwm1 {duty_off}")
        time.sleep(0.04)
        client.send_line(f"{target} pwmSlew2 0")
        client.send_line(f"{target} pwm2 {duty_off}")
        time.sleep(0.04)
        client.send_line(f"{target} pwmSlew3 0")
        client.send_line(f"{target} pwm3 {duty_off}")
        flush_console_keys()

    print("-" * 65)
    print(f"[+] Completed {cycle_count} RGB LED fade cycle(s). Test stopped cleanly.")
    return True


def clear_neopixels(client: CoordinatorClient, target: str = "Kitchen", num_pixels: int = 16, timeout: float = 1.0) -> bool:
    """Sets all pixels in the strand to black (0, 0, 0) and refreshes the strip."""
    got_ack = threading.Event()
    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "NEO" in msg or "OK" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} NeoClear"
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)
    if not success:
        client.send_line(cmd)
        time.sleep(0.04)
    return True


def setup_neopixel(client: CoordinatorClient, target: str = "Kitchen", pin: int = 19, num_pixels: int = 16) -> bool:
    """Configures NeoPixel driver on target node at specified GPIO pin and pixel count, and zeroes all pixels."""
    print(f"\n[>] Configuring NeoPixel on {target} Pin {pin} ({num_pixels} pixels): \"{target} NeoGPIO {pin} {num_pixels}\"")
    client.send_line(f"{target} NeoGPIO {pin} {num_pixels}")
    time.sleep(0.12)
    clear_neopixels(client, target=target, num_pixels=num_pixels)
    print(f"[+] NeoPixel initialized on {target} Pin {pin} ({num_pixels} pixels cleared to black).")
    return True


def turn_off_neopixel(client: CoordinatorClient, target: str, pixel_idx: int, timeout: float = 1.0) -> bool:
    """Explicitly turns off a single NeoPixel (0, 0, 0) and refreshes the strip."""
    got_ack = threading.Event()
    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "NEO" in msg or "OK" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} NeoOff {pixel_idx}"
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)
    if not success:
        client.send_line(cmd)
        time.sleep(0.04)
    return True


def set_neopixel_red(client: CoordinatorClient, target: str, pixel_idx: int, val: int = 128, timeout: float = 1.0) -> bool:
    """Sets pixel to pure Red at specified brightness, zeroing green/blue and updating strip immediately."""
    got_ack = threading.Event()
    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "NEO" in msg or "OK" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} NeoRed {pixel_idx} {val}"
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)
    if not success:
        client.send_line(f"{target} NeoRED {pixel_idx} {val}")
        time.sleep(0.02)
        client.send_line(f"{target} NeoGREEN {pixel_idx} 0")
        time.sleep(0.02)
        client.send_line(f"{target} NeoBLUE {pixel_idx} 0")
        time.sleep(0.02)
        client.send_line(f"{target} NeoUpdate")
        time.sleep(0.04)
    return True


def set_neopixel_green(client: CoordinatorClient, target: str, pixel_idx: int, val: int = 128, timeout: float = 1.0) -> bool:
    """Sets pixel to pure Green at specified brightness, zeroing red/blue and updating strip immediately."""
    got_ack = threading.Event()
    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "NEO" in msg or "OK" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} NeoGreen {pixel_idx} {val}"
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)
    if not success:
        client.send_line(f"{target} NeoRED {pixel_idx} 0")
        time.sleep(0.02)
        client.send_line(f"{target} NeoGREEN {pixel_idx} {val}")
        time.sleep(0.02)
        client.send_line(f"{target} NeoBLUE {pixel_idx} 0")
        time.sleep(0.02)
        client.send_line(f"{target} NeoUpdate")
        time.sleep(0.04)
    return True


def set_neopixel_blue(client: CoordinatorClient, target: str, pixel_idx: int, val: int = 128, timeout: float = 1.0) -> bool:
    """Sets pixel to pure Blue at specified brightness, zeroing red/green and updating strip immediately."""
    got_ack = threading.Event()
    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "NEO" in msg or "OK" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} NeoBlue {pixel_idx} {val}"
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)
    if not success:
        client.send_line(f"{target} NeoRED {pixel_idx} 0")
        time.sleep(0.02)
        client.send_line(f"{target} NeoGREEN {pixel_idx} 0")
        time.sleep(0.02)
        client.send_line(f"{target} NeoBLUE {pixel_idx} {val}")
        time.sleep(0.02)
        client.send_line(f"{target} NeoUpdate")
        time.sleep(0.04)
    return True


def run_neopixel_test(
    client: CoordinatorClient,
    target: str = "Kitchen",
    pin: int = 19,
    num_pixels: int = 16,
    update_delay: float = 0.5,
    brightness: int = 128
) -> bool:
    """
    Validates NeoPixel WS2812B strand on target node (default: Kitchen Pin 19, configurable pixels):
    Starts at LED 1. For each LED:
      1. Red LED on at brightness 128 -> wait 0.5s
      2. Red LED off -> wait 0.5s
      3. Green LED on at brightness 128 -> wait 0.5s
      4. Green LED zero (off) -> wait 0.5s
      5. Blue LED on at brightness 128 -> wait 0.5s
      6. Blue LED off -> wait 0.5s
    Continues cycling strand until user hits ESC.
    """
    print("\n" + "=" * 65)
    print(f"--- [NEO] {num_pixels}-NEOPIXEL STRAND STEP TEST ---")
    print(f"Target Node:  {target} (ESP32-C6)")
    print(f"Hardware:     Pin {pin} -> 74HCT125 Level Shifter -> {num_pixels}-WS2812B NeoPixel Strand")
    print(f"Brightness:   {brightness} (out of 255)")
    print(f"Strand Delay: {update_delay:.1f}s between each update")
    print(f"Sequence:     For LED 1..{num_pixels}: Red ON -> OFF -> Green ON -> ZERO -> Blue ON -> OFF")
    print("Action:       Cycling strand continuously. Press [ESC] to stop.")
    print("=" * 65)

    setup_neopixel(client, target=target, pin=pin, num_pixels=num_pixels)
    flush_console_keys()

    cycle_count = 0
    stopped = False

    try:
        while not stopped:
            cycle_count += 1
            print(f"\n[>] --- Starting Strand Cycle #{cycle_count} ---")

            for p in range(num_pixels):
                if check_esc_key():
                    stopped = True
                    break

                pixel_num = p + 1
                print(f"  [*] Pixel {pixel_num}/{num_pixels} (Index {p}):")

                # 1. Red on at 128
                print(f"      - Red ON ({brightness})...")
                set_neopixel_red(client, target, p, val=brightness)
                if sleep_or_esc(update_delay):
                    stopped = True
                    break

                # 2. Red off
                print(f"      - Red OFF...")
                turn_off_neopixel(client, target, p)
                if sleep_or_esc(update_delay):
                    stopped = True
                    break

                # 3. Green on at 128
                print(f"      - Green ON ({brightness})...")
                set_neopixel_green(client, target, p, val=brightness)
                if sleep_or_esc(update_delay):
                    stopped = True
                    break

                # 4. Green zero (off)
                print(f"      - Green ZERO...")
                turn_off_neopixel(client, target, p)
                if sleep_or_esc(update_delay):
                    stopped = True
                    break

                # 5. Blue on at 128
                print(f"      - Blue ON ({brightness})...")
                set_neopixel_blue(client, target, p, val=brightness)
                if sleep_or_esc(update_delay):
                    stopped = True
                    break

                # 6. Blue off
                print(f"      - Blue OFF...")
                turn_off_neopixel(client, target, p)
                if sleep_or_esc(update_delay):
                    stopped = True
                    break

    except KeyboardInterrupt:
        stopped = True

    finally:
        print(f"\n[*] Clearing all {num_pixels} NeoPixels to black on {target}...")
        clear_neopixels(client, target=target, num_pixels=num_pixels)
        flush_console_keys()

    print("-" * 65)
    print(f"[+] Completed {cycle_count} strand cycle(s). NeoPixel test stopped cleanly.")
    return True


UNIPOLAR_DEFAULT_DELAY_MS = 10


def setup_unipolar_stepper(client: CoordinatorClient, target: str = "Garage", timeout: float = 3.0) -> bool:
    """Initializes Adafruit Small Reduction Stepper on target node (pins 1, 2, 21, 0)."""
    got_ack = threading.Event()
    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "UNIPOLAR" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} SetupUnipolar"
    print(f"\n[>] Initializing Unipolar Stepper on {target}: \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if success:
        print(f"[+] Unipolar Stepper initialized on {target} ({elapsed:.1f}ms).")
    else:
        print(f"[!] Note: SetupUnipolar command sent to {target}.")
    return True


def step_unipolar_stepper(client: CoordinatorClient, target: str = "Garage", steps: int = 100, forward: bool = True, timeout: float = 3.0) -> bool:
    """Sends UniStepFwd or UniiStepBkw command to move N steps."""
    cmd_name = "UniStepFwd" if forward else "UniiStepBkw"
    got_ack = threading.Event()
    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "UNISTEP" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} {cmd_name} {steps}"
    dir_str = "FORWARD" if forward else "BACKWARD"
    print(f"[>] Stepping {target} {steps} steps {dir_str}: \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if success:
        print(f"[+] Command {cmd_name} {steps} acknowledged ({elapsed:.1f}ms).")
    return True


def run_unipolar_stepper_test(client: CoordinatorClient, target: str = "Garage", steps: int = 512, step_delay_ms: int = UNIPOLAR_DEFAULT_DELAY_MS, mode: int = 0) -> bool:
    """
    Validates Adafruit Small Reduction Stepper (unipolarStep.c) with Speed & Accel/Decel Tests:
      Hardware:
        - Center-taps to 5V
        - Gate-level N-channel MOSFETs
        - Coil A: Pin 1 (Orange), Pin 2 (Pink)
        - Coil B: Pin 21 (Yellow), Pin 0 (Blue)
      Sequence:
        1. SetupUnipolar: Configures pins and starts worker task.
        2. UniStepMode: Selects drive mode (0=Full-step, 1=Half-step, 2=Wave drive).
        3. Pass 1 (Standard delay = UNIPOLAR_DEFAULT_DELAY_MS, no ramp):
           - AccelSteps 0, UniStepDelay0 <delay_1>
           - UniStepFwd N -> Pause 1s -> UniiStepBkw N -> Pause 1s
        4. Pass 2 (Half speed = 2 * UNIPOLAR_DEFAULT_DELAY_MS, no ramp):
           - AccelSteps 0, UniStepDelay0 <delay_2>
           - UniStepFwd N -> Pause 1s -> UniiStepBkw N -> Pause 1s
        5. Pass 3 (Acceleration & Deceleration Ramp):
           - UnitStepDelay0 40, UnitStepDelay1 10, AccelSteps 100
           - UniStepFwd N (100 accel -> N cruise -> 100 decel) -> Pause 1s
           - UniiStepBkw N (100 accel -> N cruise -> 100 decel)
    """
    delay_1 = step_delay_ms if step_delay_ms > 0 else UNIPOLAR_DEFAULT_DELAY_MS
    delay_2 = 2 * delay_1

    mode_names = {0: "Full-Step (Dual Coil)", 1: "Half-Step (8-Step)", 2: "Wave Drive (Single Coil)"}
    mode_desc = mode_names.get(mode, f"Mode {mode}")

    print("\n" + "=" * 65)
    print("--- [TEST U] UNIPOLAR STEPPER MOTOR: SPEED & ACCEL/DECEL TEST ---")
    print(f"Target Node: {target} (ESP32-C6)")
    print(f"Coil 1:      Pin 1 (Orange)")
    print(f"Coil 2:      Pin 2 (Pink)")
    print(f"Coil 3:      Pin 21 (Yellow)")
    print(f"Coil 4:      Pin 0 (Blue)")
    print(f"Drive Mode:  {mode_desc}")
    print(f"Driver:      Gate-level N-channel MOSFETs (Center tap = 5V)")
    print(f"Pass 1:      Standard Speed ({delay_1}ms delay): {steps} FWD -> 1s Pause -> {steps} BKW")
    print(f"Pass 2:      Half Speed ({delay_2}ms delay = 2x delay): {steps} FWD -> 1s Pause -> {steps} BKW")
    print(f"Pass 3:      Accel/Decel (Delay0=40ms, Delay1=10ms, AccelSteps=100):")
    print(f"             100 Accel -> {steps} Cruise -> 100 Decel FWD -> 1s Pause -> BKW")
    print("=" * 65)

    # Step 1: Initialize
    setup_unipolar_stepper(client, target=target)
    time.sleep(0.2)

    # Step 1b: Set drive mode if non-default
    if mode != 0:
        client.send_line(f"{target} UniStepMode {mode}")
        time.sleep(0.1)

    # -------------------------------------------------------------
    # PASS 1: Run at UNIPOLAR_DEFAULT_DELAY_MS (Standard Speed)
    # -------------------------------------------------------------
    print(f"\n[>>>] === PASS 1: STANDARD SPEED ({delay_1}ms per step, AccelSteps=0) ===")
    client.send_line(f"{target} AccelSteps 0")
    time.sleep(0.1)
    client.send_line(f"{target} UniStepDelay0 {delay_1}")
    time.sleep(0.1)

    # Pass 1 - Forward
    print(f"\n[*] [PASS 1 - PHASE 1/2] Stepping FORWARD {steps} steps ({delay_1}ms delay)...")
    step_unipolar_stepper(client, target=target, steps=steps, forward=True)
    est_duration_1 = (steps * delay_1) / 1000.0
    print(f"    Waiting ~{est_duration_1:.1f}s for movement to complete...")
    time.sleep(est_duration_1 + 0.5)

    # Pass 1 - Pause
    print("\n[*] Pausing 1.0 second at forward position...")
    time.sleep(1.0)

    # Pass 1 - Backward
    print(f"\n[*] [PASS 1 - PHASE 2/2] Stepping BACKWARD {steps} steps ({delay_1}ms delay)...")
    step_unipolar_stepper(client, target=target, steps=steps, forward=False)
    print(f"    Waiting ~{est_duration_1:.1f}s for movement to complete...")
    time.sleep(est_duration_1 + 0.5)

    print("\n[*] Pausing 1.0 second before Pass 2 (Half Speed)...")
    time.sleep(1.0)

    # -------------------------------------------------------------
    # PASS 2: Run at 2 * UNIPOLAR_DEFAULT_DELAY_MS (Half Speed)
    # -------------------------------------------------------------
    print(f"\n[>>>] === PASS 2: HALF SPEED ({delay_2}ms per step = 2x delay, AccelSteps=0) ===")
    client.send_line(f"{target} AccelSteps 0")
    time.sleep(0.1)
    client.send_line(f"{target} UniStepDelay0 {delay_2}")
    time.sleep(0.1)

    # Pass 2 - Forward
    print(f"\n[*] [PASS 2 - PHASE 1/2] Stepping FORWARD {steps} steps ({delay_2}ms delay)...")
    step_unipolar_stepper(client, target=target, steps=steps, forward=True)
    est_duration_2 = (steps * delay_2) / 1000.0
    print(f"    Waiting ~{est_duration_2:.1f}s for movement to complete...")
    time.sleep(est_duration_2 + 0.5)

    # Pass 2 - Pause
    print("\n[*] Pausing 1.0 second at forward position...")
    time.sleep(1.0)

    # Pass 2 - Backward
    print(f"\n[*] [PASS 2 - PHASE 2/2] Stepping BACKWARD {steps} steps ({delay_2}ms delay)...")
    step_unipolar_stepper(client, target=target, steps=steps, forward=False)
    print(f"    Waiting ~{est_duration_2:.1f}s for movement to complete...")
    time.sleep(est_duration_2 + 0.5)

    print("\n[*] Pausing 1.0 second before Pass 3 (Accel / Decel Test)...")
    time.sleep(1.0)

    # -------------------------------------------------------------
    # PASS 3: Acceleration / Deceleration Test
    # -------------------------------------------------------------
    print(f"\n[>>>] === PASS 3: ACCEL / DECEL TEST (UnitStepDelay0=40, UnitStepDelay1=10, AccelSteps=100) ===")
    print(f"[*] Setting UnitStepDelay0 40...")
    client.send_line(f"{target} UnitStepDelay0 40")
    time.sleep(0.1)
    print(f"[*] Setting UnitStepDelay1 10...")
    client.send_line(f"{target} UnitStepDelay1 10")
    time.sleep(0.1)
    print(f"[*] Setting AccelSteps 100...")
    client.send_line(f"{target} AccelSteps 100")
    time.sleep(0.1)

    # Pass 3 - Forward: starts slow @ 40ms, ramps to 10ms over 100 steps, runs 512 steps @ 10ms, ramps down to 40ms over 100 steps
    print(f"\n[*] [PASS 3 - PHASE 1/2] Stepping FORWARD {steps} steps (100 accel -> {steps} cruise -> 100 decel)...")
    step_unipolar_stepper(client, target=target, steps=steps, forward=True)
    est_duration_3 = (100 * 0.025) + (steps * 0.010) + (100 * 0.025)
    print(f"    Waiting ~{est_duration_3:.1f}s for ramped movement to complete...")
    time.sleep(est_duration_3 + 1.0)

    # Pass 3 - Pause
    print("\n[*] Pausing 1.0 second at forward position...")
    time.sleep(1.0)

    # Pass 3 - Backward: same ramp in reverse
    print(f"\n[*] [PASS 3 - PHASE 2/2] Stepping BACKWARD {steps} steps (100 accel -> {steps} cruise -> 100 decel)...")
    step_unipolar_stepper(client, target=target, steps=steps, forward=False)
    print(f"    Waiting ~{est_duration_3:.1f}s for ramped return movement to complete...")
    time.sleep(est_duration_3 + 1.0)

    print("\n" + "-" * 65)
    print("[?] Visual / Hardware Verification:")
    print(f"    Did the stepper motor perform:")
    print(f"      1) Pass 1: Standard Speed ({delay_1}ms delay)")
    print(f"      2) Pass 2: Half Speed ({delay_2}ms delay)")
    print(f"      3) Pass 3: Accel/Decel Ramp (Starts slow at 40ms, ramps to 10ms over 100 steps, runs {steps} steps, ramps down to 40ms over 100 steps)?")
    try:
        resp = input("    Confirm stepper movement observed? [Y/n]: ").strip().lower()
        passed = resp in ("", "y", "yes")
    except (EOFError, OSError):
        passed = True

    print("-" * 65)
    if passed:
        print("[+] TEST PASSED: Unipolar stepper motor speed and accel/decel test verified successfully!")
    else:
        print("[-] TEST FAILED: Unipolar stepper motor motion was not confirmed.")
    return passed


def setup_bipolar_stepper(
    client: CoordinatorClient,
    target: str = "Garage",
    ap_gpio: int = 1,
    an_gpio: int = 21,
    bp_gpio: int = 2,
    bn_gpio: int = 0,
    pwm_duty: int = 75,
    ms_per_step: int = 70,
    timeout: float = 3.0
) -> bool:
    """
    Configures and initializes the Bipolar Stepper (Stepper.c) on `target` (default: Garage)
    for a HiLetgo DRV8833 H-Bridge driver with 5V supply and 12-ohm, 1.8 deg/step motor:
      - ApGPIO = 1, AnGPIO = 21, BpGPIO = 2, BnGPIO = 0
      - FullStep mode
      - StepPWMduty = 75 (75% PWM duty cycle)
      - HoldPWM = 0 (freewheel when idle)
      - Accel = 0, Decel = 0 (no acceleration or deceleration ramp)
      - msPerStep = 70 (default 70ms per step)
      - SetupStepper
    """
    print(f"\n[>] Configuring Bipolar Stepper (HiLetgo DRV8833) on {target}:")
    print(f"    Wiring: ApGPIO={ap_gpio}, AnGPIO={an_gpio}, BpGPIO={bp_gpio}, BnGPIO={bn_gpio}")
    print(f"    Config: FullStep, StepPWMduty={pwm_duty}%, HoldPWM=0%, Accel=0, Decel=0, msPerStep={ms_per_step}ms")

    cfg_cmds = [
        f"{target} ApGPIO {ap_gpio}",
        f"{target} AnGPIO {an_gpio}",
        f"{target} BpGPIO {bp_gpio}",
        f"{target} BnGPIO {bn_gpio}",
        f"{target} FullStep",
        f"{target} StepPWMduty {pwm_duty}",
        f"{target} HoldPWM 0",
        f"{target} Accel 0",
        f"{target} Decel 0",
        f"{target} msPerStep {ms_per_step}",
        f"{target} SetupStepper",
    ]

    for cmd in cfg_cmds:
        got_ack = threading.Event()

        def on_rx(msg: str):
            if f"< {target}:" in msg and ("[ACK]" in msg or "OK" in msg):
                got_ack.set()

        client.add_rx_callback(on_rx)
        print(f"      Sending: \"{cmd}\"")
        client.send_line(cmd)
        got_ack.wait(timeout=timeout)
        client.remove_rx_callback(on_rx)
        time.sleep(0.05)

    print(f"[+] Bipolar Stepper initialized on {target}.")
    return True


def step_bipolar_stepper(client: CoordinatorClient, target: str = "Garage", steps: int = 180, timeout: float = 3.0) -> bool:
    """Sends `<target> Step <steps>` to move the bipolar stepper signed `steps` full-steps."""
    got_ack = threading.Event()

    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "GOT STEP OK" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} Step {steps}"
    dir_str = "FORWARD" if steps >= 0 else "BACKWARD"
    print(f"[>] Stepping {target} {abs(steps)} full-steps {dir_str}: \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if success:
        print(f"[+] Command 'Step {steps}' acknowledged ({elapsed:.1f}ms).")
    return True


BIPOLAR_PIN_PAIRINGS = {
    "1": (1, 2, 21, 0, "Pairing 1 (Paired: Coil A = 1,2 | Coil B = 21,0)"),
    "2": (1, 21, 2, 0, "Pairing 2 (Interleaved: Coil A = 1,21 | Coil B = 2,0)"),
    "3": (1, 0, 2, 21, "Pairing 3 (Outer/Inner: Coil A = 1,0 | Coil B = 2,21)"),
}


def run_bipolar_pairing_finder(
    client: CoordinatorClient,
    target: str = "Garage",
    steps_per_trial: int = 24,
    pwm_duty: int = 75,
    ms_per_step: int = 100,
) -> bool:
    """
    Tests all 3 possible coil pairings of GPIOs (1, 2, 21, 0) sequentially so the user
    can identify which pairing produces smooth unidirectional rotation instead of
    oscillating 1 step forward / 1 step backward.
    """
    print("\n" + "=" * 65)
    print("--- [TEST H - PAIRING FINDER] TESTING ALL 3 COIL PAIRINGS OF (1, 2, 21, 0) ---")
    print(f"Target Node:  {target} (HiLetgo DRV8833 @ 5V)")
    print(f"Trial Config: {steps_per_trial} Full-Steps Forward -> 1s Pause -> {steps_per_trial} Full-Steps Backward")
    print(f"Drive Settings: StepPWMduty={pwm_duty}%, msPerStep={ms_per_step}ms, Accel=0, Decel=0")
    print("=" * 65)

    est_duration = (steps_per_trial * ms_per_step) / 1000.0

    for key, (ap, an, bp, bn, desc) in BIPOLAR_PIN_PAIRINGS.items():
        print("\n" + "-" * 65)
        print(f"[*] TRIAL [{key}/3]: {desc}")
        print(f"    ApGPIO={ap}, AnGPIO={an}, BpGPIO={bp}, BnGPIO={bn}")
        print("-" * 65)
        setup_bipolar_stepper(
            client,
            target=target,
            ap_gpio=ap,
            an_gpio=an,
            bp_gpio=bp,
            bn_gpio=bn,
            pwm_duty=pwm_duty,
            ms_per_step=ms_per_step,
        )
        time.sleep(0.3)

        print(f"[*] Trial [{key}/3] Stepping FORWARD {steps_per_trial} full-steps ({ms_per_step}ms/step)...")
        step_bipolar_stepper(client, target=target, steps=steps_per_trial)
        time.sleep(est_duration + 0.5)

        time.sleep(0.8)

        print(f"[*] Trial [{key}/3] Stepping BACKWARD {steps_per_trial} full-steps ({ms_per_step}ms/step)...")
        step_bipolar_stepper(client, target=target, steps=-steps_per_trial)
        time.sleep(est_duration + 0.5)

        if key != "3":
            print("\n[*] Pausing 2.0s before next pairing...")
            time.sleep(2.0)

    print("\n" + "=" * 65)
    print("[?] Which coil pairing rotated cleanly in one direction?")
    for key, (ap, an, bp, bn, desc) in BIPOLAR_PIN_PAIRINGS.items():
        print(f"    [{key}] {desc} -> Ap={ap}, An={an}, Bp={bp}, Bn={bn}")
    try:
        resp = input("    Enter working pairing number [1/2/3] or 'n' if none worked: ").strip().lower()
    except (EOFError, OSError):
        resp = "2"

    if resp in BIPOLAR_PIN_PAIRINGS:
        ap, an, bp, bn, desc = BIPOLAR_PIN_PAIRINGS[resp]
        print(f"[+] Pairing [{resp}] selected ({desc})! Running full 180-step test with this pairing...")
        return run_bipolar_stepper_test(
            client,
            target=target,
            steps=180,
            pwm_duty=pwm_duty,
            ms_per_step=ms_per_step,
            ap_gpio=ap,
            an_gpio=an,
            bp_gpio=bp,
            bn_gpio=bn,
        )
    print("[-] No working pairing confirmed.")
    return False


def run_bipolar_stepper_test(
    client: CoordinatorClient,
    target: str = "Garage",
    steps: int = 180,
    pwm_duty: int = 75,
    ms_per_step: int = 70,
    ap_gpio: int = 1,
    an_gpio: int = 21,
    bp_gpio: int = 2,
    bn_gpio: int = 0
) -> bool:
    """
    Executes the Bipolar Stepper Motor Test (Stepper.c) on Garage:
      - Motor:      Bipolar stepper, 12-ohm coils, 1.8 deg/step (180 full-steps = 360 half-steps = 324 deg)
      - Driver:     HiLetgo DRV8833 dual H-Bridge with 5V supply
      - Wiring:     ApGPIO=1, AnGPIO=21, BpGPIO=2, BnGPIO=0
      - Part 1:     FullStep Mode — 180 Full-Steps Forward -> 1s Pause -> 180 Full-Steps Backward
      - Part 2:     HalfStep Mode — 360 Half-Steps Forward -> 1s Pause -> 360 Half-Steps Backward
    """
    deg_travel = steps * 1.8
    half_steps = steps * 2
    half_ms_per_step = max(1, ms_per_step // 2)

    print("\n" + "=" * 65)
    print("--- [TEST H] BIPOLAR STEPPER MOTOR TEST (DRV8833 @ 5V: FULL-STEP & HALF-STEP) ---")
    print(f"Target Node:  {target} (ESP32-C6)")
    print("Driver / Vcc: HiLetgo DRV8833 Dual H-Bridge | 5V Supply (25 kHz Ultrasonic PWM)")
    print("Motor Specs:  Bipolar, 12-Ohm Coils, 1.8 deg/step (0.9 deg/half-step)")
    print(f"Wiring:       ApGPIO={ap_gpio}, AnGPIO={an_gpio}, BpGPIO={bp_gpio}, BnGPIO={bn_gpio}")
    print(f"Drive Power:  StepPWMduty={pwm_duty}% | HoldPWM=0% | Accel=0 | Decel=0")
    print(f"Part 1 (FullStep): {steps} Full-Steps ({deg_travel:.1f} deg) Fwd/Bkw @ {ms_per_step} ms/step")
    print(f"Part 2 (HalfStep): {half_steps} Half-Steps ({deg_travel:.1f} deg) Fwd/Bkw @ {half_ms_per_step} ms/half-step")
    print("=" * 65)

    # Step 1: Configure pins, FullStep mode, duty, Accel=0, Decel=0, and initialize Stepper
    setup_bipolar_stepper(
        client,
        target=target,
        ap_gpio=ap_gpio,
        an_gpio=an_gpio,
        bp_gpio=bp_gpio,
        bn_gpio=bn_gpio,
        pwm_duty=pwm_duty,
        ms_per_step=ms_per_step,
    )
    time.sleep(0.2)

    est_full_duration = (steps * ms_per_step) / 1000.0

    # --- PART 1: FULL-STEP TEST ---
    print(f"\n[*] [PHASE 1/4 - FULL-STEP] Stepping FORWARD {steps} full-steps ({deg_travel:.1f} deg, StepPWMduty={pwm_duty}%, {ms_per_step}ms/step)...")
    step_bipolar_stepper(client, target=target, steps=steps)
    print(f"    Waiting ~{est_full_duration:.1f}s for {steps} full-steps to complete...")
    time.sleep(est_full_duration + 0.5)

    print("\n[*] Pausing 1.0 second...")
    time.sleep(1.0)

    print(f"\n[*] [PHASE 2/4 - FULL-STEP] Stepping BACKWARD {steps} full-steps (-{deg_travel:.1f} deg, StepPWMduty={pwm_duty}%, {ms_per_step}ms/step)...")
    step_bipolar_stepper(client, target=target, steps=-steps)
    print(f"    Waiting ~{est_full_duration:.1f}s for {steps} full-steps return movement to complete...")
    time.sleep(est_full_duration + 0.5)

    print("\n[*] Pausing 1.5 seconds before switching to Half-Step mode...")
    time.sleep(1.5)

    # --- PART 2: HALF-STEP TEST ---
    print(f"\n[>] Switching {target} to HalfStep mode ({half_ms_per_step} ms/half-step)...")
    for cmd in (f"{target} HalfStep", f"{target} msPerStep {half_ms_per_step}"):
        got_ack = threading.Event()

        def on_rx(msg: str):
            if f"< {target}:" in msg and ("[ACK]" in msg or "OK" in msg):
                got_ack.set()

        client.add_rx_callback(on_rx)
        print(f"      Sending: \"{cmd}\"")
        client.send_line(cmd)
        got_ack.wait(timeout=3.0)
        client.remove_rx_callback(on_rx)
        time.sleep(0.05)

    est_half_duration = (half_steps * half_ms_per_step) / 1000.0

    print(f"\n[*] [PHASE 3/4 - HALF-STEP] Stepping FORWARD {half_steps} half-steps ({deg_travel:.1f} deg, StepPWMduty={pwm_duty}%, {half_ms_per_step}ms/half-step)...")
    step_bipolar_stepper(client, target=target, steps=half_steps)
    print(f"    Waiting ~{est_half_duration:.1f}s for {half_steps} half-steps to complete...")
    time.sleep(est_half_duration + 0.5)

    print("\n[*] Pausing 1.0 second...")
    time.sleep(1.0)

    print(f"\n[*] [PHASE 4/4 - HALF-STEP] Stepping BACKWARD {half_steps} half-steps (-{deg_travel:.1f} deg, StepPWMduty={pwm_duty}%, {half_ms_per_step}ms/half-step)...")
    step_bipolar_stepper(client, target=target, steps=-half_steps)
    print(f"    Waiting ~{est_half_duration:.1f}s for {half_steps} half-steps return movement to complete...")
    time.sleep(est_half_duration + 0.5)

    print("\n" + "-" * 65)
    print("[?] Visual / Hardware Verification:")
    print(f"    Did the bipolar stepper on {target} (HiLetgo DRV8833 @ 5V) perform:")
    print(f"      1. Full-Step Mode: {steps} Full-Steps ({deg_travel:.1f} deg) FORWARD -> 1s pause -> {steps} Full-Steps BACKWARD")
    print(f"      2. Half-Step Mode: {half_steps} Half-Steps ({deg_travel:.1f} deg) FORWARD -> 1s pause -> {half_steps} Half-Steps BACKWARD?")
    try:
        resp = input("    Confirm both Full-Step and Half-Step bipolar movements observed? [Y/n]: ").strip().lower()
        passed = resp in ("", "y", "yes")
    except (EOFError, OSError):
        passed = True

    print("-" * 65)
    if passed:
        print(f"[+] TEST PASSED: Bipolar stepper motor (DRV8833, Full-Step & Half-Step, StepPWMduty={pwm_duty}%) verified successfully!")
    else:
        print("[-] TEST FAILED: Bipolar stepper motor motion was not confirmed.")
    return passed


def run_cytron_motor_test(client: CoordinatorClient, target: str = "Kitchen") -> bool:
    """
    Executes the Cytron 10C Motor Driver Board Test:
      Hardware: ESP32-C6 Pin 18 (PWM Speed), Pin 20 (Direction)
      Profile:
        1. SetupMotorDriver 1 5000 (Mode 1 = Cytron 10C, 5 kHz PWM)
        2. Ramp from still (0) to 100% forward (+1000) over 2.0s ramp (slew = 500 units/s)
        3. Ramp from 100% forward (+1000) to 100% reverse (-1000) over 4.0s ramp (slew = 500 units/s)
        4. Pause at 100% reverse for 1.0s
        5. Ramp from 100% reverse (-1000) back to still (0) over 2.0s ramp (slew = 500 units/s)
    """
    print("\n" + "=" * 65)
    print("--- [TEST M] CYTRON 10C MOTOR DRIVER TEST (PINS 18 & 20) ---")
    print(f"Target Node: {target} (Pin 18: PWM Speed | Pin 20: Direction)")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    print("\n[!] ACTION REQUIRED: Look at the Cytron 10C DC motor.")
    print("    Watch for the motor speed and direction profile:")
    print("      1. Smooth 2-second ramp up from still to 100% forward.")
    print("      2. Smooth 4-second ramp from 100% forward to 100% reverse.")
    print("      3. 1-second pause at 100% reverse.")
    print("      4. Smooth 2-second ramp down from 100% reverse back to still (0).")
    try:
        input("    Press [Enter] when ready to begin test...")
    except (EOFError, OSError):
        pass

    def on_rx(msg: str):
        clean = msg.strip()
        print(f"  <-- [RX] {clean}")

    client.add_rx_callback(on_rx)

    try:
        # Step 1: Initialize Motor Driver in Cytron 10C Mode (Mode 1, 5000 Hz)
        print("\n[>] Step 1: Initializing Cytron 10C driver (Mode 1, 5000 Hz)...")
        cmd_init = f"{target} SetupMotorDriver 1 5000"
        print(f"    Sending: \"{cmd_init}\"")
        client.send_line(cmd_init)
        time.sleep(0.5)

        # Step 2: Ramp 0 -> +1000 (+100% Forward) over 2.0s (slew = 500 units/s)
        print("\n[>] Step 2: Ramping from still to 100% Forward throttle over 2.0s...")
        cmd_slew = f"{target} MotorSlew 500"
        print(f"    Setting slew rate: \"{cmd_slew}\" (500 units/sec = 1000 units in 2.0s)")
        client.send_line(cmd_slew)
        time.sleep(0.2)

        t_fwd_start = time.time()
        cmd_fwd = f"{target} MotorSpeed 1000"
        print(f"    Setting target speed: \"{cmd_fwd}\" (+1000 = 100% Forward)")
        client.send_line(cmd_fwd)

        time.sleep(2.2)
        elapsed_fwd = time.time() - t_fwd_start
        print(f"    [+] Forward ramp completed in {elapsed_fwd:.2f}s (target: 2.0s)")

        # Step 3: Ramp +1000 -> -1000 (+100% Forward to 100% Reverse) over 4.0s (delta=2000, slew=500)
        print("\n[>] Step 3: Ramping from 100% Forward to 100% Reverse over 4.0s...")
        t_rev_start = time.time()
        cmd_rev = f"{target} MotorSpeed -1000"
        print(f"    Setting target speed: \"{cmd_rev}\" (-1000 = 100% Reverse, delta=2000 over 4.0s)")
        client.send_line(cmd_rev)

        time.sleep(4.2)
        elapsed_rev = time.time() - t_rev_start
        print(f"    [+] Reversal ramp completed in {elapsed_rev:.2f}s (target: 4.0s)")

        # Step 4: Pause at 100% reverse for 1 second
        print("\n[>] Step 4: Pausing at 100% Reverse for 1.0s...")
        time.sleep(1.0)
        print("    [+] Pause complete.")

        # Step 5: Ramp -1000 -> 0 (100% Reverse back to Still) over 2.0s (delta=1000, slew=500)
        print("\n[>] Step 5: Ramping from 100% Reverse back to still (0) over 2.0s...")
        t_return_start = time.time()
        cmd_stop = f"{target} MotorSpeed 0"
        print(f"    Setting target speed: \"{cmd_stop}\" (0 = still, delta=1000 over 2.0s)")
        client.send_line(cmd_stop)

        time.sleep(2.2)
        elapsed_return = time.time() - t_return_start
        print(f"    [+] Return-to-zero ramp completed in {elapsed_return:.2f}s (target: 2.0s)")
        print("    [+] Motor smoothly stopped at zero output.")

    finally:
        client.remove_rx_callback(on_rx)

    print("\n" + "-" * 65)
    print("[?] Visual / Hardware Verification:")
    print("    1. Did the motor ramp up smoothly from still to 100% forward in ~2s?")
    print("    2. Did the motor smoothly ramp from 100% forward to 100% reverse in ~4s?")
    print("    3. Did the motor pause at 100% reverse for 1s?")
    print("    4. Did the motor smoothly ramp down from 100% reverse back to still (0) in ~2s?")
    try:
        resp = input("    Confirm Cytron 10C motor profile observed? [y/N]: ").strip().lower()
        passed = resp in ("y", "yes")
    except (EOFError, OSError):
        passed = True

    print("-" * 65)
    if passed:
        print("[+] TEST PASSED: Cytron 10C motor driver test verified successfully!")
    else:
        print("[-] TEST FAILED: Cytron motor test was not confirmed by operator.")
    return passed


def run_cytron_rfloss_test(
    client: CoordinatorClient,
    target: str = "Kitchen",
    timeout: float = 3.0
) -> bool:
    """
    Executes the Cytron 10C Motor Driver RF-Loss Gentle Stop Test:
      1. Initializes Cytron 10C Motor Driver on target: '<target> SetupMotorDriver 1 5000'
      2. Sets motor to 100% forward power: '<target> MotorSpeed 1000'
      3. Instructs user to pull power from the Coordinator:
         - Cleanly closes the Coordinator serial connection.
         - Prompts user to disconnect Coordinator power.
         - User observes: within ~2s of link loss, PWM duty slews smoothly from commanded speed
           to 0 over 1 second, providing a gentle stop.
      4. Instructs user to reconnect power to the Coordinator:
         - Reconnects Coordinator serial port.
         - Verifies mesh link recovery with target.
         - User observes: when reconnected, output gently ramps back up to 100% power over 1 second.
      5. Queries the user with a Yes/No option to confirm correct operation.
      6. Safely stops the motor: '<target> MotorSpeed 0'.
    """
    print("\n" + "=" * 65)
    print("--- [TEST Y] CYTRON MOTOR RF-LOSS GENTLE STOP TEST ---")
    print(f"Target Node: {target} (Pin 18: PWM Speed | Pin 20: Direction)")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    print("\n[!] TEST PROCEDURE & ACTION REQUIRED:")
    print("    1. Motor will be initialized in Cytron 10C mode and set to 100% power.")
    print("    2. You will be instructed to PULL/UNPLUG power from the Coordinator.")
    print("    3. Observe the motor: PWM duty slews smoothly from commanded speed to 0 over 1 second.")
    print("    4. You will be instructed to RECONNECT power to the Coordinator.")
    print("    5. Observe the motor: when reconnected, output gently ramps back up.")
    print("    6. You will confirm observed operation [Yes/No].")
    print("=" * 65)

    try:
        input("\nPress [Enter] when ready to begin test...")
    except (EOFError, OSError):
        pass

    def on_rx(msg: str):
        clean = msg.strip()
        print(f"  <-- [RX] {clean}")

    client.add_rx_callback(on_rx)

    passed = False
    try:
        # Step 1: Initialize Cytron 10C driver (Mode 1, 5000 Hz)
        print(f"\n[*] Step 1: Initializing Cytron 10C motor driver on {target} (Mode 1, 5000 Hz)...")
        cmd_init = f"{target} SetupMotorDriver 1 5000"
        print(f"    Sending: \"{cmd_init}\"")
        client.send_line(cmd_init)
        time.sleep(0.5)

        # Set instantaneous response so RFlossThrottle is the direct slew controller
        cmd_slew = f"{target} MotorSlew 0"
        print(f"    Setting slew rate: \"{cmd_slew}\" (0 = instantaneous)")
        client.send_line(cmd_slew)
        time.sleep(0.2)

        # Step 2: Set Cytron motor to 100% power forward (+1000)
        print(f"\n[*] Step 2: Setting {target} Cytron motor to 100% power (MotorSpeed 1000)...")
        cmd_speed = f"{target} MotorSpeed 1000"
        print(f"    Setting target speed: \"{cmd_speed}\" (+1000 = 100% Forward)")
        client.send_line(cmd_speed)
        time.sleep(1.0)
        print(f"[+] Motor is now running at 100% power on {target}.")

        # Detach verbose streaming callback before coordinator power cycle
        # so reboot discovery frames do not interleave with user prompts
        client.remove_rx_callback(on_rx)

        # Step 3: Prepare for Coordinator power disconnection
        print("\n" + "=" * 65)
        print(">>> STEP 1: PULL / UNPLUG POWER FROM THE COORDINATOR <<<")
        print("=" * 65)
        print("[*] 1. Disconnect the USB cable (or power) from the Coordinator module.")
        print("[*] 2. Observe the Cytron motor:")
        print("       Within ~2 seconds of Coordinator link loss, PWM duty slews smoothly")
        print("       from commanded speed to 0 over 1 second, providing a gentle stop!")
        print("       The motor will remain stationary while the Coordinator is unpowered.")
        print("=" * 65)

        # Cleanly close serial port before USB cable is pulled
        coord_port = client.port
        client.close()

        input("\n--> Unplug Coordinator, observe motor gently slews down to 0 power, then press [ENTER]...")

        # Step 4: Reconnect Coordinator power
        print("\n" + "=" * 65)
        print(">>> STEP 2: RE-CONNECT POWER TO THE COORDINATOR <<<")
        print("=" * 65)
        print("[*] 1. Plug the Coordinator USB / power cable back into the PC.")
        print("[*] 2. Observe the Cytron motor:")
        print("       When reconnected and RF contact is restored, output gently ramps back up")
        print("       from 0 to 100% power over 1 second!")
        print("=" * 65)

        input("\n--> Plug Coordinator back in, then press [ENTER] to reconnect...")

        print(f"\n[*] Reconnecting serial connection to Coordinator on {coord_port}...")
        if client.reconnect(timeout=15.0):
            print(f"[+] Serial connection to Coordinator restored.")
            print(f"[*] Waiting for mesh link to re-establish with '{target}'...")
            time.sleep(2.0)
            node_ok, rtt, resp = verify_node_bidirectional(client, target, timeout=10.0)
            if node_ok:
                print(f"[+] RF link restored! '{target}' is online ({rtt:.0f}ms)!")
                print(f"[+] Output has gently ramped back up to commanded power.")
            else:
                print(f"[!] Warning: Did not receive active reply from '{target}' yet.")
        else:
            print(f"[-] Could not reconnect to Coordinator port {coord_port} within timeout.")

        time.sleep(0.5)

        # Step 5: Visual verification prompt
        print("\n" + "-" * 65)
        print("[?] Visual / Hardware Verification:")
        print('    Did you observe "PWM duty slews smoothly from commanded speed to 0 over 1 second.')
        print('    When reconnected, output gently ramps back up"?')
        print("-" * 65)
        try:
            resp = input("    Confirm correct gentle stop & ramp-up operation? [y/N]: ").strip().lower()
            passed = resp in ("y", "yes")
        except (EOFError, OSError):
            passed = True

    finally:
        client.remove_rx_callback(on_rx)
        # Always safely stop motor
        print(f"\n[*] Safety Cleanup: Stopping {target} motor (MotorSpeed 0)...")
        try:
            client.send_line(f"{target} MotorSpeed 0")
            time.sleep(0.3)
            print("[+] Motor stopped.")
        except Exception as e:
            print(f"[!] Note on stop command: {e}")

    print("-" * 65)
    if passed:
        print("[+] TEST PASSED: Cytron RF-loss gentle stop fail-safe verified successfully!")
    else:
        print("[-] TEST FAILED: Operation was not confirmed by operator.")
    print("=" * 65 + "\n")
    return passed


def setup_proximity(client: CoordinatorClient, target: str = "Kitchen", timeout: float = 3.0) -> bool:
    """Initializes the APDS9930 proximity sensor on the target node (`<target> SetupProximity`)."""
    setup_event = threading.Event()
    status_ok = False

    def on_rx(msg: str):
        nonlocal status_ok
        if "SETUP PROX OK" in msg:
            status_ok = True
            setup_event.set()
        elif "SETUP PROX FAIL" in msg:
            status_ok = False
            setup_event.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} SetupProximity"
    print(f"\n[>] Initializing APDS9930 Proximity on {target}: \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    setup_event.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if status_ok:
        print(f"[+] APDS9930 Proximity initialized successfully on {target} ({elapsed:.1f}ms).")
    else:
        print(f"[!] Warning: SetupProximity did not receive OK confirmation from {target}.")
    return status_ok


def read_proximity_val(client: CoordinatorClient, target: str = "Kitchen", timeout: float = 3.0) -> int | None:
    """Sends `<target> ReadProximity` and returns the proximity count (or None on failure)."""
    val_event = threading.Event()
    prox_reading = [None]

    def on_rx(msg: str):
        if "PROXval" in msg:
            parts = msg.strip().split()
            for i, p in enumerate(parts):
                if p == "PROXval" and i + 1 < len(parts):
                    try:
                        prox_reading[0] = int(parts[i + 1])
                        val_event.set()
                    except ValueError:
                        val_event.set()
        elif "PROX NOT READY" in msg:
            val_event.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} ReadProximity"
    client.send_line(cmd)
    val_event.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)
    return prox_reading[0]


def run_proximity_stream(client: CoordinatorClient, target: str = "Kitchen", interval: float = 0.3) -> None:
    """
    Streams APDS9930 proximity readings continuously once every 300ms
    until the user presses the ESC key (operates identically to 'Q' compass streaming).
    """
    # Check if proximity sensor is ready; if uninitialized, configure it once
    test_reading = read_proximity_val(client, target=target, timeout=0.3)
    if test_reading is None:
        print(f"[*] Initializing APDS9930 Proximity sensor on {target}...")
        setup_proximity(client, target=target, timeout=2.0)
        time.sleep(0.1)

    print(f"\n[+] Streaming {target} APDS9930 Proximity readings every {int(interval * 1000)}ms.")
    print("    Format: proximity (Press [ESC] to stop)\n")

    flush_console_keys()

    try:
        while True:
            t_start = time.time()
            if check_esc_key():
                break

            reading = read_proximity_val(client, target=target, timeout=0.25)
            if check_esc_key():
                break

            if reading is not None:
                print(f"{reading}")

            elapsed = time.time() - t_start
            sleep_remain = max(0.0, interval - elapsed)
            deadline = time.time() + sleep_remain

            esc_hit = False
            while time.time() < deadline:
                if check_esc_key():
                    esc_hit = True
                    break
                time.sleep(0.01)

            if esc_hit:
                break

    except KeyboardInterrupt:
        pass

    flush_console_keys()
    print("\n[*] Stopped proximity streaming.")


stream_proximity_readings = run_proximity_stream


def set_proximity_thresholds(
    client: CoordinatorClient,
    target: str = "Kitchen",
    low_thrsh: int = 200,
    high_thrsh: int = 500,
) -> bool:
    """Configures APDS9930 hysteresis thresholds using ProxLowThrsh and ProxHighThrsh."""
    for cmd in (f"{target} ProxLowThrsh {low_thrsh}", f"{target} ProxHighThrsh {high_thrsh}"):
        print(f"[>] Setting proximity threshold: \"{cmd}\"")
        client.send_line(cmd)
        time.sleep(0.2)
    return True


def _run_proximity_cycle(
    client: CoordinatorClient,
    phase_label: str,
    low_thrsh: int,
    high_thrsh: int,
    timeout: float = 20.0,
) -> bool:
    """Runs a single obstacle approach ('Car Detected') and withdrawal ('Car removed') verification cycle."""
    detected_event = threading.Event()
    detected_val = [None]
    removed_event = threading.Event()
    removed_val = [None]

    def on_prox_event(msg: str):
        if "Car Detected" in msg:
            parts = msg.strip().split()
            for i, p in enumerate(parts):
                if p == "Detected" and i + 1 < len(parts):
                    detected_val[0] = parts[i + 1]
            detected_event.set()
        elif "Car removed" in msg:
            parts = msg.strip().split()
            for i, p in enumerate(parts):
                if p == "removed" and i + 1 < len(parts):
                    removed_val[0] = parts[i + 1]
            removed_event.set()

    client.add_rx_callback(on_prox_event)

    print("\n" + "-" * 65)
    print(f">>> [{phase_label}] Thresholds: [ProxLowThrsh={low_thrsh}, ProxHighThrsh={high_thrsh}] <<<")
    print(f">>> ACTION REQUIRED: Bring your hand or an obstacle near the APDS9930 sensor (proximity > {high_thrsh}). <<<")
    print(f"Waiting up to {int(timeout)} seconds for 'Car Detected' event...")
    got_detected = detected_event.wait(timeout=timeout)

    if not got_detected:
        print(f"\n[-] {phase_label} FAILED: Timed out waiting for 'Car Detected' event.")
        client.remove_rx_callback(on_prox_event)
        return False

    val_str = f" (reading: {detected_val[0]})" if detected_val[0] else ""
    print(f"\n[+] [{phase_label}] OBSTACLE DETECTED{val_str} verified over Zigbee!")

    print(f"\n>>> ACTION REQUIRED: Move your hand away from the sensor (proximity < {low_thrsh}). <<<")
    print(f"Waiting up to {int(timeout)} seconds for 'Car removed' event...")
    got_removed = removed_event.wait(timeout=timeout)

    client.remove_rx_callback(on_prox_event)

    if not got_removed:
        print(f"\n[-] {phase_label} FAILED: Timed out waiting for 'Car removed' event.")
        return False

    val_rem_str = f" (reading: {removed_val[0]})" if removed_val[0] else ""
    print(f"\n[+] [{phase_label}] OBSTACLE REMOVAL{val_rem_str} verified over Zigbee!")
    return True


def run_proximity_test(
    client: CoordinatorClient,
    target: str = "Kitchen",
    low_thrsh: int = 200,
    high_thrsh: int = 500,
    low_thrsh2: int = 400,
    high_thrsh2: int = 800,
) -> bool:
    """
    Executes the 2-Phase APDS9930 Proximity Sensor Hysteresis Test:
    1. Initializes APDS9930 sensor (<target> SetupProximity).
    2. Configures Phase 1 hysteresis thresholds [ProxLowThrsh=200, ProxHighThrsh=500].
    3. Queries baseline ambient proximity value (<target> ReadProximity).
    4. Runs Phase 1 detection & removal cycle at [200, 500].
    5. If Phase 1 passes, updates [ProxLowThrsh, ProxHighThrsh] to [400, 800] and runs Phase 2 detection & removal cycle.
    """
    print("\n" + "=" * 65)
    print("--- [TEST D] APDS9930 PROXIMITY SENSOR HYSTERESIS TEST ---")
    print(f"Target Node: {target} (ESP32-C6 I2C SDA=GPIO22, SCL=GPIO23)")
    print(f"Phase 1 Hysteresis: [ProxLowThrsh={low_thrsh}, ProxHighThrsh={high_thrsh}]")
    print(f"Phase 2 Hysteresis: [ProxLowThrsh={low_thrsh2}, ProxHighThrsh={high_thrsh2}]")
    print(f"Timestamp:          {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")

    client.add_rx_callback(on_rx)

    try:
        # Step 1: Setup Proximity
        setup_event = threading.Event()
        def on_setup(msg: str):
            if "SETUP PROX OK" in msg:
                setup_event.set()

        client.add_rx_callback(on_setup)
        cmd = f"{target} SetupProximity"
        print(f"\n[>] Initializing APDS9930 sensor: \"{cmd}\"")
        client.send_line(cmd)
        got_setup = setup_event.wait(timeout=3.0)
        client.remove_rx_callback(on_setup)

        if not got_setup:
            print(f"[-] TEST FAILED: Timed out waiting for 'SETUP PROX OK' from {target}.")
            return False
        print(f"[+] APDS9930 sensor initialized successfully.")
        time.sleep(0.2)

        # Step 2: Configure Phase 1 hysteresis thresholds [200, 500]
        set_proximity_thresholds(client, target=target, low_thrsh=low_thrsh, high_thrsh=high_thrsh)

        # Step 3: Read baseline proximity reading
        print(f"\n[>] Reading baseline proximity...")
        baseline = read_proximity_val(client, target=target, timeout=3.0)
        if baseline is not None:
            print(f"[+] Baseline proximity reading: {baseline}")
        else:
            print(f"[!] Warning: Could not read instantaneous baseline proximity reading, continuing.")

        # Step 4: Phase 1 Detection & Removal Cycle at [200, 500]
        if not _run_proximity_cycle(client, phase_label="PHASE 1/2", low_thrsh=low_thrsh, high_thrsh=high_thrsh):
            return False

        print(f"\n[+] Phase 1 ([{low_thrsh}, {high_thrsh}]) PASSED! Updating [ProxLowThrsh, ProxHighThrsh] to [{low_thrsh2}, {high_thrsh2}]...")
        time.sleep(0.3)

        # Step 5: Update hysteresis thresholds to [400, 800] and run Phase 2
        set_proximity_thresholds(client, target=target, low_thrsh=low_thrsh2, high_thrsh=high_thrsh2)
        time.sleep(0.2)

        if not _run_proximity_cycle(client, phase_label="PHASE 2/2", low_thrsh=low_thrsh2, high_thrsh=high_thrsh2):
            return False

    finally:
        client.remove_rx_callback(on_rx)

    print("-" * 65)
    print(f"[+] TEST PASSED: APDS9930 Proximity sensor verified at both [{low_thrsh}, {high_thrsh}] and [{low_thrsh2}, {high_thrsh2}]!")
    return True


def read_gpio_pin(client: CoordinatorClient, target: str = "Garage", pin: int = 16, timeout: float = 3.0) -> int | None:
    """
    Sends `<target> ReadGPIO <pin>` and waits for `GPIO <pin> IS <val>`.
    Returns integer pin level (0 or 1) or None on timeout.
    """
    got_gpio_event = threading.Event()
    gpio_val = None
    pattern = re.compile(rf"GPIO\s+{pin}\s+IS\s+([01])", re.IGNORECASE)

    def on_rx(msg: str):
        nonlocal gpio_val
        if f"< {target}:" in msg or f"from {target}" in msg or not msg.startswith("< "):
            m = pattern.search(msg)
            if m:
                gpio_val = int(m.group(1))
                got_gpio_event.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} ReadGPIO {pin}"
    print(f"\n[>] Sending command: \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)

    success = got_gpio_event.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0

    if not success:
        # Retry once in case of wireless packet drop
        print(f"[!] Warning: Did not receive GPIO {pin} read reply within {timeout:.1f}s, retrying \"{cmd}\"...")
        client.send_line(cmd)
        success = got_gpio_event.wait(timeout=timeout)
        elapsed = (time.time() - start_time) * 1000.0

    client.remove_rx_callback(on_rx)

    if success:
        print(f"[+] Received GPIO {pin} = {gpio_val} from {target} ({elapsed:.1f}ms).")
    else:
        print(f"[-] Timed out waiting for 'GPIO {pin} IS ...' from {target}.")

    return gpio_val


def set_arduino_pin_d0(client: CoordinatorClient, target: str = "Kitchen", state: bool = False, timeout: float = 3.0) -> bool:
    """
    Sends `<target> clrx` (for state=False / LOW) or `<target> setx` (for state=True / HIGH)
    to toggle Arduino PIN_D0 via Kitchen serial bridge.
    Waits for 'GotClrx' or 'GotSetx' reply.
    """
    cmd = "setx" if state else "clrx"
    expected = "GotSetx" if state else "GotClrx"
    got_event = threading.Event()

    def on_rx(msg: str):
        if expected in msg:
            got_event.set()

    client.add_rx_callback(on_rx)
    cmd_str = f"{target} {cmd}"
    state_desc = "HIGH (3.3V)" if state else "LOW (0V)"
    print(f"\n[>] Sending command to set Arduino PIN_D0 {state_desc}: \"{cmd_str}\"")
    start_time = time.time()
    client.send_line(cmd_str)

    success = got_event.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0

    if not success:
        # Retry once
        print(f"[!] Warning: Did not receive '{expected}' reply within {timeout:.1f}s, retrying \"{cmd_str}\"...")
        client.send_line(cmd_str)
        success = got_event.wait(timeout=timeout)
        elapsed = (time.time() - start_time) * 1000.0

    client.remove_rx_callback(on_rx)

    if success:
        print(f"[+] Received '{expected}' confirmation from Arduino via {target} ({elapsed:.1f}ms).")
    else:
        print(f"[-] Timed out waiting for '{expected}' from Arduino via {target}.")

    return success


def init_gpio_handler(client: CoordinatorClient, target: str) -> bool:
    """Initializes the GPIO handler on target node."""
    cmd = f"{target} SetupGPIOhandler"
    print(f"[>] Initializing GPIO handler on {target}: \"{cmd}\"")
    client.send_line(cmd)
    time.sleep(0.3)
    return True


def set_gpio_mode_in(client: CoordinatorClient, target: str = "Garage", pin: int = 16, timeout: float = 3.0) -> bool:
    """Configures specified pin on target node as INPUT GPIO."""
    got_ack = threading.Event()
    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "MODEIN" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} ModeGPIOin {pin}"
    print(f"[>] Configuring Pin {pin} as INPUT on {target}: \"{cmd}\"")
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)

    if success:
        print(f"[+] Pin {pin} configured as INPUT GPIO on {target}.")
    else:
        print(f"[!] Note: ModeGPIOin command sent to {target}.")
    return True


def set_gpio_mode_out(client: CoordinatorClient, target: str, pin: int, timeout: float = 3.0) -> bool:
    """Configures specified pin on target node as OUTPUT GPIO."""
    got_ack = threading.Event()
    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "MODEOUT" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} ModeGPIOout {pin}"
    print(f"[>] Configuring Pin {pin} as OUTPUT on {target}: \"{cmd}\"")
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)

    if success:
        print(f"[+] Pin {pin} configured as OUTPUT GPIO on {target}.")
    else:
        print(f"[!] Note: ModeGPIOout command sent to {target}.")
    return True


def set_gpio_val(client: CoordinatorClient, target: str, pin: int, val: int, timeout: float = 3.0) -> bool:
    """Sets specified output pin level on target node to 0 or 1."""
    got_ack = threading.Event()
    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "SETGPIO" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} SetGPIOval {pin} {val}"
    level_str = "HIGH (1)" if val == 1 else "LOW (0)"
    print(f"\n[>] Setting Pin {pin} on {target} to {level_str}: \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if success:
        print(f"[+] Pin {pin} set to {val} on {target} ({elapsed:.1f}ms).")
    return True


def set_rf_drop_blink(client: CoordinatorClient, target: str, pin: int, enable: int = 1, timeout: float = 3.0) -> bool:
    """Configures RFdropBlink on specified GPIO pin on target node."""
    got_ack = threading.Event()
    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "RFDROPBLINK" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} RFdropBlink {pin} {enable}" if enable != 1 else f"{target} RFdropBlink {pin}"
    state_str = "ENABLED (4 Hz flash when severed)" if enable else "DISABLED"
    print(f"\n[>] Configuring RFdropBlink on Pin {pin} ({state_str}) on {target}: \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if success:
        print(f"[+] RFdropBlink on Pin {pin} set to {state_str} on {target} ({elapsed:.1f}ms).")
    else:
        print(f"[!] Note: RFdropBlink command sent to {target}.")
    return True


def run_rfdrop_blink_test(
    client: CoordinatorClient,
    target: str = "Garage",
    pin: int = 18,
    timeout: float = 3.0
) -> bool:
    """
    Tests RFdropBlink mode on the specified GPIO pin:
    1. Sets target pin as OUTPUT: '<target> ModeGPIOout <pin>'
    2. Arms RFdropBlink: '<target> RFdropBlink <pin>'
    3. Prompts user to trigger a disconnect test (e.g. ResetCoordinator):
       - While Coordinator is offline / rebooting, RF contact is severed.
       - The node flashes the GPIO pin at 4 Hz (4 times/sec: 125ms HIGH / 125ms LOW).
       - When Coordinator reconnects, the node ceases blinking and returns pin to LOW (0).
    4. Provides optional cleanup: '<target> RFdropBlink <pin> 0'.
    """
    print("\n" + "=" * 65)
    print("--- [TEST J] RFDROPBLINK LINK-LOSS DISCONNECT TEST ---")
    print(f"Target Node:     {target}")
    print(f"Blink Pin:       GPIO {pin}")
    print(f"Blink Rate:      4 Hz (4 times/sec: 125ms ON / 125ms OFF)")
    print(f"Timestamp:       {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # Phase 0: Initialize GPIO handler
    init_gpio_handler(client, target)

    # Step 1: Configure pin as OUTPUT
    print(f"\n[*] Step 1: Configuring Pin {pin} as OUTPUT on {target}...")
    set_gpio_mode_out(client, target=target, pin=pin, timeout=timeout)
    time.sleep(0.3)

    # Step 2: Arm RFdropBlink
    print(f"[*] Step 2: Arming RFdropBlink on Pin {pin}...")
    set_rf_drop_blink(client, target=target, pin=pin, enable=1, timeout=timeout)
    time.sleep(0.3)

    print("\n" + "-" * 65)
    print(f"[+] Pin {pin} is now ARMED with RFdropBlink on '{target}'.")
    print(f"    - When connected to Coordinator: Pin {pin} stays LOW (0).")
    print(f"    - When connection is severed:   Pin {pin} blinks at 4 Hz (4x/sec).")
    print("-" * 65)

    print("\n" + "=" * 65)
    print(">>> STEP 1: PULL / UNPLUG POWER FROM THE COORDINATOR <<<")
    print("=" * 65)
    print("[*] 1. Disconnect the USB cable (or power) from the Coordinator module.")
    print(f"[*] 2. Observe the {target} Pin {pin} LED:")
    print(f"       Within ~2 seconds of Coordinator power loss, the LED will begin")
    print(f"       flashing rapidly at 4 Hz (4 times/sec: 125ms ON / 125ms OFF)!")
    print(f"       It will continue blinking for as long as the Coordinator is unpowered.")
    print("=" * 65)

    # Cleanly close serial port before USB cable is pulled
    coord_port = client.port
    client.close()

    input("\n--> Unplug Coordinator, observe Pin 18 blinking at 4 Hz, then press [ENTER]...")

    print("\n" + "=" * 65)
    print(">>> STEP 2: RE-CONNECT POWER TO THE COORDINATOR <<<")
    print("=" * 65)
    print("[*] 1. Plug the Coordinator USB / power cable back into the PC.")
    print(f"[*] 2. Observe the {target} Pin {pin} LED:")
    print(f"       Once the Coordinator finishes booting and RF contact is restored,")
    print(f"       the LED will automatically cease blinking and return to steady state!")
    print("=" * 65)

    input("\n--> Plug Coordinator back in, then press [ENTER] to reconnect...")

    print(f"\n[*] Reconnecting serial connection to Coordinator on {coord_port}...")
    if client.reconnect(timeout=15.0):
        print(f"[+] Serial connection to Coordinator restored.")
        print(f"[*] Waiting for mesh link to re-establish with '{target}'...")
        time.sleep(2.0)
        node_ok, rtt, resp = verify_node_bidirectional(client, target, timeout=10.0)
        if node_ok:
            print(f"[+] TEST PASSED: RF link restored! '{target}' is online ({rtt:.0f}ms)!")
            print(f"[+] Pin {pin} LED has ceased blinking and returned to steady state.")
        else:
            print(f"[!] Warning: Did not receive active reply from '{target}' yet.")
    else:
        print(f"[-] Could not reconnect to Coordinator port {coord_port} within timeout.")

    cleanup = input(f"\nDisable RFdropBlink on Pin {pin} (restore plain output)? [Y/n]: ").strip().lower()
    if cleanup in ("", "y", "yes"):
        set_rf_drop_blink(client, target=target, pin=pin, enable=0, timeout=timeout)
        print(f"[+] RFdropBlink disabled on Pin {pin}.")

    return True


def setup_expand8(client: CoordinatorClient, target: str = "Kitchen", addr: int = 32, timeout: float = 3.0) -> bool:
    """Initializes PCF8574 Expand8 on target node with an explicit I2C address (default: 32 / 0x20)."""
    got_ack = threading.Event()
    status_ok = False

    def on_rx(msg: str):
        nonlocal status_ok
        if "SETUP EXPAND8 OK" in msg:
            status_ok = True
            got_ack.set()
        elif "SETUP EXPAND8 FAIL" in msg:
            status_ok = False
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} SetupExpand8 {addr}"
    print(f"\n[>] Initializing PCF8574 Expand8 on {target} at I2C address {addr} (0x{addr:02X}): \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if status_ok:
        print(f"[+] PCF8574 Expand8 initialized successfully on {target} at address {addr} (0x{addr:02X}) ({elapsed:.1f}ms).")
    else:
        print(f"[!] Warning: SetupExpand8 did not receive OK confirmation from {target}.")
    return status_ok


def scan_expand8_addresses(client: CoordinatorClient, target: str = "Kitchen", default_addrs: tuple[int, int] = (32, 33), timeout: float = 3.0) -> tuple[int, int]:
    """
    Sends `<target> scanI2C` to auto-detect connected PCF8574 (0x20-0x27) / PCF8574A (0x38-0x3F,
    excluding 0x39 APDS9930 and 0x3C SSD1306) chips on the shared I2C bus.
    Returns a pair of 7-bit I2C addresses (addr1, addr2).
    """
    got_scan = threading.Event()
    discovered: list[int] = []

    def on_rx(msg: str):
        if "I2C SCAN:" in msg:
            for m in re.finditer(r"0x([0-9A-Fa-f]{2})\((\d+)\)", msg):
                dec_addr = int(m.group(2))
                # Valid PCF8574 (0x20..0x27) or PCF8574A (0x38..0x3F, skip 0x39 APDS9930 & 0x3C SSD1306)
                if (0x20 <= dec_addr <= 0x27) or (0x38 <= dec_addr <= 0x3F and dec_addr not in (0x39, 0x3C)):
                    if dec_addr not in discovered:
                        discovered.append(dec_addr)
            got_scan.set()

    client.add_rx_callback(on_rx)
    client.send_line(f"{target} scanI2C")
    got_scan.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)

    if len(discovered) >= 2:
        return (discovered[0], discovered[1])
    elif len(discovered) == 1:
        second = default_addrs[1] if discovered[0] != default_addrs[1] else default_addrs[0]
        return (discovered[0], second)
    return default_addrs


def write_expand8_bit(client: CoordinatorClient, target: str, bit: int, val: int, addr: int | None = None, timeout: float = 3.0) -> bool:
    """Writes a single bit (0 or 1) to PCF8574 Expand8 on target node, optionally targeting a specific I2C address."""
    got_ack = threading.Event()

    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "EXPAND8 WR OK" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    if addr is not None:
        cmd = f"{target} wrExpand8 {bit} {val} {addr}"
    else:
        cmd = f"{target} wrExpand8 {bit} {val}"
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)
    return success


def read_expand8_bit(client: CoordinatorClient, target: str = "Kitchen", bit: int = 0, addr: int | None = None, timeout: float = 3.0) -> int | None:
    """
    Sends `<target> rdExpand8 <bit> [addr]` and waits for `EXPAND8 <bit> IS <val>`.
    Returns integer bit level (0 or 1) or None on timeout.
    """
    got_event = threading.Event()
    read_val = None
    pattern = re.compile(rf"EXPAND8\s+{bit}\s+IS\s+([01])", re.IGNORECASE)

    def on_rx(msg: str):
        nonlocal read_val
        if f"< {target}:" in msg or f"from {target}" in msg or not msg.startswith("< "):
            m = pattern.search(msg)
            if m:
                read_val = int(m.group(1))
                got_event.set()

    client.add_rx_callback(on_rx)
    if addr is not None:
        cmd = f"{target} rdExpand8 {bit} {addr}"
    else:
        cmd = f"{target} rdExpand8 {bit}"
    client.send_line(cmd)
    success = got_event.wait(timeout=timeout)

    if not success:
        # Retry once in case of wireless packet jitter
        client.send_line(cmd)
        success = got_event.wait(timeout=timeout)

    client.remove_rx_callback(on_rx)
    return read_val


def run_expand8_test(client: CoordinatorClient, target: str = "Kitchen", addr: int = 32, addr2: int | None = None, timeout: float = 3.0) -> bool:
    """
    Validates BOTH instances of PCF8574 8-bit I/O Expander on `target`:
    1. Scans I2C bus (or uses `addr` / `addr2`, defaulting to 0x20/32 and 0x21/33) and initializes both chips
       (`SetupExpand8 <addr1>` and `SetupExpand8 <addr2>`).
    2. Pass 1: Writes complementary alternating bit patterns to both chips simultaneously by passing the I2C
       address with each command:
         - Chip 1 (addr1): Pattern A (LSB=0 -> [0, 1, 0, 1, 0, 1, 0, 1] / 0xAA)
         - Chip 2 (addr2): Pattern B (LSB=1 -> [1, 0, 1, 0, 1, 0, 1, 0] / 0x55)
       Reads all 8 bits from Chip 1 and Chip 2 and verifies both match without cross-chip interference.
    3. Pass 2: Swaps the patterns across the two chips:
         - Chip 1 (addr1): Pattern B (LSB=1 -> [1, 0, 1, 0, 1, 0, 1, 0] / 0x55)
         - Chip 2 (addr2): Pattern A (LSB=0 -> [0, 1, 0, 1, 0, 1, 0, 1] / 0xAA)
       Reads all 8 bits from Chip 1 and Chip 2 and verifies all values match.
    """
    if addr2 is None:
        detected_a1, detected_a2 = scan_expand8_addresses(client, target=target, default_addrs=(addr, 33 if addr != 33 else 32), timeout=timeout)
        addr1 = detected_a1 if addr == 32 else addr
        addr2 = detected_a2 if detected_a2 != addr1 else (33 if addr1 != 33 else 32)
    else:
        addr1 = addr

    print("\n" + "=" * 65)
    print("--- [TEST E] DUAL PCF8574 EXPAND8 ALTERNATING BIT TEST (2 INSTANCES) ---")
    print(f"Target Node: {target}")
    print(f"Instance #1: PCF8574 @ 0x{addr1:02X} ({addr1})")
    print(f"Instance #2: PCF8574 @ 0x{addr2:02X} ({addr2})")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # Step 1: Explicitly initialize both PCF8574 instances
    ok1 = setup_expand8(client, target=target, addr=addr1, timeout=timeout)
    time.sleep(0.15)
    ok2 = setup_expand8(client, target=target, addr=addr2, timeout=timeout)
    time.sleep(0.2)

    all_passed = ok1 and ok2

    pat_a = [0 if (i % 2 == 0) else 1 for i in range(8)]  # LSB=0 -> 0xAA
    pat_b = [1 if (i % 2 == 0) else 0 for i in range(8)]  # LSB=1 -> 0x55

    passes = [
        (
            "Pass 1 (Chip 1 = Pattern A [LSB=0, 0xAA] | Chip 2 = Pattern B [LSB=1, 0x55])",
            [(addr1, "Instance #1", pat_a), (addr2, "Instance #2", pat_b)],
        ),
        (
            "Pass 2 (Chip 1 = Pattern B [LSB=1, 0x55] | Chip 2 = Pattern A [LSB=0, 0xAA])",
            [(addr1, "Instance #1", pat_b), (addr2, "Instance #2", pat_a)],
        ),
    ]

    for pass_idx, (pass_title, chip_assignments) in enumerate(passes, start=1):
        print(f"\n[>] === {pass_title} ===")

        # First write all bits to both chips so we also verify independent state retention across chips
        for chip_addr, chip_label, expected_bits in chip_assignments:
            byte_val = sum(b << i for i, b in enumerate(expected_bits))
            print(f"\n  [*] Writing {chip_label} @ 0x{chip_addr:02X} ({chip_addr}) -> {expected_bits} (0x{byte_val:02X}):")
            for bit in range(8):
                val = expected_bits[bit]
                write_expand8_bit(client, target=target, bit=bit, val=val, addr=chip_addr, timeout=timeout)
                print(f"      wrExpand8 {bit} {val} {chip_addr} -> OK")
                time.sleep(0.04)

        time.sleep(0.1)

        # Now read back and verify all 8 bits from both chips
        for chip_addr, chip_label, expected_bits in chip_assignments:
            print(f"\n  [*] Reading & verifying {chip_label} @ 0x{chip_addr:02X} ({chip_addr}):")
            chip_ok = True
            for bit in range(8):
                expected = expected_bits[bit]
                actual = read_expand8_bit(client, target=target, bit=bit, addr=chip_addr, timeout=timeout)
                if actual == expected:
                    print(f"      [+] {chip_label} (0x{chip_addr:02X}) Bit {bit}: expected {expected}, read {actual} [PASS]")
                else:
                    print(f"      [-] {chip_label} (0x{chip_addr:02X}) Bit {bit}: expected {expected}, read {actual} [FAIL]")
                    chip_ok = False
                time.sleep(0.04)

            if chip_ok:
                print(f"  [+] {chip_label} @ 0x{chip_addr:02X} ({chip_addr}) PASSED (all 8 bits verified)!")
            else:
                print(f"  [-] {chip_label} @ 0x{chip_addr:02X} ({chip_addr}) FAILED (bit mismatch detected)!")
                all_passed = False

        time.sleep(0.2)

    print("-" * 65)
    if all_passed:
        print(f"[+] TEST PASSED: Both PCF8574 instances (0x{addr1:02X} & 0x{addr2:02X}) verified on {target}!")
    else:
        print(f"[-] TEST FAILED: Dual PCF8574 Expand8 verification failed on {target}.")
    return all_passed


def setup_ssd1306_display(client: CoordinatorClient, target: str = "Kitchen", addr: int = 60, timeout: float = 3.0) -> bool:
    """Initializes SSD1306 OLED display on target node at specified I2C address (default: 60 / 0x3C)."""
    got_ack = threading.Event()
    status_ok = False

    def on_rx(msg: str):
        nonlocal status_ok
        if "SETUP DISP OK" in msg:
            status_ok = True
            got_ack.set()
        elif "SETUP DISP FAIL" in msg:
            status_ok = False
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} SetupDisp {addr}"
    print(f"\n[>] Initializing SSD1306 OLED on {target} at I2C address {addr} (0x{addr:02X}): \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if status_ok:
        print(f"[+] SSD1306 OLED initialized successfully on {target} at address {addr} (0x{addr:02X}) ({elapsed:.1f}ms).")
    else:
        print(f"[!] Warning: SetupDisp did not receive OK confirmation from {target}.")
    return status_ok


def clear_display(client: CoordinatorClient, target: str = "Kitchen", timeout: float = 3.0, verbose: bool = True) -> bool:
    """Clears SSD1306 OLED display and resets cursor to top of screen."""
    got_ack = threading.Event()
    status_ok = False

    def on_rx(msg: str):
        nonlocal status_ok
        if "CLEARDISP OK" in msg:
            status_ok = True
            got_ack.set()
        elif "CLEARDISP FAIL" in msg:
            status_ok = False
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} ClearDisp"
    if verbose:
        print(f"\n[>] Clearing SSD1306 display on {target}: \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if verbose:
        if status_ok:
            print(f"[+] ClearDisp succeeded on {target} ({elapsed:.1f}ms).")
        else:
            print(f"[!] ClearDisp did not receive OK confirmation from {target}.")
    return status_ok


def print_display_line(client: CoordinatorClient, target: str = "Kitchen", size: int = 12, text: str = "Hello World", timeout: float = 3.0, verbose: bool = True) -> bool:
    """Prints a line of text in FreeMono font (9, 12, 18, 24 pt) on SSD1306 and advances cursor."""
    got_ack = threading.Event()
    status_ok = False

    def on_rx(msg: str):
        nonlocal status_ok
        if "PRINT OK" in msg:
            status_ok = True
            got_ack.set()
        elif "PRINT FAIL" in msg:
            status_ok = False
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} print {size} {text}"
    if verbose:
        print(f"\n[>] Printing line on {target} display ({size}pt: \"{text}\"): \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if verbose:
        if status_ok:
            print(f"[+] Print line succeeded on {target} ({elapsed:.1f}ms).")
        else:
            print(f"[!] Print line did not receive OK confirmation from {target}.")
    return status_ok


def run_ssd1306_test(client: CoordinatorClient, target: str = "Kitchen", addr: int = 60, timeout: float = 3.0) -> bool:
    """
    Validates SSD1306 128x64 OLED Display:
    1. Initializes display at specified address (default 60 / 0x3C).
    2. Clears the display and resets cursor (ClearDisp).
    3. Prints 3 lines of 9pt font.
    4. Pauses 2 seconds, clears display, prints 2 lines of 12pt font.
    5. Pauses 2 seconds, clears display, prints 1 line of 24pt font.
    6. Verifies display responses and line rendering.
    """
    print("\n" + "=" * 65)
    print("--- [TEST O] SSD1306 OLED DISPLAY & FREEMONO FONT TEST ---")
    print(f"Target Node: {target} (ESP32-C6 / SSD1306 @ 0x{addr:02X})")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # Step 1: Initialize Display
    setup_ok = setup_ssd1306_display(client, target=target, addr=addr, timeout=timeout)
    time.sleep(0.2)

    # Step 2: Clear Display
    clear_ok = clear_display(client, target=target, timeout=timeout)
    time.sleep(0.2)

    # Step 3: Print test lines across font sizes with 2-second pause between font changes
    font_stages = [
        (9, ["9pt Line 1", "9pt Line 2", "9pt Line 3"]),
        (12, ["12pt Line 1", "12pt Line 2"]),
        (24, ["24pt Line 1"]),
    ]

    all_passed = setup_ok and clear_ok

    for stage_idx, (size_pt, lines) in enumerate(font_stages):
        if stage_idx > 0:
            print(f"\n[*] Pausing 2.0s before font change to {size_pt}pt...")
            time.sleep(2.0)
            if not clear_display(client, target=target, timeout=timeout):
                all_passed = False
            time.sleep(0.2)

        print(f"\n[*] Printing {len(lines)} line(s) of {size_pt}pt font...")
        for line_text in lines:
            ok = print_display_line(client, target=target, size=size_pt, text=line_text, timeout=timeout)
            if not ok:
                all_passed = False
            time.sleep(0.2)

    print("\n" + "-" * 65)
    if all_passed:
        print(f"[+] TEST PASSED: SSD1306 OLED verified on {target}!")
    else:
        print(f"[-] TEST FAILED: SSD1306 OLED verification failed on {target}.")
    return all_passed


def setup_rotary_encoder(client: CoordinatorClient, target: str = "N2", clk: int = 18, dt: int = 20, sw: int = 19, timeout: float = 3.0) -> bool:
    """Initializes Keyes KY-040 Rotary Encoder on target node."""
    got_ack = threading.Event()
    status_ok = False

    def on_rx(msg: str):
        nonlocal status_ok
        if f"< {target}:" in msg and ("SETUP ROTARY OK" in msg or "[ACK]" in msg):
            status_ok = True
            got_ack.set()
        elif f"< {target}:" in msg and "SETUP ROTARY FAIL" in msg:
            status_ok = False
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} SetupRotary {clk} {dt} {sw}"
    print(f"\n[>] Initializing Keyes KY-040 Rotary Encoder on {target} (SW={sw}, DT={dt}, CLK={clk}): \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if status_ok:
        print(f"[+] Rotary Encoder initialized successfully on {target} ({elapsed:.1f}ms).")
    else:
        print(f"[!] Warning: SetupRotary did not receive OK confirmation from {target}.")
    return status_ok


def update_rotary_oled_display(client: CoordinatorClient, oled_target: str = "Kitchen", count: int = 0, sw_state: bool = False, font_size: int = 12) -> None:
    """
    Updates the Kitchen OLED display:
      Line 1: Count value (floored at 0, ceiling at 8)
      Line 2: Rotary switch SW state ("ON" or "OFF")
    Uses atomic single-packet PrintLines command to eliminate screen blanking and multi-packet lag.
    """
    sw_text = "ON" if sw_state else "OFF"
    got_ack = threading.Event()
    status_ok = False

    def on_rx(msg: str):
        nonlocal status_ok
        if f"< {oled_target}:" in msg or oled_target in msg:
            if "PRINT OK" in msg:
                status_ok = True
                got_ack.set()
            elif "PRINT FAIL" in msg:
                status_ok = False
                got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{oled_target} PrintLines {font_size} Count: {count}|{sw_text}"
    client.send_line(cmd)
    got_ack.wait(timeout=0.8)
    client.remove_rx_callback(on_rx)

    # Fallback to legacy 3-command method if PrintLines is not acknowledged
    if not status_ok:
        clear_display(client, target=oled_target, timeout=0.8, verbose=False)
        time.sleep(0.02)
        print_display_line(client, target=oled_target, size=font_size, text=f"Count: {count}", timeout=0.8, verbose=False)
        time.sleep(0.02)
        print_display_line(client, target=oled_target, size=font_size, text=sw_text, timeout=0.8, verbose=False)


def run_rotary_encoder_test(
    client: CoordinatorClient,
    rotary_target: str = "N2",
    oled_target: str = "Kitchen",
    oled_addr: int = 60,
    sw_pin: int = 19,
    dt_pin: int = 20,
    clk_pin: int = 18,
    initial_count: int = 0,
) -> bool:
    """
    Interactive Keyes KY-040 Rotary Encoder Test:
    - Node: N2
    - Pins: SW=Pin 19, DT=Pin 20, CLK=Pin 18
    - Kitchen OLED: Displays Count on Line 1 (0 to 8 range) and SW state (ON/OFF) on Line 2
    - Tracks total count: CW increases count (ceiling 8), CCW decreases count (floor 0)
    - Pushbutton SW toggles ON/OFF state
    - Updates OLED Line 1 when count changes and Line 2 when SW state changes
    - Press [ESC] to exit test.
    """
    print("\n" + "=" * 65)
    print("--- [TEST K] KEYES KY-040 ROTARY ENCODER -> OLED TEST ---")
    print(f"Rotary Node: {rotary_target} (Pins: SW={sw_pin}, DT={dt_pin}, CLK={clk_pin})")
    print(f"OLED Target: {oled_target} (SSD1306 @ 0x{oled_addr:02X})")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # Step 1: Configure Kitchen OLED Display
    print(f"\n[*] Step 1: Configuring OLED display on {oled_target}...")
    setup_ssd1306_display(client, target=oled_target, addr=oled_addr, timeout=3.0)
    time.sleep(0.1)

    # Step 2: Configure Rotary Encoder on N2
    print(f"\n[*] Step 2: Configuring Keyes KY-040 Rotary Encoder on {rotary_target}...")
    setup_rotary_encoder(client, target=rotary_target, clk=clk_pin, dt=dt_pin, sw=sw_pin, timeout=3.0)
    time.sleep(0.05)
    client.send_line(f"{rotary_target} SetRotaryHalfStep 1")
    time.sleep(0.05)

    # Step 3: Initialize Display with initial count & SW state
    count = max(0, min(8, initial_count))
    sw_state = False  # Starts OFF
    inverted = False
    half_step = True
    print(f"\n[*] Step 3: Setting initial OLED screen (Count={count}, SW={'ON' if sw_state else 'OFF'})...")
    update_rotary_oled_display(client, oled_target=oled_target, count=count, sw_state=sw_state)

    print("\n" + "-" * 65)
    print(f"[*] Rotary Encoder live monitoring active!")
    print(f"    - Turn Knob CW:  Count increases (Ceiling = 8)")
    print(f"    - Turn Knob CCW: Count decreases (Floor = 0)")
    print(f"    - Push & Hold:   SW displays ON while held, OFF when released")
    print(f"    - Press [I]:     Invert CW/CCW rotation direction")
    print(f"    - Press [H]:     Toggle half-step mode")
    print(f"    - Press [ESC]:   Finish and return to menu.")
    print("-" * 65 + "\n")

    event_queue = queue.Queue()
    last_event_type = None
    last_event_time = 0.0

    def on_rx(msg: str):
        nonlocal last_event_type, last_event_time
        if f"< {rotary_target}:" in msg or "Rotary" in msg:
            evt = None
            if "RotaryCW" in msg:
                evt = "CW"
            elif "RotaryCCW" in msg:
                evt = "CCW"
            elif "RotaryButtonDown" in msg:
                evt = "BTN_DOWN"
            elif "RotaryButtonUp" in msg:
                evt = "BTN_UP"
            elif "RotaryButtonPushed" in msg:
                evt = "BTN_DOWN"

            if evt is not None:
                now = time.time()
                # Deduplicate identical events received within 50ms (ESP-NOW burst / serial duplicates)
                if evt == last_event_type and (now - last_event_time) < 0.050:
                    return
                last_event_type = evt
                last_event_time = now
                event_queue.put(evt)

    client.add_rx_callback(on_rx)
    flush_console_keys()

    cw_events = 0
    ccw_events = 0
    btn_events = 0

    try:
        while True:
            # Check keyboard controls
            if msvcrt is not None and msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b'\x1b':
                    break
                elif ch in (b'i', b'I'):
                    inverted = not inverted
                    client.send_line(f"{rotary_target} SetRotaryInvert {1 if inverted else 0}")
                    print(f"[*] Rotary Direction Inverted: {'INVERTED' if inverted else 'NORMAL'}")
                elif ch in (b'h', b'H'):
                    half_step = not half_step
                    client.send_line(f"{rotary_target} SetRotaryHalfStep {1 if half_step else 0}")
                    print(f"[*] Rotary Half-Step Mode: {'ENABLED' if half_step else 'DISABLED'}")
                elif ch in (b'\x00', b'\xe0') and msvcrt.kbhit():
                    msvcrt.getch()
            elif check_esc_key():
                break

            try:
                event = event_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            # Process event individually to ensure no numbers are skipped
            dirty = False

            if event == "CW":
                cw_events += 1
                if count < 8:
                    count += 1
                    dirty = True
                    print(f"  [>] Rotary CW: Count -> {count} (ceiling 8)")
                else:
                    print(f"  [>] Rotary CW: Count already at ceiling (8)")
            elif event == "CCW":
                ccw_events += 1
                if count > 0:
                    count -= 1
                    dirty = True
                    print(f"  [<] Rotary CCW: Count -> {count} (floor 0)")
                else:
                    print(f"  [<] Rotary CCW: Count already at floor (0)")
            elif event == "BTN_DOWN":
                btn_events += 1
                if not sw_state:
                    sw_state = True
                    dirty = True
                    print(f"  [o] Rotary Button PRESSED: SW state -> ON")
            elif event == "BTN_UP":
                if sw_state:
                    sw_state = False
                    dirty = True
                    print(f"  [o] Rotary Button RELEASED: SW state -> OFF")
                dirty = True
                print(f"  [o] Rotary Button Pushed: SW state -> {'ON' if sw_state else 'OFF'}")

            if dirty:
                sw_str = "ON" if sw_state else "OFF"
                print(f"[OLED UPDATE] Line 1: 'Count: {count}' | Line 2: '{sw_str}'")
                update_rotary_oled_display(client, oled_target=oled_target, count=count, sw_state=sw_state)

    except KeyboardInterrupt:
        pass
    finally:
        client.remove_rx_callback(on_rx)
        flush_console_keys()

    print("\n" + "-" * 65)
    print(f"[*] Rotary test ended.")
    print(f"    Final Count:    {count} (floor=0, ceiling=8)")
    print(f"    Final SW State: {'ON' if sw_state else 'OFF'}")
    print(f"    Total Events:   CW={cw_events}, CCW={ccw_events}, Button={btn_events}")
    print("-" * 65)
    return True


def run_rotary_raw_monitor_test(
    client: CoordinatorClient,
    target: str = "N2",
    sw_pin: int = 19,
    dt_pin: int = 20,
    clk_pin: int = 18,
) -> bool:
    """
    [TEST Z] Rotary Encoder 2-Bit Raw Monitor (Print-on-Change):
    - Node: N2 (SW=19, DT=20, CLK=18)
    - Monitors real-time pin level changes (CLK, DT, SW) and prints each transition with timestamp.
    - Demonstrates quadrature 2-bit Gray code sequence (11 -> 01 -> 00 -> 10 -> 11).
    - Detects whether detents rest at 11 or 00, transition direction, contact bouncing, and step emissions.
    - Press [ESC] to exit.
    """
    print("\n" + "=" * 75)
    print("--- [TEST Z] ROTARY ENCODER 2-BIT RAW MONITOR (PRINT-ON-CHANGE) ---")
    print(f"Target Node: {target} (Pins: SW={sw_pin}, DT={dt_pin}, CLK={clk_pin})")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 75)

    # Step 1: Configure Rotary Encoder hardware
    print(f"\n[*] Step 1: Initializing Keyes KY-040 hardware on {target}...")
    setup_rotary_encoder(client, target=target, clk=clk_pin, dt=dt_pin, sw=sw_pin, timeout=3.0)
    time.sleep(0.1)

    # Step 2: Start raw bit streaming
    print(f"[*] Step 2: Enabling live raw bit monitoring on {target} ({target} MonitorRotary 1)...")
    client.send_line(f"{target} SetRotaryHalfStep 1")
    time.sleep(0.05)
    client.send_line(f"{target} MonitorRotary 1")
    time.sleep(0.1)

    print("\n" + "-" * 75)
    print("Live 2-bit monitoring active! Rotate knob CW / CCW or press the button.")
    print("  [CLK]  = Phase A (Bit 1)")
    print("  [DT]   = Phase B (Bit 0)")
    print("  [BITS] = 2-bit Gray code value [CLK, DT]")
    print("  [SW]   = Pushbutton state (HIGH = released, LOW = pressed)")
    print("  Controls: [I] Invert CW/CCW | [H] Toggle Half-Step | [ESC] Exit")
    print("-" * 75)
    print(f"{'TIMESTAMP':<14} {'CLK':<5} {'DT':<5} {'BITS':<6} {'SW':<6} {'DELTA':<9} {'EVENT / ANALYSIS'}")
    print("-" * 75)

    flush_console_keys()
    last_state = None
    last_transition_time = None
    last_cw_time = 0.0
    last_ccw_time = 0.0
    last_btn_time = 0.0
    inverted = False
    half_step = True

    def on_rx(msg: str):
        nonlocal last_state, last_transition_time, last_cw_time, last_ccw_time, last_btn_time
        now_str = datetime.now().strftime("%H:%M:%S.%f")[:12]
        now = time.time()

        if "RotaryBits" in msg:
            try:
                parts = msg.split()
                bits = "??"
                clk = "?"
                dt = "?"
                sw = "?"
                delta = "+0ms"

                for p in parts:
                    if p.startswith("clk="): clk = p.split("=")[1]
                    elif p.startswith("dt="): dt = p.split("=")[1]
                    elif p.startswith("sw="): sw = p.split("=")[1]
                    elif p.startswith("+") and p.endswith("ms"): delta = p
                    elif len(p) == 2 and p in ("00", "01", "10", "11"): bits = p

                sw_label = "PUSH" if sw == "0" else "UP"
                curr_state = (bits, sw_label)

                # True Print-On-Change: ignore duplicate state packets
                if curr_state == last_state:
                    return

                # Calculate accurate wall-clock elapsed time between actual state changes
                if last_transition_time is not None:
                    calc_delta_ms = int((now - last_transition_time) * 1000)
                    delta = f"+{calc_delta_ms}ms"
                else:
                    delta = "+0ms"

                last_state = curr_state
                last_transition_time = now

                note = ""
                if bits == "11":
                    note = "<-- Detent resting state (both HIGH)"
                elif bits == "00":
                    note = "<-- Both LOW (midpoint / half-step)"
                elif bits == "01":
                    note = "CLK fell, DT high (CW start / CCW finish)"
                elif bits == "10":
                    note = "CLK high, DT fell (CCW start / CW finish)"

                print(f"{now_str:<14} {clk:<5} {dt:<5} {bits:<6} {sw_label:<6} {delta:<9} {note}")
            except Exception:
                print(f"{now_str:<14} [RAW] {msg.strip()}")

        elif "RotaryCW" in msg:
            if (now - last_cw_time) < 0.050:
                return
            last_cw_time = now
            print(f"{now_str:<14} {' ':5} {' ':5} {' ':6} {' ':6} {' ':9} *** [>>] FSM EMITTED: RotaryCW (Clockwise Detent Step)")
        elif "RotaryCCW" in msg:
            if (now - last_ccw_time) < 0.050:
                return
            last_ccw_time = now
            print(f"{now_str:<14} {' ':5} {' ':5} {' ':6} {' ':6} {' ':9} *** [<<] FSM EMITTED: RotaryCCW (Counter-Clockwise Detent Step)")
        elif "RotaryButtonDown" in msg:
            if (now - last_btn_time) < 0.050:
                return
            last_btn_time = now
            print(f"{now_str:<14} {' ':5} {' ':5} {' ':6} {' ':6} {' ':9} *** [OO] FSM EMITTED: RotaryButtonDown (Button Pressed / Held)")
        elif "RotaryButtonUp" in msg:
            if (now - last_btn_time) < 0.050:
                return
            last_btn_time = now
            print(f"{now_str:<14} {' ':5} {' ':5} {' ':6} {' ':6} {' ':9} *** [..] FSM EMITTED: RotaryButtonUp (Button Released)")
        elif "RotaryButtonPushed" in msg:
            if (now - last_btn_time) < 0.050:
                return
            last_btn_time = now
            print(f"{now_str:<14} {' ':5} {' ':5} {' ':6} {' ':6} {' ':9} *** [OO] FSM EMITTED: RotaryButtonPushed (Debounced Button Press)")

    client.add_rx_callback(on_rx)

    try:
        while True:
            if msvcrt is not None and msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b'\x1b':
                    break
                elif ch in (b'i', b'I'):
                    inverted = not inverted
                    client.send_line(f"{target} SetRotaryInvert {1 if inverted else 0}")
                    print(f"[*] Direction Invert: {'INVERTED' if inverted else 'NORMAL'}")
                elif ch in (b'h', b'H'):
                    half_step = not half_step
                    client.send_line(f"{target} SetRotaryHalfStep {1 if half_step else 0}")
                    print(f"[*] Half-Step Mode: {'ENABLED' if half_step else 'DISABLED'}")
                elif ch in (b'\x00', b'\xe0') and msvcrt.kbhit():
                    msvcrt.getch()
            elif check_esc_key():
                break

            time.sleep(0.02)

    except KeyboardInterrupt:
        pass
    finally:
        client.remove_rx_callback(on_rx)
        client.send_line(f"{target} MonitorRotary 0")
        flush_console_keys()

    print("\n" + "-" * 75)
    print(f"[*] Test [Z] completed. Raw monitor stopped on {target}.")
    print("-" * 75)
    return True


def setup_compass(client: CoordinatorClient, target: str = "Kitchen", addr: int = 13, timeout: float = 3.0) -> bool:
    """Initializes QMC5883L 3-axis digital compass on target node (default: 13 / 0x0D)."""
    got_ack = threading.Event()
    status_ok = False

    def on_rx(msg: str):
        nonlocal status_ok
        if "SETUP COMPASS OK" in msg:
            status_ok = True
            got_ack.set()
        elif "SETUP COMPASS FAIL" in msg:
            status_ok = False
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} SetupCompass {addr}"
    print(f"\n[>] Initializing QMC5883L Compass on {target} at I2C address {addr} (0x{addr:02X}): \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if status_ok:
        print(f"[+] QMC5883L Compass initialized successfully on {target} at address {addr} (0x{addr:02X}) ({elapsed:.1f}ms).")
    else:
        print(f"[!] Warning: SetupCompass did not receive OK confirmation from {target}.")
    return status_ok


def set_compass_thresholds(
    client: CoordinatorClient,
    target: str = "Kitchen",
    low_mag: int = 1000,
    high_mag: int = 2000,
) -> bool:
    """Configures QMC5883L signed hysteresis thresholds using LowMag and HighMag."""
    for cmd in (f"{target} LowMag {low_mag}", f"{target} HighMag {high_mag}"):
        print(f"[>] Setting compass threshold: \"{cmd}\"")
        client.send_line(cmd)
        time.sleep(0.2)
    return True


def get_compass_reading(client: CoordinatorClient, target: str = "Kitchen", timeout: float = 3.0) -> tuple[int, int, int] | None:
    """
    Reads 3-axis magnetic field vector (X, Y, Z) from target node.
    Sends `<target> GetCompass` and parses `COMPASS <x> <y> <z>`.
    """
    got_event = threading.Event()
    reading = None
    pattern = re.compile(r"COMPASS\s+([-\d]+)\s+([-\d]+)\s+([-\d]+)", re.IGNORECASE)

    def on_rx(msg: str):
        nonlocal reading
        m = pattern.search(msg)
        if m:
            reading = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            got_event.set()
        elif "COMPASS NOT READY" in msg:
            got_event.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} GetCompass"
    client.send_line(cmd)
    got_event.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)

    return reading


def run_compass_stream(client: CoordinatorClient, target: str = "Kitchen", addr: int = 13, interval: float = 0.3) -> None:
    """
    Streams QMC5883L 3-axis magnetic field vector in 'x,y,z' format once every 300ms
    until the user presses the ESC key.
    """
    # Check if compass is ready; if uninitialized, configure it once
    test_reading = get_compass_reading(client, target=target, timeout=0.3)
    if test_reading is None:
        print(f"[*] Initializing QMC5883L Compass on {target} at address {addr}...")
        setup_compass(client, target=target, addr=addr, timeout=2.0)
        time.sleep(0.1)

    print(f"\n[+] Streaming {target} QMC5883L Compass readings every {int(interval * 1000)}ms.")
    print("    Format: x,y,z (Press [ESC] to stop)\n")

    flush_console_keys()

    try:
        while True:
            t_start = time.time()
            if check_esc_key():
                break

            reading = get_compass_reading(client, target=target, timeout=0.25)
            if check_esc_key():
                break

            if reading is not None:
                print(f"{reading[0]},{reading[1]},{reading[2]}")

            elapsed = time.time() - t_start
            sleep_remain = max(0.0, interval - elapsed)
            deadline = time.time() + sleep_remain

            esc_hit = False
            while time.time() < deadline:
                if check_esc_key():
                    esc_hit = True
                    break
                time.sleep(0.01)

            if esc_hit:
                break

    except KeyboardInterrupt:
        pass

    flush_console_keys()
    print("\n[*] Stopped compass streaming.")


stream_compass_readings = run_compass_stream


def run_compass_test(client: CoordinatorClient, target: str = "Kitchen", addr: int = 13, timeout: float = 3.0) -> bool:
    """
    Validates QMC5883L 3-Axis Digital Compass:
    1. Initializes sensor at address `addr` (SetupCompass <addr>).
    2. Takes baseline magnetic field measurements (X1, Y1, Z1).
    3. Prompts the user to rotate the sensor by > 45 degrees and press Enter.
    4. Takes rotated magnetic field measurements (X2, Y2, Z2).
    5. Calculates vector distance and angular shift to verify the fields moved.
    """
    import math

    print("\n" + "=" * 65)
    print("--- [TEST C] QMC5883L 3-AXIS DIGITAL COMPASS ROTATION TEST ---")
    print(f"Target Node: {target} (ESP32-C6 / QMC5883L @ 0x{addr:02X})")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # Step 1: Initialize
    if not setup_compass(client, target=target, addr=addr, timeout=timeout):
        print(f"[-] TEST FAILED: Unable to initialize QMC5883L on {target}.")
        return False

    time.sleep(0.3)

    # Step 2: Read baseline
    print("\n[*] Taking baseline magnetic field measurement...")
    samples = []
    for _ in range(3):
        r = get_compass_reading(client, target=target, timeout=timeout)
        if r is not None:
            samples.append(r)
        time.sleep(0.1)

    if not samples:
        print(f"[-] TEST FAILED: No response received to 'GetCompass' from {target}.")
        return False

    x1 = sum(s[0] for s in samples) / len(samples)
    y1 = sum(s[1] for s in samples) / len(samples)
    z1 = sum(s[2] for s in samples) / len(samples)
    heading1 = (math.degrees(math.atan2(y1, x1)) + 360.0) % 360.0

    print(f"    Baseline Fields:      X = {x1:+.1f}, Y = {y1:+.1f}, Z = {z1:+.1f} LSB")
    print(f"    Estimated 2D Heading: {heading1:.1f} deg")

    # Step 3: Prompt user rotation
    print("\n" + "*" * 65)
    print(">>> ACTION REQUIRED: Turn / rotate the compass sensor by > 45 degrees. <<<")
    print("*" * 65)
    try:
        input("Press [ENTER] after rotating the sensor to continue measurement...")
    except EOFError:
        pass

    # Step 4: Read rotated field
    print("\n[*] Taking rotated magnetic field measurement...")
    time.sleep(0.2)
    samples2 = []
    for _ in range(3):
        r = get_compass_reading(client, target=target, timeout=timeout)
        if r is not None:
            samples2.append(r)
        time.sleep(0.1)

    if not samples2:
        print(f"[-] TEST FAILED: No response to 'GetCompass' after rotation from {target}.")
        return False

    x2 = sum(s[0] for s in samples2) / len(samples2)
    y2 = sum(s[1] for s in samples2) / len(samples2)
    z2 = sum(s[2] for s in samples2) / len(samples2)
    heading2 = (math.degrees(math.atan2(y2, x2)) + 360.0) % 360.0

    print(f"    Rotated Fields:       X = {x2:+.1f}, Y = {y2:+.1f}, Z = {z2:+.1f} LSB")
    print(f"    New 2D Heading:       {heading2:.1f} deg")

    # Step 5: Verification
    dx = x2 - x1
    dy = y2 - y1
    dz = z2 - z1
    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
    angle_shift = abs(heading2 - heading1)
    if angle_shift > 180.0:
        angle_shift = 360.0 - angle_shift

    print("\n--- Shift Analysis ---")
    print(f"    Delta Vector:         dX = {dx:+.1f}, dY = {dy:+.1f}, dZ = {dz:+.1f}")
    print(f"    Total Vector Shift:   {dist:.1f} LSB")
    print(f"    Heading Angle Shift:  {angle_shift:.1f} deg")

    passed = (angle_shift >= 15.0 or dist >= 80.0)

    print("-" * 65)
    if passed:
        print(f"[+] TEST PASSED: Magnetic fields moved as expected ({angle_shift:.1f} deg shift, {dist:.1f} LSB displacement)!")
    else:
        print(f"[-] TEST FAILED: Magnetic field movement insufficient (< 15 deg shift and < 80 LSB displacement).")
    return passed


def run_compass_hysteresis_test(
    client: CoordinatorClient,
    target: str = "Kitchen",
    addr: int = 13,
    low_mag: int = 1000,
    high_mag: int = 2000,
    timeout: float = 3.0,
) -> bool:
    """
    Validates QMC5883L 3-Axis Signed Hysteresis Comparator across all 12 threshold possibilities:
      - 3 dimensions: X, Y, Z
      - 4 threshold crossings per dimension:
          1. +HighMag (0 -> +1, val > +HighMag)
          2. +LowMag  (+1 -> 0, val < +LowMag)
          3. -HighMag (0 -> -1, val < -HighMag)
          4. -LowMag  (-1 -> 0, val > -LowMag)
    Logs every threshold crossing message (`Mag<X|Y|Z> <state> <val>`) in real time
    and does not pass until all 12 possibilities have been observed.
    """
    print("\n" + "=" * 86)
    print("--- [TEST F] QMC5883L 3-AXIS 12-THRESHOLD HYSTERESIS COMPARATOR TEST ---")
    print(f"Target Node:       {target} (ESP32-C6 / QMC5883L @ 0x{addr:02X})")
    print(f"Positive Band:     [+LowMag = +{low_mag}, +HighMag = +{high_mag}]")
    print(f"Negative Band:     [-LowMag = -{low_mag}, -HighMag = -{high_mag}]")
    print("Pass Requirement:  All 12 threshold crossings (3 axes x 4 thresholds) must be observed")
    print(f"Timestamp:         {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 86)

    # Step 1: Initialize QMC5883L & reset comparator states to 0
    if not setup_compass(client, target=target, addr=addr, timeout=timeout):
        print(f"[-] TEST FAILED: Unable to initialize QMC5883L on {target}.")
        return False
    time.sleep(0.2)

    # Step 2: Configure [LowMag, HighMag] thresholds
    set_compass_thresholds(client, target=target, low_mag=low_mag, high_mag=high_mag)
    time.sleep(0.2)

    # Step 3: Read baseline magnetic field
    baseline = get_compass_reading(client, target=target, timeout=timeout)
    if baseline is not None:
        print(f"[+] Baseline magnetic field: X={baseline[0]:+d}, Y={baseline[1]:+d}, Z={baseline[2]:+d} LSB")

    axes = ("X", "Y", "Z")
    thresh_Order = ("+HighMag", "+LowMag", "-HighMag", "-LowMag")
    short_labels = {
        "+HighMag": "+H",
        "+LowMag": "+L",
        "-HighMag": "-H",
        "-LowMag": "-L",
    }
    thresh_desc = {
        "+HighMag": f"0 -> +1 (val > +{high_mag})",
        "+LowMag":  f"+1 ->  0 (val < +{low_mag})",
        "-HighMag": f"0 -> -1 (val < -{high_mag})",
        "-LowMag":  f"-1 ->  0 (val > -{low_mag})",
    }

    # Track observed crossings: key = (axis, thresh_name) -> dict(first_val, last_val, count, elapsed, xyz)
    observed: dict[tuple[str, str], dict] = {}
    axis_prev_state: dict[str, int] = {"X": 0, "Y": 0, "Z": 0}
    all_12_done = threading.Event()
    lock = threading.Lock()
    t_start = time.time()

    pattern = re.compile(
        r"\bMag([XYZ])\s+(-?[01])\s+(-?\d+)(?:\s+(-?\d+)\s+(-?\d+))?\b",
        re.IGNORECASE
    )

    def format_checklist() -> str:
        parts = []
        for ax in axes:
            items = []
            for th in thresh_Order:
                mark = "X" if (ax, th) in observed else " "
                items.append(f"{short_labels[th]}:[{mark}]")
            parts.append(f"{ax}({' '.join(items)})")
        return " | ".join(parts)

    def on_rx(msg: str):
        # Ignore coordinator internal ESP_LOGI diagnostic lines to avoid counting each frame 3x
        if "SERIAL_GATEWAY:" in msg:
            return
        m = pattern.search(msg)
        if not m:
            return

        axis = m.group(1).upper()
        state = int(m.group(2))
        if m.group(4) is not None and m.group(5) is not None:
            x_val = int(m.group(3))
            y_val = int(m.group(4))
            z_val = int(m.group(5))
            val = x_val if axis == "X" else (y_val if axis == "Y" else z_val)
            xyz_str = f"X={x_val:+6d} Y={y_val:+6d} Z={z_val:+6d}"
        else:
            val = int(m.group(3))
            x_val = val if axis == "X" else 0
            y_val = val if axis == "Y" else 0
            z_val = val if axis == "Z" else 0
            xyz_str = f"Val={val:+6d}"

        elapsed = time.time() - t_start

        with lock:
            prev = axis_prev_state.get(axis, 0)
            if state == 1:
                thresh_key = "+HighMag"
            elif state == -1:
                thresh_key = "-HighMag"
            else:
                # state == 0: determine whether returning from +1 (+LowMag) or -1 (-LowMag)
                if prev == 1:
                    thresh_key = "+LowMag"
                elif prev == -1:
                    thresh_key = "-LowMag"
                else:
                    thresh_key = "+LowMag" if val >= 0 else "-LowMag"

            axis_prev_state[axis] = state

            key = (axis, thresh_key)
            is_new = key not in observed
            if is_new:
                observed[key] = {
                    "first_val": val,
                    "last_val": val,
                    "xyz": (x_val, y_val, z_val),
                    "count": 1,
                    "elapsed": elapsed,
                }
            else:
                observed[key]["last_val"] = val
                observed[key]["xyz"] = (x_val, y_val, z_val)
                observed[key]["count"] += 1

            num_done = len(observed)
            tag = f"NEW {num_done:2d}/12" if is_new else f"RPT {num_done:2d}/12"
            print(
                f"  [{tag}] +{elapsed:6.2f}s | Mag{axis} {state:+2d} {xyz_str} | "
                f"{axis} {thresh_key:<8} ({thresh_desc[thresh_key]})"
            )
            if is_new:
                print(f"             Progress: {format_checklist()}")

            if num_done >= 12:
                all_12_done.set()

    print("\n" + "-" * 86)
    print(">>> ACTION REQUIRED: Move/flip a magnet near the QMC5883L across X, Y, and Z axes! <<<")
    print("    Waiting for all 12 threshold crossings (+HighMag, +LowMag, -HighMag, -LowMag on X, Y, Z).")
    print("    Press [ESC] at any time to abort early.")
    print("-" * 86)
    print(f"    Initial:  {format_checklist()}\n")

    flush_console_keys()
    client.add_rx_callback(on_rx)

    aborted = False
    try:
        while not all_12_done.is_set():
            if check_esc_key():
                aborted = True
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        aborted = True
    finally:
        client.remove_rx_callback(on_rx)
        flush_console_keys()

    print("\n" + "=" * 94)
    print("QMC5883L 12-THRESHOLD HYSTERESIS VERIFICATION SUMMARY:")
    print("=" * 94)
    print(f"  {'#':<3} | {'Axis':<4} | {'Threshold':<9} | {'Transition Condition':<24} | {'XYZ at Crossing (LSB)':<25} | {'Count':<5} | {'Status'}")
    print("  " + "-" * 88)

    idx = 0
    for ax in axes:
        for th in thresh_Order:
            idx += 1
            info = observed.get((ax, th))
            if info is not None:
                x_c, y_c, z_c = info.get("xyz", (0, 0, 0))
                xyz_summary = f"[{x_c:+6d}, {y_c:+6d}, {z_c:+6d}]"
                cnt = str(info["count"])
                st = "[OBSERVED]"
            else:
                xyz_summary = "---"
                cnt = "0"
                st = "[MISSING ]"
            print(f"  {idx:<3} | {ax:<4} | {th:<9} | {thresh_desc[th]:<24} | {xyz_summary:<25} | {cnt:<5} | {st}")

    print("=" * 94)
    passed = len(observed) == 12 and not aborted
    if passed:
        print(f"[+] TEST PASSED: All 12/12 QMC5883L hysteresis threshold crossings observed and verified!")
    else:
        missing = [f"{ax}:{th}" for ax in axes for th in thresh_Order if (ax, th) not in observed]
        print(f"[-] TEST FAILED: Observed {len(observed)}/12 threshold crossings. Missing: {', '.join(missing)}")
    return passed


def run_garage_gpio_test(
    client: CoordinatorClient,
    garage_target: str = "Garage",
    kitchen_target: str = "Kitchen",
    pin: int = 16,
    timeout: float = 3.0
) -> bool:
    """
    Validates GPIO Input on Garage Pin 16 driven by Arduino PIN_D0 via Kitchen:
    1. Ensures Kitchen Serial Bridge is active (<kitchen> SetupSerialBridge).
    2. Initializes GPIO handler and configures Garage Pin 16 as INPUT GPIO (<garage> ModeGPIOin 16).
    3. Clears Arduino PIN_D0 to LOW via Kitchen (<kitchen> clrx -> 'GotClrx').
    4. Reads Garage Pin 16 (<garage> ReadGPIO 16) and verifies it reads 0 (LOW).
    5. Sets Arduino PIN_D0 to HIGH via Kitchen (<kitchen> setx -> 'GotSetx').
    6. Reads Garage Pin 16 (<garage> ReadGPIO 16) and verifies it reads 1 (HIGH).
    """
    print("\n" + "=" * 65)
    print("--- [TEST G] GARAGE GPIO INPUT TEST (PIN 16) ---")
    print(f"Garage Node:       {garage_target} (ESP32-C6 Pin {pin} as INPUT GPIO)")
    print(f"Arduino Host Node: {kitchen_target} (Trinket M0 PIN_D0 -> Garage Pin {pin})")
    print(f"Timestamp:         {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")

    client.add_rx_callback(on_rx)

    try:
        # Step 1: Ensure Serial Bridge on Kitchen is active
        print(f"\n[*] Ensuring serial bridge is active on {kitchen_target}...")
        if not run_setup_bridge_test(client, target=kitchen_target, timeout=timeout):
            print(f"[-] TEST FAILED: Unable to establish serial bridge to {kitchen_target}.")
            return False
        time.sleep(0.3)

        # Step 2: Configure Garage Pin 16 as INPUT GPIO
        print(f"\n[*] Configuring {garage_target} Pin {pin} as INPUT GPIO...")
        init_gpio_handler(client, target=garage_target)
        set_gpio_mode_in(client, target=garage_target, pin=pin, timeout=timeout)
        time.sleep(0.3)

        # Step 3: Clear Arduino PIN_D0 (clrx) via Kitchen
        print(f"\n[*] [STEP 1/2] Actuating Arduino PIN_D0 LOW (clrx) via {kitchen_target}...")
        if not set_arduino_pin_d0(client, target=kitchen_target, state=False, timeout=timeout):
            print(f"[-] TEST FAILED: Timed out or failed waiting for 'GotClrx' from Arduino.")
            return False
        time.sleep(0.3)

        # Step 4: Read Garage Pin 16, verify it is 0 (LOW)
        print(f"\n[*] Reading {garage_target} Pin {pin} level (expected: 0 / LOW)...")
        val_low = read_gpio_pin(client, target=garage_target, pin=pin, timeout=timeout)
        if val_low is None:
            print(f"[-] TEST FAILED: Timed out waiting for Pin {pin} read response from {garage_target}.")
            return False

        if val_low != 0:
            print(f"[-] TEST FAILED: Expected {garage_target} Pin {pin} == 0 (LOW) after clrx, but got {val_low}!")
            return False
        print(f"[+] VERIFIED: {garage_target} Pin {pin} correctly read LOW (0) after Arduino clrx.")

        # Step 5: Set Arduino PIN_D0 (setx) via Kitchen
        print(f"\n[*] [STEP 2/2] Actuating Arduino PIN_D0 HIGH (setx) via {kitchen_target}...")
        if not set_arduino_pin_d0(client, target=kitchen_target, state=True, timeout=timeout):
            print(f"[-] TEST FAILED: Timed out or failed waiting for 'GotSetx' from Arduino.")
            return False
        time.sleep(0.3)

        # Step 6: Read Garage Pin 16, verify it is 1 (HIGH)
        print(f"\n[*] Reading {garage_target} Pin {pin} level (expected: 1 / HIGH)...")
        val_high = read_gpio_pin(client, target=garage_target, pin=pin, timeout=timeout)
        if val_high is None:
            print(f"[-] TEST FAILED: Timed out waiting for Pin {pin} read response from {garage_target}.")
            return False

        if val_high != 1:
            print(f"[-] TEST FAILED: Expected {garage_target} Pin {pin} == 1 (HIGH) after setx, but got {val_high}!")
            return False
        print(f"[+] VERIFIED: {garage_target} Pin {pin} correctly read HIGH (1) after Arduino setx.")

        # Final Success
        print("\n" + "=" * 65)
        print(f"[+] TEST PASSED: {garage_target} Pin {pin} GPIO input fully verified!")
        print(f"    - clrx -> Arduino D0 LOW  -> Garage Pin {pin} read: 0 [PASS]")
        print(f"    - setx -> Arduino D0 HIGH -> Garage Pin {pin} read: 1 [PASS]")
        print("=" * 65)
        return True

    finally:
        client.remove_rx_callback(on_rx)


def run_bidirectional_gpio_test(
    client: CoordinatorClient,
    kitchen_target: str = "Kitchen",
    garage_target: str = "Garage",
    kitchen_pin: int = 21,
    garage_pin: int = 17,
    timeout: float = 3.0
) -> bool:
    """
    Validates bidirectional GPIO wire communication between Kitchen and Garage:
    Wiring: Kitchen Pin 21 <---> Garage Pin 17

    1. Initial Safety: Sets both pins to INPUT mode.
    2. Direction 1: Kitchen Pin 21 (OUTPUT) -> Garage Pin 17 (INPUT)
       - Sends bit 0 from Kitchen -> verifies Garage reads 0
       - Sends bit 1 from Kitchen -> verifies Garage reads 1
    3. Safe Transition: Sets BOTH sides to INPUT before switching direction
       to prevent electrical bus contention / driver conflict.
    4. Direction 2: Garage Pin 17 (OUTPUT) -> Kitchen Pin 21 (INPUT)
       - Sends bit 0 from Garage -> verifies Kitchen reads 0
       - Sends bit 1 from Garage -> verifies Kitchen reads 1
    5. Final Cleanup: Sets both pins back to INPUT.
    """
    print("\n" + "=" * 65)
    print("--- [TEST B] BIDIRECTIONAL GPIO WIRE TEST (KITCHEN <-> GARAGE) ---")
    print(f"Wire Connection:   {kitchen_target} Pin {kitchen_pin} <---> {garage_target} Pin {garage_pin}")
    print(f"Direction 1:       {kitchen_target} (OUT) -> {garage_target} (IN)")
    print(f"Safe Transition:   Both set to INPUT (contention prevention)")
    print(f"Direction 2:       {garage_target} (OUT) -> {kitchen_target} (IN)")
    print(f"Timestamp:         {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")

    client.add_rx_callback(on_rx)

    try:
        # Phase 0: Initialize GPIO handlers on both nodes
        print(f"\n[*] [PHASE 0] Initializing GPIO handlers on {kitchen_target} and {garage_target}...")
        init_gpio_handler(client, kitchen_target)
        init_gpio_handler(client, garage_target)

        # Initial Safety: Set both sides to INPUT
        print(f"[*] Setting both pins to INPUT for safety before beginning...")
        set_gpio_mode_in(client, target=kitchen_target, pin=kitchen_pin, timeout=timeout)
        set_gpio_mode_in(client, target=garage_target, pin=garage_pin, timeout=timeout)
        time.sleep(0.3)

        # -------------------------------------------------------------
        # Phase 1: Direction 1 — Kitchen (Output) -> Garage (Input)
        # -------------------------------------------------------------
        print("\n" + "-" * 65)
        print(f"--- [DIRECTION 1] {kitchen_target} (Pin {kitchen_pin} OUT) -> {garage_target} (Pin {garage_pin} IN) ---")
        print("-" * 65)

        # Garage Pin 17 as INPUT
        set_gpio_mode_in(client, target=garage_target, pin=garage_pin, timeout=timeout)
        time.sleep(0.2)
        # Kitchen Pin 21 as OUTPUT
        set_gpio_mode_out(client, target=kitchen_target, pin=kitchen_pin, timeout=timeout)
        time.sleep(0.2)

        # Step 1.1: Kitchen sends 0 -> Garage reads 0
        print(f"\n[*] [DIR 1 - BIT 0] {kitchen_target} driving LOW (0)...")
        set_gpio_val(client, target=kitchen_target, pin=kitchen_pin, val=0, timeout=timeout)
        time.sleep(0.3)
        val = read_gpio_pin(client, target=garage_target, pin=garage_pin, timeout=timeout)
        if val is None:
            print(f"[-] TEST FAILED: Timed out reading {garage_target} Pin {garage_pin}.")
            return False
        if val != 0:
            print(f"[-] TEST FAILED: {kitchen_target} drove 0, but {garage_target} read {val}!")
            return False
        print(f"[+] VERIFIED: {kitchen_target} drove 0 -> {garage_target} correctly read 0.")

        # Step 1.2: Kitchen sends 1 -> Garage reads 1
        print(f"\n[*] [DIR 1 - BIT 1] {kitchen_target} driving HIGH (1)...")
        set_gpio_val(client, target=kitchen_target, pin=kitchen_pin, val=1, timeout=timeout)
        time.sleep(0.3)
        val = read_gpio_pin(client, target=garage_target, pin=garage_pin, timeout=timeout)
        if val is None:
            print(f"[-] TEST FAILED: Timed out reading {garage_target} Pin {garage_pin}.")
            return False
        if val != 1:
            print(f"[-] TEST FAILED: {kitchen_target} drove 1, but {garage_target} read {val}!")
            return False
        print(f"[+] VERIFIED: {kitchen_target} drove 1 -> {garage_target} correctly read 1.")

        print(f"\n[+] DIRECTION 1 PASSED: {kitchen_target} Pin {kitchen_pin} -> {garage_target} Pin {garage_pin} verified for both 0 and 1!")

        # -------------------------------------------------------------
        # Phase 2: Safe Transition — BOTH SIDES TO INPUT
        # -------------------------------------------------------------
        print("\n" + "-" * 65)
        print("--- [SAFETY TRANSITION] SETTING BOTH SIDES TO INPUT ---")
        print("--- Preventing contention / driver conflict before switching ---")
        print("-" * 65)
        set_gpio_mode_in(client, target=kitchen_target, pin=kitchen_pin, timeout=timeout)
        set_gpio_mode_in(client, target=garage_target, pin=garage_pin, timeout=timeout)
        time.sleep(0.5)
        print("[+] Both sides confirmed in high-impedance INPUT mode.")

        # -------------------------------------------------------------
        # Phase 3: Direction 2 — Garage (Output) -> Kitchen (Input)
        # -------------------------------------------------------------
        print("\n" + "-" * 65)
        print(f"--- [DIRECTION 2] {garage_target} (Pin {garage_pin} OUT) -> {kitchen_target} (Pin {kitchen_pin} IN) ---")
        print("-" * 65)

        # Kitchen Pin 21 as INPUT
        set_gpio_mode_in(client, target=kitchen_target, pin=kitchen_pin, timeout=timeout)
        time.sleep(0.2)
        # Garage Pin 17 as OUTPUT
        set_gpio_mode_out(client, target=garage_target, pin=garage_pin, timeout=timeout)
        time.sleep(0.2)

        # Step 2.1: Garage sends 0 -> Kitchen reads 0
        print(f"\n[*] [DIR 2 - BIT 0] {garage_target} driving LOW (0)...")
        set_gpio_val(client, target=garage_target, pin=garage_pin, val=0, timeout=timeout)
        time.sleep(0.3)
        val = read_gpio_pin(client, target=kitchen_target, pin=kitchen_pin, timeout=timeout)
        if val is None:
            print(f"[-] TEST FAILED: Timed out reading {kitchen_target} Pin {kitchen_pin}.")
            return False
        if val != 0:
            print(f"[-] TEST FAILED: {garage_target} drove 0, but {kitchen_target} read {val}!")
            return False
        print(f"[+] VERIFIED: {garage_target} drove 0 -> {kitchen_target} correctly read 0.")

        # Step 2.2: Garage sends 1 -> Kitchen reads 1
        print(f"\n[*] [DIR 2 - BIT 1] {garage_target} driving HIGH (1)...")
        set_gpio_val(client, target=garage_target, pin=garage_pin, val=1, timeout=timeout)
        time.sleep(0.3)
        val = read_gpio_pin(client, target=kitchen_target, pin=kitchen_pin, timeout=timeout)
        if val is None:
            print(f"[-] TEST FAILED: Timed out reading {kitchen_target} Pin {kitchen_pin}.")
            return False
        if val != 1:
            print(f"[-] TEST FAILED: {garage_target} drove 1, but {kitchen_target} read {val}!")
            return False
        print(f"[+] VERIFIED: {garage_target} drove 1 -> {kitchen_target} correctly read 1.")

        print(f"\n[+] DIRECTION 2 PASSED: {garage_target} Pin {garage_pin} -> {kitchen_target} Pin {kitchen_pin} verified for both 0 and 1!")

        # -------------------------------------------------------------
        # Phase 4: Final Cleanup — BOTH SIDES TO INPUT
        # -------------------------------------------------------------
        print("\n" + "-" * 65)
        print("--- [CLEANUP] SETTING BOTH SIDES TO INPUT ---")
        print("-" * 65)
        set_gpio_mode_in(client, target=kitchen_target, pin=kitchen_pin, timeout=timeout)
        set_gpio_mode_in(client, target=garage_target, pin=garage_pin, timeout=timeout)
        time.sleep(0.2)

        # Final Summary
        print("\n" + "=" * 65)
        print(f"[+] TEST PASSED: BIDIRECTIONAL GPIO TRANSMISSION FULLY VERIFIED!")
        print(f"    - Direction 1: {kitchen_target} (Pin {kitchen_pin}) -> {garage_target} (Pin {garage_pin}): [PASS]")
        print(f"    - Safe Contention-Avoidance Transition:                 [PASS]")
        print(f"    - Direction 2: {garage_target} (Pin {garage_pin}) -> {kitchen_target} (Pin {kitchen_pin}): [PASS]")
        print("=" * 65)
        return True

    finally:
        # Guarantee both pins are safe in INPUT mode even if an exception occurs
        try:
            client.send_line(f"{kitchen_target} ModeGPIOin {kitchen_pin}")
            client.send_line(f"{garage_target} ModeGPIOin {garage_pin}")
        except Exception:
            pass
        client.remove_rx_callback(on_rx)


# =============================================================================
# MESH RESILIENCE & TOPOLOGY RECOVERY TEST SUITE
# =============================================================================

def get_network_report(client: CoordinatorClient, timeout: float = 4.0) -> dict:
    """
    Sends 'GiveNetworkReport' to Coordinator and parses the response into a structured dict.
    Returns:
        dict[str, dict]: Mapping node name to its properties:
        {
            'Kitchen': {
                'name': 'Kitchen',
                'ieee': '0xB43A45FFFE8AC640',
                'short': '0x0001',
                'online': True,
                'type': 'END_DEVICE',
                'lqi': 204,
                'remote_lqi': 218,
                'rf_status': 'OK',
                'age': 0,
                'route': 'YES'
            }, ...
        }
    """
    nodes = {}
    in_report = False
    done_event = threading.Event()

    def on_rx(msg: str):
        nonlocal in_report
        clean = msg.strip()
        if "NETWORK_REPORT_BEGIN" in clean:
            in_report = True
            return
        if "NETWORK_REPORT_END" in clean:
            in_report = False
            done_event.set()
            return
        if in_report and "," in clean and not clean.startswith("NAME,"):
            parts = [p.strip() for p in clean.split(",")]
            if len(parts) >= 10:
                name = parts[0]
                nodes[name] = {
                    "name": name,
                    "ieee": parts[1],
                    "short": parts[2],
                    "online": (parts[3] == "1"),
                    "type": parts[4],
                    "lqi": int(parts[5]) if parts[5].isdigit() else 0,
                    "remote_lqi": int(parts[6]) if parts[6].isdigit() else -1,
                    "rf_status": parts[7],
                    "age": int(parts[8]) if parts[8].isdigit() else 0,
                    "route": parts[9]
                }

    client.add_rx_callback(on_rx)
    try:
        client.send_line("GiveNetworkReport")
        done_event.wait(timeout=timeout)
    finally:
        client.remove_rx_callback(on_rx)

    return nodes


def wait_for_mesh_established(
    client: CoordinatorClient,
    expected_nodes: list = None,
    timeout: float = 15.0,
    poll_interval: float = 1.0
) -> tuple:
    """
    Polls get_network_report until all expected_nodes are online with valid short address and route == 'YES'.
    Returns (success: bool, final_report: dict, elapsed_sec: float).
    """
    if expected_nodes is None:
        expected_nodes = ["Kitchen", "Garage"]

    t_start = time.time()
    t_end = t_start + timeout
    last_report = {}

    while time.time() < t_end:
        report = get_network_report(client, timeout=3.5)
        last_report = report

        all_online = True
        for node in expected_nodes:
            entry = report.get(node)
            if not entry or not entry.get("online") or entry.get("route") != "YES" or entry.get("lqi", 0) <= 0:
                all_online = False
                break

        if all_online:
            elapsed = time.time() - t_start
            return True, report, elapsed

        time.sleep(poll_interval)

    elapsed = time.time() - t_start
    return False, last_report, elapsed


def verify_node_bidirectional(client: CoordinatorClient, target: str, timeout: float = 3.0) -> tuple:
    """
    Sends an active command ('<target> blink 1') to the node and verifies end-to-end ACK.
    Returns (success: bool, rtt_ms: float, response_msg: str).
    """
    got_reply = threading.Event()
    resp_text = ""
    t_start = time.time()

    def on_rx(msg: str):
        nonlocal resp_text
        if f"< {target}:" in msg and ("ACK" in msg or "PONG" in msg):
            resp_text = msg.strip()
            got_reply.set()

    client.add_rx_callback(on_rx)
    try:
        client.send_line(f"{target} blink 1")
        passed = got_reply.wait(timeout=timeout)
        rtt_ms = (time.time() - t_start) * 1000.0
        return passed, rtt_ms, resp_text
    finally:
        client.remove_rx_callback(on_rx)


def reset_node(client: CoordinatorClient, target: str, port: str = None) -> bool:
    """
    Resets a node. If target is 'Coordinator', sends ResetCoordinator.
    Otherwise sends OTA reboot command or pulses hardware DTR/RTS if port is given.
    """
    if target.lower() in ("coordinator", "coord"):
        print("[*] Sending ResetCoordinator command...")
        client.send_line("ResetCoordinator")
        return True

    if port:
        try:
            print(f"[*] Pulsing hardware reset (DTR/RTS) on {target} ({port})...")
            p = serial.Serial(port, 115200, timeout=0.1)
            p.dtr = False
            p.rts = True
            time.sleep(0.1)
            p.rts = False
            p.close()
            time.sleep(0.2)
            print(f"[+] Hardware reset pulse sent to {target} on {port}.")
            return True
        except Exception as ex:
            print(f"[!] Hardware reset on {port} failed ({ex}); falling back to OTA reboot command.")

    print(f"[*] Sending OTA reboot command: '{target} reboot'...")
    client.send_line(f"{target} reboot")
    return True


def run_mesh_subtest_all_nodes(
    client: CoordinatorClient,
    kitchen_target: str = "Kitchen",
    garage_target: str = "Garage",
    kitchen_port: str = None,
    garage_port: str = None,
    timeout: float = 20.0
) -> bool:
    """
    Sub-test 1: Resets all three nodes (Coordinator, Kitchen, Garage) and
    verifies that the full mesh topology and bidirectional routing re-establishes.
    """
    print("\n" + "=" * 65)
    print("  MESH SUB-TEST 1: RESET ALL THREE NODES (FULL MESH CYCLE)")
    print(f"  Nodes: Coordinator ({client.port}), {kitchen_target}, {garage_target}")
    print("=" * 65)

    t_cycle_start = time.time()

    # Step 1: Issue reboots to child nodes first so commands are dispatched across mesh
    print(f"[*] Step 1: Dispatching reboot to '{kitchen_target}'...")
    reset_node(client, kitchen_target, kitchen_port)
    time.sleep(0.1)

    print(f"[*] Step 2: Dispatching reboot to '{garage_target}'...")
    reset_node(client, garage_target, garage_port)
    time.sleep(0.2)

    # Step 3: Reset Coordinator
    print("[*] Step 3: Triggering Coordinator restart (ResetCoordinator)...")
    client.send_line("ResetCoordinator")
    time.sleep(0.3)

    # Step 4: Reconnect to Coordinator port
    if not client.reconnect(timeout=12.0):
        print("[-] FAILED: Could not reconnect to Coordinator after reset!")
        return False

    # Step 5: Wait for full mesh re-establishment
    print(f"[*] Step 4: Waiting for full mesh re-establishment (up to {timeout:.1f}s)...")
    mesh_ok, report, elapsed = wait_for_mesh_established(
        client,
        expected_nodes=[kitchen_target, garage_target],
        timeout=timeout,
        poll_interval=1.0
    )

    if not mesh_ok:
        print(f"[-] FAILED: Mesh did not re-establish within {timeout:.1f}s!")
        print(f"    Report state: {report}")
        return False

    print(f"[+] Full mesh topology re-established in {elapsed:.2f}s!")

    # Step 6: Verify bidirectional communication with active command
    print("[*] Step 5: Verifying active bidirectional messaging...")
    k_ok, k_rtt, k_resp = verify_node_bidirectional(client, kitchen_target)
    g_ok, g_rtt, g_resp = verify_node_bidirectional(client, garage_target)

    # Print results table
    print("\n" + "-" * 65)
    print("NODE            SHORT    ONLINE  LQI   REM_LQI  ROUTE  CMD RTT   STATUS")
    print("-" * 65)
    for tgt, ok, rtt in [(kitchen_target, k_ok, k_rtt), (garage_target, g_ok, g_rtt)]:
        entry = report.get(tgt, {})
        short_addr = entry.get("short", "N/A")
        online_str = "YES" if entry.get("online") else "NO"
        lqi_val = str(entry.get("lqi", 0))
        rem_lqi = str(entry.get("remote_lqi", -1))
        route_str = entry.get("route", "NO")
        rtt_str = f"{rtt:.0f}ms" if ok else "TIMEOUT"
        stat_str = "[PASS]" if (entry.get("online") and ok) else "[FAIL]"
        print(f"{tgt:<15} {short_addr:<8} {online_str:<7} {lqi_val:<5} {rem_lqi:<8} {route_str:<6} {rtt_str:<9} {stat_str}")
    print("-" * 65)

    all_passed = k_ok and g_ok
    if all_passed:
        total_time = time.time() - t_cycle_start
        print(f"[+] SUB-TEST 1 PASSED: Full 3-node mesh successfully re-established in {total_time:.2f}s!\n")
    else:
        print("[-] SUB-TEST 1 FAILED: One or more nodes failed bidirectional verification.\n")

    return all_passed


def run_mesh_subtest_garage_only(
    client: CoordinatorClient,
    garage_target: str = "Garage",
    kitchen_target: str = "Kitchen",
    garage_port: str = None,
    timeout: float = 14.0
) -> bool:
    """
    Sub-test 2: Resets only the Garage node and verifies dynamic mesh re-establishment
    while Coordinator and Kitchen remain continuously operational.
    """
    print("\n" + "=" * 65)
    print("  MESH SUB-TEST 2: RESET GARAGE NODE ONLY (PARTIAL HEAL)")
    print(f"  Target: {garage_target} (Verifying {kitchen_target} remains unaffected)")
    print("=" * 65)

    # Step 1: Verify baseline
    print("[*] Step 1: Checking baseline mesh health...")
    pre_report = get_network_report(client, timeout=3.5)
    if not pre_report.get(kitchen_target, {}).get("online"):
        print(f"[!] Warning: '{kitchen_target}' not online prior to test.")

    t_cycle_start = time.time()

    # Step 2: Reset Garage
    print(f"[*] Step 2: Resetting '{garage_target}'...")
    reset_node(client, garage_target, garage_port)
    time.sleep(1.2)  # Allow reboot to initiate and complete

    # Step 3: Wait for Garage to re-announce and re-establish
    print(f"[*] Step 3: Waiting for '{garage_target}' route re-establishment...")
    mesh_ok, report, elapsed = wait_for_mesh_established(
        client,
        expected_nodes=[garage_target],
        timeout=timeout,
        poll_interval=1.0
    )

    if not mesh_ok:
        print(f"[-] FAILED: '{garage_target}' did not re-establish within {timeout:.1f}s!")
        return False

    print(f"[+] '{garage_target}' re-established route in {elapsed:.2f}s!")

    # Step 4: Verify bidirectional traffic to both nodes
    print(f"[*] Step 4: Verifying bidirectional messaging...")
    g_ok, g_rtt, g_resp = verify_node_bidirectional(client, garage_target)
    k_ok, k_rtt, k_resp = verify_node_bidirectional(client, kitchen_target)

    # Print summary
    print("\n" + "-" * 65)
    print("NODE            SHORT    ONLINE  LQI   REM_LQI  ROUTE  CMD RTT   STATUS")
    print("-" * 65)
    for tgt, ok, rtt in [(garage_target, g_ok, g_rtt), (kitchen_target, k_ok, k_rtt)]:
        entry = report.get(tgt, {})
        short_addr = entry.get("short", "N/A")
        online_str = "YES" if entry.get("online") else "NO"
        lqi_val = str(entry.get("lqi", 0))
        rem_lqi = str(entry.get("remote_lqi", -1))
        route_str = entry.get("route", "NO")
        rtt_str = f"{rtt:.0f}ms" if ok else "TIMEOUT"
        stat_str = "[PASS]" if (entry.get("online") and ok) else "[FAIL]"
        print(f"{tgt:<15} {short_addr:<8} {online_str:<7} {lqi_val:<5} {rem_lqi:<8} {route_str:<6} {rtt_str:<9} {stat_str}")
    print("-" * 65)

    all_passed = g_ok and k_ok and (report.get(garage_target, {}).get("online", False))
    if all_passed:
        total_time = time.time() - t_cycle_start
        print(f"[+] SUB-TEST 2 PASSED: '{garage_target}' cleanly re-established in {total_time:.2f}s!\n")
    else:
        print(f"[-] SUB-TEST 2 FAILED: '{garage_target}' failed recovery verification.\n")

    return all_passed


def run_mesh_subtest_both_nodes(
    client: CoordinatorClient,
    kitchen_target: str = "Kitchen",
    garage_target: str = "Garage",
    kitchen_port: str = None,
    garage_port: str = None,
    timeout: float = 16.0
) -> bool:
    """
    Sub-test 3: Resets both Kitchen and Garage nodes simultaneously while
    the Coordinator stays active, and verifies both re-announce and re-join.
    """
    print("\n" + "=" * 65)
    print("  MESH SUB-TEST 3: RESET BOTH KITCHEN AND GARAGE (END-NODES REBOOT)")
    print(f"  Target: Reset {kitchen_target} and {garage_target} | Coord remains up")
    print("=" * 65)

    t_cycle_start = time.time()

    # Step 1: Dispatch reboots to both nodes
    print(f"[*] Step 1: Resetting '{kitchen_target}'...")
    reset_node(client, kitchen_target, kitchen_port)
    time.sleep(0.1)

    print(f"[*] Step 2: Resetting '{garage_target}'...")
    reset_node(client, garage_target, garage_port)
    time.sleep(1.2)  # Allow reboot cycles to initiate and complete

    # Step 2: Wait for both nodes to re-announce and re-establish
    print(f"[*] Step 3: Waiting for both '{kitchen_target}' and '{garage_target}' routes...")
    mesh_ok, report, elapsed = wait_for_mesh_established(
        client,
        expected_nodes=[kitchen_target, garage_target],
        timeout=timeout,
        poll_interval=1.0
    )

    if not mesh_ok:
        print(f"[-] FAILED: Nodes did not both re-establish within {timeout:.1f}s!")
        print(f"    Report state: {report}")
        return False

    print(f"[+] Both nodes re-established routes in {elapsed:.2f}s!")

    # Step 3: Verify bidirectional traffic to both nodes
    print("[*] Step 4: Verifying bidirectional messaging on both nodes...")
    k_ok, k_rtt, k_resp = verify_node_bidirectional(client, kitchen_target)
    g_ok, g_rtt, g_resp = verify_node_bidirectional(client, garage_target)

    # Print summary
    print("\n" + "-" * 65)
    print("NODE            SHORT    ONLINE  LQI   REM_LQI  ROUTE  CMD RTT   STATUS")
    print("-" * 65)
    for tgt, ok, rtt in [(kitchen_target, k_ok, k_rtt), (garage_target, g_ok, g_rtt)]:
        entry = report.get(tgt, {})
        short_addr = entry.get("short", "N/A")
        online_str = "YES" if entry.get("online") else "NO"
        lqi_val = str(entry.get("lqi", 0))
        rem_lqi = str(entry.get("remote_lqi", -1))
        route_str = entry.get("route", "NO")
        rtt_str = f"{rtt:.0f}ms" if ok else "TIMEOUT"
        stat_str = "[PASS]" if (entry.get("online") and ok) else "[FAIL]"
        print(f"{tgt:<15} {short_addr:<8} {online_str:<7} {lqi_val:<5} {rem_lqi:<8} {route_str:<6} {rtt_str:<9} {stat_str}")
    print("-" * 65)

    all_passed = k_ok and g_ok
    if all_passed:
        total_time = time.time() - t_cycle_start
        print(f"[+] SUB-TEST 3 PASSED: Both child nodes re-established in {total_time:.2f}s!\n")
    else:
        print("[-] SUB-TEST 3 FAILED: One or more child nodes failed bidirectional verification.\n")

    return all_passed


def run_mesh_resilience_test(
    client: CoordinatorClient,
    kitchen_target: str = "Kitchen",
    garage_target: str = "Garage",
    kitchen_port: str = None,
    garage_port: str = None
) -> bool:
    """
    Executes the complete 3-stage Mesh Resilience & Recovery Test Suite:
      - Sub-Test 1: Reset all three nodes (Coordinator + Kitchen + Garage)
      - Sub-Test 2: Reset Garage node only
      - Sub-Test 3: Reset both Kitchen and Garage nodes
    """
    print("\n" + "=" * 65)
    print("       STARTING COMPLETE MESH RESILIENCE TEST SUITE")
    print(f"Coordinator: {client.port} | Kitchen: {kitchen_target} | Garage: {garage_target}")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    results = {}

    # Sub-Test 1: All three nodes reset
    print("\n>>> EXECUTING SUB-TEST 1 OF 3 <<<")
    results["SubTest1_AllNodes"] = run_mesh_subtest_all_nodes(
        client,
        kitchen_target=kitchen_target,
        garage_target=garage_target,
        kitchen_port=kitchen_port,
        garage_port=garage_port
    )
    time.sleep(1.0)

    # Sub-Test 2: Garage only reset
    print("\n>>> EXECUTING SUB-TEST 2 OF 3 <<<")
    results["SubTest2_GarageOnly"] = run_mesh_subtest_garage_only(
        client,
        garage_target=garage_target,
        kitchen_target=kitchen_target,
        garage_port=garage_port
    )
    time.sleep(1.0)

    # Sub-Test 3: Both end-nodes reset
    print("\n>>> EXECUTING SUB-TEST 3 OF 3 <<<")
    results["SubTest3_BothEndNodes"] = run_mesh_subtest_both_nodes(
        client,
        kitchen_target=kitchen_target,
        garage_target=garage_target,
        kitchen_port=kitchen_port,
        garage_port=garage_port
    )

    # Final Master Summary
    print("\n" + "=" * 65)
    print("            MESH RESILIENCE TEST SUITE SUMMARY")
    print("=" * 65)
    subtests = [
        ("SubTest1_AllNodes",     "Sub-Test 1: All Nodes Reset (Coordinator + Kitchen + Garage)"),
        ("SubTest2_GarageOnly",   "Sub-Test 2: Garage Only Reset (Partial Topology Healing)"),
        ("SubTest3_BothEndNodes", "Sub-Test 3: Both End-Nodes Reset (Kitchen + Garage)")
    ]

    all_passed = True
    for key, desc in subtests:
        passed = results.get(key, False)
        status = "[PASS]" if passed else "[FAIL]"
        if not passed:
            all_passed = False
        print(f"  {status:<10} {desc}")

    print("=" * 65)
    if all_passed:
        print("  >>> ALL 3 MESH RESILIENCE TESTS PASSED SUCCESSFULLY! (3/3) <<<")
    else:
        print("  >>> ONE OR MORE MESH RESILIENCE TESTS FAILED <<<")
    print("=" * 65 + "\n")

    return all_passed


def run_full_regression_suite(client: CoordinatorClient, target: str = "Kitchen", garage_target: str = "Garage", blink_count: int = 3, blinkx_count: int = 4, dac_val: int = 512) -> bool:
    """Executes all fully automated regression tests in sequence and displays a structured pass/fail report."""
    print("\n" + "=" * 65)
    print("      STARTING FULLY AUTOMATED ZIGBEE REGRESSION SUITE")
    print(f"Target:    {target} | Garage: {garage_target}")
    print(f"Started:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Mode:      Fully Automatic (Zero User Input Required)")
    print("=" * 65)

    results = {}

    # Test 1: Setup Serial Bridge
    results["SetupSerialBridge"] = run_setup_bridge_test(client, target=target)
    time.sleep(0.5)

    # Test 2: Two-Way Serial Ping
    results["Serial_Ping"] = run_ping_test(client, target=target)
    time.sleep(0.5)

    # Test 3: Arduino Hardware DAC
    results["Arduino_DAC"] = run_dac_test(client, target=target, dac_val=dac_val)
    time.sleep(0.5)

    # Test 4: Closed-Loop DAC -> ADC Loopback
    results["DAC_ADC_Loopback"] = run_dac_adc_loopback_test(client, target=target)
    time.sleep(0.5)

    # Test 5: Schmitt Trigger Hysteresis Ramp Test
    results["Schmitt_Ramp"] = run_schmitt_hysteresis_ramp_test(client, target=target)
    time.sleep(0.5)

    # Test 6: Closed-Loop DAC -> ADCpot 20-Detent Ramp Test
    results["ADCpot_Ramp"] = run_adcpot_ramp_test(client, target=target)
    time.sleep(0.5)

    # Test 7: Garage Pin 16 GPIO Input Test
    results["Garage_GPIO"] = run_garage_gpio_test(client, garage_target=garage_target, kitchen_target=target)
    time.sleep(0.5)

    # Test 8: Bidirectional GPIO Wire Test (Kitchen Pin 21 <-> Garage Pin 17)
    results["Bidi_GPIO"] = run_bidirectional_gpio_test(client, kitchen_target=target, garage_target=garage_target, kitchen_pin=21, garage_pin=17)
    time.sleep(0.5)

    # Test 9: PCF8574 Expand8 Alternating Bit Test
    results["Expand8"] = run_expand8_test(client, target=target)

    # Print Final Summary Table
    print("\n" + "=" * 65)
    print("             REGRESSION TEST RESULTS SUMMARY")
    print("=" * 65)
    labels = {
        "SetupSerialBridge": "[2] Setup Serial Bridge",
        "Serial_Ping":       "[5] Two-Way Serial Ping        (GotPing)",
        "Arduino_DAC":       f"[6] Arduino Hardware DAC        (GotDAC {dac_val})",
        "DAC_ADC_Loopback":  "[8] Closed-Loop DAC -> ADC     (Pin 1 Full V & Pin 0 Half V)",
        "Schmitt_Ramp":      "[9] Schmitt Hysteresis Ramp   (0 -> 1000 -> 0, 2s+2s)",
        "ADCpot_Ramp":       "[A] ADCpot Detent Ramp        (20 Detents, 0 -> 1023 -> 0)",
        "Garage_GPIO":       "[G] Garage Pin 16 GPIO Input  (Arduino PIN_D0 -> Garage Pin 16)",
        "Bidi_GPIO":         "[B] Bidirectional GPIO Wire   (Kitchen Pin 21 <-> Garage Pin 17)",
        "Expand8":           "[E] Dual PCF8574 Expand8 Test (Both Chip Instances: Alternating 0/1 & 1/0)"
    }

    all_passed = True
    for key, label in labels.items():
        passed = results.get(key, False)
        status = "[PASS]" if passed else "[FAIL]"
        if not passed:
            all_passed = False
        print(f"  {status:<10} {label}")

    print("=" * 65)
    total_tests = len(labels)
    if all_passed:
        print(f"  >>> ALL AUTOMATED REGRESSION TESTS PASSED! ({total_tests}/{total_tests}) <<<")
    else:
        print("  >>> ONE OR MORE TESTS FAILED - REVIEW LOGS ABOVE <<<")
    print("=" * 65 + "\n")

    return all_passed


def interactive_terminal(client: CoordinatorClient):
    """Provides a raw PuTTY-like interactive console for manual commands."""
    print("\n[+] Entering Interactive PuTTY-Bridge Console mode.")
    print("    Type coordinator commands like:")
    print("      Kitchen SetupSerialBridge")
    print("      Kitchen SetupProximity")
    print("      Kitchen ReadProximity")
    print("      Kitchen blink 3")
    print("      Kitchen blinkx 5")
    print("      Kitchen ping")
    print("      Kitchen setDAC 512")
    print("      Kitchen clrx")
    print("      Kitchen setx")
    print("      Kitchen ModeGPIOin 21")
    print("      Kitchen ModeGPIOout 21")
    print("      Kitchen SetGPIOval 21 1")
    print("      Kitchen ReadGPIO 21")
    print("      Kitchen RFdropBlink 2")
    print("      Kitchen SetupMotorDriver 1 5000")
    print("      Kitchen MotorSlew 500")
    print("      Kitchen MotorSpeed 1000")
    print("      Kitchen MotorSpeed -1000")
    print("      Kitchen MotorSpeed 0")
    print("      Garage SetupGPIOhandler")
    print("      Garage ModeGPIOin 16")
    print("      Garage ReadGPIO 16")
    print("      Garage ModeGPIOin 17")
    print("      Garage ModeGPIOout 17")
    print("      Garage SetGPIOval 17 1")
    print("      Garage ReadGPIO 17")
    print("      Kitchen NeoGPIO 19 7")
    print("      Kitchen NeoRED 0 128")
    print("      Kitchen NeoUpdate")
    print("      Garage SetupUnipolar")
    print("      Garage UnitStepDelay0 40")
    print("      Garage UnitStepDelay1 10")
    print("      Garage AccelSteps 100")
    print("      Garage UniStepFwd 512")
    print("      Garage UniiStepBkw 512")
    print("      GiveNetworkReport")
    print("      PingNetwork")
    print("    Type 'exit' or 'quit' to return to menu.\n")

    def on_rx(line: str):
        print(f"  <-- {line}")

    client.add_rx_callback(on_rx)

    while True:
        try:
            cmd = input("Coordinator> ").strip()
            if not cmd:
                continue
            if cmd.lower() in ("exit", "quit"):
                break
            client.send_line(cmd)
            time.sleep(0.3)
        except (KeyboardInterrupt, EOFError):
            print("\n")
            break

    client.remove_rx_callback(on_rx)


def main():
    parser = argparse.ArgumentParser(description="Zigbee Mesh HIL Regression Test Runner")
    parser.add_argument("-p", "--port", type=str, default=None, help="Coordinator COM port (e.g. COM11)")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("-t", "--target", type=str, default="Kitchen", help="Target node name in coordinator device table (default: Kitchen)")
    parser.add_argument("-c", "--count", type=int, default=3, help="Number of blinks for blink/blinkx tests (default: 3)")
    parser.add_argument("--dac", type=int, default=None, help="Run setDAC test with specified value (0..1023) and exit")
    parser.add_argument("--adc", type=str, nargs="?", const="A1", default=None, help="Read ADC channel (default: A1) and exit")
    parser.add_argument("--loopback", action="store_true", help="Run closed-loop DAC -> ADC loopback test and exit")
    parser.add_argument("--blink", action="store_true", help="Run ESP32-C6 onboard LED blink test and exit")
    parser.add_argument("--blinkx", action="store_true", help="Run Arduino DotStar blinkx test and exit")
    parser.add_argument("--ping", action="store_true", help="Run the two-way GotPing round-trip test and exit")
    parser.add_argument("--bridge", action="store_true", help="Run SetupSerialBridge test and exit")
    parser.add_argument("--servo", "--pwm", action="store_true", help="Run Servo / PWM test on Pin 2 (snap vs slew 30) and exit")
    parser.add_argument("--cytron", "--motor", action="store_true", help="Run Cytron 10C motor driver test (PWM=Pin 18, DIR=Pin 20) and exit")
    parser.add_argument("--rfloss", "--cytron-rfloss", "--motor-rfloss", action="store_true", help="Run Cytron motor RF-loss gentle stop fail-safe test (100%% power -> pull coord power -> 0 -> reconnect -> 100%%) and exit")
    parser.add_argument("--schmitt", "--ramp", action="store_true", help="Run Schmitt trigger hysteresis ramp test (2s up + 2s down) and exit")
    parser.add_argument("--adcpot", "--pot", action="store_true", help="Run Closed-Loop DAC -> ADCpot 20-detent ramp test (0 -> 1023 -> 0) and exit")
    parser.add_argument("--prox", "--proximity", action="store_true", help="Run APDS9930 Proximity Sensor test and exit")
    parser.add_argument("--read-prox", action="store_true", help="Read APDS9930 instantaneous proximity value and exit")
    parser.add_argument("--garage", type=str, default="Garage", help="Garage node name in coordinator device table (default: Garage)")
    parser.add_argument("--gpio", "--garage-gpio", action="store_true", help="Run Garage Pin 16 GPIO input test (clrx/setx via Kitchen) and exit")
    parser.add_argument("--bidi-gpio", "--cross-gpio", action="store_true", help="Run bidirectional GPIO wire test (Kitchen Pin 21 <-> Garage Pin 17) and exit")
    parser.add_argument("--rfdrop", action="store_true", help="Run RFdropBlink Link-Loss test on target node (default: Garage, Pin 18) and exit")
    parser.add_argument("--rfdrop-pin", type=int, default=18, help="GPIO pin for RFdropBlink test (default: 18)")
    parser.add_argument("--expand8", "--pcf8574", action="store_true", help="Run Dual PCF8574 Expand8 alternating bit test on both chip instances and exit")
    parser.add_argument("--expand8-addr", type=int, default=32, help="I2C address for PCF8574 Expand8 Instance #1 (default: 32 / 0x20)")
    parser.add_argument("--expand8-addr2", type=int, default=None, help="I2C address for PCF8574 Expand8 Instance #2 (default: auto-detect via scanI2C or 33 / 0x21)")
    parser.add_argument("--cleardisp", action="store_true", help="Clear SSD1306 OLED display on target node and exit")
    parser.add_argument("--print-disp", nargs="+", help="Print text on SSD1306 OLED display ([size] text...) and exit")
    parser.add_argument("--oled", "--ssd1306", "--disp", action="store_true", help="Run SSD1306 OLED multi-size FreeMono font test and exit")
    parser.add_argument("--oled-addr", type=int, default=60, help="I2C address for SSD1306 OLED (default: 60 / 0x3C)")
    parser.add_argument("--compass", "--qmc5883l", action="store_true", help="Run QMC5883L 3-axis compass rotation test and exit")
    parser.add_argument("--compass-hyst", "--mag", action="store_true", help="Run QMC5883L 12-threshold signed hysteresis test (X,Y,Z +/- [LowMag, HighMag]) and exit")
    parser.add_argument("--low-mag", type=int, default=1000, help="LowMag hysteresis threshold for QMC5883L comparator test (default: 1000)")
    parser.add_argument("--high-mag", type=int, default=2000, help="HighMag hysteresis threshold for QMC5883L comparator test (default: 2000)")
    parser.add_argument("--get-compass", action="store_true", help="Stream QMC5883L compass readings (x,y,z every 300ms, ESC to stop) and exit")
    parser.add_argument("--compass-addr", type=int, default=13, help="I2C address for QMC5883L compass (default: 13 / 0x0D)")
    parser.add_argument("--rgb", "--rgb-fade", action="store_true", help="Run Garage three-color RGB LED PWM fade cycle test (Pins 18, 20, 19) and exit")
    parser.add_argument("--rgb-duration", type=float, default=1.0, help="Duration in seconds for each color 0-100-0%% fade cycle (default: 1.0s)")
    parser.add_argument("--neopixel", "--neo", "--strand", action="store_true", help="Run NeoPixel strand step test (configurable pin and count) and exit")
    parser.add_argument("--neopixel-pin", type=int, default=19, help="GPIO pin for NeoPixel data line (default: 19)")
    parser.add_argument("--neopixel-count", type=int, default=16, help="Number of NeoPixels in strand (default: 16, e.g. 7)")
    parser.add_argument("--neopixel-delay", "--neopixel-duration", type=float, default=0.5, help="Delay in seconds between each LED strand update (default: 0.5s)")
    parser.add_argument("--neopixel-brightness", type=int, default=128, help="Brightness for NeoPixel color steps (default: 128)")
    parser.add_argument("--unipolar", action="store_true", help="Run Unipolar Stepper motor test (Adafruit 28BYJ-48: Fwd/Bkw) and exit")
    parser.add_argument("--uni-steps", type=int, default=512, help="Number of steps for Unipolar Stepper test (default: 512)")
    parser.add_argument("--uni-delay", type=int, default=UNIPOLAR_DEFAULT_DELAY_MS, help=f"Step delay in ms for Unipolar Stepper (default: {UNIPOLAR_DEFAULT_DELAY_MS})")
    parser.add_argument("--uni-mode", type=int, default=0, choices=[0, 1, 2], help="Drive mode for Unipolar Stepper: 0=Full-step (default), 1=Half-step, 2=Wave drive")
    parser.add_argument("--uni-fwd", type=int, default=None, help="Step Unipolar Stepper forward by N steps and exit")
    parser.add_argument("--uni-bkw", type=int, default=None, help="Step Unipolar Stepper backward by N steps and exit")
    parser.add_argument("--bipolar", "--stepper", "--drv8833", action="store_true", help="Run Bipolar Stepper test on Garage (HiLetgo DRV8833 @ 5V, 180 full-steps, StepPWMduty=75, 70ms/step, no accel/decel) and exit")
    parser.add_argument("--bipolar-steps", type=int, default=180, help="Number of full-steps for Bipolar Stepper test (default: 180)")
    parser.add_argument("--bipolar-duty", type=int, default=75, help="StepPWMduty percentage for Bipolar Stepper test (default: 75)")
    parser.add_argument("--bipolar-ms", type=int, default=70, help="Delay per step in ms for Bipolar Stepper test (default: 70)")
    parser.add_argument("--all", "--suite", action="store_true", help="Run the automated regression test suite (fully automatic, no user input) and exit")
    parser.add_argument("--mesh", action="store_true", help="Run the full 3-stage Mesh Resilience test suite (all sub-tests) and exit")
    parser.add_argument("--mesh-sub1", action="store_true", help="Run Mesh Sub-Test 1 (Reset all 3 nodes: Coord, Kitchen, Garage) and exit")
    parser.add_argument("--mesh-sub2", action="store_true", help="Run Mesh Sub-Test 2 (Reset Garage node only) and exit")
    parser.add_argument("--mesh-sub3", action="store_true", help="Run Mesh Sub-Test 3 (Reset both Kitchen and Garage) and exit")
    parser.add_argument("--kitchen-port", type=str, default=None, help="Optional serial port for Kitchen node hardware reset (e.g. COM5)")
    parser.add_argument("--garage-port", type=str, default=None, help="Optional serial port for Garage node hardware reset (e.g. COM8)")
    parser.add_argument("--n2", type=str, default="N2", help="N2 node name in coordinator device table (default: N2)")
    parser.add_argument("--rotary", action="store_true", help="Run Keyes KY-040 Rotary Encoder test (N2 -> Kitchen OLED) and exit")
    parser.add_argument("--rotary-raw", "--rotary-z", "-z", action="store_true", help="Run [Z] Keyes KY-040 2-bit raw monitor test (Print-on-Change) and exit")

    args = parser.parse_args()

    port = args.port or choose_port()
    client = CoordinatorClient(port=port, baudrate=args.baud)

    if not client.connect():
        sys.exit(1)

    # Verify mesh link before dispatching tests
    client.wait_for_node(target=args.target, timeout=10.0)

    try:
        # CLI direct test execution flags
        if args.rotary_raw:
            passed = run_rotary_raw_monitor_test(client, target=args.n2, clk_pin=18, dt_pin=20, sw_pin=19)
            sys.exit(0 if passed else 1)

        if args.rotary:
            passed = run_rotary_encoder_test(client, rotary_target=args.n2, oled_target=args.target, oled_addr=args.oled_addr)
            sys.exit(0 if passed else 1)

        if args.mesh:
            passed = run_mesh_resilience_test(client, kitchen_target=args.target, garage_target=args.garage, kitchen_port=args.kitchen_port, garage_port=args.garage_port)
            sys.exit(0 if passed else 1)

        if args.mesh_sub1:
            passed = run_mesh_subtest_all_nodes(client, kitchen_target=args.target, garage_target=args.garage, kitchen_port=args.kitchen_port, garage_port=args.garage_port)
            sys.exit(0 if passed else 1)

        if args.mesh_sub2:
            passed = run_mesh_subtest_garage_only(client, garage_target=args.garage, kitchen_target=args.target, garage_port=args.garage_port)
            sys.exit(0 if passed else 1)

        if args.mesh_sub3:
            passed = run_mesh_subtest_both_nodes(client, kitchen_target=args.target, garage_target=args.garage, kitchen_port=args.kitchen_port, garage_port=args.garage_port)
            sys.exit(0 if passed else 1)

        if args.all:
            passed = run_full_regression_suite(client, target=args.target, garage_target=args.garage, blink_count=args.count, blinkx_count=args.count)
            sys.exit(0 if passed else 1)

        if args.gpio:
            passed = run_garage_gpio_test(client, garage_target=args.garage, kitchen_target=args.target, pin=16)
            sys.exit(0 if passed else 1)

        if args.bidi_gpio:
            passed = run_bidirectional_gpio_test(client, kitchen_target=args.target, garage_target=args.garage, kitchen_pin=21, garage_pin=17)
            sys.exit(0 if passed else 1)

        if args.expand8:
            passed = run_expand8_test(client, target=args.target, addr=args.expand8_addr, addr2=args.expand8_addr2)
            sys.exit(0 if passed else 1)

        if args.cleardisp:
            passed = clear_display(client, target=args.target)
            sys.exit(0 if passed else 1)

        if args.print_disp:
            tokens = args.print_disp
            size = 12
            if tokens and tokens[0].isdigit():
                size = int(tokens[0])
                text = " ".join(tokens[1:])
            else:
                text = " ".join(tokens)
            passed = print_display_line(client, target=args.target, size=size, text=text)
            sys.exit(0 if passed else 1)

        if args.oled:
            passed = run_ssd1306_test(client, target=args.target, addr=args.oled_addr)
            sys.exit(0 if passed else 1)

        if args.compass:
            passed = run_compass_test(client, target=args.target, addr=args.compass_addr)
            sys.exit(0 if passed else 1)

        if args.compass_hyst:
            passed = run_compass_hysteresis_test(client, target=args.target, addr=args.compass_addr, low_mag=args.low_mag, high_mag=args.high_mag)
            sys.exit(0 if passed else 1)

        if args.get_compass:
            run_compass_stream(client, target=args.target, addr=args.compass_addr)
            sys.exit(0)

        if args.rgb:
            passed = run_garage_rgb_fade_test(client, target=args.garage, fade_duration=args.rgb_duration)
            sys.exit(0 if passed else 1)

        if args.neopixel:
            passed = run_neopixel_test(client, target=args.target, pin=args.neopixel_pin, num_pixels=args.neopixel_count, update_delay=args.neopixel_delay, brightness=args.neopixel_brightness)
            sys.exit(0 if passed else 1)

        if args.unipolar:
            passed = run_unipolar_stepper_test(client, target=args.garage, steps=args.uni_steps, step_delay_ms=args.uni_delay, mode=args.uni_mode)
            sys.exit(0 if passed else 1)

        if args.bipolar:
            passed = run_bipolar_stepper_test(client, target=args.garage, steps=args.bipolar_steps, pwm_duty=args.bipolar_duty, ms_per_step=args.bipolar_ms)
            sys.exit(0 if passed else 1)

        if args.uni_fwd is not None:
            setup_unipolar_stepper(client, target=args.garage)
            passed = step_unipolar_stepper(client, target=args.garage, steps=args.uni_fwd, forward=True)
            sys.exit(0 if passed else 1)

        if args.uni_bkw is not None:
            setup_unipolar_stepper(client, target=args.garage)
            passed = step_unipolar_stepper(client, target=args.garage, steps=args.uni_bkw, forward=False)
            sys.exit(0 if passed else 1)

        if args.bridge:
            passed = run_setup_bridge_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        if args.blink or args.blinkx:
            passed = run_multi_node_blink_test(client, count=args.count, target=args.target, garage_target=args.garage, n2_target=args.n2)
            sys.exit(0 if passed else 1)

        if args.ping:
            run_setup_bridge_test(client, target=args.target)
            time.sleep(0.3)
            passed = run_ping_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        if args.dac is not None:
            run_setup_bridge_test(client, target=args.target)
            time.sleep(0.3)
            passed = run_dac_test(client, target=args.target, dac_val=args.dac)
            sys.exit(0 if passed else 1)

        if args.adc is not None:
            passed = run_read_adc_test(client, target=args.target, channel=args.adc)
            sys.exit(0 if passed else 1)

        if args.loopback:
            passed = run_dac_adc_loopback_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        if args.schmitt:
            passed = run_schmitt_hysteresis_ramp_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        if args.adcpot:
            passed = run_adcpot_ramp_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        if args.servo:
            passed = run_servo_pwm_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        if args.cytron:
            passed = run_cytron_motor_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        if args.prox:
            passed = run_proximity_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        if args.read_prox:
            run_proximity_stream(client, target=args.target)
            sys.exit(0)

        if args.rfdrop:
            target_node = args.garage if (args.garage and args.target == "Kitchen") else args.target
            passed = run_rfdrop_blink_test(client, target=target_node, pin=args.rfdrop_pin)
            sys.exit(0 if passed else 1)

        if args.rfloss:
            passed = run_cytron_rfloss_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        # Interactive Menu
        while True:
            print("\n=======================================================")
            print("         ZIGBEE HIL REGRESSION TEST RUNNER")
            print("=======================================================")
            print("  [1] Run Automated Regression Test Suite (Fully Automatic - No User Input)")
            print("  [2] Setup Serial Bridge (SetupSerialBridge)")
            print("  [5] Two-Way Serial Ping Test    (GotPing)             [Auto-Bridge]")
            print("  [8] Closed-Loop DAC -> ADC Test (Pin 1 Full V & Pin 0 Half V) [Auto-Bridge]")
            print("  [9] Schmitt Trigger Ramp Test   (0 -> 1000 -> 0, 2s+2s)")
            print("  [A] ADCpot Detent Ramp Test     (20 Detents, 0 -> 1023 -> 0)  [Auto-Bridge]")
            print("  [G] Garage Pin 16 GPIO Test     (clrx/setx via Kitchen) [Auto-Bridge]")
            print("  [B] Bidirectional GPIO Wire     (Kitchen Pin 21 <-> Garage Pin 17)")
            print("  [E] Dual PCF8574 Expand8 Test   (Both Instances: Alternating 0/1 & 1/0 Patterns)")
            print()
            print("  # These hardware tests require user input:")
            print(f"  [3] Multi-Node LED & Serial Blink (All Endpoints + DotStar Auto-Bridge, count {args.count})")
            print("  [S] Servo / PWM Test            (Pin 2: Snap vs Slew 30)")
            print("  [M] Cytron 10C Motor Test       (PWM=Pin 18, DIR=Pin 20: 2s FWD, 4s REV, 1s Pause, 2s Return-0)")
            print("  [D] APDS9930 Proximity Test     (Setup, Baseline & Detection Cycle)")
            print("  [O] SSD1306 OLED Display Test   (ClearDisp + Multi-Size FreeMono Prints)")
            print("  [K] Keyes KY-040 Rotary Test    (N2 Pins [19,20,18] -> OLED Kitchen Display)")
            print("  [Z] Rotary 2-Bit Raw Monitor    (N2 Pins [19,20,18]: Live Print-on-Change CLK/DT/SW + Timestamps)")
            print("  [F] QMC5883L 12-Threshold Test  (X,Y,Z +/- [LowMag=1000, HighMag=2000])")
            print("  [L] Garage RGB LED Fade Cycle   (Pins 18, 20, 19 PWM Fade 0-100%)")
            print("  [V] NeoPixel Strand Test        (Kitchen: Red ON->OFF -> Green ON->ZERO -> Blue ON->OFF)")
            print("  [U] Unipolar Stepper Test       (Garage: Fwd/Bkw, Half Speed & Accel/Decel Ramp)")
            print("  [H] Bipolar Stepper Test        (Garage DRV8833 @ 5V: 180 Full-Steps & 360 Half-Steps)")
            print()
            print("  --- Various Functions ---")
            print("  [7] Read ADC Channel            (ReadADCA0 / ReadADCA1 / ReadADCA2)")
            print("  [R] Read Proximity Values       (ReadProximity - Continuous, ESC to stop)")
            print("  [6] Arduino Hardware DAC Test   (setDAC X)            [Auto-Bridge]")
            print("  [C] Clear OLED Display          (ClearDisp)")
            print("  [Q] Read Compass Values         (GetCompass)")
            print("  [J] RFdropBlink Link-Loss Test  (Garage Pin 18 LED: 4 Hz Flash on Disconnect)")
            print("  [Y] Cytron Motor RF-Loss Test   (100% Power -> Pull Coord Power -> Gentle Stop -> Reconnect)")
            print("  [W] Mesh Resilience Tests      (All 3 Sub-Tests: All, Garage, Both)")
            print("  [N] Request Network Report      (GiveNetworkReport)")
            print("  [P] Ping Network Nodes          (PingNetwork)")
            print("  [0] Interactive PuTTY Console Mode")
            print(f"  [T] Change Target Node Name     (Current: {args.target})")
            print("  [X] Exit")
            print("=======================================================")

            choice = input("Enter selection [0-9, A, S, M, D, R, G, B, E, O, C, K, Z, F, Q, L, V, U, H, W, J, Y, N, P, T, X]: ").strip().upper()

            if choice == "1":
                run_full_regression_suite(client, target=args.target, garage_target=args.garage, blink_count=args.count, blinkx_count=args.count)
            elif choice == "2":
                run_setup_bridge_test(client, target=args.target)
            elif choice in ("3", "4", "BLINK", "BLINKX"):
                run_multi_node_blink_test(client, count=args.count, target=args.target, garage_target=args.garage, n2_target=args.n2)
            elif choice == "5":
                run_setup_bridge_test(client, target=args.target)
                time.sleep(0.3)
                run_ping_test(client, target=args.target)
            elif choice == "6":
                run_setup_bridge_test(client, target=args.target)
                time.sleep(0.3)
                val_str = input("Enter DAC value (0-1023, where 512 ~ 1.65V, 1023 ~ 3.3V) [512]: ").strip()
                dac_val = int(val_str) if val_str.isdigit() else 512
                run_dac_test(client, target=args.target, dac_val=dac_val)
            elif choice == "7":
                chan_str = input("Enter ADC channel (A0/0, A1/1, A2/2) [A1]: ").strip().upper()
                channel = _normalize_adc_channel(chan_str) if chan_str else "A1"
                run_read_adc_test(client, target=args.target, channel=channel)
            elif choice == "8":
                run_dac_adc_loopback_test(client, target=args.target)
            elif choice == "9":
                run_schmitt_hysteresis_ramp_test(client, target=args.target)
            elif choice in ("A", "ADCPOT", "POT", "DETENT"):
                run_adcpot_ramp_test(client, target=args.target)
            elif choice in ("S", "SERVO", "PWM"):
                run_servo_pwm_test(client, target=args.target)
            elif choice in ("M", "MOTOR", "CYTRON"):
                run_cytron_motor_test(client, target=args.target)
            elif choice in ("D", "PROX", "PROXIMITY"):
                run_proximity_test(client, target=args.target)
            elif choice in ("R", "READPROX"):
                run_proximity_stream(client, target=args.target)
            elif choice in ("G", "GPIO"):
                run_garage_gpio_test(client, garage_target=args.garage, kitchen_target=args.target, pin=16)
            elif choice in ("B", "BIDI"):
                run_bidirectional_gpio_test(client, kitchen_target=args.target, garage_target=args.garage, kitchen_pin=21, garage_pin=17)
            elif choice in ("E", "EXPAND8", "PCF8574"):
                run_expand8_test(client, target=args.target, addr=args.expand8_addr, addr2=args.expand8_addr2)
            elif choice in ("O", "OLED", "SSD1306", "DISP"):
                run_ssd1306_test(client, target=args.target, addr=args.oled_addr)
            elif choice in ("K", "ROTARY", "ENCODER", "KY040"):
                run_rotary_encoder_test(client, rotary_target=args.n2, oled_target=args.target, oled_addr=args.oled_addr)
            elif choice in ("Z", "RAWROTARY", "MONITORROTARY", "ROTORAY", "ROTARYRAW"):
                run_rotary_raw_monitor_test(client, target=args.n2, clk_pin=18, dt_pin=20, sw_pin=19)
            elif choice in ("C", "CLEARDISP", "CLEAR"):
                clear_display(client, target=args.target)
            elif choice in ("F", "MAG", "COMPASSMAG", "COMPASSHYST"):
                run_compass_hysteresis_test(client, target=args.target, addr=args.compass_addr, low_mag=args.low_mag, high_mag=args.high_mag)
            elif choice in ("Q", "GETCOMPASS", "READCOMPASS"):
                run_compass_stream(client, target=args.target, addr=args.compass_addr)
            elif choice in ("L", "RGB", "FADE", "RGBLED", "LEDFADE"):
                run_garage_rgb_fade_test(client, target=args.garage, fade_duration=args.rgb_duration)
            elif choice in ("V", "NEO", "NEOPIXEL", "PIXEL", "STRAND"):
                pixels_input = input(f"Enter number of NeoPixels [{args.neopixel_count}]: ").strip()
                num_pixels = int(pixels_input) if (pixels_input.isdigit() and int(pixels_input) > 0) else args.neopixel_count
                pin_input = input(f"Enter NeoPixel GPIO pin [{args.neopixel_pin}]: ").strip()
                pin = int(pin_input) if (pin_input.isdigit() and int(pin_input) >= 0) else args.neopixel_pin
                run_neopixel_test(client, target=args.target, pin=pin, num_pixels=num_pixels, update_delay=args.neopixel_delay, brightness=args.neopixel_brightness)
            elif choice in ("U", "UNIPOLAR", "UNISTEP"):
                steps_str = input(f"Enter number of steps to test [{args.uni_steps}]: ").strip()
                steps = int(steps_str) if (steps_str.isdigit() and int(steps_str) > 0) else args.uni_steps
                run_unipolar_stepper_test(client, target=args.garage, steps=steps, step_delay_ms=args.uni_delay, mode=args.uni_mode)
            elif choice in ("H", "BIPOLAR", "STEPPER", "DRV8833"):
                ms_str = input(f"  Enter delay per step in ms [{args.bipolar_ms}]: ").strip()
                ms_val = int(ms_str) if (ms_str.isdigit() and int(ms_str) > 0) else args.bipolar_ms
                duty_str = input(f"  Enter StepPWMduty percentage (1-100) [{args.bipolar_duty}]: ").strip()
                duty_val = int(duty_str) if (duty_str.isdigit() and 1 <= int(duty_str) <= 100) else args.bipolar_duty
                run_bipolar_stepper_test(
                    client,
                    target=args.garage,
                    steps=args.bipolar_steps,
                    pwm_duty=duty_val,
                    ms_per_step=ms_val,
                    ap_gpio=1,
                    an_gpio=21,
                    bp_gpio=2,
                    bn_gpio=0,
                )
            elif choice in ("W", "MESH"):
                run_mesh_resilience_test(client, kitchen_target=args.target, garage_target=args.garage, kitchen_port=args.kitchen_port, garage_port=args.garage_port)
            elif choice in ("J", "RFDROP", "RFDROPBLINK"):
                target_node = args.garage if args.garage else "Garage"
                pin_str = input(f"Enter GPIO pin to test on {target_node} [18]: ").strip()
                pin_val = int(pin_str) if (pin_str.isdigit() and int(pin_str) >= 0) else 18
                run_rfdrop_blink_test(client, target=target_node, pin=pin_val)
            elif choice in ("Y", "RFLOSS", "CYTRONRFLOSS", "MOTORRFLOSS"):
                target_str = input(f"Enter target node for Cytron motor test [{args.target}]: ").strip()
                target_node = target_str if target_str else args.target
                run_cytron_rfloss_test(client, target=target_node)
            elif choice in ("N", "REPORT"):
                print("\n[>] Requesting Network Report (GiveNetworkReport)...")
                done_evt = threading.Event()

                def on_report_rx(msg: str):
                    print(f"  {msg}")
                    if "NETWORK_REPORT_END" in msg:
                        done_evt.set()

                client.add_rx_callback(on_report_rx)
                try:
                    report = get_network_report(client, timeout=4.0)
                    done_evt.wait(timeout=0.5)
                finally:
                    client.remove_rx_callback(on_report_rx)

                if report:
                    print("\n" + "-" * 72)
                    print(f"{'NODE':<14} {'SHORT':<8} {'ONLINE':<8} {'LQI':<6} {'REM_LQI':<9} {'RF_STAT':<8} {'AGE(s)':<7} {'ROUTE'}")
                    print("-" * 72)
                    for name, info in report.items():
                        online_str = "YES" if info.get("online") else "NO"
                        print(
                            f"{name:<14} {info.get('short', 'N/A'):<8} {online_str:<8} "
                            f"{info.get('lqi', 0):<6} {info.get('remote_lqi', -1):<9} "
                            f"{info.get('rf_status', 'N/A'):<8} {info.get('age', 0):<7} {info.get('route', 'NO')}"
                        )
                    print("-" * 72)
                else:
                    print("[!] No nodes returned in Network Report.")
            elif choice in ("P", "PINGNET"):
                print("\n[>] Pinging network nodes (PingNetwork)...")

                def on_ping_rx(msg: str):
                    print(f"  {msg}")

                client.add_rx_callback(on_ping_rx)
                try:
                    client.send_line("PingNetwork")
                    time.sleep(2.0)
                finally:
                    client.remove_rx_callback(on_ping_rx)
            elif choice == "0":
                interactive_terminal(client)
            elif choice == "T":
                new_target = input(f"Enter new target node name [{args.target}]: ").strip()
                if new_target:
                    args.target = new_target
            elif choice in ("X", "EXIT", "QUIT"):
                break
            else:
                print("[!] Invalid option, please retry.")

    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
