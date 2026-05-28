#!/usr/bin/env python3
"""
RingCentral helper for:
1. Loading JWT app credentials from rc-credentials.json
2. Trading the JWT for an OAuth access token
3. Calling the device list endpoint

Optional:
    Add --call-alan to start a RingOut call from device name "Alan"
    to +19512681518.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BASE_DIR = Path(r"E:\Ringcentral")
DOWNLOADS_CREDENTIALS = Path(r"E:\Downloads\rc-credentials.json")
CREDENTIALS_FILE = BASE_DIR / "rc-credentials.json"
DEVICE_LIST_FILE = BASE_DIR / "device-list.json"
RING_OUT_RESULT_FILE = BASE_DIR / "ring-out-result.json"

ALAN_DEVICE_NAME = "Alan"
ALAN_TO_NUMBER = "+19512681518"


def ensure_credentials_file() -> None:
    """Create E:\\Ringcentral and move rc-credentials.json there if needed."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    if not CREDENTIALS_FILE.exists() and DOWNLOADS_CREDENTIALS.exists():
        DOWNLOADS_CREDENTIALS.replace(CREDENTIALS_FILE)

    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(f"Credentials file not found: {CREDENTIALS_FILE}")


def load_credentials() -> dict[str, Any]:
    ensure_credentials_file()
    with CREDENTIALS_FILE.open("r", encoding="utf-8") as file:
        credentials = json.load(file)

    required_fields = ["clientId", "clientSecret", "server", "jwt"]
    missing = [field for field in required_fields if field not in credentials]
    if missing:
        raise ValueError(f"Missing credential fields: {', '.join(missing)}")

    return credentials


def extract_jwt(jwt_value: Any) -> str:
    """
    RingCentral's downloaded sample JSON can store JWT as:
        "jwt": { "Demonstration JWT credential": "..." }
    This accepts both that shape and a plain string.
    """
    if isinstance(jwt_value, dict):
        if not jwt_value:
            raise ValueError("JWT object is empty")
        jwt_value = next(iter(jwt_value.values()))

    assertion = "".join(str(jwt_value).split())
    if assertion.count(".") != 2:
        raise ValueError("JWT does not look like a three-part JWT assertion")

    return assertion


def api_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code} calling {url}: {error_body}") from error

    return json.loads(body) if body else {}


def get_access_token(credentials: dict[str, Any]) -> str:
    server = str(credentials["server"]).rstrip("/")
    token_url = f"{server}/restapi/oauth/token"
    assertion = extract_jwt(credentials["jwt"])

    basic_auth = base64.b64encode(
        f"{credentials['clientId']}:{credentials['clientSecret']}".encode("ascii")
    ).decode("ascii")

    form_body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }
    ).encode("ascii")

    response = api_request(
        "POST",
        token_url,
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data=form_body,
    )

    access_token = response.get("access_token")
    if not access_token:
        raise RuntimeError("Token response did not contain access_token")

    return str(access_token)


def list_devices(server: str, access_token: str) -> list[dict[str, Any]]:
    device_url = f"{server.rstrip('/')}/restapi/v1.0/account/~/device"
    response = api_request(
        "GET",
        device_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )

    with DEVICE_LIST_FILE.open("w", encoding="utf-8") as file:
        json.dump(response, file, indent=2)

    return list(response.get("records", []))


def print_devices(devices: list[dict[str, Any]]) -> None:
    print(f"Device count: {len(devices)}")
    for device in devices:
        print(
            f"- id={device.get('id')} "
            f"name={device.get('name')} "
            f"type={device.get('type')} "
            f"status={device.get('status')}"
        )


def find_device_by_name(devices: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for device in devices:
        if str(device.get("name", "")).lower() == name.lower():
            return device
    raise RuntimeError(f"Could not find device named {name!r}")


def get_first_phone_number(device: dict[str, Any]) -> str:
    for line in device.get("phoneLines", []):
        phone_info = line.get("phoneInfo") or {}
        phone_number = phone_info.get("phoneNumber")
        if phone_number:
            return str(phone_number)
    raise RuntimeError(f"Device {device.get('name')} has no phone number")


def call_ring_out(server: str, access_token: str, from_number: str, to_number: str) -> dict[str, Any]:
    ring_out_url = f"{server.rstrip('/')}/restapi/v1.0/account/~/extension/~/ring-out"
    request_body = json.dumps(
        {
            "from": {"phoneNumber": from_number},
            "to": {"phoneNumber": to_number},
            "playPrompt": False,
        }
    ).encode("utf-8")

    response = api_request(
        "POST",
        ring_out_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=request_body,
    )

    with RING_OUT_RESULT_FILE.open("w", encoding="utf-8") as file:
        json.dump(response, file, indent=2)

    return response


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RingCentral JWT/device helper")
    parser.add_argument(
        "--call-alan",
        action="store_true",
        help=f"Start RingOut from {ALAN_DEVICE_NAME} to {ALAN_TO_NUMBER}",
    )
    parser.add_argument(
        "--to",
        default=ALAN_TO_NUMBER,
        help="Destination phone number for --call-alan",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    credentials = load_credentials()
    server = str(credentials["server"]).rstrip("/")

    print("Getting RingCentral access token...")
    access_token = get_access_token(credentials)
    print("Access token received.")

    print("Fetching device list...")
    devices = list_devices(server, access_token)
    print_devices(devices)
    print(f"Saved full device list to {DEVICE_LIST_FILE}")

    if args.call_alan:
        alan = find_device_by_name(devices, ALAN_DEVICE_NAME)
        from_number = get_first_phone_number(alan)
        print(f"Starting RingOut from {ALAN_DEVICE_NAME} ({from_number}) to {args.to}...")
        result = call_ring_out(server, access_token, from_number, args.to)
        status = result.get("status", {})
        print(f"RingOut id: {result.get('id')}")
        print(f"Status: {json.dumps(status)}")
        print(f"Saved RingOut result to {RING_OUT_RESULT_FILE}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
