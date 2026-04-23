#!/usr/bin/env python3
"""
Restore Switch Port Configuration

Operational recovery script for MAREN. Clears and re-applies proper port_config
on EX switches after bounce_port actions cause LLDP power negotiation issues.

Use this when a port bounce leaves a device (usually an AP) in power_constrained
state or disrupts wired clients due to incomplete port_config push.

Usage:
    python scripts/restore_switch_config.py

Prerequisites:
    - .env file with MIST_API_TOKEN and MIST_BASE_URL
    - Device and port IDs configured in DEVICE_PORT_MAP below
"""

import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import requests
from dotenv import load_dotenv


# Lab constants — update if device inventory changes
SITE_ID = "d871d9fb-05fc-4822-9b59-310ce9381f26"  # Home-HQ
DEVICE_PORT_MAP: Dict[str, List[str]] = {
    # device_id: [port_ids to restore]
    "00000000-0000-0000-1000-485a0de3a580": ["ge-0/0/10"],  # EX4100 Access-Switch-1
    "00000000-0000-0000-1000-6c78c1c3fa80": [],             # EX4000 Core-Switch-1
}


def load_config() -> tuple[str, str]:
    """
    Load API token and base URL from environment.
    
    Returns:
        (api_token, base_url) tuple
        
    Raises:
        SystemExit: If required environment variables are missing
    """
    load_dotenv()
    
    token = os.getenv("MIST_API_TOKEN")
    base_url = os.getenv("MIST_BASE_URL", "https://api.ac2.mist.com/api/v1")
    
    if not token:
        print("ERROR: MIST_API_TOKEN not found in environment", file=sys.stderr)
        print("Ensure .env file exists with valid credentials", file=sys.stderr)
        sys.exit(1)
    
    return token, base_url


def restore_port_config(
    device_id: str,
    port_ids: List[str],
    site_id: str,
    token: str,
    base_url: str
) -> bool:
    """
    Execute three-step PUT sequence to restore port config.
    
    Step 1: Clear port_config to {} (removes partial overrides)
    Step 2: Re-apply with proper usage: "ap" + poe_disabled: false
    Step 3: Clear again to match network template baseline
    
    Args:
        device_id: Switch device UUID (full 00000000-0000-0000-1000-{mac} format)
        port_ids: List of port identifiers to restore (e.g., ["ge-0/0/10"])
        site_id: Mist site UUID
        token: Mist API token
        base_url: API base URL (already includes /api/v1)
        
    Returns:
        True if all steps succeeded, False otherwise
    """
    url = f"{base_url}/sites/{site_id}/devices/{device_id}"
    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json"
    }
    
    print(f"\n{'='*60}")
    print(f"Restoring port config for device {device_id}")
    print(f"Ports: {', '.join(port_ids)}")
    print(f"{'='*60}\n")
    
    # Step 1: Clear port_config
    print("Step 1: Clearing port_config overrides...")
    payload = {"port_config": {}}
    
    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"  ✓ HTTP {resp.status_code} — port_config cleared")
    except requests.exceptions.RequestException as e:
        print(f"  ✗ Step 1 failed: {e}", file=sys.stderr)
        return False
    
    time.sleep(2)  # Allow config push to settle
    
    # Step 2: Re-apply with proper usage context
    print("\nStep 2: Re-applying port config with usage: ap...")
    port_config = {}
    for port_id in port_ids:
        port_config[port_id] = {
            "usage": "ap",
            "poe_disabled": False
        }
    
    payload = {"port_config": port_config}
    
    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"  ✓ HTTP {resp.status_code} — port config applied with usage context")
    except requests.exceptions.RequestException as e:
        print(f"  ✗ Step 2 failed: {e}", file=sys.stderr)
        return False
    
    time.sleep(2)  # Allow LLDP renegotiation
    
    # Step 3: Final clear to match template baseline
    print("\nStep 3: Final clear to match network template baseline...")
    payload = {"port_config": {}}
    
    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"  ✓ HTTP {resp.status_code} — port config cleared to template baseline")
    except requests.exceptions.RequestException as e:
        print(f"  ✗ Step 3 failed: {e}", file=sys.stderr)
        return False
    
    print("\n✓ Port config restoration complete")
    print("Monitor device status in Mist dashboard to confirm power and radios recover")
    return True


def main():
    """Main entry point."""
    token, base_url = load_config()
    
    print("MAREN Operational Recovery — Switch Port Config Restore")
    print("=" * 60)
    print(f"Site: {SITE_ID}")
    print(f"Base URL: {base_url}")
    print(f"Devices to restore: {len(DEVICE_PORT_MAP)}")
    
    success_count = 0
    total_devices = len([d for d in DEVICE_PORT_MAP.values() if d])
    
    for device_id, port_ids in DEVICE_PORT_MAP.items():
        if not port_ids:
            print(f"\nSkipping {device_id} — no ports configured for restore")
            continue
        
        if restore_port_config(device_id, port_ids, SITE_ID, token, base_url):
            success_count += 1
        else:
            print(f"\n✗ Restore failed for {device_id}", file=sys.stderr)
    
    print(f"\n{'='*60}")
    print(f"Restoration complete: {success_count}/{total_devices} devices succeeded")
    print(f"{'='*60}")
    
    sys.exit(0 if success_count == total_devices else 1)


if __name__ == "__main__":
    main()
