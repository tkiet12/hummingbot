import base64
import hashlib
import hmac
import json
from collections import OrderedDict
from typing import Any, Dict
from urllib.parse import urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSRequest


class BinanceAuth(AuthBase):
    def __init__(self, api_key: str, secret_key: str, time_provider: TimeSynchronizer):
        self.api_key = api_key
        self.secret_key = secret_key
        self.time_provider = time_provider
        self._is_ed25519 = False
        self._private_key = None

        if "-----BEGIN" in secret_key:
            try:
                self._private_key = serialization.load_pem_private_key(
                    secret_key.encode("utf-8"),
                    password=None
                )
                if isinstance(self._private_key, ed25519.Ed25519PrivateKey):
                    self._is_ed25519 = True
            except Exception:
                # Fallback to HMAC if parsing fails or key is not ED25519
                pass

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Adds the server time and the signature to the request, required for authenticated interactions. It also adds
        the required parameter in the request header.
        :param request: the request to be configured for authenticated interaction
        """
        if request.method == RESTMethod.POST:
            request.data = self.add_auth_to_params(params=json.loads(request.data))
        else:
            request.params = self.add_auth_to_params(params=request.params)

        headers = {}
        if request.headers is not None:
            headers.update(request.headers)
        headers.update(self.header_for_authentication())
        request.headers = headers

        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        """
        This method is intended to configure a websocket request to be authenticated. Binance does not use this
        functionality
        """
        return request  # pass-through

    def add_auth_to_params(self,
                           params: Dict[str, Any]):
        timestamp = int(self.time_provider.time() * 1e3)

        request_params = OrderedDict(params or {})
        request_params["timestamp"] = timestamp

        signature = self._generate_signature(params=request_params)
        request_params["signature"] = signature

        return request_params

    def header_for_authentication(self) -> Dict[str, str]:
        return {"X-MBX-APIKEY": self.api_key}

    def generate_ws_signature(self, params: Dict[str, Any]) -> str:
        """Generate HMAC-SHA256 signature for WebSocket API requests.

        WS API signing differs from REST: params are sorted alphabetically,
        not URL-encoded, and apiKey is included in the signed string.
        """
        sorted_params = sorted(params.items())
        payload = "&".join(f"{k}={v}" for k, v in sorted_params)
        return hmac.new(
            self.secret_key.encode("utf8"),
            payload.encode("utf8"),
            hashlib.sha256,
        ).hexdigest()

    def generate_ws_subscribe_params(self) -> Dict[str, Any]:
        """Build the full params dict for userDataStream.subscribe.signature."""
        timestamp = int(self.time_provider.time() * 1e3)
        params: Dict[str, Any] = {
            "apiKey": self.api_key,
            "timestamp": timestamp,
        }
        params["signature"] = self.generate_ws_signature(params)
        return params

    def _generate_signature(self, params: Dict[str, Any]) -> str:
        """Signature for REST API requests.

        REST API: sign params in insertion order (as passed), do NOT sort.
        Per Binance REST docs: the payload is the query string / request body
        exactly as constructed, without any alphabetical reordering.
        """
        encoded_params_str = urlencode(params)
        if self._is_ed25519:
            signature = self._private_key.sign(encoded_params_str.encode("utf-8"))
            return base64.b64encode(signature).decode("utf-8")
        else:
            digest = hmac.new(self.secret_key.encode("utf8"), encoded_params_str.encode("utf8"), hashlib.sha256).hexdigest()
            return digest

    def _generate_ws_signature(self, params: Dict[str, Any]) -> str:
        """Signature for WebSocket API requests.

        WS API (Ed25519): sort all params alphabetically, then encode as
        'key=value' pairs joined with '&' and sign.
        Per Binance WS API docs: Step 1 – sort params alphabetically.
        """
        sorted_params = sorted(params.items())
        encoded_params_str = urlencode(sorted_params)
        if self._is_ed25519:
            signature = self._private_key.sign(encoded_params_str.encode("utf-8"))
            return base64.b64encode(signature).decode("utf-8")
        else:
            digest = hmac.new(self.secret_key.encode("utf8"), encoded_params_str.encode("utf8"), hashlib.sha256).hexdigest()
            return digest
