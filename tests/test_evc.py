import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ChargerStatusTest(unittest.TestCase):
    def test_status_since_survives_same_status_and_changes_on_transition(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "DB_PATH", Path(directory) / "status.sqlite3"
        ), patch.object(app, "config", {
            "poll_interval_seconds": 60,
            "chargers": [{"qr_code": "A", "description": "Parking", "latitude": 1}],
        }), patch.object(app, "next_poll", 0):
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
                app.next_poll = 0
                app.poll()
                self.assertEqual(app.snapshot()[0]["evses"][0]["since"], since)
                app.next_poll = 0
                app.poll()
                self.assertEqual(app.snapshot()[0]["evses"][0]["status"], "AVAILABLE")
                history = app.charger_history("A", 100)["history"]
                self.assertEqual([item["status"] for item in history],
                                 ["AVAILABLE", "CHARGING"])
                self.assertEqual(history[1]["since"], since)
                self.assertEqual(history[1]["until"], history[0]["since"])
                self.assertIsNone(history[0]["until"])
                self.assertEqual(len(app.charger_history("A", 1)["history"]), 1)
