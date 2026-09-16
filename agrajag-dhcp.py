#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Change this if your isolated test interface has another name.
INTERFACE = "eth0"

CONNECTION_NAME = "agrajag-dhcp-192.168.77"
PI_ADDRESS = "192.168.77.1/24"
DNSMASQ_CONFIG = Path("/etc/dnsmasq.d/agrajag-dhcp.conf")
STATE_FILE = Path("/run/agrajag-dhcp-state.json")


def run(command, check=True, capture=False):
    """Run a command and optionally return its standard output."""
    print("+", " ".join(command))

    result = subprocess.run(
        command,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )

    if capture:
        return result.stdout.strip()

    return ""


def require_root():
    if os.geteuid() != 0:
        print("Error: run this script with sudo.", file=sys.stderr)
        sys.exit(1)


def require_command(command):
    if shutil.which(command) is None:
        print(f"Error: required command not found: {command}", file=sys.stderr)
        sys.exit(1)


def interface_exists():
    result = subprocess.run(
        ["ip", "link", "show", "dev", INTERFACE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def connection_exists(name):
    result = subprocess.run(
        ["nmcli", "-t", "-f", "NAME", "connection", "show", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def get_active_connection():
    output = run(
        [
            "nmcli",
            "-t",
            "-f",
            "GENERAL.CONNECTION",
            "device",
            "show",
            INTERFACE,
        ],
        capture=True,
        check=False,
    )

    if not output:
        return None

    _, _, value = output.partition(":")
    value = value.strip()

    if not value or value == "--":
        return None

    return value


def save_state(previous_connection):
    STATE_FILE.write_text(
        json.dumps(
            {
                "interface": INTERFACE,
                "previous_connection": previous_connection,
            }
        )
    )


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def create_or_update_connection():
    if not connection_exists(CONNECTION_NAME):
        run(
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "ethernet",
                "ifname",
                INTERFACE,
                "con-name",
                CONNECTION_NAME,
            ]
        )

    run(
        [
            "nmcli",
            "connection",
            "modify",
            CONNECTION_NAME,
            "connection.interface-name",
            INTERFACE,
            "connection.autoconnect",
            "no",
            "ipv4.method",
            "manual",
            "ipv4.addresses",
            PI_ADDRESS,
            "ipv4.gateway",
            "",
            "ipv4.dns",
            "",
            "ipv4.never-default",
            "yes",
            "ipv6.method",
            "disabled",
        ]
    )


def start():
    if not interface_exists():
        print(f"Error: interface {INTERFACE} does not exist.", file=sys.stderr)
        sys.exit(1)

    if not DNSMASQ_CONFIG.exists():
        print(
            f"Error: dnsmasq configuration not found: {DNSMASQ_CONFIG}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate before changing the interface.
    run(["dnsmasq", "--test"])

    previous_connection = get_active_connection()

    if previous_connection == CONNECTION_NAME:
        previous_connection = None

    save_state(previous_connection)

    create_or_update_connection()

    # Activating this profile may replace another profile on the interface.
    run(["nmcli", "connection", "up", CONNECTION_NAME])

    # Confirm that NetworkManager assigned the intended address.
    address_output = run(
        ["ip", "-4", "-brief", "address", "show", "dev", INTERFACE],
        capture=True,
    )

    if "192.168.77.1/24" not in address_output:
        print(
            f"Error: {PI_ADDRESS} was not assigned to {INTERFACE}.",
            file=sys.stderr,
        )
        sys.exit(1)

    run(["systemctl", "restart", "dnsmasq"])

    if subprocess.run(
        ["systemctl", "is-active", "--quiet", "dnsmasq"]
    ).returncode != 0:
        print("Error: dnsmasq did not start.", file=sys.stderr)
        run(
            [
                "journalctl",
                "-u",
                "dnsmasq",
                "-n",
                "30",
                "--no-pager",
            ],
            check=False,
        )
        sys.exit(1)

    print()
    print("Agrajag DHCP server is ON")
    print(f"Interface:       {INTERFACE}")
    print(f"Pi address:      {PI_ADDRESS}")
    print("DHCP pool:       192.168.77.10 - 192.168.77.20")
    print("Default gateway: 192.168.77.1")
    print("DNS servers:     208.67.222.222, 208.67.220.220")


def stop():
    # Stop DHCP first so it cannot answer while the interface changes.
    run(["systemctl", "stop", "dnsmasq"], check=False)

    if connection_exists(CONNECTION_NAME):
        run(
            ["nmcli", "connection", "down", CONNECTION_NAME],
            check=False,
        )

    state = load_state()
    previous_connection = state.get("previous_connection")

    if previous_connection and connection_exists(previous_connection):
        print(f"Restoring previous connection: {previous_connection}")
        run(
            ["nmcli", "connection", "up", previous_connection],
            check=False,
        )

    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass

    print("Agrjag DHCP server is OFF")


def status():
    service_active = (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", "dnsmasq"]
        ).returncode
        == 0
    )

    profile_active = get_active_connection() == CONNECTION_NAME

    print(f"DHCP service:    {'active' if service_active else 'inactive'}")
    print(f"Test connection: {'active' if profile_active else 'inactive'}")
    print(f"Interface:       {INTERFACE}")

    run(
        ["ip", "-4", "-brief", "address", "show", "dev", INTERFACE],
        check=False,
    )

    if service_active:
        print("\nCurrent DHCP leases:")
        lease_file = Path("/var/lib/misc/dnsmasq.leases")

        if lease_file.exists() and lease_file.stat().st_size > 0:
            print(lease_file.read_text(), end="")
        else:
            print("No leases have been issued.")


def leases():
    lease_file = Path("/var/lib/misc/dnsmasq.leases")

    if not lease_file.exists() or lease_file.stat().st_size == 0:
        print("No DHCP leases found.")
        return

    print("Expiry\tMAC address\tIP address\tHostname\tClient ID")
    print(lease_file.read_text(), end="")


def main():
    parser = argparse.ArgumentParser(
        description="Control the isolated Agrajag DHCP server."
    )
    parser.add_argument(
        "action",
        choices=["start", "stop", "restart", "status", "leases"],
    )
    args = parser.parse_args()

    require_root()

    for command in ("ip", "nmcli", "dnsmasq", "systemctl"):
        require_command(command)

    if args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    elif args.action == "restart":
        stop()
        time.sleep(1)
        start()
    elif args.action == "status":
        status()
    elif args.action == "leases":
        leases()


if __name__ == "__main__":
    main()
