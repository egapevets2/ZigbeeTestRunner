#!/usr/bin/env python3
"""
Workbench Provisioning Script (provision.py)
Automates role assignment and persistent configuration via the ESP32-C6 USB-Serial CLI.

Usage:
  python provision.py -p COM11 --show
  python provision.py -p COM11 --role Coordinator
  python provision.py -p COM5  --role EndDevice --name Kitchen
  python provision.py -p COM8  --role EndDevice --name Garage
  python provision.py -p COM5  --reset
  python provision.py -p COM5  --channel 6
"""

import argparse
import sys
import time
import serial
from serial.tools import list_ports


def choose_port() -> str:
    """Lists available serial ports and prompts user to pick one if not specified."""
    ports = list(list_ports.comports())
    if not ports:
        print("[!] No serial ports discovered.")
        sys.exit(1)

    print("\n--- Discovered Serial Ports ---")
    for idx, p in enumerate(ports):
        print(f"  [{idx + 1}] {p.device} - {p.description}")

    choice = input("\nSelect port number: ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(ports):
            return ports[idx].device
    except ValueError:
        pass

    print("[!] Invalid selection.")
    sys.exit(1)


class Provisioner:
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None

    def connect(self):
        print(f"[*] Opening {self.port} @ {self.baudrate} baud...")
        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        time.sleep(0.3)
        self.ser.reset_input_buffer()

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            print(f"[*] Closed {self.port}.")

    def send_command(self, cmd: str, wait_sec: float = 1.0) -> list[str]:
        """Sends a CLI command string and captures the returned response lines."""
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Serial port not open.")

        self.ser.reset_input_buffer()
        payload = (cmd.strip() + "\r\n").encode("utf-8")
        self.ser.write(payload)
        self.ser.flush()

        time.sleep(wait_sec)

        lines = []
        while self.ser.in_waiting > 0:
            raw = self.ser.readline()
            if raw:
                decoded = raw.decode("utf-8", errors="replace").strip("\r\n")
                if decoded:
                    lines.append(decoded)
        return lines

    def show_config(self):
        print("\n=== Current Board Configuration ===")
        lines = self.send_command("ShowConfig", wait_sec=0.8)
        for line in lines:
            print(f"  {line}")
        print("===================================\n")

    def set_role(self, role: str, name: str = None):
        cmd = f"SetRole {role}"
        if name:
            cmd += f" {name}"
        print(f"[*] Sending: '{cmd}'")
        lines = self.send_command(cmd, wait_sec=1.5)
        for line in lines:
            print(f"  {line}")

    def set_name(self, name: str):
        cmd = f"SetName {name}"
        print(f"[*] Sending: '{cmd}'")
        lines = self.send_command(cmd, wait_sec=0.8)
        for line in lines:
            print(f"  {line}")

    def set_channel(self, channel: int):
        cmd = f"SetChannel {channel}"
        print(f"[*] Sending: '{cmd}'")
        lines = self.send_command(cmd, wait_sec=0.8)
        for line in lines:
            print(f"  {line}")

    def set_tx_power(self, power: int):
        cmd = f"SetTxPower {power}"
        print(f"[*] Sending: '{cmd}'")
        lines = self.send_command(cmd, wait_sec=0.8)
        for line in lines:
            print(f"  {line}")

    def factory_reset(self):
        print("[*] Sending: 'FactoryReset'")
        lines = self.send_command("FactoryReset", wait_sec=1.5)
        for line in lines:
            print(f"  {line}")


def main():
    parser = argparse.ArgumentParser(description="ESP32-C6 Mesh Workbench Provisioning Tool")
    parser.add_argument("-p", "--port", type=str, help="Serial port (e.g. COM5, COM11)")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("--show", action="store_true", help="Display current board configuration (ShowConfig)")
    parser.add_argument("--role", type=str, choices=["Coordinator", "EndDevice", "Unprovisioned"], help="Set node role")
    parser.add_argument("--name", type=str, help="Set node name (e.g. Kitchen, Garage)")
    parser.add_argument("--channel", type=int, help="Set primary RF channel (1-14)")
    parser.add_argument("--power", type=int, help="Set maximum TX power in quarter-dBm (80 = +20.0 dBm)")
    parser.add_argument("--reset", action="store_true", help="Factory reset board to UNPROVISIONED")

    args = parser.parse_args()

    port = args.port or choose_port()
    client = Provisioner(port=port, baudrate=args.baud)

    try:
        client.connect()

        # If no specific action specified, default to showing configuration
        if not any([args.show, args.role, args.name, args.channel, args.power, args.reset]):
            client.show_config()
            return

        if args.show:
            client.show_config()

        if args.reset:
            client.factory_reset()

        if args.channel is not None:
            client.set_channel(args.channel)

        if args.power is not None:
            client.set_tx_power(args.power)

        if args.name is not None and args.role is None:
            client.set_name(args.name)

        if args.role is not None:
            client.set_role(args.role, args.name)

    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
    except Exception as e:
        print(f"[!] Error: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    main()

