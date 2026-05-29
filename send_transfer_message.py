#!/usr/bin/env python3
"""
Send a pre-transfer context message to number2.

Default message:
    Incoming transferred call: customer is angry about this service
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
CREDENTIALS_FILE = BASE_DIR / "alan.json"
MESSAGE_RESULT_FILE = BASE_DIR / "transfer-message-result.json"

DEFAULT_NUMBER2 = "+19512681518"
DEFAULT_MESSAGE = "Incoming transferred call: customer is angry about this service"


def load_credentials() -> dict[str, Any]:
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


def get_access_token(credentials: dict[str, Any]) -> str:
    server = str(credentials["server"]).rstrip("/")
    basic_auth = base64.b64encode(
        f"{credentials['clientId']}:{credentials['clientSecret']}".encode("ascii")
    ).decode("ascii")
    form_body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": extract_jwt(credentials["jwt"]),
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


def send_sms(
    server: str,
    access_token: str,
    from_number: str,
    to_number: str,
    text: str,
) -> dict[str, Any]:
    request_body = json.dumps(
        {
            "from": {"phoneNumber": from_number},
            "to": [{"phoneNumber": to_number}],
            "text": text,
        }
    ).encode("utf-8")

    _, response = api_request(
        "POST",
        f"{server}/restapi/v1.0/account/~/extension/~/sms",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=request_body,
    )

    with MESSAGE_RESULT_FILE.open("w", encoding="utf-8") as file:
        json.dump(response, file, indent=2)

    return response


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send transfer context message to number2.")
    parser.add_argument("--to", default=DEFAULT_NUMBER2, help="number2 message recipient")
    parser.add_argument(
        "--from-number",
        help="SMS sender number. Defaults to caller from alan.json.",
    )
    parser.add_argument("--text", default=DEFAULT_MESSAGE, help="Message text")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    credentials = load_credentials()
    server = str(credentials["server"]).rstrip("/")
    from_number = args.from_number or str(credentials.get("caller") or "")
    if not from_number:
        raise RuntimeError("No sender number provided and alan.json has no caller field")

    print("Getting Alan access token...")
    access_token = get_access_token(credentials)
    print(f"Sending message from {from_number} to {args.to}...")
    response = send_sms(server, access_token, from_number, args.to, args.text)
    print(f"Message status: {response.get('messageStatus')}")
    print(f"Message id: {response.get('id')}")
    print(f"Saved message response to {MESSAGE_RESULT_FILE}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
