# Boondock Edge API

The local API and device-services backend for **Boondock Edge**. This Flask and
Socket.IO application receives radio/device events and audio, manages channels
and recordings, provides optional local transcription and backup services, and
serves the bundled dashboard documentation.

> [!IMPORTANT]
> This software is a supplemental informational or hobby tool. It is **not** a
> PSAP, 911 service, emergency dispatch or notification system, or life-safety
> system. Do not rely on it to protect life or property. Read
> [SAFETY.md](SAFETY.md) before installing or using it.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Initial setup](#initial-setup)
- [Running and configuration](#running-and-configuration)
- [Using the API](#using-the-api)
- [Data and backups](#data-and-backups)
- [Troubleshooting](#troubleshooting)
- [Development and testing](#development-and-testing)
- [License](#license)

## Features

- Local REST API and Socket.IO server for the Boondock Edge dashboard.
- Support for Boondock Edge recorders, compatible cloud-style devices, Uniden
  scanners, USB audio devices, and optional GPIO controls.
- Recording ingestion, channel management, history, tags, incident reports,
  health metrics, and device logs.
- Optional local speech-to-text using `faster-whisper`.
- Optional S3 and network-drive backup workflows.
- Swagger UI and bundled operator documentation.

Device firmware compatibility and supported device endpoints are documented in
[COMPATIBILITY.md](COMPATIBILITY.md).

## Requirements

- Python 3 with `venv` and `pip` (a currently supported Python release is
  recommended).
- A writable directory for SQLite databases, recordings, and logs.
- For hardware features: compatible USB/serial/audio hardware and permission to
  access it. Linux users may need membership in groups such as `dialout` and
  `audio`.
- Platform libraries required by optional packages such as PortAudio, FFmpeg,
  and the selected Whisper backend.

The server can run on Linux or Windows. Linux is recommended for an appliance
deployment and is required for systemd-based service management.

## Quick start

```bash
git clone https://github.com/Boondock-Echo/Boondock-Edge-API.git
cd Boondock-Edge-API

python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create the initial administrator and application settings as described below,
then start the API:

```bash
python manage.py setup --config setup.json
python run.py
```

The default address is <http://localhost:4000>. Stop the foreground server with
<kbd>Ctrl</kbd>+<kbd>C</kbd>.

## Initial setup

`manage.py setup` validates a JSON setup document, creates the SQLite
databases, saves an administrator account, and applies the selected device and
inbox preferences. Create `setup.json` in the repository root:

```json
{
  "admin": {
    "email": "admin@example.com",
    "password": "replace-with-a-long-unique-password"
  },
  "selected_devices": [
    "boondock_edge",
    "uniden_scanner"
  ],
  "wifi": {
    "ssid": "your-network-name",
    "password": "your-network-password",
    "ip_address": "192.168.1.50"
  },
  "preferences": {
    "inbox_view": "continuous",
    "message_sorting": "newest"
  }
}
```

Valid `selected_devices` entries are `boondock_edge`, `uniden_scanner`,
`usb_audio`, and `gpio`. The inbox view may be `continuous` or `pagination`,
and message sorting may be `newest` or `oldest`. Provide the `wifi` object even
when no Edge recorder is selected; these host settings are saved for later use.

Run setup with:

```bash
python manage.py setup --config setup.json
```

Setup is designed to be idempotent, but running it again updates the named
administrator and installation preferences. If a Boondock Edge USB device is
selected and connected, setup also attempts to configure the compatible serial
device with the saved host network settings.

Setup also imports the bundled phrases in `app/services/hallucinations.txt`
into the hallucination-pattern database. These defaults match the whole
transcript, ignoring case and surrounding whitespace, so words such as `you`
do not flag otherwise valid sentences. Repeated setup skips defaults already
present and preserves custom patterns. This import does not reclassify existing
recordings; classification occurs when transcription text is saved.

> [!CAUTION]
> `setup.json` contains credentials. Do not commit it, share it, or leave it
> readable by other users. Delete it or move it to a protected location after
> setup.

## Running and configuration

### Development mode

Development mode enables Flask debugging and automatic reload:

```bash
PRODUCTION_MODE=false python run.py
```

On Windows Command Prompt:

```bat
set PRODUCTION_MODE=false && python run.py
```

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `FLASK_PORT` | `4000` | TCP port on which the API listens. |
| `PRODUCTION_MODE` | `true` | Use production behavior; set to `false` for debug/reload. |
| `PRODUCTION_SERVER` | `gevent` | Requested production server mode. |
| `SOCKETIO_ASYNC_MODE` | `auto` | Set to `threading` to bypass an incompatible gevent installation. |
| `BOONDOCK_DATA_ROOT` | Parent of the repository | Root for persistent databases, recordings, logs, dashboard assets, and device settings. |
| `SECRET_KEY` | Generated and persisted | Stable Flask signing secret; set an explicit high-entropy value in production. |
| `CORS_ALLOWED_ORIGINS` | `*` | Comma-separated trusted dashboard origins. Do not leave unrestricted on an exposed deployment. |

For example:

```bash
export BOONDOCK_DATA_ROOT=/var/lib/boondock-edge
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export CORS_ALLOWED_ORIGINS=https://edge.example.com
python run.py
```

The application binds to all interfaces (`0.0.0.0`). Use a host firewall and a
TLS-terminating reverse proxy when making it available outside a trusted LAN.
Do not expose device, administration, Swagger, or recording endpoints directly
to the public internet.

## Using the API

After startup, the following entry points are useful:

| URL | Description |
| --- | --- |
| `/` | Dashboard, when dashboard assets exist under the data root. |
| `/api-docs` | Interactive Swagger API reference. |
| `/apispec.json` | Machine-readable Swagger specification. |
| `/docs/` | Bundled Boondock Edge documentation. |
| `/api/settings/ping` | Simple API connectivity check. |
| `/api/health/system?current=true` | Current system health metrics. |

Most administration endpoints use a bearer session token obtained through the
authentication API. External integrations can use scoped API keys created by
an administrator. Send either credential in the standard header:

```text
Authorization: Bearer <token-or-api-key>
```

Consult Swagger for current request and response schemas. For device-facing
firmware, event, audio, and log endpoint behavior, see
[COMPATIBILITY.md](COMPATIBILITY.md).

## Data and backups

By default, persistent data is written to the parent directory of this checkout.
Set `BOONDOCK_DATA_ROOT` explicitly so upgrades and deployments do not move or
overwrite operational data. The data root may contain:

```text
db/                 SQLite settings, recordings metadata, logs, and secret key
recordings/         Received and processed audio
logs/               Application and device logs
dashboard/          Optional dashboard build
docs/               Downloadable documentation files
```

Device settings reported to `POST /api/device/settings` update the channel selected
by `mac_address` in SQLite. The legacy form fields (`mac_address` and a JSON
string named `settings`) remain supported. `GET /api/device/settings/<mac>` reads
that channel and returns supported settings in the firmware schema: `a.ath`,
`a.stm`, `a.mrm`, `a.xrm`, `a.cg`, `ho`, and `cip.ip`. Other firmware fields are
not stored by this endpoint. Device reports do not initiate an outbound sync.
Legacy `device_settings/<MAC>.json` files are no longer read or written.
The `/api/v1/settings` POST and `/api/v1/settings/<mac>` GET routes remain
aliases during migration. Both route families accept a trailing slash.

Channels persist a `synced` boolean (initially false). A user channel save sets
it to false; a successful USB sync sets it to true. On an authenticated
`POST /api/v1/events`, a false flag includes the channel's firmware `settings`
object in the successful response and sets the flag to true. Later events omit
`settings` until another user save. Payloads over 4096 serialized bytes remain
pending. Device settings reports preserve the flag. Event delivery tracks one
response attempt, not acknowledgment that the device applied the settings.
Clean installs define `synced` in the channel schema. Existing installations
must run `python -m migrations.channel_settings_sync` from the API repository
before starting the updated API. This standalone migration adds the column
without requiring changes to `manage.py`.

Successful transcriptions are matched against configured group keywords when
saved. Each recording stores `matched_keywords`, including the pattern, matching
options, occurrence count, and highlight ranges (UTF-16 offsets). Inbox responses
include only matches available through the requesting user's groups. The dashboard
uses these saved matches for highlighting, sidebar counts, and filtering.
Editing or retranscribing a recording replaces its matches; adding a keyword
does not backfill historical recordings. Actions can be configured as described below.
Existing installations must run `python -m migrations.keyword_actions` before
starting the updated API; `manage.py upgrade` also applies this migration.

### Keyword actions and email

In **Settings → Keyword tracking → Actions**, administrators create reusable
popup or email actions. There are no action group, enabled, or trigger-keyword
controls. On the **Keywords** screen, check actions when adding a keyword, or
use **Edit actions** to change an existing keyword's selections. An action is
active for a keyword precisely while it is selected there. The same action can
be reused by multiple keywords and groups.

Actions are queued when the automatic transcription pipeline receives a result. Access
comes from the keyword's groups: their channel ownership must include the
recording's channel. Duplicate recordings and hallucinations do not trigger
actions. Manual edits, version restores, and user-requested retranscription only
update the transcript and its matches; they never queue actions. The incoming
transcription handler saves the result and queues matching actions in the same
transaction. There is no first-result flag or additional tracking in the database.
Creating or assigning an action does not scan existing recordings. Different
actions are independent.

Popup actions appear while an authorized group member has the inbox open, even
with live mode off. Existing historical events are not replayed on initial page
load. Audio playback is optional and depends on browser autoplay permissions.

Configure **Email delivery** with SMTP routing only: host and port,
STARTTLS/TLS or a local relay, optional username/password, and the dashboard
base URL used for links. Email is absent from the action-type dropdown until
routing is configured. Each email action owns its From address/name, Reply-To,
To/Cc/Bcc recipients, subject, and plain-text or HTML message template. Sender
and recipient fields are never stored in SMTP routing settings.

Recording alert emails include the original WAV file as an attachment, alongside
any recording link in the template. Recipients can play the attachment without
logging into the dashboard. Missing or unreadable audio causes delivery to retry
and eventually show as failed; alerts are not silently sent without their audio.
SMTP test messages have no recording attachment.

Blank passwords preserve the saved password; the separate clear option removes
it. Passwords are never returned by the email settings API. The test-email button
on an email action sends a real test message from that action's sender to the
supplied test recipient. Template preview uses sample values and sends no email.

Templates support `{keyword}`, `{keywords}`, `{matched_text}`, `{transcript}`,
`{recording_id}`, `{channel}`, `{timestamp}`, `{recording_url}`, and
`{dashboard_url}`. Times use UTC; HTML substitutions are escaped. Literal braces
must be doubled. Recording links open the dashboard's authenticated recording
page and preserve their destination through login. Recipients still need access.

Action events are committed in the same transaction as the recording. Email is
sent by a background outbox worker started by `run.py`, so SMTP failures do not
block transcription. Pending messages survive restarts. Delivery failures retry
up to three attempts, with 30- and 60-second delays; partial recipient acceptance
is marked failed without resending to accepted recipients. SMTP acceptance is
reported as sent, not a guarantee of inbox delivery. A crash after SMTP acceptance
but before committing the sent state can cause a retry; retries reuse Message-ID.
Removing SMTP routing pauses queued delivery. Removing the matching keyword
assignments or deleting an action cancels its pending deliveries when processed. The Actions page shows recent
delivery states and error categories without SMTP replies or credentials.

Before starting the updated API on an existing installation, run
`python -m migrations.keyword_actions`, or the normal `python manage.py upgrade`.
Fresh installs define the outbox directly; the standalone migration only adds
the new outbox and does not alter historical transcripts or matches.

Action definitions live in the `keyword_actions` table in settings.db, keyed by
stable action ID. The `keyword_action_links` table stores keyword selections.
The single `migrations/keyword_actions.py` upgrade handles matched keywords,
action definitions, keyword selections, SMTP routing conversion, and the delivery
outbox. It upgrades legacy settings lists and group-scoped action tables directly
to the current schema. Previously upgraded databases can run it again safely.
Action IDs, selections, historical matches, and queued deliveries are preserved.
Fresh installs define the current schema in their database initializers.

Admin endpoints are `/api/settings/email` (GET/PATCH),
`/api/settings/email/test` (POST), `/api/keyword-actions` (GET/POST),
`/api/keyword-actions/<id>` (PATCH/DELETE), `/api/keyword-actions/preview` (POST),
`/api/keyword-actions/deliveries` (GET, latest 50), and
`/api/keywords/<id>/actions` (PUT with `action_ids`). Settings use the existing
settings repository; delivery events live in the recordings database. Additional
action types can be added without changing the keyword matching contract.

### Kasa device keyword actions

Install the updated requirements and run `python -m migrations.keyword_actions`
before restarting the API. The migration expands the allowed action kinds while
preserving action IDs, keyword selections, and queued deliveries. Rebuild the
dashboard to expose the new configuration.

The integration requires `python-kasa` 0.12.x. Earlier 0.10.x releases choose
the wrong KLAP handshake for some IOT-family devices advertising login version 2,
which can reject valid credentials. See the upstream
[KLAP v2 issue](https://github.com/python-kasa/python-kasa/issues/1648).
After applying an update, install the requirements in the API's Python environment
with `python -m pip install --upgrade -r requirements.txt`, restart the API, and
discover again. Applying source changes alone does not upgrade the dependency.
IOT devices are connected using their system information so encrypted HS300/KP200
strips retain their individual outlets instead of appearing as single plugs.

An authentication error does not establish that the password is wrong. Check
that credentials are saved, use the owning TP-Link account email with its original
case, and check Third-Party Compatibility in the vendor app when available.
Firmware or library incompatibilities can also reject local authentication.

Mixed networks can contain devices with and without local authentication.
Legacy XOR devices keep their discovered connection and do not use the saved
account credentials. When an IOT device advertises an authenticated protocol but
rejects authentication, the API also attempts a read-only legacy connection on
TCP port 9999 without credentials. It selects that connection only if the device
read succeeds. Modern SMART devices do not use this fallback. Working
authenticated connections remain preferred, and protocol selection never
switches an outlet. If the device no longer supports legacy access, the original
authentication error remains; the fallback cannot repair account or firmware
problems on devices requiring authentication.

On the Actions screen, choose **Kasa device** and click **Discover Kasa devices**.
Choose a discovered device by name, model, and IP address, or enter its IPv4
address manually and click **Read outlets**. For devices requiring authentication,
first save the TP-Link/Kasa account email and password in **Kasa account**.
Select the named outlet and **Turn on** or **Turn off**.
**Test action** immediately executes that command and reads back the outlet state.
Reading outlets and saving an action do not switch any sockets. Save the action,
then select it on a keyword to activate automatic execution. Outlet IDs remain
stable when their display names change. Reserve the strip's IP in your router.

The API host must reach the devices on their local network. Discovery uses UDP
broadcast on ports 9999 and 20002; legacy control uses TCP port 9999 and
newer devices use their supported local authentication protocols and ports
(typically TCP 80 or 443).
Discovery normally stays within the API host's subnet and may not cross VLANs,
VPNs, or container bridges. A manual IP can work when broadcast discovery cannot,
provided targeted UDP discovery and TCP control can reach the device.

This integration uses `python-kasa` and supports on/off control for compatible
legacy and authenticated smart Kasa plugs, power strips (including HS300), bulbs,
light strips, and wall switches/dimmers supported by the installed library.
A single device appears as one selectable target; power strips
expose their individual outlets. Older devices can work without credentials.
Newer devices authenticate locally using the saved TP-Link/Kasa account email
and password. No Alexa Routine is required. Authentication failures and devices
without supported on/off controls are listed as unavailable with a reason.
Unreadable devices do not
hide successfully discovered devices. Unknown models that python-kasa cannot
discover are not listed. USB charging ports are not switchable outlets.

Kasa credentials are configured through admin-only `GET/PATCH /api/settings/kasa`.
PATCH accepts `username`, `password`, and `clear_password`. Blank or omitted
passwords preserve the saved password; clearing explicitly removes it. Changing
accounts requires entering a new password. Settings responses expose only the
username and configured indicators. Credentials are stored in the API settings
database, consistently with SMTP settings; they are never returned by the API,
embedded in actions, or copied into delivery events. Discovery, manual reads,
test commands, and queued deliveries use the currently saved account credentials.
Saving credentials does not validate them or switch a device; discover or read
a device to verify authentication.

Kasa actions use the existing transactional outbox independently of SMTP setup,
with the same delivery history, three-attempt retry limit, and cancellation when
keyword assignments are removed. Explicit on/off commands are safe to repeat
when a connection fails after a device accepts a command. A queued delivery uses
the device, outlet, and command saved when it was triggered. For immediate
cancellation, remove the keyword assignment or delete the action.

Admin-only endpoints: `POST /api/keyword-actions/kasa/discover` accepts an empty
JSON object and returns discovered devices and their available targets.
`POST /api/keyword-actions/kasa/outlets` accepts
`{"device_host":"192.168.1.50"}`; `POST /api/keyword-actions/kasa/test` also accepts
`outlet_id` and `command` (`turn_on` or `turn_off`). Create or edit actions through
the existing action endpoints with `kind: "kasa"` and those three fields.
Device reads and commands have a 15-second timeout. A failed verification does
not prove that the outlet stayed unchanged; read outlets to check its state.

USB writes and Wi-Fi responses share the same outbound mapping and numeric
types. USB merges those fields into its last full device export; Wi-Fi returns
only the supported fields. Speaker and audio-stream settings are currently
stored on the channel but have no outbound firmware mapping in either path.

Back up the entire data root while writes are stopped, or use the application's
configured S3/network-drive backup features. Protect backups as sensitive data:
they may contain credentials, operational configuration, logs, transcriptions,
and recorded audio. Test restores regularly; a successful upload alone is not a
verified backup.

## Troubleshooting

### The server does not start

1. Activate the intended virtual environment and reinstall dependencies:
   `python -m pip install -r requirements.txt`.
2. Confirm the configured port is free. Change it with, for example,
   `FLASK_PORT=4001 python run.py`.
3. Confirm the user can create and write to `BOONDOCK_DATA_ROOT`, especially its
   `db`, `logs`, and `recordings` directories.
4. Read the final exception in the console output rather than only the first
   warning; optional hardware services may warn without stopping the API.

### `Illegal instruction`, gevent, or WebSocket startup errors

Some older CPUs cannot execute instructions used by a prebuilt gevent/greenlet
wheel. The application probes gevent before using it and normally falls back to
threading. Force that safe fallback when diagnosing the issue:

```bash
SOCKETIO_ASYNC_MODE=threading python run.py
```

### USB, scanner, or serial device is missing

- Check the cable, power, and device enumeration (`python -m serial.tools.list_ports`).
- On Linux, verify group membership and permissions for `/dev/ttyUSB*` or
  `/dev/ttyACM*`, then sign out and back in after changing groups.
- Stop other programs that may have opened the serial port.
- Re-run initial setup with the correct item in `selected_devices`.

### Audio capture or transcription fails

- Verify the audio device appears to the operating system and is not exclusively
  held by another process.
- Install the platform's PortAudio and FFmpeg runtime packages when required.
- Confirm there is enough free disk space and memory. Whisper model downloads
  and inference can be large and slow, especially without supported acceleration.
- Review application logs and the saved transcription settings before retrying.

### Dashboard is blank or returns 404

The API checkout includes the backend and documentation, but the dashboard is
served from `dashboard/` under `BOONDOCK_DATA_ROOT`. Ensure a compatible
dashboard build is installed there. The Swagger UI at `/api-docs` remains the
best way to validate the backend independently.

### Browser requests are blocked by CORS

Set `CORS_ALLOWED_ORIGINS` to the exact scheme and host used by the dashboard.
Multiple origins are comma-separated. Restart the API after changing it:

```bash
CORS_ALLOWED_ORIGINS=https://edge.example.com,http://192.168.1.50:3000 python run.py
```

### Authentication stops working after restart

Set a stable `SECRET_KEY` and ensure the process can read its data root. Without
an explicit key, the application persists one at `db/.secret_key`; deleting or
changing that file invalidates existing sessions.

## Development and testing

Install the dependencies, then run the regression suite from the repository
root:

```bash
python -m pytest -q
```

Keep changes focused and add tests for behavior changes. Contributions are not
currently accepted until the project's contributor agreement process is active;
read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Release
history is maintained in [RELEASE_NOTES.md](RELEASE_NOTES.md).

## License

Copyright © 2026 Boondock Technologies, LLC. All rights reserved.

This repository is **source-available, not open source** as that term is defined
by the Open Source Initiative. Personal and non-commercial educational use is
available only under the conditions in [LICENSE.md](LICENSE.md). Commercial use
requires a separate written commercial license; see
[COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

The license prohibits use as or within emergency dispatch, 911/PSAP,
life-safety, or property-safety systems. Distribution requires attribution,
preservation of legal notices, inclusion of the license and `NOTICE`, and the
other conditions stated in the license. This summary is not a substitute for
the license; if it conflicts with `LICENSE.md`, the license controls.

Commercial licensing and permission requests:

- Website: <https://boondockecho.com>
- Email: riki@boondocktechnologies.com
