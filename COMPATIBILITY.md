# Device API compatibility — Edge backend vs new (cloud-style) devices

This document explains how **new API–style devices** (behaving like `api.boondock.cloud` per [DEVICE_API.md](./DEVICE_API.md)) relate to **this Edge backend**.

---

## Edge implementation status (implemented)

The Edge backend in this repo now supports **both** legacy Edge firmware and **cloud-style** requests on the same routes where noted.

| Cloud-style endpoint | Edge behavior |
|---------------------|---------------|
| `POST /api/v2/audio/s3` | **Yes** — same handler as v1; **default `convert_to_mp3` is true** on v2 (false on v1 if omitted). |
| `POST /api/v1/events` | **Yes** — JSON body `{ mac_address, event_type, event_data? }` when **`mac` query param is absent**. With **`?mac=`**, legacy channel/ping behavior is unchanged. |
| `POST /api/v1/upload/logs` | **Yes** — alias of `/api/V1/upload/logs`; **`.txt` / `.log` / `.json`**. |
| `GET /api/v1/firmware/check` | **Yes** — reads `firmware/firmware.json` (settings DB copy first, else repo `firmware/firmware.json`). |
| OTA download | **`GET /api/v1/firmware/download/<firmware_id>/firmware.bin`** (also `bootloader.bin`, `partitions.bin`). `download_link` in check response points at this URL on the Edge host. |

**Cloud lifecycle events** are stored in SQLite table **`cloud_device_events`** on the **recordings** database (async insert after `200`, MAC stored as **12 hex uppercase**). Those events also drive **channel visual state** (online / recording / idle / error / warning) and **offline** when no activity for ~150s.

**Dashboard device logs:** `GET /api/v1/devices/<mac>/logs/files`, `GET /api/v1/devices/<mac>/logs/content?date=YYYY-MM-DD` or `?path=…`, and `GET /api/v1/devices/<mac>/events?limit=100&types=warning,error,fatal_error` (optional `types` filter).

**Firmware OTA:** Each entry in `firmware.json` should include a **`version`** string (e.g. `1.2.0`) for comparisons, and **`firmware/firmware.bin`** must exist under that entry’s folder for an upgrade to be offered.

Differences from hosted cloud (expected): S3 bucket layout, local recording pipeline, no MySQL/Redis for events.

---

## Definitions

| Term | Meaning |
|------|--------|
| **Legacy / Edge-style device** | Firmware using `GET/POST /api/v1/events?mac=…`, `/api/v1/audio/s3`, `/api/V1/upload/logs`, settings endpoints. |
| **New / cloud-style device** | Firmware using JSON `POST /api/v1/events`, `POST /api/v2/audio/s3`, `POST /api/v1/upload/logs`, `GET /api/v1/firmware/check`. |

---

## Legacy matrix (historical — pre-implementation)

| Endpoint (new API) | Was missing on Edge |
|--------------------|---------------------|
| `POST /api/v2/audio/s3` | Now implemented |
| `POST /api/v1/events` (JSON) | Now implemented (dispatch) |
| `POST /api/v1/upload/logs` | Now implemented |
| `GET /api/v1/firmware/check` | Now implemented |

---

## Optional follow-ups (not required for basic parity)

- Mirror log uploads to S3 when enabled.
- Add `"code": "AUDIO_S3_ERROR"` on audio 500 responses.
- Shared Redis/MySQL if you need exact cloud persistence semantics.

---

## Related docs

- [DEVICE_API.md](./DEVICE_API.md) — request/response shapes.
- Code: [backend/app/routes/device_routes.py](backend/app/routes/device_routes.py), [backend/app/services/cloud_device_events.py](backend/app/services/cloud_device_events.py), [backend/app/services/firmware_device_service.py](backend/app/services/firmware_device_service.py).
