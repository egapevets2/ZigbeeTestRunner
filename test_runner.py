#!/usr/bin/env python3
"""
Zigbee HIL (Hardware-in-the-Loop) Regression Test Runner
Communicates with the Zigbee Coordinator and validates end-to-end mesh
operations on the ESP32-C6 node (PNPzigbee) and test fixture (TXRXproto).
"""

import sys
import time
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

    def connect(self) -> bool:
        try:
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
            print(f"[+] Connected to Coordinator on {self.port} @ {self.baudrate} baud.")
            return True
        except serial.SerialException as ex:
            print(f"[-] Failed to open serial port {self.port}: {ex}")
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
            self.ser.close()
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


def run_setup_bridge_test(client: CoordinatorClient, target: str = "Kitchen", timeout: float = 3.0) -> bool:
    """Sends `<target> SetupSerialBridge` and verifies response."""
    print("\n" + "=" * 65)
    print("--- [TEST 1/5] SETUP SERIAL BRIDGE ---")
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
    print("--- [TEST 2/5] ESP32-C6 ONBOARD LED BLINK TEST ---")
    print(f"Target Node:    {target} (ESP32-C6 Onboard LED)")
    print(f"Requested Blinks: {count}")
    print(f"Timestamp:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

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
    print("--- [TEST 3/5] ARDUINO DOTSTAR LED BLINKX TEST ---")
    print(f"Target Node:    {target} -> Arduino Test Fixture (Blue DotStar LED)")
    print(f"Requested Blinks: {count}")
    print(f"Timestamp:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

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
    print("--- [TEST 4/5] TWO-WAY SERIAL PING TEST ---")
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
    print("--- [TEST 5/5] ARDUINO HARDWARE DAC OUTPUT TEST ---")
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
    print("--- [TEST 6/7] CLOSED-LOOP DAC -> ADC LOOPBACK TEST ---")
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
    print("--- SCHMITT TRIGGER THRESHOLD CONTROLLABILITY RAMP TEST ---")
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
    print("--- SERVO / PWM HARDWARE TEST (PIN 2) ---")
    print(f"Target Node: {target} (ESP32-C6 Pin 2 -> 74HCT125 -> Servo)")
    print(f"Timestamp:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

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


def run_full_regression_suite(client: CoordinatorClient, target: str = "Kitchen", blink_count: int = 3, blinkx_count: int = 4, dac_val: int = 512) -> bool:
    """Executes all regression tests in sequence and displays a structured report."""
    print("\n" + "=" * 65)
    print("        STARTING FULL ZIGBEE HIL REGRESSION SUITE")
    print(f"Target:    {target}")
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

    # Test 8: Servo / PWM Snap and Slew Test
    results["Servo_PWM"] = run_servo_pwm_test(client, target=target)

    # Print Final Summary Table
    print("\n" + "=" * 65)
    print("             REGRESSION TEST RESULTS SUMMARY")
    print("=" * 65)
    labels = {
        "SetupSerialBridge": "1. Setup Serial Bridge",
        "ESP32_Blink":       f"2. ESP32-C6 Onboard LED Blink ({blink_count} flashes)",
        "Arduino_Blinkx":    f"3. Arduino DotStar Blinkx     ({blinkx_count} flashes)",
        "Serial_Ping":       "4. Two-Way Serial Ping        (GotPing)",
        "Arduino_DAC":       f"5. Arduino Hardware DAC        (GotDAC {dac_val})",
        "DAC_ADC_Loopback":  "6. DAC -> ADC Loopback        (Pin 1~ -> Pin A1)",
        "Schmitt_Ramp":      "7. Schmitt Hysteresis Ramp   (0 -> 1000 -> 0, 2s+2s)",
        "Servo_PWM":         "8. Servo / PWM Motion         (Snap & Slew 30)"
    }

    all_passed = True
    for key, label in labels.items():
        passed = results.get(key, False)
        status = "[PASS]" if passed else "[FAIL]"
        if not passed:
            all_passed = False
        print(f"  {status:<10} {label}")

    print("=" * 65)
    if all_passed:
        print("  >>> ALL REGRESSION TESTS PASSED SUCCESSFULLY! (8/8) <<<")
    else:
        print("  >>> ONE OR MORE TESTS FAILED - REVIEW LOGS ABOVE <<<")
    print("=" * 65 + "\n")

    return all_passed


def interactive_terminal(client: CoordinatorClient):
    """Provides a raw PuTTY-like interactive console for manual commands."""
    print("\n[+] Entering Interactive PuTTY-Bridge Console mode.")
    print("    Type coordinator commands like:")
    print("      Kitchen SetupSerialBridge")
    print("      Kitchen blink 3")
    print("      Kitchen blinkx 5")
    print("      Kitchen ping")
    print("      Kitchen setDAC 512")
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
    parser.add_argument("--schmitt", "--ramp", action="store_true", help="Run Schmitt trigger hysteresis ramp test (2s up + 2s down) and exit")
    parser.add_argument("--all", "--suite", action="store_true", help="Run the entire full regression test suite (1-8) and exit")

    args = parser.parse_args()

    port = args.port or choose_port()
    client = CoordinatorClient(port=port, baudrate=args.baud)

    if not client.connect():
        sys.exit(1)

    # Verify mesh link before dispatching tests
    client.wait_for_node(target=args.target, timeout=10.0)

    try:
        # CLI direct test execution flags
        if args.all:
            passed = run_full_regression_suite(client, target=args.target, blink_count=args.count, blinkx_count=args.count)
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

        # Interactive Menu
        while True:
            print("\n=======================================================")
            print("         ZIGBEE HIL REGRESSION TEST RUNNER")
            print("=======================================================")
            print("  [1] Run Full Regression Test Suite (Tests 1-8)")
            print("  [2] Setup Serial Bridge (SetupSerialBridge)")
            print(f"  [3] ESP32-C6 Onboard LED Blink (blink {args.count})")
            print(f"  [4] Arduino DotStar LED Blinkx  (blinkx {args.count}) [Auto-Bridge]")
            print("  [5] Two-Way Serial Ping Test    (GotPing)             [Auto-Bridge]")
            print("  [6] Arduino Hardware DAC Test   (setDAC X)            [Auto-Bridge]")
            print("  [7] Read ADC Channel            (ReadADCA1)")
            print("  [8] Closed-Loop DAC -> ADC Test (Pin 1~ -> Pin A1)    [Auto-Bridge]")
            print("  [9] Schmitt Trigger Ramp Test   (0 -> 1000 -> 0, 2s+2s)")
            print("  [S] Servo / PWM Test            (Pin 2: Snap vs Slew 30)")
            print("  [N] Request Network Report      (GiveNetworkReport)")
            print("  [P] Ping Network Nodes          (PingNetwork)")
            print("  [0] Interactive PuTTY Console Mode")
            print(f"  [T] Change Target Node Name     (Current: {args.target})")
            print("  [X] Exit")
            print("=======================================================")

            choice = input("Enter selection [0-9, S, N, P, T, X]: ").strip().upper()

            if choice == "1":
                run_full_regression_suite(client, target=args.target, blink_count=args.count, blinkx_count=args.count)
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
            elif choice in ("X", "EXIT", "QUIT", "Q"):
                break
            else:
                print("[!] Invalid option, please retry.")

    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
