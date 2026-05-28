#!/usr/bin/env python3
"""
Create a RingCentral Call Control call from Alan, then transfer Alan's party.

Default flow:
    Alan +19513947346 -> number1 +18338515503
    Transfer Alan/caller party -> number2 +19512681518

After the transfer, number2 should talk with number1.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BASE_DIR = Path(r"E:\Ringcentral")
CREDENTIALS_FILE = BASE_DIR / "alan.json"
DEVICE_LIST_FILE = BASE_DIR / "device-list.json"
CALL_OUT_RESULT_FILE = BASE_DIR / "call-out-result.json"
TRANSFER_RESULT_FILE = BASE_DIR / "transfer-result.json"

ALAN_DEVICE_NAME = "Alan"
DEFAULT_NUMBER1 = "+18338515503"
DEFAULT_NUMBER2 = "+19512681518"


def ensure_credentials_file() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(f"Credentials file not found: {CREDENTIALS_FILE}")


def load_credentials() -> dict[str, Any]:
    ensure_credentials_file()
    with CREDENTIALS_FILE.open("r", encoding="utf-8-sig") as file:
        credentials = json.load(file)

    missing = [
        field
        for field in ("clientId", "clientSecret", "server", "jwt")
        if field not in credentials
    ]
    if missing:
        raise ValueError(f"Missing credential fields: {', '.join(missing)}")

    return credentials


def extract_jwt(jwt_value: Any) -> str:
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
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status_code = response.getcode()
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code} calling {url}: {error_body}") from error

    return status_code, json.loads(body) if body else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)


def get_access_token(credentials: dict[str, Any]) -> str:
    server = str(credentials["server"]).rstrip("/")
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

    _, response = api_request(
        "POST",
        f"{server}/restapi/oauth/token",
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


def list_extension_devices(server: str, access_token: str) -> list[dict[str, Any]]:
    _, response = api_request(
        "GET",
        f"{server}/restapi/v1.0/account/~/extension/~/device",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    write_json(DEVICE_LIST_FILE, response)
    return list(response.get("records", []))


def list_account_devices(server: str, access_token: str) -> list[dict[str, Any]]:
    _, response = api_request(
        "GET",
        f"{server}/restapi/v1.0/account/~/device",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    write_json(DEVICE_LIST_FILE, response)
    return list(response.get("records", []))


def find_device_id(devices: list[dict[str, Any]], device_name: str) -> str:
    for device in devices:
        if str(device.get("name", "")).lower() == device_name.lower():
            device_id = device.get("id")
            if device_id:
                return str(device_id)

    known = ", ".join(str(device.get("name")) for device in devices)
    raise RuntimeError(f"Could not find device named {device_name!r}. Known devices: {known}")


def create_call_out(
    server: str,
    access_token: str,
    device_id: str,
    number1: str,
) -> dict[str, Any]:
    request_body = json.dumps(
        {
            "from": {"deviceId": device_id},
            "to": {"phoneNumber": number1},
        }
    ).encode("utf-8")

    status_code, response = api_request(
        "POST",
        f"{server}/restapi/v1.0/account/~/telephony/call-out",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=request_body,
    )
    write_json(CALL_OUT_RESULT_FILE, response)

    if status_code != 201:
        raise RuntimeError(f"Expected HTTP 201 from call-out, got HTTP {status_code}")

    return response


def extract_session_and_caller_party(
    call_out_response: dict[str, Any],
    alan_device_id: str,
) -> tuple[str, str]:
    session = call_out_response.get("session") or {}
    telephony_session_id = session.get("id")
    if not telephony_session_id:
        raise RuntimeError("Call-out response did not include session.id")

    parties = list(session.get("parties") or [])
    if not parties:
        raise RuntimeError("Call-out response did not include session.parties")

    # Transfer Alan's caller party so number2 replaces Alan and talks to number1.
    for party in parties:
        from_info = party.get("from") or {}
        if str(from_info.get("deviceId")) == str(alan_device_id):
            party_id = party.get("id")
            if party_id:
                return str(telephony_session_id), str(party_id)

    for party in parties:
        if str(party.get("direction", "")).lower() == "outbound" and party.get("id"):
            return str(telephony_session_id), str(party["id"])

    first_party_id = parties[0].get("id")
    if not first_party_id:
        raise RuntimeError("Could not identify caller party id from call-out response")

    return str(telephony_session_id), str(first_party_id)


def transfer_caller_party(
    server: str,
    access_token: str,
    telephony_session_id: str,
    party_id: str,
    number2: str,
) -> dict[str, Any]:
    request_body = json.dumps({"phoneNumber": number2}).encode("utf-8")
    transfer_url = (
        f"{server}/restapi/v1.0/account/~/telephony/sessions/"
        f"{urllib.parse.quote(telephony_session_id, safe='')}/parties/"
        f"{urllib.parse.quote(party_id, safe='')}/transfer"
    )

    _, response = api_request(
        "POST",
        transfer_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=request_body,
    )
    write_json(TRANSFER_RESULT_FILE, response)
    return response


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call number1 from Alan, then transfer Alan's party to number2."
    )
    parser.add_argument("--number1", default=DEFAULT_NUMBER1, help="First number Alan calls")
    parser.add_argument("--number2", default=DEFAULT_NUMBER2, help="Human transfer number")
    parser.add_argument("--device-name", default=ALAN_DEVICE_NAME, help="RingCentral device name")
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=10,
        help="Seconds to wait after call creation before transfer",
    )
    parser.add_argument(
        "--confirm-before-transfer",
        action="store_true",
        help="Pause for Enter before transferring to number2",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    credentials = load_credentials()
    server = str(credentials["server"]).rstrip("/")

    print("Getting RingCentral access token...")
    access_token = get_access_token(credentials)
    print("Access token received.")

    print(f"Finding device named {args.device_name!r}...")
    devices = list_extension_devices(server, access_token)
    if not any(str(device.get("name", "")).lower() == args.device_name.lower() for device in devices):
        print(f"{args.device_name!r} was not in extension devices; checking account devices...")
        devices = list_account_devices(server, access_token)
    alan_device_id = find_device_id(devices, args.device_name)
    print(f"Using Alan deviceId: {alan_device_id}")

    print(f"Creating Call Control call: Alan -> number1 {args.number1}...")
    call_out_response = create_call_out(server, access_token, alan_device_id, args.number1)
    telephony_session_id, caller_party_id = extract_session_and_caller_party(
        call_out_response,
        alan_device_id,
    )
    print(f"telephonySessionId: {telephony_session_id}")
    print(f"Alan caller partyId: {caller_party_id}")
    print(f"Saved call-out response to {CALL_OUT_RESULT_FILE}")

    if args.confirm_before_transfer:
        input(f"Press Enter to transfer Alan's party to number2 {args.number2}...")
    elif args.wait_seconds > 0:
        print(f"Waiting {args.wait_seconds} seconds before transfer...")
        time.sleep(args.wait_seconds)

    print(f"Transferring Alan's party to number2 {args.number2}...")
    transfer_response = transfer_caller_party(
        server,
        access_token,
        telephony_session_id,
        caller_party_id,
        args.number2,
    )
    print("Transfer request sent. number2 should now talk with number1.")
    print(json.dumps(transfer_response, indent=2))
    print(f"Saved transfer response to {TRANSFER_RESULT_FILE}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
