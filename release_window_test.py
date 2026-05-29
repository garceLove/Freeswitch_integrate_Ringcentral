#!/usr/bin/env python3
"""
Measure how long a RingCentral telephony session remains visible after transfer.

Flow:
1. Alan calls number1.
2. Wait before transfer.
3. Transfer Alan's party to number2.
4. Poll active-calls every N seconds until the session disappears.

This script is report-only after transfer. It never deletes sessions or parties.
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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


BASE_DIR = Path(r"E:\Ringcentral")
CREDENTIALS_FILE = BASE_DIR / "alan.json"
LOG_DIR = BASE_DIR

ALAN_DEVICE_NAME = "Alan"
DEFAULT_NUMBER1 = "+18338515503"
DEFAULT_NUMBER2 = "+19512681518"


def now_local() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def ts() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S %z")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)


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
    body = urllib.parse.urlencode(
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
        data=body,
    )
    access_token = response.get("access_token")
    if not access_token:
        raise RuntimeError("Token response did not include access_token")
    return str(access_token)


def list_devices(server: str, access_token: str) -> list[dict[str, Any]]:
    _, response = api_request(
        "GET",
        f"{server}/restapi/v1.0/account/~/extension/~/device",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    return list(response.get("records") or [])


def find_device_id(devices: list[dict[str, Any]], name: str) -> str:
    for device in devices:
        if str(device.get("name", "")).lower() == name.lower() and device.get("id"):
            return str(device["id"])
    raise RuntimeError(f"Could not find device named {name!r}")


def create_call_out(server: str, access_token: str, device_id: str, number1: str) -> dict[str, Any]:
    body = json.dumps(
        {"from": {"deviceId": device_id}, "to": {"phoneNumber": number1}}
    ).encode("utf-8")
    status_code, response = api_request(
        "POST",
        f"{server}/restapi/v1.0/account/~/telephony/call-out",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=body,
    )
    if status_code != 201:
        raise RuntimeError(f"Expected HTTP 201 from call-out, got HTTP {status_code}")
    write_json(BASE_DIR / "release-window-call-out-result.json", response)
    return response


def extract_session_and_party(response: dict[str, Any], device_id: str) -> tuple[str, str]:
    session = response.get("session") or {}
    session_id = session.get("id")
    if not session_id:
        raise RuntimeError("Call-out response did not include session.id")

    parties = list(session.get("parties") or [])
    for party in parties:
        if str((party.get("from") or {}).get("deviceId")) == str(device_id) and party.get("id"):
            return str(session_id), str(party["id"])

    for party in parties:
        if party.get("id"):
            return str(session_id), str(party["id"])

    raise RuntimeError("Call-out response did not include a usable party id")


def transfer_party(
    server: str,
    access_token: str,
    session_id: str,
    party_id: str,
    number2: str,
) -> dict[str, Any]:
    body = json.dumps({"phoneNumber": number2}).encode("utf-8")
    _, response = api_request(
        "POST",
        (
            f"{server}/restapi/v1.0/account/~/telephony/sessions/"
            f"{urllib.parse.quote(session_id, safe='')}/parties/"
            f"{urllib.parse.quote(party_id, safe='')}/transfer"
        ),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=body,
    )
    write_json(BASE_DIR / "release-window-transfer-result.json", response)
    return response


def list_active_calls(server: str, access_token: str) -> dict[str, Any]:
    _, response = api_request(
        "GET",
        f"{server}/restapi/v1.0/account/~/extension/~/active-calls?view=Detailed",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    return response


def read_session(server: str, access_token: str, session_id: str) -> dict[str, Any] | None:
    try:
        _, response = api_request(
            "GET",
            (
                f"{server}/restapi/v1.0/account/~/telephony/sessions/"
                f"{urllib.parse.quote(session_id, safe='')}"
            ),
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        return response
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise


def active_call_session_ids(active_calls: dict[str, Any]) -> set[str]:
    session_ids: set[str] = set()
    for call in active_calls.get("records") or []:
        if call.get("telephonySessionId"):
            session_ids.add(str(call["telephonySessionId"]))
        for leg in call.get("legs") or []:
            if leg.get("telephonySessionId"):
                session_ids.add(str(leg["telephonySessionId"]))
    return session_ids


def party_summary(session: dict[str, Any] | None) -> str:
    if session is None:
        return "session detail: 404/not found"

    parts: list[str] = []
    for party in session.get("parties") or []:
        status = party.get("status") or {}
        from_info = party.get("from") or {}
        to_info = party.get("to") or {}
        parts.append(
            (
                f"{party.get('id')}|{party.get('direction')}|"
                f"{status.get('code')}|{status.get('reason')}|"
                f"{from_info.get('phoneNumber')}->{to_info.get('phoneNumber')}"
            )
        )
    return "parties: " + "; ".join(parts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure release window after transfer.")
    parser.add_argument("--number1", default=DEFAULT_NUMBER1)
    parser.add_argument("--number2", default=DEFAULT_NUMBER2)
    parser.add_argument("--wait-before-transfer", type=int, default=30)
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--max-poll-seconds", type=int, default=900)
    parser.add_argument(
        "--log-file",
        default=str(LOG_DIR / f"release-window-test-{now_local().strftime('%Y%m%d%H%M%S')}.log"),
    )
    return parser.parse_args()


class Logger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        line = f"[{ts()}] {message}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")


def main() -> int:
    args = parse_args()
    logger = Logger(Path(args.log_file))

    credentials = load_json(CREDENTIALS_FILE)
    server = str(credentials["server"]).rstrip("/")

    logger.log("START release-window test")
    logger.log(f"log_file={args.log_file}")
    logger.log(f"number1={args.number1}")
    logger.log(f"number2={args.number2}")
    logger.log(f"wait_before_transfer={args.wait_before_transfer}s")
    logger.log(f"poll_interval={args.poll_seconds}s")
    logger.log(f"max_poll_window={args.max_poll_seconds}s")
    logger.log("cleanup_policy=report-only; no delete API will be called")

    logger.log("Getting Alan access token")
    access_token = get_access_token(credentials)

    logger.log("Finding Alan device")
    device_id = find_device_id(list_devices(server, access_token), ALAN_DEVICE_NAME)
    logger.log(f"alan_device_id={device_id}")

    logger.log("Creating Call Control call to number1")
    call_out = create_call_out(server, access_token, device_id, args.number1)
    session_id, party_id = extract_session_and_party(call_out, device_id)
    created_at = time.monotonic()
    logger.log(f"created_session_id={session_id}")
    logger.log(f"alan_party_id={party_id}")

    logger.log(f"Waiting {args.wait_before_transfer}s before transfer")
    time.sleep(args.wait_before_transfer)

    logger.log("Transferring Alan party to number2")
    transfer = transfer_party(server, access_token, session_id, party_id, args.number2)
    transferred_at = time.monotonic()
    transfer_status = transfer.get("status") or {}
    logger.log(
        f"transfer_result party_status={transfer_status.get('code')} "
        f"reason={transfer_status.get('reason')}"
    )

    logger.log("Begin polling active-calls and session detail")
    first_all_parties_disconnected_at: float | None = None
    disappeared_at: float | None = None
    deadline = transferred_at + args.max_poll_seconds
    poll_index = 0

    while time.monotonic() <= deadline:
        poll_index += 1
        active_calls = list_active_calls(server, access_token)
        write_json(BASE_DIR / "release-window-active-calls.json", active_calls)
        listed = session_id in active_call_session_ids(active_calls)
        session = read_session(server, access_token, session_id)
        if session is not None:
            write_json(BASE_DIR / "release-window-session-check.json", session)

        parties = list((session or {}).get("parties") or [])
        all_disconnected = bool(parties) and all(
            str((party.get("status") or {}).get("code")) == "Disconnected"
            for party in parties
        )
        if all_disconnected and first_all_parties_disconnected_at is None:
            first_all_parties_disconnected_at = time.monotonic()

        elapsed_from_create = int(time.monotonic() - created_at)
        elapsed_from_transfer = int(time.monotonic() - transferred_at)
        logger.log(
            f"poll={poll_index} elapsed_create={elapsed_from_create}s "
            f"elapsed_transfer={elapsed_from_transfer}s "
            f"active_calls_records={len(active_calls.get('records') or [])} "
            f"session_listed={listed} party_count={len(parties)} "
            f"all_parties_disconnected={all_disconnected} {party_summary(session)}"
        )

        if not listed:
            disappeared_at = time.monotonic()
            break

        time.sleep(args.poll_seconds)

    if first_all_parties_disconnected_at is not None:
        logger.log(
            "first_all_parties_disconnected_after_transfer="
            f"{int(first_all_parties_disconnected_at - transferred_at)}s"
        )
    else:
        logger.log("first_all_parties_disconnected_after_transfer=not observed")

    if disappeared_at is not None:
        logger.log(f"session_removed_from_active_calls_after_transfer={int(disappeared_at - transferred_at)}s")
        logger.log(f"session_removed_from_active_calls_after_create={int(disappeared_at - created_at)}s")
    else:
        logger.log(
            "session_removed_from_active_calls_after_transfer=not observed "
            f"within {args.max_poll_seconds}s"
        )

    logger.log("END release-window test")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
