import json
import time
from typing import Any, Dict, Optional

import requests

from lib.config import DEFAULT_BASE_URL, DEFAULT_TIMEOUT
from lib.debug import debug_enabled, debug_log


class JsonRpcError(RuntimeError):
    pass


class R2BridgeClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.req_id = 1
        self._session = requests.Session()
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._session.close()
        except (OSError, RuntimeError, AttributeError):
            return

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": self.req_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.req_id += 1
        url = f"{self.base_url}/mcp"
        headers = {"Content-Type": "application/json"}
        body = json.dumps(payload)

        def _resp_snippet(response: Optional[requests.Response]) -> str:
            if response is None:
                return ""
            try:
                txt = (response.text or "").strip()
            except (UnicodeDecodeError, AttributeError, TypeError):
                return ""
            if not txt:
                return ""
            if len(txt) > 800:
                txt = txt[:800] + "...(truncated)"
            return txt

        last_exc: Optional[BaseException] = None
        for attempt in range(3):
            resp: requests.Response
            try:
                resp = self._session.post(url, headers=headers, data=body, timeout=self.timeout)
                if resp.status_code in {408, 429, 502, 503, 504}:
                    snip = _resp_snippet(resp)
                    msg = f"transient http {resp.status_code}"
                    if snip:
                        msg += f" body={snip}"
                    if debug_enabled():
                        debug_log("bridge_retry",
                                  {"method": method, "attempt": attempt + 1, "status_code": resp.status_code})
                    raise requests.HTTPError(msg, response=resp)
                resp.raise_for_status()
                try:
                    data = resp.json()
                except ValueError as exc:
                    snip = _resp_snippet(resp)
                    if debug_enabled():
                        debug_log("bridge_invalid_json", {"method": method, "attempt": attempt + 1, "snippet": snip})
                    raise ValueError(f"invalid json response: {snip}") from exc
                break
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError, ValueError) as exc:
                last_exc = exc
                if debug_enabled():
                    debug_log("bridge_error", {"method": method, "attempt": attempt + 1, "error": str(exc)[:400]})
                if attempt < 2:
                    time.sleep(0.4 * (2 ** attempt))
                    continue
                raise
        else:
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("rpc retry loop ended unexpectedly")
        if isinstance(data, dict) and data.get("error") is not None:
            err_obj = data.get("error")
            raise JsonRpcError(
                f"JSON-RPC error calling {method}: {json.dumps(err_obj, ensure_ascii=False)} "
                f"(params={json.dumps(params, ensure_ascii=False)[:800] if params is not None else 'null'})"
            )
        return data

    def list_remote_tools(self) -> Dict[str, Any]:
        return self._rpc("tools/list")

    def health(self) -> str:
        url = f"{self.base_url}/health"

        def _resp_snippet(response: Optional[requests.Response]) -> str:
            if response is None:
                return ""
            try:
                txt = (response.text or "").strip()
            except (UnicodeDecodeError, AttributeError, TypeError):
                return ""
            if not txt:
                return ""
            if len(txt) > 800:
                txt = txt[:800] + "...(truncated)"
            return txt

        last_exc: Optional[BaseException] = None
        for attempt in range(3):
            resp: requests.Response
            try:
                resp = self._session.get(url, timeout=self.timeout)
                if resp.status_code in {408, 429, 502, 503, 504}:
                    snip = _resp_snippet(resp)
                    msg = f"transient http {resp.status_code}"
                    if snip:
                        msg += f" body={snip}"
                    raise requests.HTTPError(msg, response=resp)
                resp.raise_for_status()
                return resp.text.strip()
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.4 * (2 ** attempt))
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("health retry loop ended unexpectedly")

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self._rpc("tools/call", {"name": tool_name, "arguments": arguments})
