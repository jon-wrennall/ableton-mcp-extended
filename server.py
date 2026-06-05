# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
import time
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Union, Optional

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
        sock.settimeout(15.0)  # Increased timeout for operations that might take longer
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
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
            
        # If we get here, we either timed out or broke out of the loop
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
        
        # Check if this is a state-modifying command
        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "set_track_name",
            "create_clip", "add_notes_to_clip", "set_clip_name",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
            "start_playback", "stop_playback", "load_instrument_or_effect"
        ]
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # For state-modifying commands, add a small delay to give Ableton time to process
            if is_modifying_command:
                import time
                time.sleep(0.1)  # 100ms delay
            
            # Set timeout based on command type
            timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)
            
            # Receive the response
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")
            
            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")
            
            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))
            
            # For state-modifying commands, add another small delay after receiving response
            if is_modifying_command:
                import time
                time.sleep(0.1)  # 100ms delay
            
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
            # Test the connection with a simple ping
            # We'll try to send an empty message, which should fail if the connection is dead
            # but won't affect Ableton if it's alive
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
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        # Try to connect up to 3 times with a short delay between attempts
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host="localhost", port=9877)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    
                    # Validate connection with a simple command
                    try:
                        # Get session info as a test
                        _ableton_connection.send_command("get_session_info")
                        logger.info("Connection validated successfully")
                        return _ableton_connection
                    except Exception as e:
                        logger.error(f"Connection validation failed: {str(e)}")
                        _ableton_connection.disconnect()
                        _ableton_connection = None
                        # Continue to next attempt
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None
            
            # Wait before trying again, but only if we have more attempts left
            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


# Core Tool endpoints

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
    """
    Set the name of a track.
    
    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    """
    try:
        track_index = _i(track_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

@mcp.tool()
def create_clip(ctx: Context, track_index: Union[int, str], clip_index: Union[int, str], length: float = 4.0) -> str:
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
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute
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
def set_clip_name(ctx: Context, track_index: Union[int, str], clip_index: Union[int, str], name: str) -> str:
    """
    Set the name of a clip.
    
    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    """
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
    """
    Set the session tempo in BPM.
    """
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
        
        # Check if the instrument was loaded successfully
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
    """
    Start playing a clip.
    
    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    """
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
    """
    Stop playing a clip.
    
    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    """
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
    # VST3 only: ~20 manufacturers × 1 API call each = fast build
    # AUv2 has the same plugins so no need to duplicate
    flat = _get_plugin_items(ableton, "plugins/VST3")

    # Also add a shallow pass over native Ableton instruments and effects (1 level only)
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
        # Coerce max_results in case model sends it as a string
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
            where category is one of the available browser categories in Ableton
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path", {
            "path": path
        })
        
        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")
        
        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

@mcp.tool()
def load_drum_kit(ctx: Context, track_index: Union[int, str], rack_uri: str, kit_path: str) -> str:
    """
    Load a drum rack and then load a specific drum kit into it.
    
    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    """
    try:
        track_index = _i(track_index)
        ableton = get_ableton_connection()
        
        # Step 1: Load the drum rack
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })
        
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"
        
        # Step 2: Get the drum kit items at the specified path
        kit_result = ableton.send_command("get_browser_items_at_path", {
            "path": kit_path
        })
        
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"
        
        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]
        
        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
        
        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })
        
        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"


@mcp.tool()
def get_device_parameters(ctx: Context, track_index: Union[int, str], device_index: Union[int, str]) -> str:
    """
    Get all parameters for a device (plugin) on a track, including current values, min, max, and name.
    Use this to inspect NI, Arturia, or any VST/AU plugin before adjusting it.

    Parameters:
    - track_index: The index of the track (0-based)
    - device_index: The index of the device on the track (0-based)
    """
    try:
        track_index = _i(track_index)
        device_index = _i(device_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"


@mcp.tool()
def set_device_parameter(ctx: Context, track_index: Union[int, str], device_index: Union[int, str], value: float,
                         param_index: Union[int, str] = None, param_name: str = None) -> str:
    """
    Set a parameter value on a device (plugin). Identify the parameter by index or name.
    Values are automatically clamped to the parameter's valid range.

    Parameters:
    - track_index: The index of the track (0-based)
    - device_index: The index of the device on the track (0-based)
    - value: The new value to set
    - param_index: The index of the parameter (use get_device_parameters to find indices)
    - param_name: The name of the parameter (case-insensitive, alternative to param_index)
    """
    try:
        track_index = _i(track_index)
        device_index = _i(device_index)
        param_index = _i(param_index)
        value = _f(value)
        ableton = get_ableton_connection()
        params = {"track_index": track_index, "device_index": device_index, "value": value}
        if param_index is not None:
            params["param_index"] = param_index
        if param_name is not None:
            params["param_name"] = param_name
        result = ableton.send_command("set_device_parameter", params)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"


@mcp.tool()
def save_device_snapshot(ctx: Context, track_index: Union[int, str], device_index: Union[int, str], snapshot_name: str) -> str:
    """
    Save all current parameter values for a device as a named snapshot.
    Snapshots persist for the session and can be recalled at any time.

    Parameters:
    - track_index: The index of the track (0-based)
    - device_index: The index of the device on the track (0-based)
    - snapshot_name: A name to identify this snapshot (e.g. "bright_pad", "bass_init")
    """
    try:
        track_index = _i(track_index)
        device_index = _i(device_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("save_device_snapshot", {
            "track_index": track_index,
            "device_index": device_index,
            "snapshot_name": snapshot_name
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error saving device snapshot: {str(e)}")
        return f"Error saving device snapshot: {str(e)}"


@mcp.tool()
def recall_device_snapshot(ctx: Context, track_index: Union[int, str], device_index: Union[int, str], snapshot_name: str) -> str:
    """
    Recall a previously saved snapshot, restoring all parameter values for a device.

    Parameters:
    - track_index: The index of the track (0-based)
    - device_index: The index of the device on the track (0-based)
    - snapshot_name: The name of the snapshot to recall
    """
    try:
        track_index = _i(track_index)
        device_index = _i(device_index)
        ableton = get_ableton_connection()
        result = ableton.send_command("recall_device_snapshot", {
            "track_index": track_index,
            "device_index": device_index,
            "snapshot_name": snapshot_name
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error recalling device snapshot: {str(e)}")
        return f"Error recalling device snapshot: {str(e)}"

# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()