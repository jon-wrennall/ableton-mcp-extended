"""ExtensionsSDKClient — optional HTTP client for the Ableton Extensions SDK bridge.

The bridge is a companion Node.js process (AbletonParameterBridge) that communicates
with Ableton Live via the official Extensions SDK introduced in Live 12.4.5 Suite.
It exposes parameter reads/writes via HTTP on port 9883.

When active, parameter tools prefer this bridge over the _Framework Remote Script
because the Extensions SDK uses async LiveAPI calls — more reliable for VST3/AU
plugins, especially on Apple Silicon.

**Setup (optional):**
  1. Build and install AbletonParameterBridge (see docs/extensions_sdk_bridge.md)
  2. Run: npx extensions-cli run --live "/Applications/Ableton Live 12 Beta.app" .
  3. The MCP server detects it automatically on next tool call

**Graceful fallback:**
  If the bridge is not running, all tools fall back to M4L → _Framework automatically.
  Nothing breaks. Use get_bridge_status to check which transport is active.

Requires:
  - Ableton Live 12.4.5+ Suite (beta)
  - Node.js v20+
  - Ableton Extensions SDK (download from ableton.com/beta)
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("AbletonBridge")

# HTTP port for the Extensions SDK bridge (distinct from M4L UDP 9878/9879,
# dashboard 9880, singleton lock 9881, and UDP real-time params 9882)
SDK_BRIDGE_HOST = "127.0.0.1"
SDK_BRIDGE_PORT = 9883


class ExtensionsSDKClient:
    """Thin HTTP client for the AbletonParameterBridge Extensions SDK server."""

    def __init__(self, host: str = SDK_BRIDGE_HOST, port: int = SDK_BRIDGE_PORT):
        self.base_url = f"http://{host}:{port}"

    def _get(self, path: str, timeout: float = 2.0) -> dict:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def _post(self, path: str, data: dict, timeout: float = 10.0) -> dict:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def is_available(self) -> bool:
        """Fast liveness check — returns True if the bridge HTTP server is responding."""
        try:
            self._get("/health", timeout=0.5)
            return True
        except Exception:
            return False

    def get_version(self) -> str:
        """Return the bridge version string, or 'unknown' if unavailable."""
        try:
            result = self._get("/health", timeout=0.5)
            return result.get("version", "unknown")
        except Exception:
            return "unknown"

    def get_tracks(self) -> dict:
        """List all tracks visible to the bridge."""
        return self._get("/tracks")

    def get_params(self, track_index: int, device_index: int) -> dict:
        """Read all parameters for a device. Returns {params: [...], device_name: str}."""
        return self._get(f"/params?track={track_index}&device={device_index}")

    def set_param(
        self,
        track_index: int,
        device_index: int,
        value: float,
        param_index: Optional[int] = None,
        param_name: Optional[str] = None,
    ) -> dict:
        """Set a single parameter by index or name."""
        body: Dict[str, Any] = {
            "track": track_index,
            "device": device_index,
            "value": value,
        }
        if param_index is not None:
            body["param_index"] = param_index
        if param_name is not None:
            body["param_name"] = param_name
        return self._post("/params", body)

    def get_snapshot(self, track_index: int, device_index: int) -> dict:
        """Capture all current parameter values as a snapshot dict."""
        return self._get(f"/snapshot?track={track_index}&device={device_index}")

    def restore_snapshot(
        self,
        track_index: int,
        device_index: int,
        params: Dict[str, float],
    ) -> dict:
        """Bulk-restore a snapshot — sets all params in one HTTP call (POST /snapshot)."""
        return self._post(
            "/snapshot",
            {"track": track_index, "device": device_index, "params": params},
            timeout=30.0,
        )


# ---------------------------------------------------------------------------
# Module-level singleton helpers
# ---------------------------------------------------------------------------

_sdk_client: Optional[ExtensionsSDKClient] = None


def get_sdk_client() -> Optional[ExtensionsSDKClient]:
    """Return the Extensions SDK client if the bridge is reachable, else None.

    Uses a cached client instance; liveness is checked via a fast /health ping.
    Returns None (not raises) so callers can fall back gracefully.
    """
    global _sdk_client
    if _sdk_client is None:
        _sdk_client = ExtensionsSDKClient()
    if _sdk_client.is_available():
        return _sdk_client
    return None
