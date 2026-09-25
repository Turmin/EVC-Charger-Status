import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response

load_dotenv()
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
CONFIG_PATH = Path(__file__).with_name("config.json")
DB_PATH = Path(os.getenv("STATUS_DB_PATH", Path(__file__).with_name("status.sqlite3")))
BASE_URL = os.getenv("EVC_BASE_URL", "https://mobile-gateway.evc-net.com/api/v1").rstrip("/")
CONTEXT = {
    "locale": "nl",
    "platform": "ECQ-WEB:1.0.0--windows-Windows NT 10.0; Win64; x64",
    "serviceName": "ECQ",
    "serviceVersion": "2.11.0",
    "appIdentifier": "ECQ-WEB",
    "appVersion": "2.11.0",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_config():
    with CONFIG_PATH.open(encoding="utf-8") as source:
        data = json.load(source)
    chargers = data.get("chargers", [])
    if not isinstance(chargers, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("qr_code"), str)
        or not item["qr_code"] for item in chargers
    ):
        raise ValueError("chargers must be a list with a qr_code for each charger")
    if len({item["qr_code"] for item in chargers}) != len(chargers):
        raise ValueError("qr_code must be unique")
    schedule = data.get("polling_schedule")
    if schedule is not None:
        if not isinstance(schedule, list) or not schedule or any(
            not isinstance(item, dict)
            or type(item.get("start_hour")) is not int
            or not 0 <= item["start_hour"] < 24
            or type(item.get("interval_seconds")) is not int
            or item["interval_seconds"] < 300
            for item in schedule
        ):
            raise ValueError("polling_schedule must contain valid hours and intervals")
        hours = [item["start_hour"] for item in schedule]
        if hours[0] != 0 or hours != sorted(set(hours)):
            raise ValueError("polling_schedule must start at hour 0 and have unique ascending hours")
    weekend = data.get("weekend_interval_seconds", 3600)
    if type(weekend) is not int or weekend < 300:
        raise ValueError("weekend_interval_seconds must be at least 300")
    return data


def database():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS charger_status (
        qr_code TEXT NOT NULL, evse_id TEXT NOT NULL, status TEXT NOT NULL,
        since TEXT NOT NULL, retrieved_at TEXT NOT NULL,
        PRIMARY KEY (qr_code, evse_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        qr_code TEXT NOT NULL, evse_id TEXT NOT NULL,
        status TEXT NOT NULL, since TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS status_history_charger
        ON status_history (qr_code, evse_id, since)""")
    db.execute("""INSERT INTO status_history (qr_code, evse_id, status, since)
        SELECT current.qr_code, current.evse_id, current.status, current.since
        FROM charger_status AS current
        WHERE NOT EXISTS (
            SELECT 1 FROM status_history AS history
            WHERE history.qr_code = current.qr_code
              AND history.evse_id = current.evse_id
        )""")
    db.commit()
    return db


class EVCClient:
    def __init__(self):
        self.api_key = os.getenv("EVC_API_KEY")
        self.device_id = os.getenv("EVC_DEVICE_ID") or str(uuid.uuid4())
        self.token = None
        self.token_lock = threading.Lock()

    def request(self, path, body):
        if not self.api_key:
            raise RuntimeError("EVC_API_KEY is not configured")
        response = requests.post(
            f"{BASE_URL}/{path}",
            headers={"x-api-key": self.api_key},
            json={**CONTEXT, "deviceId": self.device_id, **body},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "valid":
            raise RuntimeError("EVC-net rejected the request")
        return payload["data"]

    def location(self, qr_code):
        with self.token_lock:
            if not self.token:
                self.token = self.request("user/guestLogin", {})["token"]
            token = self.token
        body = {
            "locationId": "", "channelId": "", "qrCode": qr_code,
            "evseId": "", "token": token, "referenceGeoBounds": {},
        }
        try:
            return self.request("location/getLocationDetails", body)
        except requests.HTTPError as exc:
            if exc.response.status_code not in (401, 403):
                raise
        with self.token_lock:
            if self.token == token:
                self.token = self.request("user/guestLogin", {})["token"]
            body["token"] = self.token
        return self.request("location/getLocationDetails", body)


config = load_config()
client = EVCClient()
poll_lock = threading.Lock()
next_poll = {}
poll_window = {}
errors = {}


def refresh_loop(stop):
    while not stop.is_set():
        try:
            poll()
        except Exception:
            logging.exception("Scheduled charger refresh failed")
        stop.wait(30)


@asynccontextmanager
async def lifespan(_app):
    stop = threading.Event()
    worker = threading.Thread(target=refresh_loop, args=(stop,), daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join()


app = FastAPI(title="EVC Charger Status API", version="2.0.0", lifespan=lifespan)


def snapshot():
    db = database()
    try:
        rows = db.execute("SELECT * FROM charger_status").fetchall()
    finally:
        db.close()
    known = {}
    for row in rows:
        known.setdefault(row["qr_code"], []).append({
            "evseId": row["evse_id"], "status": row["status"],
            "since": row["since"], "retrievedAt": row["retrieved_at"],
        })
    return [{**charger, "evses": known.get(charger["qr_code"], []),
             "error": errors.get(charger["qr_code"])}
            for charger in config["chargers"]]


def polling_interval():
    local = datetime.now(AMSTERDAM)
    window = local.date()
    schedule = config.get("polling_schedule")
    if not schedule:
        return max(900, int(config.get("poll_interval_seconds", 900))), (window, "default")
    if local.weekday() >= 5:
        return config.get("weekend_interval_seconds", 3600), (window, "weekend")
    current = max(
        (item for item in schedule if item["start_hour"] <= local.hour),
        key=lambda item: item["start_hour"],
    )
    return current["interval_seconds"], (window, current["start_hour"])


def live_refresh_status(now=None):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(AMSTERDAM)
    path = DB_PATH.with_name("live_refresh_budget.json")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Live refresh budget is unavailable") from exc
    if not isinstance(state, dict):
        raise HTTPException(status_code=503, detail="Live refresh budget is unavailable")
    try:
        last = datetime.fromisoformat(state["last_refresh_at"]) if state.get("last_refresh_at") else None
        count = state.get("count", 0) if state.get("date") == local.date().isoformat() else 0
        if type(count) is not int or count < 0 or (last and last.tzinfo is None):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="Live refresh budget is unavailable") from exc
    cooldown = max(0, math.ceil(60 - (now - last).total_seconds())) if last else 0
    reset = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), AMSTERDAM)
    remaining = max(0, 20 - count)
    return {
        "dailyLimit": 20,
        "usedToday": count,
        "remainingToday": remaining,
        "cooldownSecondsRemaining": cooldown,
        "available": remaining > 0 and cooldown == 0,
        "resetsAt": reset.isoformat(),
    }


def reserve_live_refresh():
    now = datetime.now(timezone.utc)
    status = live_refresh_status(now)
    if status["remainingToday"] == 0:
        code, message = "daily_limit_reached", "Daily live refresh limit reached"
        retry = math.ceil((datetime.fromisoformat(status["resetsAt"]) - now).total_seconds())
    elif status["cooldownSecondsRemaining"]:
        code, message = "cooldown_active", "Live refresh is available once per minute"
        retry = status["cooldownSecondsRemaining"]
    else:
        code = None
    if code:
        raise HTTPException(
            status_code=429,
            detail={"code": code, "message": message, "retryAfterSeconds": retry,
                    "liveRefresh": status},
            headers={"Retry-After": str(retry)},
        )
    updated = {
        "date": now.astimezone(AMSTERDAM).date().isoformat(),
        "count": status["usedToday"] + 1,
        "last_refresh_at": now.isoformat(),
    }
    path = DB_PATH.with_name("live_refresh_budget.json")
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(updated), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Live refresh budget is unavailable") from exc


def poll(qr_code=None, force=False):
    with poll_lock:
        interval, window = polling_interval()
        now = time.monotonic()
        due = [
            charger for charger in config["chargers"]
            if (qr_code is None or charger["qr_code"] == qr_code)
            and (force or now >= next_poll.get(charger["qr_code"], 0)
                 or poll_window.get(charger["qr_code"]) != window)
        ]
        if not due:
            return
        if force:
            reserve_live_refresh()
        for charger in due:
            next_poll[charger["qr_code"]] = now + interval
            poll_window[charger["qr_code"]] = window
        with ThreadPoolExecutor(max_workers=4) as executor:
            locations = {
                charger["qr_code"]: executor.submit(client.location, charger["qr_code"])
                for charger in due
            }
            for charger in due:
                code = charger["qr_code"]
                try:
                    location = locations[code].result()
                    if not isinstance(location, dict):
                        raise ValueError("Invalid location data")
                    observed_at = now_iso()
                    db = database()
                    try:
                        for evse in location.get("evses", []):
                            evse_id, status = evse.get("evseId"), evse.get("status")
                            if not evse_id or not status:
                                continue
                            previous = db.execute(
                                "SELECT status FROM charger_status WHERE qr_code = ? AND evse_id = ?",
                                (code, evse_id),
                            ).fetchone()
                            if previous is None or previous["status"] != status:
                                db.execute("""INSERT INTO status_history
                                    (qr_code, evse_id, status, since)
                                    VALUES (?, ?, ?, ?)""",
                                    (code, evse_id, status, observed_at))
                            db.execute("""INSERT INTO charger_status
                                (qr_code, evse_id, status, since, retrieved_at)
                                VALUES (?, ?, ?, ?, ?)
                                ON CONFLICT(qr_code, evse_id) DO UPDATE SET
                                status = excluded.status,
                                since = CASE WHEN status = excluded.status
                                    THEN since ELSE excluded.since END,
                                retrieved_at = excluded.retrieved_at""",
                                (code, evse_id, status, observed_at, observed_at))
                        db.commit()
                    finally:
                        db.close()
                    errors.pop(code, None)
                except (requests.RequestException, ValueError, KeyError,
                        RuntimeError, sqlite3.Error) as exc:
                    errors[code] = type(exc).__name__


@app.get("/")
def home():
    return {
        "status": "ok",
        "configuredChargers": len(config["chargers"]),
        "endpoints": [
            {"method": "GET", "path": "/", "description": "API overview"},
            {"method": "GET", "path": "/health", "description": "API health"},
            {"method": "GET", "path": "/chargers", "description": "Current charger statuses"},
            {"method": "GET", "path": "/chargers/live", "description": "Live refresh availability"},
            {"method": "POST", "path": "/chargers/live", "description": "Force a live refresh"},
            {"method": "GET", "path": "/chargers/{qr_code}", "description": "Current status for one charger"},
            {"method": "GET", "path": "/chargers/{qr_code}/history", "description": "Charger status history"},
            {"method": "POST", "path": "/reload", "description": "Reload configuration"},
            {"method": "GET", "path": "/docs", "description": "Interactive API documentation"},
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok", "configuredChargers": len(config["chargers"])}


@app.get("/chargers")
def chargers():
    poll()
    return {"chargers": snapshot()}


@app.get("/chargers/live")
def live_status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    with poll_lock:
        return live_refresh_status()


@app.post("/chargers/live")
def live_chargers():
    poll(force=True)
    return {"chargers": snapshot(), "liveRefresh": live_refresh_status()}


@app.get("/chargers/{qr_code}")
def charger(qr_code: str):
    if not any(item["qr_code"] == qr_code for item in config["chargers"]):
        raise HTTPException(status_code=404, detail="Charger not found")
    poll(qr_code)
    return {"charger": next(item for item in snapshot() if item["qr_code"] == qr_code)}


@app.get("/chargers/{qr_code}/history")
def charger_history(qr_code: str, limit: int = Query(100, ge=1, le=1000)):
    db = database()
    try:
        rows = db.execute("""
            SELECT evse_id, status, since, until FROM (
                SELECT evse_id, status, since,
                    LEAD(since) OVER (
                        PARTITION BY evse_id ORDER BY since, id
                    ) AS until
                FROM status_history WHERE qr_code = ?
            ) ORDER BY since DESC LIMIT ?
        """, (qr_code, limit)).fetchall()
    finally:
        db.close()
    return {"qr_code": qr_code, "history": [
        {"evseId": row["evse_id"], "status": row["status"],
         "since": row["since"], "until": row["until"]}
        for row in rows
    ]}


@app.post("/reload")
def reload_config():
    global config
    try:
        updated = load_config()
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with poll_lock:
        config = updated
        next_poll.clear()
        poll_window.clear()
        errors.clear()
    return {"status": "reloaded", "configuredChargers": len(config["chargers"])}
