import asyncio
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Response

import app


class ChargerStatusTest(unittest.TestCase):
    def test_live_status_reports_remaining_budget_without_fetching(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "DB_PATH", Path(directory) / "status.sqlite3"
        ), patch.object(app.client, "location") as fetch:
            response = Response()
            status = app.live_status(response)
        self.assertEqual(status["dailyLimit"], 20)
        self.assertEqual(status["usedToday"], 0)
        self.assertEqual(status["remainingToday"], 20)
        self.assertEqual(status["cooldownSecondsRemaining"], 0)
        self.assertTrue(status["available"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        fetch.assert_not_called()

    def test_live_status_counts_down_cooldown(self):
        now = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "DB_PATH", Path(directory) / "status.sqlite3"
        ):
            (Path(directory) / "live_refresh_budget.json").write_text(json.dumps({
                "date": now.astimezone(app.AMSTERDAM).date().isoformat(),
                "count": 3,
                "last_refresh_at": (now - timedelta(seconds=20.2)).isoformat(),
            }))
            status = app.live_refresh_status(now)
        self.assertEqual(status["usedToday"], 3)
        self.assertEqual(status["remainingToday"], 17)
        self.assertEqual(status["cooldownSecondsRemaining"], 40)
        self.assertFalse(status["available"])
        self.assertEqual(status["resetsAt"], "2026-09-25T00:00:00+02:00")

    def test_invalid_schedule_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "chargers": [],
                "polling_schedule": [
                    {"start_hour": 7, "interval_seconds": 300},
                    {"start_hour": 0, "interval_seconds": 3600},
                ],
            }))
            with patch.object(app, "CONFIG_PATH", path):
                with self.assertRaises(ValueError):
                    app.load_config()

    def test_legacy_interval_applies_on_weekends(self):
        with patch.object(app, "config", {"poll_interval_seconds": 1200}), patch.object(
            app, "datetime"
        ) as clock:
            clock.now.return_value = datetime(2026, 9, 26, 8, 0, tzinfo=app.AMSTERDAM)
            interval, window = app.polling_interval()
        self.assertEqual(interval, 1200)
        self.assertEqual(window[1], "default")

    def test_weekday_schedule_and_weekend_interval(self):
        schedule = {
            "polling_schedule": [
                {"start_hour": 0, "interval_seconds": 3600},
                {"start_hour": 7, "interval_seconds": 300},
                {"start_hour": 10, "interval_seconds": 900},
                {"start_hour": 18, "interval_seconds": 3600},
            ],
            "weekend_interval_seconds": 3600,
        }
        cases = [
            (datetime(2026, 9, 24, 6, 59, tzinfo=app.AMSTERDAM), 3600, 0),
            (datetime(2026, 9, 24, 7, 0, tzinfo=app.AMSTERDAM), 300, 7),
            (datetime(2026, 9, 24, 10, 0, tzinfo=app.AMSTERDAM), 900, 10),
            (datetime(2026, 9, 24, 18, 0, tzinfo=app.AMSTERDAM), 3600, 18),
            (datetime(2026, 9, 26, 8, 0, tzinfo=app.AMSTERDAM), 3600, "weekend"),
        ]
        with patch.object(app, "config", schedule), patch.object(app, "datetime") as clock:
            for moment, expected_interval, expected_window in cases:
                clock.now.return_value = moment
                interval, window = app.polling_interval()
                self.assertEqual(interval, expected_interval)
                self.assertEqual(window, (moment.date(), expected_window))

    def test_schedule_boundary_triggers_refresh(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "DB_PATH", Path(directory) / "status.sqlite3"
        ), patch.object(app, "config", {"chargers": [{"qr_code": "A"}]}), patch.object(
            app, "next_poll", {}
        ), patch.object(app, "poll_window", {}), patch.object(
            app, "polling_interval",
            side_effect=[(3600, ("day", 0)), (300, ("day", 7))],
        ), patch.object(app.time, "monotonic", return_value=1000), patch.object(
            app.client, "location", return_value={"evses": []}
        ) as fetch:
            app.poll()
            app.poll()
            self.assertEqual(fetch.call_count, 2)

    def test_live_refresh_has_cooldown_and_persistent_budget(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "DB_PATH", Path(directory) / "status.sqlite3"
        ), patch.object(app, "config", {
            "chargers": [{"qr_code": "A"}, {"qr_code": "B"}],
        }), patch.object(app, "next_poll", {}), patch.object(
            app, "poll_window", {}
        ), patch.object(app.client, "location", return_value={"evses": []}) as fetch:
            response = app.live_chargers()
            self.assertEqual(len(response["chargers"]), 2)
            self.assertEqual(response["liveRefresh"]["usedToday"], 1)
            self.assertEqual(fetch.call_count, 2)
            budget = json.loads((Path(directory) / "live_refresh_budget.json").read_text())
            self.assertEqual(budget["count"], 1)
            with self.assertRaises(HTTPException) as error:
                app.live_chargers()
            self.assertEqual(error.exception.status_code, 429)
            self.assertEqual(error.exception.detail["code"], "cooldown_active")
            self.assertGreater(error.exception.detail["retryAfterSeconds"], 0)
            self.assertEqual(error.exception.headers["Retry-After"],
                             str(error.exception.detail["retryAfterSeconds"]))
            self.assertEqual(error.exception.detail["liveRefresh"]["usedToday"], 1)
            self.assertEqual(fetch.call_count, 2)

    def test_live_refresh_budget_resets_on_next_day(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "DB_PATH", Path(directory) / "status.sqlite3"
        ), patch.object(app, "config", {"chargers": [{"qr_code": "A"}]}), patch.object(
            app, "next_poll", {}
        ), patch.object(app, "poll_window", {}), patch.object(
            app.client, "location", return_value={"evses": []}
        ) as fetch:
            now = datetime.now(timezone.utc)
            path = Path(directory) / "live_refresh_budget.json"
            path.write_text(json.dumps({
                "date": (now.astimezone(app.AMSTERDAM).date() - timedelta(days=1)).isoformat(),
                "count": 20,
                "last_refresh_at": (now - timedelta(minutes=2)).isoformat(),
            }))
            app.live_chargers()
            self.assertEqual(json.loads(path.read_text())["count"], 1)
            self.assertEqual(fetch.call_count, 1)

    def test_live_refresh_has_daily_limit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "DB_PATH", Path(directory) / "status.sqlite3"
        ), patch.object(app, "config", {"chargers": [{"qr_code": "A"}]}), patch.object(
            app, "next_poll", {}
        ), patch.object(app, "poll_window", {}), patch.object(
            app.client, "location"
        ) as fetch:
            now = datetime.now(timezone.utc)
            (Path(directory) / "live_refresh_budget.json").write_text(json.dumps({
                "date": now.astimezone(app.AMSTERDAM).date().isoformat(),
                "count": 20,
                "last_refresh_at": (now - timedelta(minutes=2)).isoformat(),
            }))
            with self.assertRaises(HTTPException) as error:
                app.live_chargers()
            self.assertEqual(error.exception.status_code, 429)
            self.assertEqual(error.exception.detail["code"], "daily_limit_reached")
            self.assertGreater(error.exception.detail["retryAfterSeconds"], 0)
            self.assertEqual(error.exception.headers["Retry-After"],
                             str(error.exception.detail["retryAfterSeconds"]))
            self.assertEqual(error.exception.detail["liveRefresh"]["remainingToday"], 0)
            fetch.assert_not_called()

    def test_lifespan_starts_scheduled_refresh(self):
        started = threading.Event()

        async def run_lifespan():
            async with app.lifespan(app.app):
                self.assertTrue(started.wait(1))

        with patch.object(app, "poll", side_effect=started.set):
            asyncio.run(run_lifespan())

    def test_scheduled_loop_runs_without_requests(self):
        class StopAfterOnePoll:
            calls = 0

            def is_set(self):
                return self.calls > 0

            def wait(self, seconds):
                self.calls += 1

        with patch.object(app, "poll") as fetch:
            app.refresh_loop(StopAfterOnePoll())
            fetch.assert_called_once_with()

    def test_poll_interval_has_fifteen_minute_minimum(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "DB_PATH", Path(directory) / "status.sqlite3"
        ), patch.object(app, "config", {
            "poll_interval_seconds": 60,
            "chargers": [{"qr_code": "A"}],
        }), patch.object(app, "next_poll", {}), patch.object(
            app.time, "monotonic", return_value=1000
        ), patch.object(app.client, "location", return_value={"evses": []}) as fetch:
            app.poll()
            self.assertEqual(app.next_poll["A"], 1900)
            app.poll()
            self.assertEqual(fetch.call_count, 1)

    def test_single_charger_refreshes_only_requested_code(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "DB_PATH", Path(directory) / "status.sqlite3"
        ), patch.object(app, "config", {
            "poll_interval_seconds": 60,
            "chargers": [{"qr_code": "A"}, {"qr_code": "B"}],
        }), patch.object(app, "next_poll", {}), patch.object(
            app.client, "location",
            side_effect=lambda code: {"evses": [{"evseId": code, "status": "AVAILABLE"}]},
        ) as fetch:
            selected = app.charger("A")["charger"]
            self.assertEqual(selected["qr_code"], "A")
            self.assertEqual(selected["evses"][0]["evseId"], "A")
            self.assertEqual(fetch.call_args_list[0].args, ("A",))
            self.assertEqual(fetch.call_count, 1)

            all_chargers = app.chargers()["chargers"]
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(fetch.call_args_list[1].args, ("B",))
            self.assertEqual([item["evses"][0]["evseId"] for item in all_chargers],
                             ["A", "B"])

    def test_single_charger_rejects_unknown_code(self):
        with patch.object(app, "config", {"chargers": [{"qr_code": "A"}]}), patch.object(
            app.client, "location"
        ) as fetch:
            with self.assertRaises(HTTPException) as error:
                app.charger("unknown")
        self.assertEqual(error.exception.status_code, 404)
        fetch.assert_not_called()

    def test_chargers_waits_for_parallel_refresh(self):
        barrier = threading.Barrier(2, timeout=2)

        def location(code):
            barrier.wait()
            return {"evses": [{"evseId": code, "status": "AVAILABLE"}]}

        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "DB_PATH", Path(directory) / "status.sqlite3"
        ), patch.object(app, "config", {
            "poll_interval_seconds": 60,
            "chargers": [{"qr_code": "A"}, {"qr_code": "B"}],
        }), patch.object(app, "next_poll", {}), patch.object(
            app.client, "location", side_effect=location
        ):
            response = app.chargers()

        self.assertEqual(
            [charger["evses"][0]["evseId"] for charger in response["chargers"]],
            ["A", "B"],
        )

    def test_parallel_locations_share_one_login(self):
        client = app.EVCClient()
        logins = []

        def request(path, body):
            if path == "user/guestLogin":
                logins.append(path)
                time.sleep(0.05)
                return {"token": "test-token"}
            self.assertEqual(body["token"], "test-token")
            return {"evses": []}

        with patch.object(client, "request", side_effect=request):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(client.location, ["A", "B"]))

        self.assertEqual(results, [{"evses": []}, {"evses": []}])
        self.assertEqual(len(logins), 1)

    def test_home_lists_status_and_endpoints(self):
        with patch.object(app, "config", {"chargers": [{"qr_code": "A"}]}):
            response = app.home()

        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["configuredChargers"], 1)
        self.assertIn({"method": "GET", "path": "/health", "description": "API health"},
                      response["endpoints"])
        self.assertIn({"method": "GET", "path": "/chargers/live",
                       "description": "Live refresh availability"}, response["endpoints"])
        self.assertIn({"method": "POST", "path": "/chargers/live",
                       "description": "Force a live refresh"}, response["endpoints"])
        self.assertIn({"method": "GET", "path": "/chargers/{qr_code}",
                       "description": "Current status for one charger"}, response["endpoints"])
        self.assertIn({"method": "POST", "path": "/reload",
                       "description": "Reload configuration"}, response["endpoints"])

    def test_status_since_survives_same_status_and_changes_on_transition(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "DB_PATH", Path(directory) / "status.sqlite3"
        ), patch.object(app, "config", {
            "poll_interval_seconds": 60,
            "chargers": [{"qr_code": "A", "description": "Parking", "latitude": 1}],
        }), patch.object(app, "next_poll", {}):
            with patch.object(app.client, "location", side_effect=[
                {"evses": [{"evseId": "E1", "status": "CHARGING"}]},
                {"evses": [{"evseId": "E1", "status": "CHARGING"}]},
                {"evses": [{"evseId": "E1", "status": "AVAILABLE"}]},
            ]) as fetch:
                app.poll()
                first = app.snapshot()[0]
                self.assertEqual(first["description"], "Parking")
                self.assertEqual(first["evses"][0]["status"], "CHARGING")
                since = first["evses"][0]["since"]
                app.poll()
                self.assertEqual(fetch.call_count, 1)
                app.next_poll["A"] = 0
                app.poll()
                self.assertEqual(app.snapshot()[0]["evses"][0]["since"], since)
                app.next_poll["A"] = 0
                app.poll()
                self.assertEqual(app.snapshot()[0]["evses"][0]["status"], "AVAILABLE")
                history = app.charger_history("A", 100)["history"]
                self.assertEqual([item["status"] for item in history],
                                 ["AVAILABLE", "CHARGING"])
                self.assertEqual(history[1]["since"], since)
                self.assertEqual(history[1]["until"], history[0]["since"])
                self.assertIsNone(history[0]["until"])
                self.assertEqual(len(app.charger_history("A", 1)["history"]), 1)
