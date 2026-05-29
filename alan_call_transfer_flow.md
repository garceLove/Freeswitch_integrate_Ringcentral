# Alan Call And Transfer Flow

## Current Default Flow

Use Alan's RingCentral JWT credentials to run this flow:

```text
Alan device -> call number1 +18338515503 -> wait 30 seconds -> transfer Alan's party to number2 +19512681518
```

After the blind transfer, Alan's call leg is removed and `number2` should talk with `number1`.

Current defaults:

```text
number1: +18338515503
number2: +19512681518
wait before transfer: 30 seconds
Alan deviceId: 805169921011
Alan extension: 102
Alan extensionId: 3243776011
Alan device phone: +19513947346
```

Run the default flow:

```powershell
python E:\Ringcentral\alan_call_transfer.py --wait-seconds 30
```

## Step 1: Alan Authentication

Use `alan.json` for Alan's JWT credentials. This is required because RingCentral Call Control requires the `from.deviceId` to belong to the authenticated extension.

Endpoint:

```http
POST /restapi/oauth/token
```

The script exchanges Alan's JWT for an access token using:

```text
grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
```

Status: working. Alan auth was tested successfully and maps to extension `102`.

## Step 2: Start Call From Alan To Number1

Endpoint:

```http
POST /restapi/v1.0/account/~/telephony/call-out
```

Request shape:

```json
{
  "from": {
    "deviceId": "805169921011"
  },
  "to": {
    "phoneNumber": "+18338515503"
  }
}
```

Expected response:

```http
HTTP/1.1 201 Created
```

Important values captured from the `201` response:

```text
telephonySessionId = session.id
alanCallerPartyId = session.parties[] where from.deviceId is Alan's deviceId
```

Status: working. The latest script saves the full response to `call-out-result.json`.

## Step 3: Transfer Alan's Party To Number2

After 30 seconds, transfer Alan's party to `number2`.

Endpoint:

```http
POST /restapi/v1.0/account/~/telephony/sessions/{telephonySessionId}/parties/{alanCallerPartyId}/transfer
```

Request body:

```json
{
  "phoneNumber": "+19512681518"
}
```

The `partyId` in the URL is Alan's existing call party, not `number2`. The transfer target is only in the JSON body.

Status: working. RingCentral returns Alan's party as `Disconnected` with reason `BlindTransfer`, which means Alan's leg was transferred away and `number2` should continue with `number1`.

## Step 4: Send Context Message Before Transfer

Goal:

```text
Before transferring to number2, send:
"Incoming transferred call: customer is angry about this service"
```

The current implementation attempts to send this as RingCentral SMS:

```http
POST /restapi/v1.0/account/~/extension/~/sms
```

Request shape:

```json
{
  "from": {
    "phoneNumber": "+19515822473"
  },
  "to": [
    {
      "phoneNumber": "+19512681518"
    }
  ],
  "text": "Incoming transferred call: customer is angry about this service"
}
```

Current status: blocked by RingCentral account setup.

The test failed with:

```text
HTTP 403
FeatureNotAvailable
MSG-242: The requested feature is not available
```

Reason:

```text
Outbound SMS cannot be enabled yet because the RingCentral business certificate / business verification documentation has not been updated or approved.
```

What must be enabled:

```text
1. Alan's extension/account must be allowed to send outbound SMS.
2. Alan's sending number must be SMS-capable.
3. The RingCentral developer app must include SMS permission.
4. RingCentral business SMS / registration paperwork must be completed and approved.
```

Until outbound SMS is enabled, the transfer flow can still perform the call and blind transfer, but the pre-transfer SMS message will fail unless skipped.

Standalone message test:

```powershell
python E:\Ringcentral\send_transfer_message.py
```

Call/transfer flow with message attempt enabled:

```powershell
python E:\Ringcentral\alan_call_transfer.py --wait-seconds 30
```

Call/transfer flow without message attempt:

```powershell
python E:\Ringcentral\alan_call_transfer.py --wait-seconds 30 --skip-transfer-message
```

## Step 5: Avoid Zombie Sessions

Zombie-session cleanup should be conservative:

1. List existing active call sessions first.
2. Inspect each active telephony session and its parties.
3. Only delete active parties when the session has existed for at least 5 minutes.
4. Never delete by default; require an explicit cleanup flag.
5. Never delete any session or party without explicit permission from a human being.

Human approval rule:

```text
Always show the list of existing sessions first.
Only after a human reviews that list and explicitly approves cleanup should deletion be run.
Do not automatically delete sessions as part of normal call/transfer flow.
```

The helper script uses:

```http
GET /restapi/v1.0/account/~/extension/~/active-calls?view=Detailed
GET /restapi/v1.0/account/~/telephony/sessions/{telephonySessionId}
DELETE /restapi/v1.0/account/~/telephony/sessions/{telephonySessionId}/parties/{partyId}
```

Report only:

```powershell
python E:\Ringcentral\avoid_zombie_sessions.py
```

Delete zombie parties only after they have existed for at least 5 minutes:

```powershell
python E:\Ringcentral\avoid_zombie_sessions.py --drop-active-parties
```

Before running the delete command, first run report-only mode and get human approval:

```powershell
python E:\Ringcentral\avoid_zombie_sessions.py
```

The default minimum age is `300` seconds. Override only when testing:

```powershell
python E:\Ringcentral\avoid_zombie_sessions.py --drop-active-parties --min-age-seconds 300
```

## Python Files

`alan_call_transfer.py`: Main Call Control flow. Authenticates as Alan, finds Alan's device, creates the call to default `number1`, waits, optionally sends the pre-transfer SMS message to `number2`, transfers Alan's party to default `number2`, and saves call/message/transfer responses. Use `--skip-transfer-message` while outbound SMS is unavailable.

`send_transfer_message.py`: Standalone pre-transfer message test. Attempts to send `"Incoming transferred call: customer is angry about this service"` to default `number2` using RingCentral SMS. Currently blocked by `MSG-242 FeatureNotAvailable` until business SMS verification/documents are completed.

`avoid_zombie_sessions.py`: Safety helper for zombie sessions. Lists existing active calls first, reads session party status, and optionally deletes active parties only when they are older than the configured minimum age, default `300` seconds.

`release_window_test.py`: Timing test helper. Creates a call, transfers it, polls active calls every few seconds, and records how long RingCentral keeps the disconnected session visible before it disappears from `active-calls`.

`ringcentral_steps.py`: Earlier helper script. Uses the original `rc-credentials.json` account credentials to list devices and optionally place a RingOut call. It is useful for account-level checks, but the transfer flow should use `alan_call_transfer.py`.

## JSON Files

`alan.json`: Alan's RingCentral JWT credentials and default RingOut caller/recipient values. Treat this as secret.

`rc-credentials.json`: Original RingCentral credentials from `E:\Downloads`. Useful for account-level device listing. Treat this as secret.

`device-list.json`: Latest saved RingCentral device-list response. It may be from account-level or extension-level device listing depending on the script that wrote it last.

`call-out-result.json`: Latest Call Control call-out response from `alan_call_transfer.py`. Contains `session.id` and party IDs needed for transfer or cleanup.

`transfer-result.json`: Latest transfer response. A successful blind transfer usually shows Alan's party as `Disconnected` with reason `BlindTransfer`.

`transfer-message-result.json`: Latest pre-transfer SMS response when message sending succeeds. This file may not exist or may not update while SMS is blocked by RingCentral `MSG-242 FeatureNotAvailable`.

`active-calls.json`: Latest active-calls listing saved by `avoid_zombie_sessions.py` before any cleanup attempt.

`zombie-session-check.json`: Latest detailed telephony session status saved by `avoid_zombie_sessions.py`.

`zombie-session-cleanup.json`: Latest cleanup result from `avoid_zombie_sessions.py` when `--drop-active-parties` is used.

`active-session.json`: Older debug output from manually reading a live session.

`alan-device-list.json`: Alan extension device-list output from earlier testing.

`alan-ring-out-result.json`: Latest Alan RingOut test result. RingOut can place calls, but it is not the preferred path for transfer because Call Control call-out provides cleaner session and party IDs.

`ring-out-result.json`: Earlier RingOut result from the original account credential test.

`release-window-call-out-result.json`: Call-out response from the release-window timing test.

`release-window-transfer-result.json`: Transfer response from the release-window timing test.

`release-window-active-calls.json`: Latest active-calls poll output from the release-window timing test.

`release-window-session-check.json`: Latest detailed session poll output from the release-window timing test.

## Status

The Alan Call Control flow is working with Alan's auth:

```text
call-out -> 201 response -> extract telephonySessionId and partyId -> wait -> blind transfer to number2
```

The zombie-session helper is now set up to list existing active calls before cleanup and delete only sessions older than 5 minutes when explicitly requested.

The pre-transfer SMS message feature has been added in code but is blocked by RingCentral account configuration until outbound SMS is enabled after business certificate / business verification documentation is completed.
