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
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
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
            except (serial.SerialException, TypeError):
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
        print(f"[✓] TEST PASSED: Serial bridge initialized successfully ({elapsed:.1f}ms)!")
    else:
        print(f"[✗] TEST FAILED: Timed out waiting for 'SETUP SERIAL OK' from {target}.")
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
        print(f"[✓] TEST PASSED: Confirmed {count} flashes on ESP32-C6 onboard LED!")
        return True
    else:
        print(f"[✗] TEST FAILED: Expected {count} flashes, but {observed} flashes were reported.")
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
        print(f"[✓] TEST PASSED: Confirmed {count} blue flashes on Arduino DotStar LED!")
        return True
    else:
        print(f"[✗] TEST FAILED: Expected {count} flashes, but {observed} flashes were reported.")
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
        print(f"[✓] TEST PASSED: Received 'GotPing' reply in {elapsed_ms:.1f}ms!")
        print("    End-to-End Link Verified:")
        print("    Host -> Coordinator -> Zigbee -> ESP32-C6 -> Arduino -> ESP32-C6 -> Zigbee -> Coordinator -> Host")
    else:
        print(f"[✗] TEST FAILED: Timed out after {timeout:.1f}s waiting for 'GotPing' from {target}.")
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
        print(f"[✓] TEST PASSED: Received '{expected_token}' reply in {elapsed_ms:.1f}ms!")
        print(f"    Trinket M0 Pin A0 is now driven at ~{voltage:.2f}V.")
    else:
        print(f"[✗] TEST FAILED: Did not receive '{expected_token}' from {target} within {timeout:.1f}s.")
    return success


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
        print("[✓] TEST PASSED: Servo PWM snap and slew motion verified successfully!")
    else:
        print("[✗] TEST FAILED: Servo motion was not confirmed by operator.")
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

    # Test 6: Servo / PWM Snap and Slew Test
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
        "Servo_PWM":         "6. Servo / PWM Motion         (Snap & Slew 30)"
    }

    all_passed = True
    for key, label in labels.items():
        passed = results.get(key, False)
        status = "[PASS] ✓" if passed else "[FAIL] ✗"
        if not passed:
            all_passed = False
        print(f"  {status:<10} {label}")

    print("=" * 65)
    if all_passed:
        print("  >>> ALL REGRESSION TESTS PASSED SUCCESSFULLY! (6/6) <<<")
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
    parser.add_argument("--blink", action="store_true", help="Run ESP32-C6 onboard LED blink test and exit")
    parser.add_argument("--blinkx", action="store_true", help="Run Arduino DotStar blinkx test and exit")
    parser.add_argument("--ping", action="store_true", help="Run the two-way GotPing round-trip test and exit")
    parser.add_argument("--bridge", action="store_true", help="Run SetupSerialBridge test and exit")
    parser.add_argument("--servo", "--pwm", action="store_true", help="Run Servo / PWM test on Pin 2 (snap vs slew 30) and exit")
    parser.add_argument("--all", "--suite", action="store_true", help="Run the entire full regression test suite (1-6) and exit")

    args = parser.parse_args()

    port = args.port or choose_port()
    client = CoordinatorClient(port=port, baudrate=args.baud)

    if not client.connect():
        sys.exit(1)

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
            passed = run_blinkx_test(client, target=args.target, count=args.count)
            sys.exit(0 if passed else 1)

        if args.ping:
            passed = run_ping_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        if args.dac is not None:
            passed = run_dac_test(client, target=args.target, dac_val=args.dac)
            sys.exit(0 if passed else 1)

        if args.servo or args.pwm:
            passed = run_servo_pwm_test(client, target=args.target)
            sys.exit(0 if passed else 1)

        # Interactive Menu
        while True:
            print("\n=======================================================")
            print("         ZIGBEE HIL REGRESSION TEST RUNNER")
            print("=======================================================")
            print("  [1] Run Full Regression Test Suite (Tests 1-6)")
            print("  [2] Setup Serial Bridge (SetupSerialBridge)")
            print(f"  [3] ESP32-C6 Onboard LED Blink (blink {args.count})")
            print(f"  [4] Arduino DotStar LED Blinkx  (blinkx {args.count})")
            print("  [5] Two-Way Serial Ping Test    (GotPing)")
            print("  [6] Arduino Hardware DAC Test   (setDAC X)")
            print("  [7] Servo / PWM Test (Pin 2: Snap vs Slew 30)")
            print("  [8] Request Network Report      (GiveNetworkReport)")
            print("  [9] Ping Network Nodes          (PingNetwork)")
            print("  [0] Interactive PuTTY Console Mode")
            print(f"  [T] Change Target Node Name     (Current: {args.target})")
            print("  [X] Exit")
            print("=======================================================")

            choice = input("Enter selection [0-9, T, X]: ").strip().upper()

            if choice == "1":
                run_full_regression_suite(client, target=args.target, blink_count=args.count, blinkx_count=args.count)
            elif choice == "2":
                run_setup_bridge_test(client, target=args.target)
            elif choice == "3":
                run_blink_test(client, target=args.target, count=args.count)
            elif choice == "4":
                run_blinkx_test(client, target=args.target, count=args.count)
            elif choice == "5":
                run_ping_test(client, target=args.target)
            elif choice == "6":
                val_str = input("Enter DAC value (0-1023, where 512 ~ 1.65V, 1023 ~ 3.3V) [512]: ").strip()
                dac_val = int(val_str) if val_str.isdigit() else 512
                run_dac_test(client, target=args.target, dac_val=dac_val)
            elif choice == "7":
                run_servo_pwm_test(client, target=args.target)
            elif choice == "8":
                print("\n[>] Requesting Network Report...")
                client.send_line("GiveNetworkReport")
                time.sleep(2.0)
            elif choice == "9":
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
