import unittest
from unittest.mock import patch

from app import EVCClient


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"status": "valid", "data": {"token": "guest-token"}}


class EVCRequestTest(unittest.TestCase):
    def test_guest_login_uses_configured_key_and_device_context(self):
        with patch.dict("os.environ", {"EVC_API_KEY": "example-key"}), patch(
            "app.requests.post", return_value=FakeResponse()
        ) as request:
            client = EVCClient()
            self.assertEqual(client.request("user/guestLogin", {})["token"],
                             "guest-token")
        self.assertEqual(
            request.call_args.args[0],
            "https://mobile-gateway.evc-net.com/api/v1/user/guestLogin",
        )
        self.assertEqual(request.call_args.kwargs["headers"]["x-api-key"],
                         "example-key")
        self.assertTrue(request.call_args.kwargs["json"]["deviceId"])
        self.assertEqual(request.call_args.kwargs["timeout"], 15)
