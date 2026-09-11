"""
Dynu DDNS Smart IP Change Monitor and Updater.

This script determines the current public IPv4 address by querying multiple
external resolvers sequentially. It compares the discovered IP against the last
recorded successful IP in a local history log. If a change is detected, it
triggers the `ddclient.service` on-demand to perform the DNS record update.

This local-first state verification prevents making redundant API requests to
the DNS provider, avoiding rate-limiting, IP bans, or API abuse flags.
"""

import urllib.request
import re
import json
import os
import subprocess
import sys
from datetime import datetime

# History file location storing the transition log of public IP states.
# Defaults to a state directory managed securely by the systemd service unit.
HISTORY_FILE = os.environ.get("HISTORY_FILE", "/var/lib/dynu/ip_history.jsonl")

# Fallback sequence of external services to discover the public WAN IP.
PROVIDERS = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://wtfismyip.com/text"
]


def is_valid_public_ipv4(ip):
    """
    Validates that a string is a properly formatted public IPv4 address.

    This function excludes standard private, loopback, link-local, and CGNAT
    blocks to avoid recording temporary, local, or invalid network states.
    """
    match = re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$", ip)
    if not match:
        return False
    octets = [int(x) for x in match.groups()]
    if any(x > 255 for x in octets):
        return False
    a, b, c, d = octets

    # RFC 1918 Private ranges: 10.0.0.0/8, 192.168.0.0/16, 172.16.0.0/12
    if a == 10:
        return False
    if a == 192 and b == 168:
        return False
    if a == 172 and 16 <= b <= 31:
        return False

    # Carrier-Grade NAT (CGNAT) RFC 6598: 100.64.0.0/10
    if a == 100 and 64 <= b <= 127:
        return False

    # Loopback address range: 127.0.0.0/8
    if a == 127:
        return False

    # Link-local addresses: 169.254.0.0/16
    if a == 169 and b == 254:
        return False

    return True


def get_public_ip():
    """
    Queries public IP providers until a valid public IPv4 is returned.

    Returns:
        str: The validated public IP string, or None if all providers fail.
    """
    errors = []
    for provider in PROVIDERS:
        try:
            req = urllib.request.Request(
                provider,
                headers={
                    'User-Agent': 'Mozilla/5.0 (NixOS WAN Monitor)'
                }
            )
            # Fetch with 5s timeout to avoid hanging the systemd service
            with urllib.request.urlopen(req, timeout=5) as response:
                ip = response.read().decode('utf-8').strip()
                if is_valid_public_ipv4(ip):
                    return ip
                else:
                    errors.append(f"{provider}: invalid public IPv4 '{ip}'")
        except Exception as e:
            errors.append(f"{provider}: {str(e)}")

    print(
        f"Error: All IP discovery providers failed. Details: {errors}",
        file=sys.stderr
    )
    return None


def get_last_recorded_ip():
    """
    Reads the history log to locate the last successful IP address.

    Returns:
        str: The last successfully updated IP, or None if no log exists yet.
    """
    if not os.path.exists(HISTORY_FILE):
        return None
    try:
        with open(HISTORY_FILE, "r") as f:
            lines = f.readlines()
            if not lines:
                return None
            # Search backwards to find the most recent successful transition
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                    if entry.get("status") == "success":
                        return entry.get("ip")
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"Warning: Failed to read history file: {e}", file=sys.stderr)
    return None


def record_ip(ip, status, details=None):
    """
    Appends a new event entry into the JSONLines history registry.
    """
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "ip": ip,
        "status": status
    }
    if details:
        entry["details"] = details
    try:
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"Error: Failed to write to history file: {e}", file=sys.stderr)


def trigger_ddclient():
    """
    Invokes the systemd service unit for `ddclient` on-demand.

    Returns:
        tuple: (bool, str) representing (success_boolean, error_details_string)
    """
    try:
        # Run ddclient as a one-shot activation via systemd to perform API call
        res = subprocess.run(
            ["systemctl", "start", "ddclient.service"],
            capture_output=True,
            text=True
        )
        if res.returncode == 0:
            return True, None
        else:
            err_msg = (
                f"systemctl exited with {res.returncode}: "
                f"{res.stderr.strip()}"
            )
            return False, err_msg
    except Exception as e:
        return False, str(e)


def main():
    """
    Orchestrator logic for DDNS state management.
    """
    current_ip = get_public_ip()
    if not current_ip:
        sys.exit(1)

    last_ip = get_last_recorded_ip()
    if current_ip == last_ip:
        # IP matches last recorded working IP; do nothing to conserve quota
        sys.exit(0)

    print(f"IP rotation detected! Old: {last_ip}, New: {current_ip}")

    # Trigger ddclient to perform the DNS record update
    success, error_msg = trigger_ddclient()
    if success:
        print("Dynu DDNS update via ddclient triggered successfully.")
        record_ip(current_ip, "success")
    else:
        err_msg = f"Error: Failed to trigger ddclient update: {error_msg}"
        print(err_msg, file=sys.stderr)
        record_ip(current_ip, "failed_update", details=error_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
