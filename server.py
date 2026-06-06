# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
import time
import urllib.request
import urllib.error
import os
import glob
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Union, Optional

try:
    import mido
    HAS_MIDO = True
except ImportError:
    HAS_MIDO = False

def _i(v) -> int:
    """Coerce string or int to int — handles Llama 3.x sending ints as strings."""
    try: return int(v)
    except: return 0

def _f(v) -> float:
    """Coerce string or float to float."""
    try: return float(v)
    except: return 0.0

try:
    from rapidfuzz import fuzz as _fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")

# Browser cache: stores flattened item list + timestamp
_browser_cache: Optional[List[dict]] = None
_browser_cache_time: float = 0.0
BROWSER_CACHE_TTL = 120  # seconds before cache expires

# ── AbletonParameterBridge (Extensions SDK HTTP bridge) ──────────────────────
# Optional companion — enhances parameter access when the AbletonParameterBridge
# Extension is active in Live 12.4.5 Suite. Falls back to _Framework if not running.
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 9878

# Server-side snapshot store: key = "track:device:name" → {param_name: value}
_mcp_snapshots: Dict[str, Dict[str, float]] = {}


class ParameterBridgeClient:
    """HTTP client for the AbletonParameterBridge Extensions SDK server (port 9878)."""

    def __init__(self, host: str = BRIDGE_HOST, port: int = BRIDGE_PORT):
        self.base_url = f"http://{host}:{port}"

    def _get(self, path: str, timeout: float = 5.0) -> dict:
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
        """Fast liveness check — returns True if bridge is running."""
        try:
            self._get("/health", timeout=1.0)
            return True
        except Exception:
            return False

    def get_tracks(self) -> dict:
        return self._get("/tracks")

    def get_params(self, track_index: int, device_index: int) -> dict:
        return self._get(f"/params?track={track_index}&device={device_index}")

    def set_param(self, track_index: int, device_index: int, value: float,
                  param_index: Optional[int] = None, param_name: Optional[str] = None) -> dict:
        body: Dict[str, Any] = {"track": track_index, "device": device_index, "value": value}
        if param_index is not None:
            body["param_index"] = param_index
        if param_name is not None:
            body["param_name"] = param_name
        return self._post("/params", body)

    def get_snapshot(self, track_index: int, device_index: int) -> dict:
        return self._get(f"/snapshot?track={track_index}&device={device_index}")

    def restore_snapshot(self, track_index: int, device_index: int,
                         params: Dict[str, float]) -> dict:
        """Bulk restore — sets all params in one call via POST /snapshot."""
        return self._post("/snapshot", {
            "track": track_index,
            "device": device_index,
            "params": params,
        }, timeout=30.0)


# Singleton bridge client
_bridge_client: Optional[ParameterBridgeClient] = None


def get_bridge_client() -> Optional[ParameterBridgeClient]:
    """Return the bridge client if the Extension HTTP server is reachable, else None."""
    global _bridge_client
    if _bridge_client is None:
        _bridge_client = ParameterBridgeClient()
    if _bridge_client.is_available():
        return _bridge_client
    return None


# ── MIDI CC Controller ────────────────────────────────────────────────────────
# Sends MIDI CC to a virtual port ("AbletonMCP") to control plugin parameters
# that aren't exposed through the VST3/AU parameter interface (e.g. Arturia, NI).
#
# Setup in Ableton: set each plugin track's MIDI input to "AbletonMCP" on its
# assigned channel. Default: track_index + 1 maps to MIDI channel 1-16.

CC_MAPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "midi_cc")
MIDI_PORT_NAME = "AbletonMCP"

# track_index → assigned MIDI channel (1-16)
_track_cc_channels: Dict[int, int] = {}

# Loaded CC maps: {filename_stem: map_dict}
_cc_maps: Dict[str, dict] = {}
_cc_maps_loaded = False

# plugin device name → cc map (populated on first use)
_plugin_cc_map_cache: Dict[str, Optional[dict]] = {}


def _load_cc_maps() -> None:
    global _cc_maps, _cc_maps_loaded
    if _cc_maps_loaded:
        return
    pattern = os.path.join(CC_MAPS_DIR, "*.json")
    for path in glob.glob(pattern):
        try:
            with open(path) as f:
                data = json.load(f)
            stem = os.path.splitext(os.path.basename(path))[0]
            _cc_maps[stem] = data
        except Exception as e:
            logger.warning(f"Failed to load CC map {path}: {e}")
    _cc_maps_loaded = True
    logger.info(f"Loaded {len(_cc_maps)} CC maps from {CC_MAPS_DIR}")


def _find_cc_map_for_plugin(plugin_name: str) -> Optional[dict]:
    """Match a device name to a CC map using the map's match_patterns list."""
    global _plugin_cc_map_cache
    if plugin_name in _plugin_cc_map_cache:
        return _plugin_cc_map_cache[plugin_name]
    _load_cc_maps()
    name_lower = plugin_name.lower()
    for _stem, cc_map in _cc_maps.items():
        for pattern in cc_map.get("match_patterns", []):
            if pattern.lower() in name_lower or name_lower in pattern.lower():
                _plugin_cc_map_cache[plugin_name] = cc_map
                return cc_map
    _plugin_cc_map_cache[plugin_name] = None
    return None


def _get_midi_output() -> Optional[Any]:
    """Open (or reuse) the AbletonMCP virtual MIDI output port."""
    if not HAS_MIDO:
        return None
    try:
        # Try to open existing port first; if not found, create virtual port
        available = mido.get_output_names()
        if MIDI_PORT_NAME in available:
            return mido.open_output(MIDI_PORT_NAME)
        return mido.open_output(MIDI_PORT_NAME, virtual=True)
    except Exception as e:
        logger.error(f"MIDI port error: {e}")
        return None


def _send_cc(channel: int, cc: int, value: int) -> bool:
    """Send a single MIDI CC message. channel is 1-16, value is 0-127."""
    port = _get_midi_output()
    if port is None:
        return False
    try:
        msg = mido.Message("control_change", channel=channel - 1, control=cc, value=value)
        port.send(msg)
        port.close()
        return True
    except Exception as e:
        logger.error(f"Failed to send MIDI CC: {e}")
        return False


@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None

    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton: {str(e)}")
            self.sock = None
            return False

    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Ableton: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        sock.settimeout(15.0)

        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break

                    chunks.append(chunk)

                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise

        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response"""
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")

        command = {
            "type": command_type,
            "params": params or {}
        }

        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "set_track_name",
            "create_clip", "add_notes_to_clip", "set_clip_name",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
            "start_playback", "stop_playback", "load_instrument_or_effect"
        ]

        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")

            if is_modifying_command:
                time.sleep(0.1)

            timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)

            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))

            if is_modifying_command:
                time.sleep(0.1)

            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.sock = None
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self.sock = None
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Ableton: {str(e)}")


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")

        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")

        bridge = get_bridge_client()
        if bridge:
            logger.info(f"AbletonParameterBridge detected on port {BRIDGE_PORT} "
                        f"— enhanced parameter access active (Extensions SDK)")
        else:
            logger.info(f"AbletonParameterBridge not detected on port {BRIDGE_PORT} "
                        f"— parameter tools using _Framework fallback")

        yield {}
    finally:
        global _ableton_connection
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        logger.info("AbletonMCP server shut down")


# Create the MCP server with lifespan support
mcp = FastMCP(
    "AbletonMCP",
    lifespan=server_lifespan
)

# Global connection for resources
_ableton_connection = None


def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection

    if _ableton_connection is not None:
        try:
            _ableton_connection.sock.settimeout(1.0)
            _ableton_connection.sock.sendall(b'')
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None

    if _ableton_connection is None:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host="localhost", port=9877)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    try:
                        _ableton_connection.send_command("get_session_info")
                        logger.info("Connection validated successfully")
                        return _ableton_connection
                    except Exception as e:
                        logger.error(f"Connection validation failed: {str(e)}")
                        _ableton_connection.disconnect()
                        _ableton_connection = None
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None

            if attempt < max_attempts:
                time.sleep(1.0)

        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")

    return _ableton_connection


# ── Core Tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def get_session_info(ctx: Context) -> str:
    """Get detailed information about the current Ableton session"""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"


@mcp.tool()
def get_track_info(ctx: Context, track_index: Union[int, str]) -> str:
    """
    Get info about a track: name, devices, clips. track_index is 0-based.
    """
    try:
        track_index = _i(track_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"


@mcp.tool()
def create_midi_track(ctx: Context, index: Union[int, str] = -1) -> str:
    """
    Create a new MIDI track. index=-1 adds it at the end.
    """
    try:
        index = _i(index)
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
def set_track_name(ctx: Context, track_index: Union[int, str], name: str) -> str:
    """Set the name of a track."""
    try:
        track_index = _i(track_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"


@mcp.tool()
def create_clip(ctx: Context, track_index: Union[int, str], clip_index: Union[int, str],
                length: float = 4.0) -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    """
    try:
        track_index = _i(track_index)
        clip_index = _i(clip_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("create_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"


@mcp.tool()
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]]
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dicts, each with pitch, start_time, duration, velocity, and mute
    """
    try:
        track_index = _i(track_index)
        clip_index = _i(clip_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })
        return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"


@mcp.tool()
def set_clip_name(ctx: Context, track_index: Union[int, str], clip_index: Union[int, str],
                  name: str) -> str:
    """Set the name of a clip."""
    try:
        track_index = _i(track_index)
        clip_index = _i(clip_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"


@mcp.tool()
def set_tempo(ctx: Context, tempo: float) -> str:
    """Set the session tempo in BPM."""
    try:
        tempo = _f(tempo)
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
def load_instrument_or_effect(ctx: Context, track_index: Union[int, str], uri: str) -> str:
    """
    Load a plugin or instrument onto a track using a URI from search_browser. track_index is 0-based.
    """
    try:
        track_index = _i(track_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri
        })

        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            else:
                devices = result.get("devices_after", [])
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
        else:
            return f"Failed to load instrument with URI '{uri}'"
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"


@mcp.tool()
def fire_clip(ctx: Context, track_index: Union[int, str], clip_index: Union[int, str]) -> str:
    """Start playing a clip."""
    try:
        track_index = _i(track_index)
        clip_index = _i(clip_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"


@mcp.tool()
def stop_clip(ctx: Context, track_index: Union[int, str], clip_index: Union[int, str]) -> str:
    """Stop playing a clip."""
    try:
        track_index = _i(track_index)
        clip_index = _i(clip_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"


@mcp.tool()
def start_playback(ctx: Context) -> str:
    """Start playing the Ableton session."""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"


@mcp.tool()
def stop_playback(ctx: Context) -> str:
    """Stop playing the Ableton session."""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"


# ── Browser Tools ─────────────────────────────────────────────────────────────

@mcp.tool()
def get_browser_tree(ctx: Context, category_type: str = "all") -> dict:
    """
    Return the Ableton browser tree as structured JSON.
    Use search_browser for finding specific items by name — it is faster and LLM-friendly.

    Parameters:
    - category_type: 'all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects'
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {"category_type": category_type})
        return result.get("result", result)
    except Exception as e:
        logger.error(f"Error getting browser tree: {str(e)}")
        return {"error": str(e)}


def _flatten_browser_tree(node: dict, results: list, path: str = "") -> None:
    """Recursively flatten a browser tree node into a list of loadable items."""
    if not node:
        return
    name = node.get("name", "Unknown")
    current_path = f"{path}/{name}" if path else name
    uri = node.get("uri")
    if uri and node.get("is_loadable", False):
        results.append({
            "name": name,
            "uri": uri,
            "path": current_path,
            "is_device": node.get("is_device", False),
            "is_folder": node.get("is_folder", False),
        })
    for child in node.get("items", node.get("children", [])):
        _flatten_browser_tree(child, results, current_path)


def _get_plugin_items(ableton, fmt: str) -> list:
    """Get all loadable plugins for one format (VST3 or AUv2) in two API calls per manufacturer."""
    flat = []
    try:
        top = ableton.send_command("get_browser_items_at_path", {"path": fmt})
        if isinstance(top, str):
            top = json.loads(top)
        for mfr in top.get("items", []):
            if not mfr.get("is_folder", False):
                continue
            mfr_path = f"{fmt}/{mfr['name']}"
            try:
                mfr_result = ableton.send_command("get_browser_items_at_path", {"path": mfr_path})
                if isinstance(mfr_result, str):
                    mfr_result = json.loads(mfr_result)
                for plugin in mfr_result.get("items", []):
                    if plugin.get("is_loadable", False) and plugin.get("uri"):
                        flat.append({
                            "name": plugin["name"],
                            "uri": plugin["uri"],
                            "path": mfr_path,
                            "is_device": plugin.get("is_device", False),
                            "manufacturer": mfr["name"],
                        })
            except Exception as e:
                logger.warning(f"Crawl error at {mfr_path}: {e}")
    except Exception as e:
        logger.warning(f"Crawl error at {fmt}: {e}")
    return flat


def _get_flat_browser(ableton) -> list:
    """Return cached flat browser list. Crawls VST3 only (covers all third-party plugins)."""
    global _browser_cache, _browser_cache_time
    now = time.time()
    if _browser_cache is not None and (now - _browser_cache_time) < BROWSER_CACHE_TTL:
        logger.info(f"Browser cache hit ({len(_browser_cache)} items)")
        return _browser_cache

    logger.info("Browser cache miss — building from VST3 plugin list")
    flat = _get_plugin_items(ableton, "plugins/VST3")

    for category in ["Instruments", "Audio Effects", "MIDI Effects", "Drums"]:
        try:
            result = ableton.send_command("get_browser_items_at_path", {"path": category})
            if isinstance(result, str):
                result = json.loads(result)
            for item in result.get("items", []):
                if item.get("is_loadable", False) and item.get("uri"):
                    flat.append({
                        "name": item["name"],
                        "uri": item["uri"],
                        "path": category,
                        "is_device": item.get("is_device", False),
                    })
        except Exception as e:
            logger.warning(f"Crawl error at {category}: {e}")

    _browser_cache = flat
    _browser_cache_time = now
    logger.info(f"Browser cache built: {len(flat)} items")
    return flat


@mcp.tool()
def search_browser(ctx: Context, query: str, max_results: Union[int, str] = 20) -> dict:
    """
    Find plugins, instruments and effects in Ableton by name.
    Returns a list of matches with name, uri, and score.
    Use the uri from results to load items onto tracks.
    Example: search_browser("1176") finds UAD 1176 compressors.
    """
    try:
        max_results = _i(max_results) or 20
        if not HAS_RAPIDFUZZ:
            return {"error": "rapidfuzz not installed. Run: pip install rapidfuzz", "matches": []}
        ableton = get_ableton_connection()
        flat_items = _get_flat_browser(ableton)
        try:
            max_results = min(int(max_results), 50)
        except (TypeError, ValueError):
            max_results = 20
        matches = []
        for item in flat_items:
            score = _fuzz.partial_ratio(query.lower(), item["name"].lower())
            if score > 60:
                matches.append({**item, "score": score})
        matches.sort(key=lambda x: x["score"], reverse=True)
        top = matches[:max_results]
        return {
            "query": query,
            "total_found": len(matches),
            "returned": len(top),
            "matches": top,
        }
    except Exception as e:
        logger.error(f"search_browser error: {str(e)}")
        return {"error": str(e), "query": query, "matches": []}


@mcp.tool()
def invalidate_browser_cache(ctx: Context) -> str:
    """Force a browser cache refresh. Call this after installing new plugins."""
    global _browser_cache, _browser_cache_time
    _browser_cache = None
    _browser_cache_time = 0.0
    return "Browser cache cleared. Next search_browser call will rebuild it."


@mcp.tool()
def get_browser_items_at_path(ctx: Context, path: str) -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path", {"path": path})

        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                    f"Available browser categories: {', '.join(available_cats)}")

        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            return "Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded."
        elif "Could not access Live application" in error_msg:
            return "Error: Could not access the Ableton Live application."
        elif "Unknown or unavailable category" in error_msg:
            return f"Error: {error_msg}. Check available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            return f"Error: {error_msg}. Check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"


@mcp.tool()
def load_drum_kit(ctx: Context, track_index: Union[int, str], rack_uri: str, kit_path: str) -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load
    - kit_path: Path to the drum kit inside the browser
    """
    try:
        track_index = _i(track_index)
        ableton = get_ableton_connection()

        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })

        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"

        kit_result = ableton.send_command("get_browser_items_at_path", {"path": kit_path})

        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"

        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]

        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"

        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })

        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"


# ── Parameter Tools (bridge-enhanced) ────────────────────────────────────────

@mcp.tool()
def get_bridge_status(ctx: Context) -> str:
    """
    Check whether the AbletonParameterBridge Extensions SDK server is running.
    When active, parameter tools use the Extensions SDK (async, more reliable for VST3/AU plugins).
    When inactive, parameter tools fall back to the _Framework Remote Script.
    """
    bridge = get_bridge_client()
    if bridge:
        try:
            tracks = bridge.get_tracks()
            track_count = len(tracks.get("tracks", []))
            return (
                f"AbletonParameterBridge: ACTIVE (port {BRIDGE_PORT})\n"
                f"Enhanced parameter access is enabled via the Extensions SDK.\n"
                f"Tracks visible to bridge: {track_count}"
            )
        except Exception as e:
            return f"AbletonParameterBridge: ACTIVE but error fetching tracks: {str(e)}"
    else:
        return (
            f"AbletonParameterBridge: NOT RUNNING (port {BRIDGE_PORT})\n"
            f"Parameter tools are using _Framework fallback.\n"
            f"To enable enhanced access, activate the AbletonParameterBridge Extension in "
            f"Ableton Live (Settings → Extensions)."
        )


@mcp.tool()
def get_device_parameters(ctx: Context, track_index: Union[int, str],
                           device_index: Union[int, str]) -> str:
    """
    Get all parameters for a device (plugin) on a track, including current values, min, max, and name.
    Prefers AbletonParameterBridge (Extensions SDK async reads) when running;
    falls back to _Framework if not.

    Parameters:
    - track_index: The index of the track (0-based)
    - device_index: The index of the device on the track (0-based)
    """
    track_index = _i(track_index)
    device_index = _i(device_index)

    bridge = get_bridge_client()
    if bridge:
        try:
            result = bridge.get_params(track_index, device_index)
            result["_source"] = "extensions_sdk"
            logger.info(f"get_device_parameters via bridge: track={track_index} device={device_index}")
            return json.dumps(result, indent=2)
        except Exception as e:
            logger.warning(f"Bridge get_params failed, falling back to _Framework: {e}")

    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index
        })
        result["_source"] = "_framework"
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"


@mcp.tool()
def set_device_parameter(ctx: Context, track_index: Union[int, str],
                          device_index: Union[int, str], value: float,
                          param_index: Union[int, str] = None,
                          param_name: str = None) -> str:
    """
    Set a parameter value on a device (plugin). Identify the parameter by index or name.
    Values are automatically clamped to the parameter's valid range.
    Prefers AbletonParameterBridge (async write); falls back to _Framework.

    Parameters:
    - track_index: The index of the track (0-based)
    - device_index: The index of the device on the track (0-based)
    - value: The new value to set
    - param_index: The index of the parameter (use get_device_parameters to find indices)
    - param_name: The name of the parameter (case-insensitive, alternative to param_index)
    """
    track_index = _i(track_index)
    device_index = _i(device_index)
    value = _f(value)
    p_index = _i(param_index) if param_index is not None else None

    bridge = get_bridge_client()
    if bridge:
        try:
            result = bridge.set_param(track_index, device_index, value,
                                      param_index=p_index, param_name=param_name)
            result["_source"] = "extensions_sdk"
            logger.info(f"set_device_parameter via bridge: track={track_index} device={device_index}")
            return json.dumps(result, indent=2)
        except Exception as e:
            logger.warning(f"Bridge set_param failed, falling back to _Framework: {e}")

    try:
        ableton = get_ableton_connection()
        params: Dict[str, Any] = {
            "track_index": track_index,
            "device_index": device_index,
            "value": value
        }
        if p_index is not None:
            params["param_index"] = p_index
        if param_name is not None:
            params["param_name"] = param_name
        result = ableton.send_command("set_device_parameter", params)
        result["_source"] = "_framework"
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"


@mcp.tool()
def save_device_snapshot(ctx: Context, track_index: Union[int, str],
                          device_index: Union[int, str], snapshot_name: str) -> str:
    """
    Save all current parameter values for a device as a named snapshot.
    Snapshots persist for the MCP server session and can be recalled at any time.
    Prefers AbletonParameterBridge for async-accurate capture; falls back to _Framework.

    Parameters:
    - track_index: The index of the track (0-based)
    - device_index: The index of the device on the track (0-based)
    - snapshot_name: A name to identify this snapshot (e.g. "bright_pad", "bass_init")
    """
    track_index = _i(track_index)
    device_index = _i(device_index)
    snapshot_key = f"{track_index}:{device_index}:{snapshot_name}"

    bridge = get_bridge_client()
    if bridge:
        try:
            result = bridge.get_snapshot(track_index, device_index)
            snapshot_data = result.get("snapshot", {})
            _mcp_snapshots[snapshot_key] = snapshot_data
            logger.info(f"Snapshot '{snapshot_name}' saved via bridge: {len(snapshot_data)} params")
            return json.dumps({
                "snapshot_name": snapshot_name,
                "device_name": result.get("device_name", ""),
                "param_count": len(snapshot_data),
                "_source": "extensions_sdk",
                "status": "saved"
            }, indent=2)
        except Exception as e:
            logger.warning(f"Bridge snapshot capture failed, falling back to _Framework: {e}")

    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("save_device_snapshot", {
            "track_index": track_index,
            "device_index": device_index,
            "snapshot_name": snapshot_name
        })
        result["_source"] = "_framework"
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error saving device snapshot: {str(e)}")
        return f"Error saving device snapshot: {str(e)}"


@mcp.tool()
def recall_device_snapshot(ctx: Context, track_index: Union[int, str],
                            device_index: Union[int, str], snapshot_name: str) -> str:
    """
    Recall a previously saved snapshot, restoring all parameter values for a device.
    Uses AbletonParameterBridge bulk restore (POST /snapshot) when available;
    falls back to _Framework recall.

    Parameters:
    - track_index: The index of the track (0-based)
    - device_index: The index of the device on the track (0-based)
    - snapshot_name: The name of the snapshot to recall
    """
    track_index = _i(track_index)
    device_index = _i(device_index)
    snapshot_key = f"{track_index}:{device_index}:{snapshot_name}"

    # Try bridge-based recall first (server-side snapshot store)
    bridge = get_bridge_client()
    if bridge and snapshot_key in _mcp_snapshots:
        try:
            params = _mcp_snapshots[snapshot_key]
            result = bridge.restore_snapshot(track_index, device_index, params)
            result["_source"] = "extensions_sdk"
            logger.info(f"Snapshot '{snapshot_name}' recalled via bridge: "
                        f"{result.get('applied', 0)}/{result.get('total', 0)} params restored")
            return json.dumps(result, indent=2)
        except Exception as e:
            logger.warning(f"Bridge snapshot recall failed, falling back to _Framework: {e}")

    # Fall back to _Framework recall
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("recall_device_snapshot", {
            "track_index": track_index,
            "device_index": device_index,
            "snapshot_name": snapshot_name
        })
        result["_source"] = "_framework"
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error recalling device snapshot: {str(e)}")
        return f"Error recalling device snapshot: {str(e)}"


# ── MIDI CC Tools ─────────────────────────────────────────────────────────────

@mcp.tool()
def set_plugin_parameter_cc(ctx: Context, track_index: Union[int, str],
                             param_name: str, value: float,
                             midi_channel: Union[int, str] = None) -> str:
    """
    Set a plugin parameter via MIDI CC — works for any plugin regardless of VST3 exposure.
    Arturia and NI plugins have built-in CC maps; others can use custom channel assignments.

    Requires mido: uv run --with mido --with python-rtmidi ...
    In Ableton: set the plugin track's MIDI input to "AbletonMCP" on the assigned channel.

    Parameters:
    - track_index: The track index (0-based)
    - param_name: Parameter name (e.g. "Filter Cutoff", "LFO Rate", "Macro 1")
    - value: 0.0 to 1.0 (automatically mapped to CC range 0-127)
    - midi_channel: Override MIDI channel 1-16 (default: track_index + 1)
    """
    if not HAS_MIDO:
        return "Error: mido not installed. Run: pip install mido python-rtmidi"

    track_index = _i(track_index)
    value = max(0.0, min(1.0, _f(value)))

    # Determine MIDI channel
    if midi_channel is not None:
        channel = _i(midi_channel)
    elif track_index in _track_cc_channels:
        channel = _track_cc_channels[track_index]
    else:
        channel = (track_index % 16) + 1  # default: track 0→ch1, 1→ch2, etc.

    # Find plugin on track to look up CC map
    try:
        ableton = get_ableton_connection()
        track_info = ableton.send_command("get_track_info", {"track_index": track_index})
        devices = track_info.get("devices", [])
        plugin_name = devices[0].get("name", "") if devices else ""
    except Exception:
        plugin_name = ""

    cc_map = _find_cc_map_for_plugin(plugin_name) if plugin_name else None

    if cc_map is None:
        return (
            f"No CC map found for plugin '{plugin_name}' on track {track_index}. "
            f"Call list_cc_maps() to see available maps, or use midi_channel to send raw CC."
        )

    params = cc_map.get("parameters", {})
    # Case-insensitive param lookup
    param_key = next(
        (k for k in params if k.lower() == param_name.lower()), None
    )
    if param_key is None:
        available = ", ".join(params.keys())
        return f"Parameter '{param_name}' not found in {cc_map['plugin_name']} CC map. Available: {available}"

    cc_num = params[param_key]["cc"]
    cc_value = int(round(value * 127))

    ok = _send_cc(channel, cc_num, cc_value)
    if ok:
        return (
            f"Sent CC #{cc_num} = {cc_value} (value={value:.3f}) → "
            f"'{param_key}' on {cc_map['plugin_name']} (track {track_index}, MIDI ch {channel})"
        )
    else:
        return f"Failed to send MIDI CC. Is the 'AbletonMCP' virtual port available?"


@mcp.tool()
def get_cc_map(ctx: Context, track_index: Union[int, str]) -> str:
    """
    Show the full CC parameter map for the plugin on a given track.
    Lists every controllable parameter with its CC number and description.

    Parameters:
    - track_index: The track index (0-based)
    """
    track_index = _i(track_index)
    try:
        ableton = get_ableton_connection()
        track_info = ableton.send_command("get_track_info", {"track_index": track_index})
        devices = track_info.get("devices", [])
        plugin_name = devices[0].get("name", "") if devices else ""
    except Exception as e:
        return f"Error getting track info: {str(e)}"

    cc_map = _find_cc_map_for_plugin(plugin_name) if plugin_name else None
    if cc_map is None:
        _load_cc_maps()
        available = [m.get("plugin_name", k) for k, m in _cc_maps.items()]
        return (
            f"No CC map found for '{plugin_name}' on track {track_index}. "
            f"Available maps: {', '.join(available)}"
        )

    channel = _track_cc_channels.get(track_index, (track_index % 16) + 1)
    result = {
        "track_index": track_index,
        "plugin": cc_map["plugin_name"],
        "manufacturer": cc_map.get("manufacturer", ""),
        "midi_channel": channel,
        "notes": cc_map.get("notes", ""),
        "parameters": {
            name: {"cc": p["cc"], "description": p.get("description", "")}
            for name, p in cc_map.get("parameters", {}).items()
        }
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def list_cc_maps(ctx: Context) -> str:
    """
    List all available MIDI CC maps with their supported plugins and parameter counts.
    """
    _load_cc_maps()
    if not _cc_maps:
        return f"No CC maps found in {CC_MAPS_DIR}"

    summary = []
    for _stem, cc_map in sorted(_cc_maps.items()):
        param_count = len(cc_map.get("parameters", {}))
        patterns = ", ".join(cc_map.get("match_patterns", []))
        summary.append({
            "plugin": cc_map.get("plugin_name", _stem),
            "manufacturer": cc_map.get("manufacturer", ""),
            "matches": patterns,
            "parameter_count": param_count,
            "notes": cc_map.get("notes", "")[:80] + ("..." if len(cc_map.get("notes", "")) > 80 else "")
        })
    return json.dumps(summary, indent=2)


@mcp.tool()
def assign_cc_channel(ctx: Context, track_index: Union[int, str],
                      midi_channel: Union[int, str]) -> str:
    """
    Assign a specific MIDI channel (1-16) to a track for CC control.
    Default is track_index + 1. Override here if your Ableton routing uses different channels.

    Parameters:
    - track_index: The track index (0-based)
    - midi_channel: MIDI channel 1-16
    """
    track_index = _i(track_index)
    channel = _i(midi_channel)
    if channel < 1 or channel > 16:
        return "Error: MIDI channel must be 1-16"
    _track_cc_channels[track_index] = channel
    return f"Track {track_index} → MIDI channel {channel}. Set the track's MIDI input to 'AbletonMCP' on channel {channel} in Ableton."


# Main execution
def main():
    """Run the MCP server"""
    mcp.run()


if __name__ == "__main__":
    main()
