# ableton-mcp-extended

An extended version of [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) — connecting Ableton Live to AI assistants via the Model Context Protocol (MCP).

Built on the excellent original work by [Siddharth Ahuja](https://github.com/ahujasid). This fork adds deep plugin parameter control, fuzzy browser search, device snapshots, and small-LLM compatibility fixes.

---

## Architecture

This project has two components that work independently or together:

| Component | SDK | Transport | Required |
|-----------|-----|-----------|----------|
| `AbletonMCP_Remote_Script/__init__.py` | `_Framework` (Python) | Socket port 9877 | Yes |
| `AbletonParameterBridge/` | Extensions SDK (Node.js) | HTTP port 9878 | No — enhances parameter tools |

The Remote Script handles all session, track, clip, browser, and transport commands. The `AbletonParameterBridge` Extension is an optional upgrade — when active, it intercepts parameter reads and writes and routes them through the Extensions SDK's async API, which is more reliable for VST3/AU plugins. Everything falls back gracefully to `_Framework` if the Extension isn't running.

Use `get_bridge_status` to check which mode is active.

---

## What's New in This Fork

| Feature | Description |
|---------|-------------|
| `search_browser` | Fuzzy search across all installed plugins and instruments — returns ranked results with loadable URIs. No more navigating the browser tree. |
| `get_device_parameters` | Read every exposed parameter from any plugin on a track — name, value, min, max, quantised options. |
| `set_device_parameter` | Set any plugin parameter by name or index. Values are automatically clamped to valid range. |
| `save_device_snapshot` | Capture the complete parameter state of a plugin as a named snapshot (session-persistent). |
| `recall_device_snapshot` | Restore any saved snapshot instantly. |
| `invalidate_browser_cache` | Force a browser cache rebuild after installing new plugins. |
| `get_chain_device_parameters` | Access parameters of devices inside Instrument/Audio Effect Racks (bypasses macro layer). |
| `AbletonParameterBridge` integration | Optional Extensions SDK bridge — parameter tools automatically prefer async SDK reads/writes over `_Framework` when the Bridge Extension is active. Bulk snapshot restore via `POST /snapshot`. |
| `get_bridge_status` | New tool — reports whether the Extensions SDK bridge is live and which transport is active for parameter tools. |
| Type coercion | All integer and float parameters accept strings — fixes `params.track_index is not of a type(s) integer` errors from Llama 3.x and other small LLMs. |
| Browser caching | `search_browser` builds a flat index on first call and caches it for 2 minutes — fast subsequent searches. |
| Fixed `get_browser_tree` | The upstream `process_item` function never populated children. Fixed with proper recursive traversal. |
| Fixed `_find_browser_item_by_uri` | Added `plugins` to the browser category search list — upstream couldn't load VST/AU plugins by URI. |

---

## Compatibility

- **Ableton Live 12.4.x** — tested on **12.4.5 beta**. **Live 10 and 11 are not supported**: the browser `plugins` category, device parameter API behaviour, and browser traversal this fork depends on are only present in Live 12.
- **Python 3.11** (embedded in Ableton Live 12 — no separate install needed for the Remote Script)
- **Python 3.8+** and **uv** for the MCP server process
- Works with **Claude Desktop**, **LM Studio** (Llama 3.1 8B+), **Cursor**, and any MCP-compatible client
- Requires **rapidfuzz** for `search_browser` (`uv run --with rapidfuzz ...`)

### SDK / Framework

This project uses Ableton's **`_Framework` Control Surface SDK** — the classic Python Remote Script API that has shipped inside Live for many years. It is **not** the new [Ableton Extensions SDK](https://www.ableton.com/en/live/extensions/) introduced in Live 12.4.5 beta.

Key APIs used:

| API | Purpose |
|-----|---------|
| `_Framework.ControlSurface` | Base class for the Remote Script |
| `application().browser` | Browser tree traversal and plugin loading |
| `song().tracks` / `clip_slots` | Track and clip access |
| `device.parameters` | Plugin parameter read/write |
| `clip.set_notes()` | MIDI note writing |

> **Why does it still require Live 12.4.5?** Although `_Framework` is old, the browser's `plugins` category — which `search_browser` and `load_instrument_or_effect` depend on — only exposes VST3/AU plugins correctly in Live 12. The browser traversal and device parameter behaviour also changed significantly between Live 11 and 12. The `_Framework` SDK is internal and undocumented by Ableton; it is not the same as the new JavaScript Extensions SDK.

#### What is the Ableton Extensions SDK?

Ableton's new [Extensions SDK](https://www.ableton.com/en/live/extensions/) (available in Live 12.4.5 Suite beta) is a separate **JavaScript/Node.js API** for building Extensions that run from Live's right-click context menu. It is open, documented, and purpose-built for Live 12. This MCP server does **not** use it — all communication is via the `_Framework` Remote Script socket only.

---

## Installation

### 1. Install the Remote Script

Copy `AbletonMCP_Remote_Script/__init__.py` into a folder called `AbletonMCP` inside Ableton's User Library Remote Scripts directory:

**macOS:**
```bash
mkdir -p ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP
cp AbletonMCP_Remote_Script/__init__.py ~/Music/Ableton/User\ Library/Remote\ Scripts/AbletonMCP/
```

**Windows:**
```
%USERPROFILE%\Documents\Ableton\User Library\Remote Scripts\AbletonMCP\
```

Then in Ableton Live: **Preferences → Link, Tempo & MIDI → Control Surface → AbletonMCP** (Input/Output: None).

### 2. (Optional) Install the AbletonParameterBridge Extension

This enables enhanced parameter access via the Extensions SDK. Requires **Live 12.4.5 Suite beta** and **Node.js v24 LTS**.

```bash
cd AbletonParameterBridge
npm install
npm run build
```

Then in Ableton Live: **Settings → Extensions → Install Extension** and select the `AbletonParameterBridge` folder. Activate it from the Extensions panel. The bridge starts an HTTP server on port 9878; the MCP server detects it automatically.

> If the Extension is not installed or not active, all parameter tools continue to work via the `_Framework` Remote Script fallback.

### 3. Configure Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "AbletonMCP": {
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "run",
        "--with", "mcp",
        "--with", "rapidfuzz",
        "/path/to/MCP_Server/server.py"
      ]
    }
  }
}
```

Replace `/path/to/MCP_Server/server.py` with the actual path to `server.py` in this repo.

### 4. Configure LM Studio (Llama 3.x / small LLMs)

Add to your LM Studio MCP config:

```json
{
  "mcpServers": {
    "ableton-mcp": {
      "command": "uv",
      "args": ["run", "--with", "mcp", "--with", "rapidfuzz", "/path/to/server.py"]
    }
  }
}
```

**Recommended system prompt for small models:**
```
You are a music production assistant with access to Ableton Live via MCP tools.
When asked to find or load a plugin, always call search_browser first to get the URI,
then call load_instrument_or_effect with that URI.
Always use tools when available. URIs always start with "query:" — never invent them.
```

**Context length:** Set to at least 8192 tokens. The tool schemas consume ~2000 tokens before your message starts.

---

## Available Tools

### Session & Tracks
| Tool | Description |
|------|-------------|
| `get_session_info` | Get tempo, time signature, track count |
| `get_track_info` | Get track name, devices, and clips |
| `create_midi_track` | Create a new MIDI track |
| `set_track_name` | Rename a track |
| `set_tempo` | Set BPM |
| `start_playback` / `stop_playback` | Transport control |

### Clips & MIDI
| Tool | Description |
|------|-------------|
| `create_clip` | Create a MIDI clip in a slot |
| `add_notes_to_clip` | Add MIDI notes to a clip |
| `set_clip_name` | Rename a clip |
| `fire_clip` / `stop_clip` | Trigger or stop a clip |

### Browser & Loading
| Tool | Description |
|------|-------------|
| `search_browser` | **Fuzzy search** for any plugin, instrument, or effect |
| `load_instrument_or_effect` | Load a plugin onto a track using a URI |
| `get_browser_tree` | Get raw browser tree as JSON |
| `get_browser_items_at_path` | Get items at a specific browser path |
| `load_drum_kit` | Load a drum rack and kit |
| `invalidate_browser_cache` | Force cache refresh after installing plugins |

### Plugin Parameter Control *(new in this fork)*
| Tool | Description |
|------|-------------|
| `get_bridge_status` | Check whether the Extensions SDK bridge is active |
| `get_device_parameters` | Read all parameters for a plugin (bridge or `_Framework`) |
| `set_device_parameter` | Set a parameter by name or index (bridge or `_Framework`) |
| `save_device_snapshot` | Save all parameter values as a named snapshot (bridge or `_Framework`) |
| `recall_device_snapshot` | Restore a saved snapshot — bulk restore via bridge when available |
| `get_chain_device_parameters` | Read parameters inside a Rack chain (`_Framework`) |

---

## Usage Examples

```
"Search for the UAD 1176 compressor and load it on track 2"
→ search_browser("1176") → load_instrument_or_effect(track=2, uri=...)

"Make the Wavetable synth on track 3 brighter"
→ get_device_parameters(track=3, device=0)
→ set_device_parameter(track=3, device=0, param_name="Flt 1 Freq", value=0.8)

"Save the current synth settings as 'bright_lead'"
→ save_device_snapshot(track=3, device=0, snapshot_name="bright_lead")

"Recall the 'bright_lead' snapshot"
→ recall_device_snapshot(track=3, device=0, snapshot_name="bright_lead")
```

---

## Plugin Parameter Notes

**Native Ableton devices** (Wavetable, Operator, Analog, Drift, etc.) expose all parameters directly — typically 60–100+ parameters per device.

**Third-party VST3/AU plugins** (NI, Arturia, UAD, etc.) expose only what the plugin manufacturer chooses to expose. For NI and Arturia instruments this is typically 8 macro controls. To maximise control:

1. Right-click the device in the chain → **Group** (creates an Instrument Rack)
2. Open the Rack's macro view
3. Right-click parameters inside the plugin → **Map to Macro**

UAD audio effects (1176, SSL E, Neve etc.) expose their full parameter set directly without any additional setup.

---

## Credits

This project is built on top of [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) by [Siddharth Ahuja](https://github.com/ahujasid). The original architecture, socket protocol, and core toolset are his work. 

Extensions developed for advanced music production workflows with Claude and LM Studio.

---

## Contributing

PRs welcome. The most impactful upstream contributions would be the type coercion fixes and device parameter tools — consider raising those against [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) directly.

## License

MIT — same as the original project.
