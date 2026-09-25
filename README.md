# EVC Charger Status

A small API for tracking the current status and observed status history of configured EVC-net charging points.

## Setup

Add charging points to `config.json` under `chargers`. Each entry needs a unique `qr_code`. The optional `name`, `description`, `latitude`, and `longitude` fields are returned to map clients unchanged.

Set `EVC_API_KEY` in the environment or in a local, unshared `.env` file. `EVC_DEVICE_ID` and `EVC_BASE_URL` are optional. The API starts without a key, but `GET /chargers` reports an error for each charging point it cannot refresh. Use an API key only where your EVC-net integration permits it.

Start the server from the project directory:

```powershell
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

## Endpoints

- `GET /` returns JSON with the service status, configured charging point count, and available endpoints.
- `GET /health` returns the service status and configured charging point count.
- `GET /chargers` returns each configured point with its latest EVSE status, `retrievedAt`, `since`, and any refresh error. A refresh error leaves the last known status available; check `retrievedAt` before using it.
- `GET /chargers/live` returns `dailyLimit`, `usedToday`, `remainingToday`, `cooldownSecondsRemaining`, `available`, and `resetsAt`. It makes no upstream request and is never cached.
- `POST /chargers/live` forces a fresh fetch for all configured chargers and returns `chargers` plus the updated `liveRefresh` status. This is intended for an explicit user action such as a Live button. It allows at most 20 refreshes per Amsterdam calendar day, at least 60 seconds apart. The counter is stored beside the SQLite database and survives restarts. When a limit is reached, the API returns HTTP 429 with a `Retry-After` header and a `detail` object containing `code` (`cooldown_active` or `daily_limit_reached`), `message`, `retryAfterSeconds`, and `liveRefresh`.
- `GET /chargers/{qr_code}` returns one configured charger's current status in a `charger` object. Only that charger is refreshed when its polling interval expires; unknown QR codes return 404.
- `GET /chargers/{qr_code}/history?limit=100` returns status periods from SQLite, newest first. `since` marks the first observation of a status and `until` marks the first observation of the next status. The current period has `until: null`. The limit can be 1 to 1000. This endpoint does not call EVC-net.
- `POST /reload` reloads `config.json` without restarting the server.

### Live refresh examples

`GET /chargers/live` returns the current allowance without contacting EVC-net:

```json
{
  "dailyLimit": 20,
  "usedToday": 3,
  "remainingToday": 17,
  "cooldownSecondsRemaining": 42,
  "available": false,
  "resetsAt": "2026-09-25T00:00:00+02:00"
}
```

A successful `POST /chargers/live` returns the fresh charger data and the updated allowance:

```json
{
  "chargers": [
    {
      "qr_code": "EXAMPLE-001",
      "name": "Main entrance",
      "evses": [
        {
          "evseId": "EVSE-1",
          "status": "AVAILABLE",
          "since": "2026-09-24T07:30:00+00:00",
          "retrievedAt": "2026-09-24T07:35:00+00:00"
        }
      ],
      "error": null
    }
  ],
  "liveRefresh": {
    "dailyLimit": 20,
    "usedToday": 4,
    "remainingToday": 16,
    "cooldownSecondsRemaining": 60,
    "available": false,
    "resetsAt": "2026-09-25T00:00:00+02:00"
  }
}
```

If the cooldown is active, the API responds with HTTP 429 and `Retry-After: 42`:

```json
{
  "detail": {
    "code": "cooldown_active",
    "message": "Live refresh is available once per minute",
    "retryAfterSeconds": 42,
    "liveRefresh": {
      "dailyLimit": 20,
      "usedToday": 4,
      "remainingToday": 16,
      "cooldownSecondsRemaining": 42,
      "available": false,
      "resetsAt": "2026-09-25T00:00:00+02:00"
    }
  }
}
```

When the daily limit is reached, the same HTTP 429 shape uses `daily_limit_reached`; `retryAfterSeconds` and `Retry-After` then indicate the time until the next Amsterdam midnight.

The API preserves EVC-net's source status values. Its timestamps record when this service observed a status, not the exact moment the charger changed state. History starts with the first status recorded by this service; earlier changes cannot be reconstructed.

The API polls automatically while it is running, so history can grow even without visitors. The schedule in `config.json` uses the Europe/Amsterdam time zone: weekdays 07:00-10:00 every 5 minutes, 10:00-18:00 every 15 minutes, and 18:00-07:00 every hour. Weekends use an hourly interval. The `polling_schedule` entries define the start hour and interval in seconds; `weekend_interval_seconds` controls weekends. A schedule entry must start at hour 0, entries must be ordered, and no interval may be shorter than 5 minutes.

Regular charger requests use the most recent observation and start a fetch only when due. They wait for an in-progress fetch, and up to four upstream charger requests can run concurrently. The Live action bypasses the schedule and counts toward its separate daily limit. Scheduled and request-triggered fetches share per-charger deadlines, so they do not duplicate a due refresh. With ten chargers, the automatic schedule makes about 810 location requests per day; the 20 allowed full Live refreshes can add up to 200, plus login requests. Restarts and configuration reloads can add requests. SQLite data is stored in `status.sqlite3` by default; set `STATUS_DB_PATH` to use another path. Run a single API process to keep the in-memory polling limit and Live budget predictable.
