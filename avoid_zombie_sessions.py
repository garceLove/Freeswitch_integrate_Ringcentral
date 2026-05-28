#!/usr/bin/env python3
"""
Avoid RingCentral "zombie sessions" by checking a telephony session and
optionally dropping parties that are still active after your call flow.

Safe default:
    The script only reports active parties.

Actually clean up:
    Add --drop-active-parties to send DELETE requests for active parties.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(r"E:\Ringcentral")
CREDENTIALS_FILE = BASE_DIR / "alan.json"
CALL_OUT_RESULT_FILE = BASE_DIR / "call-out-result.json"
ACTIVE_CALLS_FILE = BASE_DIR / "active-calls.json"
ZOMBIE_CHECK_FILE = BASE_DIR / "zombie-session-check.json"
ZOMBIE_CLEANUP_FILE = BASE_DIR / "zombie-session-cleanup.json"

TERMINAL_STATUS_CODES = {
    "Disconnected",
    "NoCall",
    "Gone",
    "Voicemail",
    "Rejected",
    "Missed",
    "Finished",
}


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


def default_session_id() -> str:
    payload = load_json(CALL_OUT_RESULT_FILE)
    session = payload.get("session") or {}
    session_id = session.get("id")
    if not session_id:
        raise RuntimeError(f"No session.id found in {CALL_OUT_RESULT_FILE}")
    return str(session_id)


def read_session(server: str, access_token: str, session_id: str) -> dict[str, Any]:
    _, response = api_request(
        "GET",
        f"{server}/restapi/v1.0/account/~/telephony/sessions/{urllib.parse.quote(session_id, safe='')}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    write_json(ZOMBIE_CHECK_FILE, response)
    return response


def list_active_calls(server: str, access_token: str) -> list[dict[str, Any]]:
    _, response = api_request(
        "GET",
        f"{server}/restapi/v1.0/account/~/extension/~/active-calls?view=Detailed",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    write_json(ACTIVE_CALLS_FILE, response)
    return list(response.get("records") or [])


def parse_ringcentral_time(value: Any) -> datetime | None:
    if not value:
        return None

    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def active_call_session_ids(active_calls: list[dict[str, Any]]) -> dict[str, datetime | None]:
    sessions: dict[str, datetime | None] = {}

    for call in active_calls:
        candidates = [call]
        candidates.extend(list(call.get("legs") or []))

        for item in candidates:
            session_id = item.get("telephonySessionId")
            if not session_id:
                continue

            start_time = parse_ringcentral_time(
                item.get("startTime") or item.get("startTimeFrom") or call.get("startTime")
            )
            existing_start = sessions.get(str(session_id))
            if existing_start is None or (start_time and start_time < existing_start):
                sessions[str(session_id)] = start_time

    return sessions


def session_age_seconds(start_time: datetime | None) -> float | None:
    if start_time is None:
        return None
    return (datetime.now(timezone.utc) - start_time).total_seconds()


def is_active_party(party: dict[str, Any]) -> bool:
    status = party.get("status") or {}
    code = str(status.get("code") or "")
    return bool(party.get("id")) and code not in TERMINAL_STATUS_CODES


def describe_party(party: dict[str, Any]) -> str:
    status = party.get("status") or {}
    from_info = party.get("from") or {}
    to_info = party.get("to") or {}
    return (
        f"id={party.get('id')} "
        f"direction={party.get('direction')} "
        f"status={status.get('code')} "
        f"reason={status.get('reason')} "
        f"from={from_info.get('phoneNumber')} "
        f"to={to_info.get('phoneNumber')}"
    )


def drop_party(
    server: str,
    access_token: str,
    session_id: str,
    party_id: str,
) -> dict[str, Any]:
    _, response = api_request(
        "DELETE",
        (
            f"{server}/restapi/v1.0/account/~/telephony/sessions/"
            f"{urllib.parse.quote(session_id, safe='')}/parties/"
            f"{urllib.parse.quote(party_id, safe='')}"
        ),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    return response


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check or clean up RingCentral zombie sessions.")
    parser.add_argument(
        "--session-id",
        help="Specific telephony session ID to inspect. By default, active-calls is used.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help="Wait before checking, useful after transfer or hangup.",
    )
    parser.add_argument(
        "--drop-active-parties",
        action="store_true",
        help="Actually DELETE active parties older than --min-age-seconds.",
    )
    parser.add_argument(
        "--min-age-seconds",
        type=int,
        default=300,
        help="Only delete active sessions that have existed at least this long. Default: 300.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.wait_seconds > 0:
        print(f"Waiting {args.wait_seconds} seconds before zombie-session check...")
        time.sleep(args.wait_seconds)

    credentials = load_json(CREDENTIALS_FILE)
    server = str(credentials["server"]).rstrip("/")

    print("Getting Alan access token...")
    access_token = get_access_token(credentials)

    print("Listing existing active calls first...")
    active_calls = list_active_calls(server, access_token)
    sessions = active_call_session_ids(active_calls)
    print(f"Existing active call records: {len(active_calls)}")
    if sessions:
        for session_id, start_time in sessions.items():
            age = session_age_seconds(start_time)
            age_text = "unknown age" if age is None else f"{int(age)} seconds old"
            print(f"- session={session_id} ({age_text})")
    else:
        print("- no active telephony sessions reported by active-calls")

    if args.session_id:
        session_targets = {args.session_id: None}
    elif sessions:
        session_targets = sessions
    else:
        session_targets = {}

    cleanup_results: list[dict[str, Any]] = []
    if not session_targets:
        print("No active sessions to inspect or clean up.")

    checked_session = False
    for session_id, start_time in session_targets.items():
        checked_session = True
        print(f"Reading telephony session: {session_id}")
        session = read_session(server, access_token, session_id)
        parties = list(session.get("parties") or [])
        active_parties = [party for party in parties if is_active_party(party)]

        print(f"Party count: {len(parties)}")
        for party in parties:
            prefix = "ACTIVE" if is_active_party(party) else "DONE"
            print(f"- {prefix}: {describe_party(party)}")

        age = session_age_seconds(start_time)
        old_enough = age is not None and age >= args.min_age_seconds
        if args.drop_active_parties and active_parties and old_enough:
            for party in active_parties:
                party_id = str(party["id"])
                print(f"Dropping active party: {party_id}")
                response = drop_party(server, access_token, session_id, party_id)
                cleanup_results.append(
                    {"sessionId": session_id, "partyId": party_id, "response": response}
                )
        elif args.drop_active_parties and active_parties:
            age_text = "unknown" if age is None else f"{int(age)} seconds"
            print(
                f"Skipping cleanup for {session_id}: age is {age_text}, "
                f"minimum is {args.min_age_seconds} seconds."
            )
        elif active_parties:
            print("Active parties found. Re-run with --drop-active-parties to clean old zombies.")
        else:
            print("No active parties found for this session.")

    if args.drop_active_parties:
        write_json(ZOMBIE_CLEANUP_FILE, cleanup_results)
        print(f"Saved cleanup result to {ZOMBIE_CLEANUP_FILE}")
    else:
        print("Report-only mode. No parties were deleted.")

    print(f"Saved active-call list to {ACTIVE_CALLS_FILE}")
    if checked_session:
        print(f"Saved session check to {ZOMBIE_CHECK_FILE}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
