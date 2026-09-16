#!/usr/bin/env python3

import argparse
import os
import shutil
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WIFI_INTERFACE = "wlan0"

# Existing NetworkManager profile used to connect the Pi to normal Wi-Fi.
# Change this to the NAME shown by:
# nmcli -f NAME,TYPE,DEVICE connection show
CLIENT_PROFILE = "Home-WiFi"

# Hotspot settings
HOTSPOT_PROFILE = "Pi-Hotspot"
HOTSPOT_SSID = "Pi-Lab"
HOTSPOT_PASSWORD = "ChangeThisPassword123"
HOTSPOT_ADDRESS = "192.168.77.1/24"

COMMAND_TIMEOUT = 30


class CommandError(RuntimeError):
    pass


def run(command, check=True):
    """Run a command and return stripped stdout."""
    print("+", " ".join(command))

    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(
            f"Command timed out after {COMMAND_TIMEOUT} seconds: "
            + " ".join(command)
        ) from exc

    if result.stdout.strip():
        print(result.stdout.strip())

    if result.returncode != 0 and result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)

    if check and result.returncode != 0:
        raise CommandError(
            f"Command failed with exit code {result.returncode}: "
            + " ".join(command)
        )

    return result.stdout.strip()


def require_root():
    if os.geteuid() != 0:
        sys.exit("Run this program with sudo.")


def require_nmcli():
    if shutil.which("nmcli") is None:
        sys.exit("nmcli was not found. NetworkManager must be installed.")


def connection_exists(profile):
    result = subprocess.run(
        ["nmcli", "-g", "connection.id", "connection", "show", profile],
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT,
    )
    return result.returncode == 0


def device_state():
    result = subprocess.run(
        [
            "nmcli",
            "-g",
            "GENERAL.STATE",
            "device",
            "show",
            WIFI_INTERFACE,
        ],
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT,
    )

    if result.returncode != 0:
        return "unknown"

    return result.stdout.strip()


def active_wifi_profile():
    result = subprocess.run(
        [
            "nmcli",
            "-t",
            "-f",
            "NAME,TYPE,DEVICE",
            "connection",
            "show",
            "--active",
        ],
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT,
    )

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        fields = line.rsplit(":", 2)

        if len(fields) != 3:
            continue

        name, connection_type, device = fields

        if connection_type == "802-11-wireless" and device == WIFI_INTERFACE:
            return name

    return None


def ensure_hotspot_profile():
    """Create or normalize the hotspot connection profile."""

    if not connection_exists(HOTSPOT_PROFILE):
        print(f"Creating hotspot profile: {HOTSPOT_PROFILE}")

        run(
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                WIFI_INTERFACE,
                "con-name",
                HOTSPOT_PROFILE,
                "ssid",
                HOTSPOT_SSID,
            ]
        )

    # Configure the Wi-Fi connection as an access point.
    run(
        [
            "nmcli",
            "connection",
            "modify",
            HOTSPOT_PROFILE,
            "connection.interface-name",
            WIFI_INTERFACE,
            "802-11-wireless.mode",
            "ap",
            "802-11-wireless.ssid",
            HOTSPOT_SSID,
            "802-11-wireless.band",
            "bg",
            "802-11-wireless-security.key-mgmt",
            "wpa-psk",
            "802-11-wireless-security.psk",
            HOTSPOT_PASSWORD,
            "ipv4.method",
            "shared",
            "ipv4.addresses",
            HOTSPOT_ADDRESS,
            "ipv6.method",
            "disabled",
            "connection.autoconnect",
            "no",
        ]
    )


def activate_profile(profile):
    run(
        [
            "nmcli",
            "--wait",
            "20",
            "connection",
            "up",
            profile,
            "ifname",
            WIFI_INTERFACE,
        ]
    )


def hotspot_mode():
    ensure_hotspot_profile()

    print()
    print("Switching to hotspot mode...")

    current = active_wifi_profile()

    if current and current != HOTSPOT_PROFILE:
        print(f"Disconnecting Wi-Fi profile: {current}")
        run(["nmcli", "connection", "down", current], check=False)

    activate_profile(HOTSPOT_PROFILE)

    print()
    print("Hotspot is active.")
    print(f"  SSID:       {HOTSPOT_SSID}")
    print(f"  Gateway:    {HOTSPOT_ADDRESS.split('/')[0]}")
    print(f"  Interface:  {WIFI_INTERFACE}")


def wifi_mode():
    if not connection_exists(CLIENT_PROFILE):
        raise CommandError(
            f'Wi-Fi profile "{CLIENT_PROFILE}" does not exist. '
            "Check CLIENT_PROFILE at the top of the script."
        )

    print()
    print("Switching to normal Wi-Fi client mode...")

    if connection_exists(HOTSPOT_PROFILE):
        run(
            ["nmcli", "connection", "down", HOTSPOT_PROFILE],
            check=False,
        )

    # Give NetworkManager a moment to release AP mode.
    time.sleep(1)

    run(["nmcli", "radio", "wifi", "on"])
    activate_profile(CLIENT_PROFILE)

    print()
    print("Normal Wi-Fi mode is active.")
    print(f"  Profile:    {CLIENT_PROFILE}")
    print(f"  Interface:  {WIFI_INTERFACE}")


def toggle_mode():
    current = active_wifi_profile()

    if current == HOTSPOT_PROFILE:
        wifi_mode()
    else:
        hotspot_mode()


def show_status():
    current = active_wifi_profile()

    print(f"Interface:       {WIFI_INTERFACE}")
    print(f"Device state:    {device_state()}")
    print(f"Active profile:  {current or 'none'}")

    if current == HOTSPOT_PROFILE:
        print("Operating mode:  hotspot")
        print(f"SSID:            {HOTSPOT_SSID}")
        print(f"Gateway:         {HOTSPOT_ADDRESS.split('/')[0]}")
    elif current:
        print("Operating mode:  Wi-Fi client")
    else:
        print("Operating mode:  disconnected")


def main():
    parser = argparse.ArgumentParser(
        description="Switch Raspberry Pi Wi-Fi between client and hotspot modes."
    )

    parser.add_argument(
        "mode",
        choices=["hotspot", "wifi", "toggle", "status"],
        help=(
            "hotspot: start the AP; "
            "wifi: reconnect to normal Wi-Fi; "
            "toggle: switch modes; "
            "status: display current mode"
        ),
    )

    args = parser.parse_args()

    require_root()
    require_nmcli()

    try:
        if args.mode == "hotspot":
            hotspot_mode()
        elif args.mode == "wifi":
            wifi_mode()
        elif args.mode == "toggle":
            toggle_mode()
        elif args.mode == "status":
            show_status()
    except CommandError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
