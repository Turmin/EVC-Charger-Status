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

- `GET /health` returns the service status and configured charging point count.
- `GET /chargers` returns each configured point with its latest EVSE status, `retrievedAt`, `since`, and any refresh error. A refresh error leaves the last known status available; check `retrievedAt` before using it.
- `GET /chargers/{qr_code}/history?limit=100` returns status periods from SQLite, newest first. `since` marks the first observation of a status and `until` marks the first observation of the next status. The current period has `until: null`. The limit can be 1 to 1000. This endpoint does not call EVC-net.
- `POST /reload` reloads `config.json` without restarting the server.

The API preserves EVC-net's source status values. Its timestamps record when this service observed a status, not the exact moment the charger changed state. History starts with the first status recorded by this service; earlier changes cannot be reconstructed.

The default refresh interval is 300 seconds, with a minimum of 60 seconds. Requests within that interval use cached data. Refreshes happen when `GET /chargers` is called. SQLite data is stored in `status.sqlite3` by default; set `STATUS_DB_PATH` to use another path. Run a single API process to keep the in-memory polling limit predictable.
