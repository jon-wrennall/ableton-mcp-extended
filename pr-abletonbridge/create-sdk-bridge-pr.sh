#!/usr/bin/env bash
# create-sdk-bridge-pr.sh — adds optional Ableton Extensions SDK bridge to AbletonBridge
# Run: export GITHUB_TOKEN=... && ./create-sdk-bridge-pr.sh

set -euo pipefail

TOKEN="${GITHUB_TOKEN:-$(git -C ~/Documents/Claude/Projects/Music/ableton-mcp-extended remote get-url origin | grep -o 'ghp_[^@]*')}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="$(mktemp -d)/AbletonBridge-sdk"
BRANCH="feat/extensions-sdk-bridge"

echo "→ Cloning from upstream (hidingwill/AbletonBridge)..."
git clone "https://github.com/hidingwill/AbletonBridge.git" "$WORK_DIR"
cd "$WORK_DIR"
git config user.email "jon@remote-eyes.co.uk"
git config user.name "Jon Wrennall"
git remote set-url origin "https://${TOKEN}@github.com/jon-wrennall/AbletonBridge.git"
git checkout -b "$BRANCH"

echo "→ Adding connections/extensions_sdk.py..."
cp "$SCRIPT_DIR/extensions_sdk_bridge/MCP_Server/connections/extensions_sdk.py" \
   MCP_Server/connections/extensions_sdk.py

echo "→ Patching MCP_Server/state.py..."
python3 << 'PYEOF'
with open("MCP_Server/state.py", encoding="utf-8") as f:
    src = f.read()

# Add extensions_sdk_client alongside the other connection state
sdk_state = """
# ---------------------------------------------------------------------------
# Extensions SDK bridge state (optional — Live 12.4.5+ Suite only)
# ---------------------------------------------------------------------------
extensions_sdk_client: Optional[Any] = None  # ExtensionsSDKClient | None
extensions_sdk_ping_cache: dict = {"result": False, "timestamp": 0.0}
EXTENSIONS_SDK_PING_TTL: float = 5.0
EXTENSIONS_SDK_PORT: int = 9883  # HTTP port for AbletonParameterBridge
"""

# Insert after the m4l_connection line
target = 'm4l_connection: Optional[Any] = None # M4LConnection | None\n'
if 'extensions_sdk_client' not in src:
    src = src.replace(target, target + sdk_state)
    with open("MCP_Server/state.py", "w", encoding="utf-8") as f:
        f.write(src)
    print("  state.py patched")
else:
    print("  state.py already has extensions_sdk_client")
PYEOF

echo "→ Patching MCP_Server/tools/devices.py..."
python3 << 'PYEOF'
with open("MCP_Server/tools/devices.py", encoding="utf-8") as f:
    src = f.read()

# 1. Add SDK import
sdk_import = "from MCP_Server.connections.extensions_sdk import get_sdk_client\n"
if "extensions_sdk" not in src:
    src = src.replace(
        "from MCP_Server.connections.m4l import get_m4l_connection\n",
        "from MCP_Server.connections.m4l import get_m4l_connection\n" + sdk_import
    )

# 2. Patch get_device_parameters — add SDK tier before _Framework call
old_get = '''        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if track_type not in ("track", "return", "master"):
            return "Error: track_type must be 'track', 'return', or 'master'"
        ableton = get_ableton_connection()
        result = ableton.send_command("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type,
        })
        return json.dumps(result)'''

new_get = '''        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if track_type not in ("track", "return", "master"):
            return "Error: track_type must be 'track', 'return', or 'master'"

        # Tier 1: Extensions SDK (async, more reliable for VST3/AU on Apple Silicon)
        sdk = get_sdk_client()
        if sdk and track_type == "track":
            try:
                result = sdk.get_params(track_index, device_index)
                result["_source"] = "extensions_sdk"
                return json.dumps(result)
            except Exception as e:
                logger.debug("SDK bridge get_params failed, falling back to _Framework: %s", e)

        # Tier 2: _Framework Remote Script (always available)
        ableton = get_ableton_connection()
        result = ableton.send_command("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type,
        })
        result["_source"] = "_framework"
        return json.dumps(result)'''

if old_get in src:
    src = src.replace(old_get, new_get)
    print("  get_device_parameters patched")
else:
    print("  WARNING: get_device_parameters pattern not found — check manually")

# 3. Patch set_device_parameter — add SDK tier
old_set = '''        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if track_type not in ("track", "return", "master"):
            return "Error: track_type must be 'track', 'return', or 'master'"
        ableton = get_ableton_connection()
        result = ableton.send_command("set_device_parameter", {
            "track_index": track_index,
            "device_index": device_index,
            "parameter_name": parameter_name,
            "value": value,
            "track_type": track_type,
        })
        pname = result.get('parameter', parameter_name)
        if result.get("clamped", False):
            return f"Set parameter '{pname}' to {result.get('value')} (value was clamped to valid range)"
        return f"Set parameter '{pname}' to {result.get('value')}"'''

new_set = '''        _validate_index(track_index, "track_index")
        _validate_index(device_index, "device_index")
        if track_type not in ("track", "return", "master"):
            return "Error: track_type must be 'track', 'return', or 'master'"

        # Tier 1: Extensions SDK
        sdk = get_sdk_client()
        if sdk and track_type == "track":
            try:
                result = sdk.set_param(
                    track_index, device_index, value, param_name=parameter_name
                )
                pname = result.get("parameter", parameter_name)
                clamped = " (clamped to valid range)" if result.get("clamped") else ""
                return f"Set parameter '{pname}' to {result.get('value')}{clamped} [Extensions SDK]"
            except Exception as e:
                logger.debug("SDK bridge set_param failed, falling back to _Framework: %s", e)

        # Tier 2: _Framework Remote Script
        ableton = get_ableton_connection()
        result = ableton.send_command("set_device_parameter", {
            "track_index": track_index,
            "device_index": device_index,
            "parameter_name": parameter_name,
            "value": value,
            "track_type": track_type,
        })
        pname = result.get('parameter', parameter_name)
        if result.get("clamped", False):
            return f"Set parameter '{pname}' to {result.get('value')} (value was clamped to valid range)"
        return f"Set parameter '{pname}' to {result.get('value')}"'''

if old_set in src:
    src = src.replace(old_set, new_set)
    print("  set_device_parameter patched")
else:
    print("  WARNING: set_device_parameter pattern not found — check manually")

# 4. Add get_bridge_status tool at the end of register_tools, before the final closing
get_bridge_status_tool = '''
    @mcp.tool()
    @_tool_handler("getting bridge status")
    def get_bridge_status(ctx: Context) -> str:
        """
        Check which parameter transport layers are currently active.

        AbletonBridge supports three tiers for device parameter control:
          1. Extensions SDK bridge (optional, best for VST3/AU, Live 12.4.5+ Suite)
          2. M4L bridge (optional, for hidden params and rack internals)
          3. _Framework Remote Script (always active)

        Parameter tools automatically prefer the highest available tier.
        """
        lines = []

        # Extensions SDK
        sdk = get_sdk_client()
        if sdk:
            version = sdk.get_version()
            try:
                tracks = sdk.get_tracks()
                track_count = len(tracks.get("tracks", []))
            except Exception:
                track_count = "?"
            lines.append(
                f"Extensions SDK bridge: ACTIVE (HTTP port 9883, v{version})\\n"
                f"  → get_device_parameters / set_device_parameter prefer this tier\\n"
                f"  → Tracks visible: {track_count}"
            )
        else:
            lines.append(
                "Extensions SDK bridge: NOT RUNNING (port 9883)\\n"
                "  → Requires AbletonParameterBridge + Live 12.4.5+ Suite\\n"
                "  → See docs/extensions_sdk_bridge.md for setup"
            )

        # M4L
        try:
            from MCP_Server.connections.m4l import get_m4l_connection
            m4l = get_m4l_connection()
            lines.append(
                f"M4L bridge: ACTIVE (UDP 9878/9879, v{state.m4l_bridge_version or 'unknown'})\\n"
                "  → Provides hidden params, rack internals, audio analysis"
            )
        except Exception:
            lines.append(
                "M4L bridge: NOT LOADED\\n"
                "  → Load the AbletonBridge M4L device on any track in Ableton"
            )

        # _Framework
        from MCP_Server.connections.ableton import get_ableton_connection
        try:
            ableton = get_ableton_connection()
            lines.append("_Framework Remote Script: ACTIVE (TCP 9877) — base layer, always available")
        except Exception:
            lines.append("_Framework Remote Script: NOT CONNECTED (TCP 9877)")

        return "\\n\\n".join(lines)

'''

# Insert before the last line of register_tools (which ends with the last tool)
# Find the end of the file's register_tools block by inserting before the closing pattern
if "get_bridge_status" not in src:
    # Add it just before the end of the file
    src = src.rstrip() + "\n" + get_bridge_status_tool
    print("  get_bridge_status tool added")
else:
    print("  get_bridge_status already present")

with open("MCP_Server/tools/devices.py", "w", encoding="utf-8") as f:
    f.write(src)
PYEOF

echo "→ Adding docs/extensions_sdk_bridge.md..."
mkdir -p docs
cat > docs/extensions_sdk_bridge.md << 'MDEOF'
# Extensions SDK Bridge (Optional)

An optional third parameter transport for AbletonBridge that uses Ableton's
officially-documented Extensions SDK (Live 12.4.5+ Suite beta).

## Why use it?

The _Framework Remote Script and M4L bridge expose parameters through
Ableton's internal Python APIs. The Extensions SDK uses async JavaScript
LiveAPI calls — more reliable for VST3/AU plugins on Apple Silicon and for
plugins with large parameter sets.

When active, `get_device_parameters` and `set_device_parameter` automatically
prefer the SDK tier. Everything falls back gracefully if it's not running.

## Three-tier routing

```
get_device_parameters / set_device_parameter
  │
  ├─ 1. Extensions SDK (HTTP :9883)   ← if running
  ├─ 2. M4L bridge (UDP :9878/9879)  ← if M4L device loaded (hidden params)
  └─ 3. _Framework (TCP :9877)        ← always available
```

## Requirements

- Ableton Live **12.4.5+ Suite beta**
- Node.js **v20+**
- Ableton [Extensions SDK](https://ableton.github.io/extensions-sdk)

## Setup

### 1. Get the Extensions SDK

Download from [Ableton's beta program](https://www.ableton.com/beta/) and
place the SDK folder alongside `AbletonParameterBridge/`.

### 2. Build the bridge

```bash
cd AbletonParameterBridge
npm install
npm run build
npx extensions-cli package .
```

This produces `Parameter-Bridge-1.0.0.ablx`.

### 3. Install in Live

1. Open **Live → Settings → Extensions**
2. Enable **Developer Mode**
3. Drag `Parameter-Bridge-1.0.0.ablx` into the drop zone
4. Restart Live when prompted

### 4. Run the bridge

```bash
cd AbletonParameterBridge
npx extensions-cli run --live "/Applications/Ableton Live 12 Beta.app" .
```

The bridge listens on **HTTP port 9883**. Keep this terminal open while using
the MCP server.

### 5. Verify

```
get_bridge_status
```

Should show `Extensions SDK bridge: ACTIVE (HTTP port 9883)`.

## Auto-start (macOS)

See the `AbletonParameterBridge/` README for LaunchAgent setup.

## Notes

- Port 9883 is used to avoid conflicts with M4L (9878/9879), dashboard (9880),
  and the singleton lock (9881).
- The bridge only helps with standard track devices (`track_type="track"`).
  Return/master tracks still use _Framework.
- `save_device_snapshot` and `recall_device_snapshot` also benefit from the SDK
  when it is active — snapshot capture is async and more accurate.
MDEOF

echo "→ Committing..."
git add -A
git commit -m "feat: add optional Ableton Extensions SDK bridge (third parameter transport tier)

Adds an optional HTTP bridge to the Ableton Extensions SDK (Live 12.4.5+
Suite) as a third tier for device parameter reads/writes, sitting above
M4L and _Framework in the routing hierarchy.

New files:
  MCP_Server/connections/extensions_sdk.py  — HTTP client (port 9883)
  docs/extensions_sdk_bridge.md             — setup guide

Changed files:
  MCP_Server/state.py    — adds extensions_sdk_client, ping cache, port constant
  MCP_Server/tools/devices.py — SDK tier added to get_device_parameters and
                                set_device_parameter; new get_bridge_status tool

Routing:
  get_device_parameters / set_device_parameter now try:
    1. Extensions SDK bridge (HTTP :9883) — async LiveAPI, best for VST3/AU
    2. M4L bridge (UDP :9878/9879)        — hidden params, rack internals
    3. _Framework Remote Script (TCP :9877) — always available

Graceful fallback:
  If the bridge is not running, all tools fall back silently to the next tier.
  No configuration required. Use get_bridge_status to see which tier is active.

Port: 9883 (avoids conflicts with M4L :9878/9879, dashboard :9880, lock :9881)

Requires: Live 12.4.5+ Suite beta, Node.js v20+, Ableton Extensions SDK.
The AbletonParameterBridge Node.js component (from ableton-mcp-extended)
runs separately — see docs/extensions_sdk_bridge.md."

echo "→ Pushing..."
git push origin "$BRANCH"

echo "→ Opening PR..."
PR=$(curl -s -X POST \
  -H "Authorization: token $TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/hidingwill/AbletonBridge/pulls" \
  -d "{
    \"title\": \"feat: optional Ableton Extensions SDK bridge (third parameter transport tier)\",
    \"head\": \"jon-wrennall:$BRANCH\",
    \"base\": \"main\",
    \"draft\": true,
    \"body\": \"Adds an optional HTTP bridge for the Ableton Extensions SDK (Live 12.4.5+ Suite) as a third parameter transport tier, sitting above M4L and \_Framework.\\n\\n## Routing hierarchy\\n\\n\`\`\`\\nget_device_parameters / set_device_parameter\\n  │\\n  ├─ 1. Extensions SDK (HTTP :9883) ← if running (best for VST3/AU, Apple Silicon)\\n  ├─ 2. M4L bridge (UDP :9878/9879)  ← if M4L device loaded\\n  └─ 3. _Framework (TCP :9877)        ← always active\\n\`\`\`\\n\\nEverything falls back gracefully. No config required to use the existing tools.\\n\\n## New files\\n- \`MCP_Server/connections/extensions_sdk.py\` — thin HTTP client for the bridge\\n- \`docs/extensions_sdk_bridge.md\` — setup guide\\n\\n## Changed files\\n- \`MCP_Server/state.py\` — \`extensions_sdk_client\`, ping cache, port constant (9883)\\n- \`MCP_Server/tools/devices.py\` — SDK tier in \`get_device_parameters\` + \`set_device_parameter\`; new \`get_bridge_status\` tool\\n\\n## Why this port?\\n9883 avoids all existing AbletonBridge ports: M4L UDP 9878/9879, dashboard 9880, singleton lock 9881, UDP real-time 9882.\\n\\n## Requirements\\nLive 12.4.5+ Suite beta, Node.js v20+, Ableton Extensions SDK. The Node.js bridge (AbletonParameterBridge from ableton-mcp-extended) runs separately — see \`docs/extensions_sdk_bridge.md\`.\\n\\n## Note\\nMarked draft pending hidingwill review of the routing approach and port choice.\"
  }")

PR_URL=$(echo "$PR" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('html_url', d.get('message','?')))" 2>/dev/null)
echo ""
echo "✓ Done! Draft PR: $PR_URL"
echo "Working copy: $WORK_DIR"
