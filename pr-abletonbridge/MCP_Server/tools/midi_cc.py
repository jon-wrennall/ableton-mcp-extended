"""MIDI CC plugin control for AbletonBridge.

Sends MIDI CC to a virtual port ('AbletonBridge') to control plugin parameters
that VST3/AU Configure mode doesn't expose. Covers Arturia V Collection 11 Pro
(45 instruments) and NI Komplete Collector's Edition (55 instruments) via
100 built-in JSON maps in the midi_cc/ directory.

Setup in Ableton:
  - Set each plugin track's MIDI input to 'AbletonBridge' (the virtual port this
    module creates) on its assigned channel.
  - Default channel assignment: track_index + 1  (track 0 → ch 1, track 15 → ch 16)
  - Use assign_cc_channel() to override for any track.

Dependencies (optional — CC tools gracefully report unavailable if missing):
  pip install mido python-rtmidi
"""

import glob
import json
import logging
import os
from typing import Any, Dict, Optional, Union

from mcp.server.fastmcp import Context

from MCP_Server.connections.ableton import get_ableton_connection
from MCP_Server.tools._base import _tool_handler

logger = logging.getLogger("AbletonBridge")

# ── Constants ──────────────────────────────────────────────────────────────────

MIDI_PORT_NAME = "AbletonBridge"

# Path to the midi_cc/ directory (sits alongside the MCP_Server/ package)
_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CC_MAPS_DIR = os.path.join(_PACKAGE_ROOT, "midi_cc")

# ── Module-level state ────────────────────────────────────────────────────────

# track_index → assigned MIDI channel (1–16)
_track_cc_channels: Dict[int, int] = {}

# Loaded CC maps: filename_stem → map dict
_cc_maps: Dict[str, dict] = {}
_cc_maps_loaded: bool = False

# plugin_name → matched cc_map (None if no match found)
_plugin_cc_map_cache: Dict[str, Optional[dict]] = {}

# ── Optional mido import ──────────────────────────────────────────────────────

try:
    import mido
    _HAS_MIDO = True
except ImportError:
    _HAS_MIDO = False


# ── Internal helpers ──────────────────────────────────────────────────────────

def _i(v) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def _f(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _load_cc_maps() -> None:
    global _cc_maps, _cc_maps_loaded
    if _cc_maps_loaded:
        return
    if not os.path.isdir(CC_MAPS_DIR):
        logger.warning("midi_cc/ directory not found at %s", CC_MAPS_DIR)
        _cc_maps_loaded = True
        return
    for path in glob.glob(os.path.join(CC_MAPS_DIR, "*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            stem = os.path.splitext(os.path.basename(path))[0]
            _cc_maps[stem] = data
        except Exception as e:
            logger.warning("Failed to load CC map %s: %s", path, e)
    _cc_maps_loaded = True
    logger.info("Loaded %d CC maps from %s", len(_cc_maps), CC_MAPS_DIR)


def _find_cc_map_for_plugin(plugin_name: str) -> Optional[dict]:
    """Match a device name to a CC map using each map's match_patterns list."""
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
    """Open (or reuse) the AbletonBridge virtual MIDI output port."""
    if not _HAS_MIDO:
        return None
    try:
        available = mido.get_output_names()
        if MIDI_PORT_NAME in available:
            return mido.open_output(MIDI_PORT_NAME)
        return mido.open_output(MIDI_PORT_NAME, virtual=True)
    except Exception as e:
        logger.error("MIDI port error: %s", e)
        return None


def _send_cc(channel: int, cc: int, value: int) -> bool:
    """Send a single MIDI CC message. channel is 1–16, value is 0–127."""
    port = _get_midi_output()
    if port is None:
        return False
    try:
        msg = mido.Message("control_change", channel=channel - 1, control=cc, value=value)
        port.send(msg)
        port.close()
        return True
    except Exception as e:
        logger.error("Failed to send MIDI CC: %s", e)
        return False


def _mido_unavailable() -> str:
    return (
        "MIDI CC tools require mido and python-rtmidi.\n"
        "Install with:  pip install mido python-rtmidi\n"
        "Then restart the MCP server."
    )


# ── Tool registration ─────────────────────────────────────────────────────────

def register_tools(mcp) -> None:
    """Register MIDI CC tools with the MCP server."""

    @mcp.tool()
    @_tool_handler("setting plugin parameter via MIDI CC")
    def set_plugin_parameter_cc(
        ctx: Context,
        track_index: Union[int, str],
        param_name: str,
        value: float,
        midi_channel: Union[int, str, None] = None,
    ) -> str:
        """Set a plugin parameter via MIDI CC.

        Works for any VST/AU plugin regardless of how many parameters it exposes
        through Configure mode. Arturia V Collection 11 (45 instruments) and NI
        Komplete Collector's Edition (55 instruments) have built-in CC maps.

        In Ableton: set the plugin track's MIDI input to 'AbletonBridge' on the
        assigned channel. Default assignment: track_index + 1.

        Parameters:
        - track_index: Track containing the plugin (0-based)
        - param_name: Parameter name matching the CC map (e.g. 'Filter Cutoff', 'Expression')
        - value: 0.0–1.0 (mapped to CC 0–127)
        - midi_channel: Override MIDI channel 1–16 (default: track_index + 1)
        """
        if not _HAS_MIDO:
            return _mido_unavailable()

        track_index = _i(track_index)
        value = max(0.0, min(1.0, _f(value)))

        # Determine MIDI channel
        if midi_channel is not None:
            channel = _i(midi_channel)
        elif track_index in _track_cc_channels:
            channel = _track_cc_channels[track_index]
        else:
            channel = (track_index % 16) + 1

        # Look up plugin name to find CC map
        ableton = get_ableton_connection()
        track_info = ableton.send_command("get_track_info", {"track_index": track_index})
        devices = track_info.get("devices", [])
        plugin_name = devices[0].get("name", "") if devices else ""

        cc_map = _find_cc_map_for_plugin(plugin_name) if plugin_name else None
        if cc_map is None:
            return (
                f"No CC map found for plugin '{plugin_name}' on track {track_index}.\n"
                f"Call list_cc_maps() to see all available maps, or use assign_cc_channel() "
                f"and send a raw CC number directly."
            )

        params = cc_map.get("parameters", {})
        param_key = next((k for k in params if k.lower() == param_name.lower()), None)
        if param_key is None:
            available = ", ".join(params.keys())
            return (
                f"Parameter '{param_name}' not found in {cc_map['plugin_name']} CC map.\n"
                f"Available parameters: {available}"
            )

        cc_num = params[param_key]["cc"]
        cc_value = int(round(value * 127))

        if _send_cc(channel, cc_num, cc_value):
            return (
                f"Sent CC #{cc_num} = {cc_value} (value={value:.3f}) → "
                f"'{param_key}' on {cc_map['plugin_name']} "
                f"(track {track_index}, MIDI ch {channel})"
            )
        return "Failed to send MIDI CC. Is the 'AbletonBridge' virtual port available?"

    @mcp.tool()
    @_tool_handler("getting CC map for plugin")
    def get_cc_map(ctx: Context, track_index: Union[int, str]) -> str:
        """Show the full CC parameter map for the plugin on a given track.

        Lists every controllable parameter name, its CC number, and description.
        Use this before set_plugin_parameter_cc to confirm available parameter names.

        Parameters:
        - track_index: The track index (0-based)
        """
        track_index = _i(track_index)
        ableton = get_ableton_connection()
        track_info = ableton.send_command("get_track_info", {"track_index": track_index})
        devices = track_info.get("devices", [])
        plugin_name = devices[0].get("name", "") if devices else ""

        cc_map = _find_cc_map_for_plugin(plugin_name) if plugin_name else None
        if cc_map is None:
            _load_cc_maps()
            available = sorted(m.get("plugin_name", k) for k, m in _cc_maps.items())
            return (
                f"No CC map found for '{plugin_name}' on track {track_index}.\n"
                f"Available maps ({len(available)}): {', '.join(available)}"
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
            },
        }
        return json.dumps(result, indent=2)

    @mcp.tool()
    @_tool_handler("listing CC maps")
    def list_cc_maps(ctx: Context, manufacturer: str = "all") -> str:
        """List all available MIDI CC plugin maps.

        Shows plugin name, manufacturer, match patterns, and parameter count.
        100 maps included: NI Komplete Collector's Edition + Arturia V Collection 11.

        Parameters:
        - manufacturer: Filter by 'arturia', 'ni' (Native Instruments), or 'all' (default)
        """
        _load_cc_maps()
        if not _cc_maps:
            return f"No CC maps found in {CC_MAPS_DIR}"

        mfr_filter = manufacturer.lower()
        summary = []
        for _stem, cc_map in sorted(_cc_maps.items()):
            mfr = cc_map.get("manufacturer", "").lower()
            if mfr_filter not in ("all", "") and mfr_filter not in mfr:
                continue
            note = cc_map.get("notes", "")
            summary.append({
                "plugin": cc_map.get("plugin_name", _stem),
                "manufacturer": cc_map.get("manufacturer", ""),
                "matches": ", ".join(cc_map.get("match_patterns", [])),
                "parameter_count": len(cc_map.get("parameters", {})),
                "notes_preview": note[:80] + ("..." if len(note) > 80 else ""),
            })

        return json.dumps(summary, indent=2)

    @mcp.tool()
    @_tool_handler("assigning CC channel to track")
    def assign_cc_channel(
        ctx: Context,
        track_index: Union[int, str],
        midi_channel: Union[int, str],
    ) -> str:
        """Assign a specific MIDI channel (1–16) to a track for CC control.

        The default is track_index + 1. Override here if your Ableton routing
        uses different channels.

        In Ableton: set the track's MIDI input to 'AbletonBridge' on the same channel.

        Parameters:
        - track_index: The track index (0-based)
        - midi_channel: MIDI channel 1–16
        """
        track_index = _i(track_index)
        channel = _i(midi_channel)
        if channel < 1 or channel > 16:
            return "Error: MIDI channel must be between 1 and 16."
        _track_cc_channels[track_index] = channel
        return (
            f"Track {track_index} → MIDI channel {channel}.\n"
            f"In Ableton: set the track's MIDI input to 'AbletonBridge' on channel {channel}."
        )

    @mcp.tool()
    @_tool_handler("sending raw MIDI CC")
    def send_raw_cc(
        ctx: Context,
        midi_channel: Union[int, str],
        cc_number: Union[int, str],
        value: Union[int, str, float],
    ) -> str:
        """Send a raw MIDI CC message to any channel without a plugin map lookup.

        Use this when you know the CC number directly — useful for custom mappings
        or instruments not yet in the built-in map library.

        Parameters:
        - midi_channel: MIDI channel 1–16
        - cc_number: CC number 0–127
        - value: 0–127 (integer) or 0.0–1.0 (float, auto-scaled to 0–127)
        """
        if not _HAS_MIDO:
            return _mido_unavailable()

        channel = _i(midi_channel)
        cc_num = _i(cc_number)

        # Accept 0-127 int or 0.0-1.0 float
        raw = value
        if isinstance(raw, str):
            try:
                raw = int(raw)
            except ValueError:
                raw = float(raw)
        if isinstance(raw, float) and raw <= 1.0:
            cc_val = int(round(raw * 127))
        else:
            cc_val = max(0, min(127, int(raw)))

        if channel < 1 or channel > 16:
            return "Error: midi_channel must be 1–16."
        if cc_num < 0 or cc_num > 127:
            return "Error: cc_number must be 0–127."

        if _send_cc(channel, cc_num, cc_val):
            return f"Sent CC #{cc_num} = {cc_val} on MIDI channel {channel}."
        return "Failed to send MIDI CC. Is the 'AbletonBridge' virtual port available?"
