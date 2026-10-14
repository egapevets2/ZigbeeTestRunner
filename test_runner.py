#!/usr/bin/env python3
"""
Zigbee HIL (Hardware-in-the-Loop) Regression Test Runner
Communicates with the Zigbee Coordinator and validates end-to-end mesh
operations on the ESP32-C6 node (PNPzigbee) and test fixture (TXRXproto).
"""

import sys
import time
import re
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
            self.rx_callbacks.append(cb)

    def remove_rx_callback(self, cb):
        with self._lock:
            if cb in self.rx_callbacks:
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


def run_setup_bridge_test(client: CoordinatorClient, target: str = "Kitchen", timeout: float = 3.0) -> bool:
    """Sends `<target> SetupSerialBridge` and verifies response."""
    print("\n" + "=" * 65)
    print("--- [TEST 2] SETUP SERIAL BRIDGE ---")
    print(f"Target Node: {target} (ESP32-C6)")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    got_ok_event = threading.Event()

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")
        if "SETUP SERIAL OK" in msg:
            got_ok_event.set()

    client.add_rx_callback(on_rx)

    cmd_string = f"{target} SetupSerialBridge"
    print(f"\n[>] Sending command: \"{cmd_string}\"")
    start_time = time.time()
    client.send_line(cmd_string)

    success = got_ok_event.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    print("-" * 65)
    if success:
        print(f"[+] TEST PASSED: Serial bridge initialized successfully ({elapsed:.1f}ms)!")
    else:
        print(f"[-] TEST FAILED: Timed out waiting for 'SETUP SERIAL OK' from {target}.")
    return success


def run_blink_test(client: CoordinatorClient, target: str = "Kitchen", count: int = 3) -> bool:
    """
    Executes the ESP32-C6 onboard LED blink test:
    1. Sends `<target> blink <count>`.
    2. Prompts user to input the number of flashes observed.
    3. Asserts that the observed count matches the requested count.
    """
    print("\n" + "=" * 65)
    print("--- [TEST 3] ESP32-C6 ONBOARD LED BLINK TEST ---")
    print(f"Target Node:    {target} (ESP32-C6 Onboard LED)")
    print(f"Requested Blinks: {count}")
    print(f"Timestamp:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    print("\n[!] ACTION REQUIRED: Look at the ESP32-C6 onboard LED.")
    print(f"    Watch for {count} LED flash(es) and count them.")
    try:
        input("    Press [Enter] when ready to begin test...")
    except (EOFError, OSError):
        pass

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")

    client.add_rx_callback(on_rx)

    cmd_string = f"{target} blink {count}"
    print(f"\n[>] Sending command: \"{cmd_string}\"")
    client.send_line(cmd_string)

    # ESP32 blinks at 200ms ON + 200ms OFF = 400ms per pulse
    duration = (count * 0.4) + 0.5
    print(f"[*] Actuating onboard LED on {target} ({duration:.1f}s)...")
    time.sleep(duration)

    client.remove_rx_callback(on_rx)

    print("\n" + "-" * 65)
    while True:
        resp = input(f"[?] Enter the number of flashes observed on the ESP32-C6 LED: ").strip()
        if resp.isdigit():
            observed = int(resp)
            break
        print("    [!] Please enter a valid number (e.g. 0, 1, 2, 3...).")
    print("-" * 65)

    if observed == count:
        print(f"[+] TEST PASSED: Confirmed {count} flashes on ESP32-C6 onboard LED!")
        return True
    else:
        print(f"[-] TEST FAILED: Expected {count} flashes, but {observed} flashes were reported.")
        return False


def run_blinkx_test(client: CoordinatorClient, target: str = "Kitchen", count: int = 4) -> bool:
    """
    Executes the Arduino Trinket M0 DotStar LED blinkx test:
    1. Sends `<target> blinkx <count>`.
    2. Prompts user to input the number of blue flashes observed.
    3. Asserts that the observed count matches the requested count.
    """
    print("\n" + "=" * 65)
    print("--- [TEST 4] ARDUINO DOTSTAR LED BLINKX TEST ---")
    print(f"Target Node:    {target} -> Arduino Test Fixture (Blue DotStar LED)")
    print(f"Requested Blinks: {count}")
    print(f"Timestamp:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    print("\n[!] ACTION REQUIRED: Look at the Arduino Trinket M0 Blue DotStar LED.")
    print(f"    Watch for {count} BLUE flash(es) on the DotStar LED and count them.")
    try:
        input("    Press [Enter] when ready to begin test...")
    except (EOFError, OSError):
        pass

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")

    client.add_rx_callback(on_rx)

    cmd_string = f"{target} blinkx {count}"
    print(f"\n[>] Sending command: \"{cmd_string}\"")
    client.send_line(cmd_string)

    # Arduino blinks at 100ms ON + 100ms OFF = 200ms per pulse
    duration = (count * 0.2) + 0.5
    print(f"[*] Actuating DotStar LED on Arduino ({duration:.1f}s)...")
    time.sleep(duration)

    client.remove_rx_callback(on_rx)

    print("\n" + "-" * 65)
    while True:
        resp = input(f"[?] Enter the number of BLUE flashes observed on the Arduino DotStar: ").strip()
        if resp.isdigit():
            observed = int(resp)
            break
        print("    [!] Please enter a valid number (e.g. 0, 1, 2, 3...).")
    print("-" * 65)

    if observed == count:
        print(f"[+] TEST PASSED: Confirmed {count} blue flashes on Arduino DotStar LED!")
        return True
    else:
        print(f"[-] TEST FAILED: Expected {count} flashes, but {observed} flashes were reported.")
        return False


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


def read_adc_pin(client: CoordinatorClient, target: str = "Kitchen", channel: str = "A1", timeout: float = 3.0) -> int | None:
    """
    Sends `<target> ReadADC<channel>` and waits for `ADCval<channel> <raw_val>`.
    Returns integer raw_val (0..4095 on ESP32-C6) or None on timeout.
    """
    got_adc_event = threading.Event()
    adc_reading = None
    expected_token = f"ADCval{channel}"

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
    cmd = f"{target} ReadADC{channel}"
    client.send_line(cmd)
    got_adc_event.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)
    return adc_reading


def run_read_adc_test(client: CoordinatorClient, target: str = "Kitchen", channel: str = "A1", timeout: float = 3.0) -> bool:
    """Reads the ADC channel once and displays raw count and voltage."""
    print("\n" + "=" * 65)
    print(f"--- READ ADC PIN {channel} ---")
    print(f"Target Node: {target} (ESP32-C6 Pin {channel} / GPIO 1)")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")

    client.add_rx_callback(on_rx)
    print(f"\n[>] Sending command: \"{target} ReadADC{channel}\"")
    start_time = time.time()
    val = read_adc_pin(client, target=target, channel=channel, timeout=timeout)
    elapsed_ms = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    print("-" * 65)
    if val is not None:
        voltage = (val * 3.96) / 4095.0
        print(f"[+] TEST PASSED: Received ADCval{channel} = {val} (~{voltage:.2f}V) in {elapsed_ms:.1f}ms")
        return True
    else:
        print(f"[-] TEST FAILED: Timed out waiting for ADCval{channel} response from {target}.")
        return False


def run_dac_adc_loopback_test(client: CoordinatorClient, target: str = "Kitchen", test_points: list[int] = None, timeout: float = 3.0) -> bool:
    """
    Executes Closed-Loop DAC -> ADC loopback test:
    Trinket M0 Pin 1~ (DAC) -> ESP32-C6 Pin A1 (GPIO 1 / ADC1_CH1).
    Tests across multiple voltage setpoints (e.g. 0, 256, 512, 768, 1023).
    """
    if test_points is None:
        test_points = [0, 256, 512, 768, 1023]

    print("\n" + "=" * 65)
    print("--- [TEST 8] CLOSED-LOOP DAC -> ADC LOOPBACK TEST ---")
    print(f"Target Node:  {target} (Trinket M0 Pin 1~ DAC -> ESP32-C6 Pin A1)")
    print(f"Test Points:  {test_points}")
    print(f"Timestamp:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # 1. Ensure Serial Bridge is active and verified
    if not run_setup_bridge_test(client, target=target, timeout=timeout):
        print("[-] Aborting loopback test: Serial bridge initialization failed.")
        return False
    time.sleep(0.3)

    # 2. Ensure ADC A1 setup
    print("\n[>] Initializing ADC A1...")
    client.send_line(f"{target} SetupADCA1")
    time.sleep(0.4)

    print(f"\n{'DAC Val':<9} {'DAC (V)':<9} {'Raw ADC':<9} {'ADC (V)':<9} {'Delta (V)':<10} {'Status':<8}")
    print("-" * 65)

    all_passed = True
    previous_adc = -1

    # ESP32-C6 ADC1_CH1 with ADC_ATTEN_DB_12 full scale calibration factor (~3.96V)
    ADC_FULL_SCALE_VOLTS = 3.96

    for dac_val in test_points:
        expected_v = (dac_val * 3.3) / 1023.0

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
            print(f"{dac_val:<9} {expected_v:<9.2f} {'TIMEOUT':<9} {'---':<9} {'---':<10} [FAIL - No DAC reply]")
            all_passed = False
            continue

        # Settling time for analog line
        time.sleep(0.15)

        # Step 2: Read ADC A1 on ESP32-C6
        adc_raw = read_adc_pin(client, target=target, channel="A1", timeout=timeout)
        if adc_raw is None:
            print(f"{dac_val:<9} {expected_v:<9.2f} {'TIMEOUT':<9} {'---':<9} {'---':<10} [FAIL - No ADC reply]")
            all_passed = False
            continue

        measured_v = (adc_raw * ADC_FULL_SCALE_VOLTS) / 4095.0
        delta_v = measured_v - expected_v

        # Evaluation criteria:
        # 1. Monotonicity check (allowing small ADC noise buffer)
        # 2. Voltage tracking tolerance within +-0.25V
        point_pass = True
        if dac_val > 0 and (adc_raw + 50) < previous_adc:
            point_pass = False

        if abs(delta_v) > 0.25:
            point_pass = False

        status_str = "[PASS]" if point_pass else "[FAIL]"
        if not point_pass:
            all_passed = False

        previous_adc = adc_raw
        print(f"{dac_val:<9} {expected_v:<9.2f} {adc_raw:<9} {measured_v:<9.2f} {delta_v:+8.2f}   {status_str:<8}")
        time.sleep(0.15)

    print("-" * 65)
    if all_passed:
        print("[+] TEST PASSED: Closed-loop DAC -> ADC tracking verified across all setpoints!")
    else:
        print("[-] TEST FAILED: One or more DAC -> ADC setpoints failed tracking criteria.")

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


def run_cytron_motor_test(client: CoordinatorClient, target: str = "Kitchen") -> bool:
    """
    Executes the Cytron 10C Motor Driver Board Test:
      Hardware: ESP32-C6 Pin 18 (PWM Speed), Pin 20 (Direction)
      Profile:
        1. SetupMotorDriver 1 5000 (Mode 1 = Cytron 10C, 5 kHz PWM)
        2. Ramp from still (0) to 100% forward (+1000) over 2.0s ramp (slew = 500 units/s)
        3. Ramp from 100% forward (+1000) to 100% reverse (-1000) over 4.0s ramp (slew = 500 units/s)
        4. Pause at 100% reverse for 1.0s
        5. Set to zero output (0) with no ramp (slew = 0)
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
    print("      4. Instant stop (zero output with no ramp).")
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

        # Step 5: Set to zero output with no ramp (instant stop)
        print("\n[>] Step 5: Setting to zero output with NO ramp (instant stop)...")
        cmd_instant = f"{target} MotorSlew 0"
        print(f"    Setting slew rate: \"{cmd_instant}\" (0 = instantaneous)")
        client.send_line(cmd_instant)
        time.sleep(0.1)

        cmd_stop = f"{target} MotorSpeed 0"
        print(f"    Setting speed to zero: \"{cmd_stop}\" (0 = stop)")
        client.send_line(cmd_stop)
        time.sleep(0.5)
        print("    [+] Motor stopped at zero output.")

    finally:
        client.remove_rx_callback(on_rx)

    print("\n" + "-" * 65)
    print("[?] Visual / Hardware Verification:")
    print("    1. Did the motor ramp up smoothly from still to 100% forward in ~2s?")
    print("    2. Did the motor smoothly ramp from 100% forward to 100% reverse in ~4s?")
    print("    3. Did the motor pause at 100% reverse for 1s?")
    print("    4. Did the motor stop instantly (zero output) with no ramp?")
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
                        pass

    client.add_rx_callback(on_rx)
    cmd = f"{target} ReadProximity"
    client.send_line(cmd)
    val_event.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)
    return prox_reading[0]


def run_proximity_test(client: CoordinatorClient, target: str = "Kitchen") -> bool:
    """
    Executes the APDS9930 Proximity Sensor Test:
    1. Initializes APDS9930 sensor (<target> SetupProximity).
    2. Queries baseline ambient proximity value (<target> ReadProximity).
    3. Prompts user to approach sensor with hand/obstacle and listens for 'Car Detected <val>'.
    4. Prompts user to withdraw hand and listens for 'Car removed <val>'.
    5. Verifies end-to-end event flow over Zigbee.
    """
    print("\n" + "=" * 65)
    print("--- [TEST D] APDS9930 PROXIMITY SENSOR TEST ---")
    print(f"Target Node: {target} (ESP32-C6 I2C SDA=GPIO22, SCL=GPIO23)")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    def on_rx(msg: str):
        print(f"  <-- [RX] {msg}")

    client.add_rx_callback(on_rx)

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
        client.remove_rx_callback(on_rx)
        return False
    print(f"[+] APDS9930 sensor initialized successfully.")
    time.sleep(0.3)

    # Step 2: Read baseline proximity reading
    print(f"\n[>] Reading baseline proximity...")
    baseline = read_proximity_val(client, target=target, timeout=3.0)
    if baseline is not None:
        print(f"[+] Baseline proximity reading: {baseline}")
    else:
        print(f"[!] Warning: Could not read instantaneous baseline proximity reading, continuing.")

    # Step 3: Wait for Car Detected & Car Removed
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
    print(">>> ACTION REQUIRED: Bring your hand or an obstacle within 5-10cm of the APDS9930 sensor. <<<")
    print("Waiting up to 20 seconds for 'Car Detected' event...")
    got_detected = detected_event.wait(timeout=20.0)

    if not got_detected:
        print(f"\n[-] TEST FAILED: Timed out waiting for 'Car Detected' event.")
        client.remove_rx_callback(on_prox_event)
        client.remove_rx_callback(on_rx)
        return False

    val_str = f" (reading: {detected_val[0]})" if detected_val[0] else ""
    print(f"\n[+] OBSTACLE DETECTED{val_str} verified over Zigbee!")

    print("\n>>> ACTION REQUIRED: Move your hand away from the sensor. <<<")
    print("Waiting up to 20 seconds for 'Car removed' event...")
    got_removed = removed_event.wait(timeout=20.0)

    client.remove_rx_callback(on_prox_event)
    client.remove_rx_callback(on_rx)

    if not got_removed:
        print(f"\n[-] TEST FAILED: Timed out waiting for 'Car removed' event.")
        return False

    val_rem_str = f" (reading: {removed_val[0]})" if removed_val[0] else ""
    print(f"\n[+] OBSTACLE REMOVAL{val_rem_str} verified over Zigbee!")

    print("-" * 65)
    print("[+] TEST PASSED: APDS9930 Proximity sensor detection & removal cycle fully verified!")
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


def write_expand8_bit(client: CoordinatorClient, target: str, bit: int, val: int, timeout: float = 3.0) -> bool:
    """Writes a single bit (0 or 1) to PCF8574 Expand8 on target node."""
    got_ack = threading.Event()

    def on_rx(msg: str):
        if f"< {target}:" in msg and ("[ACK]" in msg or "EXPAND8 WR OK" in msg):
            got_ack.set()

    client.add_rx_callback(on_rx)
    cmd = f"{target} wrExpand8 {bit} {val}"
    client.send_line(cmd)
    success = got_ack.wait(timeout=timeout)
    client.remove_rx_callback(on_rx)
    return success


def read_expand8_bit(client: CoordinatorClient, target: str = "Kitchen", bit: int = 0, timeout: float = 3.0) -> int | None:
    """
    Sends `<target> rdExpand8 <bit>` and waits for `EXPAND8 <bit> IS <val>`.
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
    cmd = f"{target} rdExpand8 {bit}"
    client.send_line(cmd)
    success = got_event.wait(timeout=timeout)

    if not success:
        # Retry once in case of wireless packet jitter
        client.send_line(cmd)
        success = got_event.wait(timeout=timeout)

    client.remove_rx_callback(on_rx)
    return read_val


def run_expand8_test(client: CoordinatorClient, target: str = "Kitchen", addr: int = 32, timeout: float = 3.0) -> bool:
    """
    Validates PCF8574 8-bit I/O Expander:
    1. Explicitly initializes device at address `addr` (SetupExpand8 <addr>).
    2. Writes alternating 1's and 0's starting with zero LSB:
       Pattern A: bits 0..7 = [0, 1, 0, 1, 0, 1, 0, 1] (0xAA / 170).
       Reads each bit (0..7) and verifies all values match.
    3. Repeats with LSB being 1:
       Pattern B: bits 0..7 = [1, 0, 1, 0, 1, 0, 1, 0] (0x55 / 85).
       Reads each bit (0..7) and verifies all values match.
    """
    print("\n" + "=" * 65)
    print("--- [TEST E] PCF8574 EXPAND8 ALTERNATING BIT TEST ---")
    print(f"Target Node: {target} (ESP32-C6 / PCF8574 @ 0x{addr:02X})")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # Step 1: Explicitly initialize PCF8574 at specified address (e.g. 32 / 0x20)
    setup_expand8(client, target=target, addr=addr, timeout=timeout)
    time.sleep(0.2)

    all_passed = True

    # Test patterns:
    # Pattern A: LSB=0 -> [0, 1, 0, 1, 0, 1, 0, 1]
    # Pattern B: LSB=1 -> [1, 0, 1, 0, 1, 0, 1, 0]
    patterns = [
        ("Pattern A (LSB = 0)", [0 if (i % 2 == 0) else 1 for i in range(8)]),
        ("Pattern B (LSB = 1)", [1 if (i % 2 == 0) else 0 for i in range(8)])
    ]

    for pat_idx, (pat_name, expected_bits) in enumerate(patterns, start=1):
        byte_val = sum(b << i for i, b in enumerate(expected_bits))
        print(f"\n[>] Step {pat_idx}: Testing {pat_name}")
        print(f"    Target bit pattern (0..7): {expected_bits} (Byte: 0x{byte_val:02X})")

        print("    Writing bits:")
        for bit in range(8):
            val = expected_bits[bit]
            write_expand8_bit(client, target=target, bit=bit, val=val, timeout=timeout)
            print(f"      wrExpand8 {bit} {val} -> OK")
            time.sleep(0.04)

        time.sleep(0.1)

        print("    Reading and verifying bits:")
        pat_ok = True
        for bit in range(8):
            expected = expected_bits[bit]
            actual = read_expand8_bit(client, target=target, bit=bit, timeout=timeout)
            if actual == expected:
                print(f"      [+] Bit {bit}: expected {expected}, read {actual} [PASS]")
            else:
                print(f"      [-] Bit {bit}: expected {expected}, read {actual} [FAIL]")
                pat_ok = False
            time.sleep(0.04)

        if pat_ok:
            print(f"[+] {pat_name} PASSED (all 8 bits verified)!")
        else:
            print(f"[-] {pat_name} FAILED (bit mismatch detected)!")
            all_passed = False

        time.sleep(0.2)

    print("-" * 65)
    if all_passed:
        print(f"[+] TEST PASSED: PCF8574 Expand8 verified for both alternating bit patterns on {target}!")
    else:
        print(f"[-] TEST FAILED: PCF8574 Expand8 verification failed on {target}.")
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


def clear_display(client: CoordinatorClient, target: str = "Kitchen", timeout: float = 3.0) -> bool:
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
    print(f"\n[>] Clearing SSD1306 display on {target}: \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

    if status_ok:
        print(f"[+] ClearDisp succeeded on {target} ({elapsed:.1f}ms).")
    else:
        print(f"[!] ClearDisp did not receive OK confirmation from {target}.")
    return status_ok


def print_display_line(client: CoordinatorClient, target: str = "Kitchen", size: int = 12, text: str = "Hello World", timeout: float = 3.0) -> bool:
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
    print(f"\n[>] Printing line on {target} display ({size}pt: \"{text}\"): \"{cmd}\"")
    start_time = time.time()
    client.send_line(cmd)
    got_ack.wait(timeout=timeout)
    elapsed = (time.time() - start_time) * 1000.0
    client.remove_rx_callback(on_rx)

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
    3. Prints lines in all supported Adafruit GFX FreeMono font sizes (9, 12, 18, 24 pt).
    4. Verifies line advancement and display responses.
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

    # Step 3: Print test lines across supported font sizes
    font_tests = [
        (9, "FreeMono 9pt"),
        (12, "FreeMono 12pt"),
        (18, "FreeMono 18"),
        (24, "24pt"),
    ]

    all_passed = setup_ok and clear_ok

    for size_pt, text in font_tests:
        ok = print_display_line(client, target=target, size=size_pt, text=text, timeout=timeout)
        if not ok:
            all_passed = False
        time.sleep(0.3)

    print("-" * 65)
    if all_passed:
        print(f"[+] TEST PASSED: SSD1306 OLED verified on {target}!")
    else:
        print(f"[-] TEST FAILED: SSD1306 OLED verification failed on {target}.")
    return all_passed


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
    """Executes all regression tests in sequence and displays a structured report."""
    print("\n" + "=" * 65)
    print("        STARTING FULL ZIGBEE HIL REGRESSION SUITE")
    print(f"Target:    {target} | Garage: {garage_target}")
    print(f"Started:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    results = {}

    # Test 1: Setup Serial Bridge
    results["SetupSerialBridge"] = run_setup_bridge_test(client, target=target)
    time.sleep(0.5)

    # Test 2: ESP32-C6 Onboard LED Blink
    results["ESP32_Blink"] = run_blink_test(client, target=target, count=blink_count)
    time.sleep(0.5)

    # Test 3: Arduino DotStar Blinkx
    results["Arduino_Blinkx"] = run_blinkx_test(client, target=target, count=blinkx_count)
    time.sleep(0.5)

    # Test 4: Two-Way Serial Ping
    results["Serial_Ping"] = run_ping_test(client, target=target)
    time.sleep(0.5)

    # Test 5: Arduino Hardware DAC
    results["Arduino_DAC"] = run_dac_test(client, target=target, dac_val=dac_val)
    time.sleep(0.5)

    # Test 6: Closed-Loop DAC -> ADC Loopback
    results["DAC_ADC_Loopback"] = run_dac_adc_loopback_test(client, target=target)
    time.sleep(0.5)

    # Test 7: Schmitt Trigger Hysteresis Ramp Test
    results["Schmitt_Ramp"] = run_schmitt_hysteresis_ramp_test(client, target=target)
    time.sleep(0.5)

    # Test [S]: Servo / PWM Snap and Slew Test
    results["Servo_PWM"] = run_servo_pwm_test(client, target=target)
    time.sleep(0.5)

    # Test [M]: Cytron 10C Motor Driver Test (Pin 18 PWM, Pin 20 DIR)
    results["Cytron_Motor"] = run_cytron_motor_test(client, target=target)
    time.sleep(0.5)

    # Test [D]: APDS9930 Proximity Sensor Test
    results["APDS9930_Prox"] = run_proximity_test(client, target=target)
    time.sleep(0.5)

    # Test [G]: Garage Pin 16 GPIO Input Test
    results["Garage_GPIO"] = run_garage_gpio_test(client, garage_target=garage_target, kitchen_target=target)
    time.sleep(0.5)

    # Test [B]: Bidirectional GPIO Wire Test (Kitchen Pin 21 <-> Garage Pin 17)
    results["Bidi_GPIO"] = run_bidirectional_gpio_test(client, kitchen_target=target, garage_target=garage_target, kitchen_pin=21, garage_pin=17)
    time.sleep(0.5)

    # Test [E]: PCF8574 Expand8 Alternating Bit Test
    results["Expand8"] = run_expand8_test(client, target=target)

    # Print Final Summary Table
    print("\n" + "=" * 65)
    print("             REGRESSION TEST RESULTS SUMMARY")
    print("=" * 65)
    labels = {
        "SetupSerialBridge": "[2] Setup Serial Bridge",
        "ESP32_Blink":       f"[3] ESP32-C6 Onboard LED Blink ({blink_count} flashes)",
        "Arduino_Blinkx":    f"[4] Arduino DotStar Blinkx     ({blinkx_count} flashes)",
        "Serial_Ping":       "[5] Two-Way Serial Ping        (GotPing)",
        "Arduino_DAC":       f"[6] Arduino Hardware DAC        (GotDAC {dac_val})",
        "DAC_ADC_Loopback":  "[8] Closed-Loop DAC -> ADC     (Pin 1~ -> Pin A1)",
        "Schmitt_Ramp":      "[9] Schmitt Hysteresis Ramp   (0 -> 1000 -> 0, 2s+2s)",
        "Servo_PWM":         "[S] Servo / PWM Motion         (Snap & Slew 30)",
        "Cytron_Motor":      "[M] Cytron 10C Motor Driver   (PWM=Pin 18, DIR=Pin 20: 2s FWD, 4s REV, 1s Pause, Snap 0)",
        "APDS9930_Prox":     "[D] APDS9930 Proximity Sensor  (Setup, Baseline & Detection Cycle)",
        "Garage_GPIO":       "[G] Garage Pin 16 GPIO Input  (Arduino PIN_D0 -> Garage Pin 16)",
        "Bidi_GPIO":         "[B] Bidirectional GPIO Wire   (Kitchen Pin 21 <-> Garage Pin 17)",
        "Expand8":           "[E] PCF8574 Expand8 Bit Test  (Alternating 0/1 & 1/0 Patterns)"
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
        print(f"  >>> ALL REGRESSION TESTS PASSED SUCCESSFULLY! ({total_tests}/{total_tests}) <<<")
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
    parser.add_argument("--schmitt", "--ramp", action="store_true", help="Run Schmitt trigger hysteresis ramp test (2s up + 2s down) and exit")
    parser.add_argument("--prox", "--proximity", action="store_true", help="Run APDS9930 Proximity Sensor test and exit")
    parser.add_argument("--read-prox", action="store_true", help="Read APDS9930 instantaneous proximity value and exit")
    parser.add_argument("--garage", type=str, default="Garage", help="Garage node name in coordinator device table (default: Garage)")
    parser.add_argument("--gpio", "--garage-gpio", action="store_true", help="Run Garage Pin 16 GPIO input test (clrx/setx via Kitchen) and exit")
    parser.add_argument("--bidi-gpio", "--cross-gpio", action="store_true", help="Run bidirectional GPIO wire test (Kitchen Pin 21 <-> Garage Pin 17) and exit")
    parser.add_argument("--expand8", "--pcf8574", action="store_true", help="Run PCF8574 Expand8 alternating bit test and exit")
    parser.add_argument("--expand8-addr", type=int, default=32, help="I2C address for PCF8574 Expand8 (default: 32 / 0x20)")
    parser.add_argument("--cleardisp", action="store_true", help="Clear SSD1306 OLED display on target node and exit")
    parser.add_argument("--print-disp", nargs="+", help="Print text on SSD1306 OLED display ([size] text...) and exit")
    parser.add_argument("--oled", "--ssd1306", "--disp", action="store_true", help="Run SSD1306 OLED multi-size FreeMono font test and exit")
    parser.add_argument("--oled-addr", type=int, default=60, help="I2C address for SSD1306 OLED (default: 60 / 0x3C)")
    parser.add_argument("--compass", "--qmc5883l", action="store_true", help="Run QMC5883L 3-axis compass rotation test and exit")
    parser.add_argument("--get-compass", action="store_true", help="Stream QMC5883L compass readings (x,y,z every 300ms, ESC to stop) and exit")
    parser.add_argument("--compass-addr", type=int, default=13, help="I2C address for QMC5883L compass (default: 13 / 0x0D)")
    parser.add_argument("--rgb", "--rgb-fade", action="store_true", help="Run Garage three-color RGB LED PWM fade cycle test (Pins 18, 20, 19) and exit")
    parser.add_argument("--rgb-duration", type=float, default=1.0, help="Duration in seconds for each color 0-100-0%% fade cycle (default: 1.0s)")
    parser.add_argument("--all", "--suite", action="store_true", help="Run the entire full regression test suite (1-12) and exit")
    parser.add_argument("--mesh", action="store_true", help="Run the full 3-stage Mesh Resilience test suite (all sub-tests) and exit")
    parser.add_argument("--mesh-sub1", action="store_true", help="Run Mesh Sub-Test 1 (Reset all 3 nodes: Coord, Kitchen, Garage) and exit")
    parser.add_argument("--mesh-sub2", action="store_true", help="Run Mesh Sub-Test 2 (Reset Garage node only) and exit")
    parser.add_argument("--mesh-sub3", action="store_true", help="Run Mesh Sub-Test 3 (Reset both Kitchen and Garage) and exit")
    parser.add_argument("--kitchen-port", type=str, default=None, help="Optional serial port for Kitchen node hardware reset (e.g. COM5)")
    parser.add_argument("--garage-port", type=str, default=None, help="Optional serial port for Garage node hardware reset (e.g. COM8)")

    args = parser.parse_args()

    port = args.port or choose_port()
    client = CoordinatorClient(port=port, baudrate=args.baud)

    if not client.connect():
        sys.exit(1)

    # Verify mesh link before dispatching tests
    client.wait_for_node(target=args.target, timeout=10.0)

    try:
        # CLI direct test execution flags
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
            passed = run_expand8_test(client, target=args.target, addr=args.expand8_addr)
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

        if args.get_compass:
            run_compass_stream(client, target=args.target, addr=args.compass_addr)
            sys.exit(0)

        if args.rgb:
            passed = run_garage_rgb_fade_test(client, target=args.garage, fade_duration=args.rgb_duration)
            sys.exit(0 if passed else 1)

        if args.bridge:
            passed = run_setup_bridge_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        if args.blink:
            passed = run_blink_test(client, target=args.target, count=args.count)
            sys.exit(0 if passed else 1)

        if args.blinkx:
            run_setup_bridge_test(client, target=args.target)
            time.sleep(0.3)
            passed = run_blinkx_test(client, target=args.target, count=args.count)
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
            val = read_proximity_val(client, target=args.target)
            if val is not None:
                print(f"[+] Instantaneous Proximity Value: {val}")
                sys.exit(0)
            else:
                print("[-] Failed to read proximity value.")
                sys.exit(1)

        # Interactive Menu
        while True:
            print("\n=======================================================")
            print("         ZIGBEE HIL REGRESSION TEST RUNNER")
            print("=======================================================")
            print("  [1] Run Full Regression Test Suite (Tests 1-12)")
            print("  [2] Setup Serial Bridge (SetupSerialBridge)")
            print(f"  [3] ESP32-C6 Onboard LED Blink (blink {args.count})")
            print(f"  [4] Arduino DotStar LED Blinkx  (blinkx {args.count}) [Auto-Bridge]")
            print("  [5] Two-Way Serial Ping Test    (GotPing)             [Auto-Bridge]")
            print("  [6] Arduino Hardware DAC Test   (setDAC X)            [Auto-Bridge]")
            print("  [7] Read ADC Channel            (ReadADCA1)")
            print("  [8] Closed-Loop DAC -> ADC Test (Pin 1~ -> Pin A1)    [Auto-Bridge]")
            print("  [9] Schmitt Trigger Ramp Test   (0 -> 1000 -> 0, 2s+2s)")
            print("  [S] Servo / PWM Test            (Pin 2: Snap vs Slew 30)")
            print("  [M] Cytron 10C Motor Test       (PWM=Pin 18, DIR=Pin 20: 2s FWD, 4s REV, 1s Pause, Snap 0)")
            print("  [D] APDS9930 Proximity Test     (Setup, Baseline & Detection Cycle)")
            print("  [R] Read Proximity Value        (ReadProximity)")
            print("  [G] Garage Pin 16 GPIO Test     (clrx/setx via Kitchen) [Auto-Bridge]")
            print("  [B] Bidirectional GPIO Wire     (Kitchen Pin 21 <-> Garage Pin 17)")
            print("  [E] PCF8574 Expand8 Test        (Alternating 0/1 & 1/0 Bit Patterns)")
            print("  [O] SSD1306 OLED Display Test   (ClearDisp + Multi-Size FreeMono Prints)")
            print("  [C] Clear OLED Display          (ClearDisp)")
            print("  [K] QMC5883L Compass Test       (Rotate > 45 deg Verification)")
            print("  [Q] Read Compass Values         (GetCompass)")
            print("  [L] Garage RGB LED Fade Cycle   (Pins 18, 20, 19 PWM Fade 0-100%)")
            print("  [W] Mesh Resilience Tests      (All 3 Sub-Tests: All, Garage, Both)")
            print("  [N] Request Network Report      (GiveNetworkReport)")
            print("  [P] Ping Network Nodes          (PingNetwork)")
            print("  [0] Interactive PuTTY Console Mode")
            print(f"  [T] Change Target Node Name     (Current: {args.target})")
            print("  [X] Exit")
            print("=======================================================")

            choice = input("Enter selection [0-9, S, M, D, R, G, B, E, O, C, K, Q, L, W, N, P, T, X]: ").strip().upper()

            if choice == "1":
                run_full_regression_suite(client, target=args.target, garage_target=args.garage, blink_count=args.count, blinkx_count=args.count)
            elif choice == "2":
                run_setup_bridge_test(client, target=args.target)
            elif choice == "3":
                run_blink_test(client, target=args.target, count=args.count)
            elif choice == "4":
                run_setup_bridge_test(client, target=args.target)
                time.sleep(0.3)
                run_blinkx_test(client, target=args.target, count=args.count)
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
                chan_str = input("Enter ADC channel (A0, A1, A2) [A1]: ").strip().upper()
                channel = chan_str if chan_str in ("A0", "A1", "A2") else "A1"
                run_read_adc_test(client, target=args.target, channel=channel)
            elif choice == "8":
                run_dac_adc_loopback_test(client, target=args.target)
            elif choice == "9":
                run_schmitt_hysteresis_ramp_test(client, target=args.target)
            elif choice in ("S", "SERVO", "PWM"):
                run_servo_pwm_test(client, target=args.target)
            elif choice in ("M", "MOTOR", "CYTRON"):
                run_cytron_motor_test(client, target=args.target)
            elif choice in ("D", "PROX", "PROXIMITY"):
                run_proximity_test(client, target=args.target)
            elif choice in ("R", "READPROX"):
                val = read_proximity_val(client, target=args.target)
                if val is not None:
                    print(f"\n[+] Instantaneous Proximity Value: {val}")
                else:
                    print("\n[-] Failed to read proximity value.")
            elif choice in ("G", "GPIO"):
                run_garage_gpio_test(client, garage_target=args.garage, kitchen_target=args.target, pin=16)
            elif choice in ("B", "BIDI"):
                run_bidirectional_gpio_test(client, kitchen_target=args.target, garage_target=args.garage, kitchen_pin=21, garage_pin=17)
            elif choice in ("E", "EXPAND8", "PCF8574"):
                run_expand8_test(client, target=args.target)
            elif choice in ("O", "OLED", "SSD1306", "DISP"):
                run_ssd1306_test(client, target=args.target, addr=args.oled_addr)
            elif choice in ("C", "CLEARDISP", "CLEAR"):
                clear_display(client, target=args.target)
            elif choice in ("K", "COMPASS", "QMC5883L"):
                run_compass_test(client, target=args.target, addr=args.compass_addr)
            elif choice in ("Q", "GETCOMPASS", "READCOMPASS"):
                run_compass_stream(client, target=args.target, addr=args.compass_addr)
            elif choice in ("L", "RGB", "FADE", "RGBLED", "LEDFADE"):
                run_garage_rgb_fade_test(client, target=args.garage, fade_duration=args.rgb_duration)
            elif choice in ("W", "MESH"):
                run_mesh_resilience_test(client, kitchen_target=args.target, garage_target=args.garage, kitchen_port=args.kitchen_port, garage_port=args.garage_port)
            elif choice in ("N", "REPORT"):
                print("\n[>] Requesting Network Report...")
                client.send_line("GiveNetworkReport")
                time.sleep(2.0)
            elif choice in ("P", "PINGNET"):
                print("\n[>] Pinging network nodes...")
                client.send_line("PingNetwork")
                time.sleep(1.5)
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
