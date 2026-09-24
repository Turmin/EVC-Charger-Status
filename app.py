import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

load_dotenv()
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
        if not self.token:
            self.token = self.request("user/guestLogin", {})["token"]
        body = {
            "locationId": "", "channelId": "", "qrCode": qr_code,
            "evseId": "", "token": self.token, "referenceGeoBounds": {},
        }
        try:
            return self.request("location/getLocationDetails", body)
        except requests.HTTPError as exc:
            if exc.response.status_code not in (401, 403):
                raise
        self.token = self.request("user/guestLogin", {})["token"]
        body["token"] = self.token
        return self.request("location/getLocationDetails", body)


config = load_config()
client = EVCClient()
poll_lock = threading.Lock()
next_poll = 0.0
errors = {}
app = FastAPI(title="EVC Charger Status API", version="2.0.0")


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


def poll():
    global next_poll
    interval = max(60, int(config.get("poll_interval_seconds", 300)))
    with poll_lock:
        if time.monotonic() < next_poll:
            return
        next_poll = time.monotonic() + interval
        for charger in config["chargers"]:
            code = charger["qr_code"]
            try:
                location = client.location(code)
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


@app.get("/health")
def health():
    return {"status": "ok", "configuredChargers": len(config["chargers"])}


@app.get("/chargers")
def chargers():
    poll()
    return {"chargers": snapshot()}


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
    global config, next_poll
    try:
        updated = load_config()
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with poll_lock:
        config = updated
        next_poll = 0.0
        errors.clear()
    return {"status": "reloaded", "configuredChargers": len(config["chargers"])}
