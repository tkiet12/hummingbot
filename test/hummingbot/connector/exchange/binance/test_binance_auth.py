import asyncio
import hashlib
import hmac
from copy import copy
from unittest import TestCase
from unittest.mock import MagicMock

from typing_extensions import Awaitable

from hummingbot.connector.exchange.binance.binance_auth import BinanceAuth
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest


class BinanceAuthTests(TestCase):

    def setUp(self) -> None:
        self._api_key = "testApiKey"
        self._secret = "testSecret"

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: float = 1):
        ret = asyncio.get_event_loop().run_until_complete(asyncio.wait_for(coroutine, timeout))
        return ret

    def test_rest_authenticate(self):
        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        params = {
            "symbol": "LTCBTC",
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": 1,
            "price": "0.1",
        }
        full_params = copy(params)

        auth = BinanceAuth(api_key=self._api_key, secret_key=self._secret, time_provider=mock_time_provider)
        request = RESTRequest(method=RESTMethod.GET, params=params, is_auth_required=True)
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        full_params.update({"timestamp": 1234567890000})
        encoded_params = "&".join([f"{key}={value}" for key, value in full_params.items()])
        expected_signature = hmac.new(
            self._secret.encode("utf-8"),
            encoded_params.encode("utf-8"),
            hashlib.sha256).hexdigest()
        self.assertEqual(now * 1e3, configured_request.params["timestamp"])
        self.assertEqual(expected_signature, configured_request.params["signature"])
        self.assertEqual({"X-MBX-APIKEY": self._api_key}, configured_request.headers)

    def test_rest_authenticate_ed25519(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        import base64

        private_key = ed25519.Ed25519PrivateKey.generate()
        pem_private_key = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ).decode("utf-8")

        now = 1234567890.000
        mock_time_provider = MagicMock()
        mock_time_provider.time.return_value = now

        params = {
            "symbol": "LTCBTC",
        }
        auth = BinanceAuth(api_key=self._api_key, secret_key=pem_private_key, time_provider=mock_time_provider)
        request = RESTRequest(method=RESTMethod.GET, params=params, is_auth_required=True)
        configured_request = self.async_run_with_timeout(auth.rest_authenticate(request))

        expected_msg = f"symbol=LTCBTC&timestamp=1234567890000".encode("utf-8")
        expected_signature = base64.b64encode(private_key.sign(expected_msg)).decode("utf-8")

        self.assertEqual(expected_signature, configured_request.params["signature"])
        self.assertEqual({"X-MBX-APIKEY": self._api_key}, configured_request.headers)
