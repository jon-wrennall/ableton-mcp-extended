#!/usr/bin/env bash
# fix-review-issues.sh — applies all CodeRabbit/Qodo fixes and opens a follow-up PR
# Run from Terminal: ./fix-review-issues.sh

set -euo pipefail

TOKEN="${GITHUB_TOKEN:-$(git -C ~/Documents/Claude/Projects/Music/ableton-mcp-extended remote get-url origin | grep -o 'ghp_[^@]*')}"
WORK_DIR="$(mktemp -d)/AbletonBridge-fix2"
BRANCH="fix/midi-cc-review-issues"

echo "→ Syncing fork with upstream and cloning..."
# Sync fork via API
curl -s -X POST \
  -H "Authorization: token $TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/jon-wrennall/AbletonBridge/merge-upstream" \
  -d '{"branch":"main"}' > /dev/null 2>&1 || true

mkdir -p "$WORK_DIR"
git clone "https://${TOKEN}@github.com/jon-wrennall/AbletonBridge.git" "$WORK_DIR"
cd "$WORK_DIR"
git config user.email "jon@remote-eyes.co.uk"
git config user.name "Jon Wrennall"
git checkout -b "$BRANCH"

echo "→ Fixing midi_cc.py..."
python3 << 'PYEOF'
with open("MCP_Server/tools/midi_cc.py", encoding="utf-8") as f:
    src = f.read()

# Fix 1: UTF-8 encoding on file opens
src = src.replace(
    'with open(path) as f:',
    'with open(path, encoding="utf-8") as f:'
)

# Fix 2: channel validation when explicitly provided
src = src.replace(
    '        if midi_channel is not None:\n'
    '            channel = _i(midi_channel)\n'
    '        elif track_index in _track_cc_channels:',
    '        if midi_channel is not None:\n'
    '            channel = _i(midi_channel)\n'
    '            if channel < 1 or channel > 16:\n'
    '                return f"Error: midi_channel must be 1\\u201316, got {channel}."\n'
    '        elif track_index in _track_cc_channels:'
)

# Fix 3: iterate all devices to find the one with a CC map (not just devices[0])
# Replace the plugin name lookup block in set_plugin_parameter_cc
src = src.replace(
    '        try:\n'
    '            ableton = get_ableton_connection()\n'
    '            track_info = ableton.send_command("get_track_info", {"track_index": track_index})\n'
    '            devices = track_info.get("devices", [])\n'
    '            plugin_name = devices[0].get("name", "") if devices else ""\n'
    '        except Exception as e:\n'
    '            return f"Could not read track {track_index}: {e}"',
    '        try:\n'
    '            ableton = get_ableton_connection()\n'
    '            track_info = ableton.send_command("get_track_info", {"track_index": track_index})\n'
    '            devices = track_info.get("devices", [])\n'
    '            # Search all devices for a CC map match — target plugin may not be first\n'
    '            plugin_name = ""\n'
    '            for _dev in devices:\n'
    '                _name = _dev.get("name", "")\n'
    '                if _name and _find_cc_map_for_plugin(_name):\n'
    '                    plugin_name = _name\n'
    '                    break\n'
    '            if not plugin_name and devices:\n'
    '                plugin_name = devices[0].get("name", "")\n'
    '        except Exception as e:\n'
    '            return f"Could not read track {track_index}: {e}"'
)

# Fix 3 also in get_cc_map
src = src.replace(
    '        try:\n'
    '            ableton = get_ableton_connection()\n'
    '            track_info = ableton.send_command("get_track_info", {"track_index": track_index})\n'
    '            devices = track_info.get("devices", [])\n'
    '            plugin_name = devices[0].get("name", "") if devices else ""\n'
    '        except Exception as e:\n'
    '            return f"Could not read track {track_index}: {e}"',
    '        try:\n'
    '            ableton = get_ableton_connection()\n'
    '            track_info = ableton.send_command("get_track_info", {"track_index": track_index})\n'
    '            devices = track_info.get("devices", [])\n'
    '            # Search all devices for a CC map match — target plugin may not be first\n'
    '            plugin_name = ""\n'
    '            for _dev in devices:\n'
    '                _name = _dev.get("name", "")\n'
    '                if _name and _find_cc_map_for_plugin(_name):\n'
    '                    plugin_name = _name\n'
    '                    break\n'
    '            if not plugin_name and devices:\n'
    '                plugin_name = devices[0].get("name", "")\n'
    '        except Exception as e:\n'
    '            return f"Could not read track {track_index}: {e}"'
)

with open("MCP_Server/tools/midi_cc.py", "w", encoding="utf-8") as f:
    f.write(src)
print("  midi_cc.py fixed")
PYEOF

echo "→ Fixing pyproject.toml packaging..."
python3 << 'PYEOF'
with open("pyproject.toml", encoding="utf-8") as f:
    src = f.read()

# Merge our separate package-data section into the existing one
# Existing: MCP_Server = ["browser_cache_seed.json"]
# Our add:  "MCP_Server" = ["midi_cc/*.json"]
# Result:   MCP_Server = ["browser_cache_seed.json", "midi_cc/*.json"]
src = src.replace(
    'MCP_Server = ["browser_cache_seed.json"]',
    'MCP_Server = ["browser_cache_seed.json", "midi_cc/*.json"]'
)
# Remove our duplicate section if present
if '"MCP_Server" = ["midi_cc/*.json"]' in src:
    lines = src.splitlines()
    out = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        if line.strip() == '"MCP_Server" = ["midi_cc/*.json"]':
            skip_next = False
            continue
        out.append(line)
    src = "\n".join(out) + "\n"

with open("pyproject.toml", "w", encoding="utf-8") as f:
    f.write(src)
print("  pyproject.toml fixed")
PYEOF

echo "→ Fixing JSON map issues..."
python3 << 'PYEOF'
import json, os

fixes = {}

# Fix: ni_massive_x.json — remove "Massive" (matches original Massive plugin)
p = "MCP_Server/midi_cc/ni_massive_x.json"
with open(p, encoding="utf-8") as f: d = json.load(f)
d["match_patterns"] = [x for x in d["match_patterns"] if x != "Massive"]
d["notes"] = d.get("notes","") + " Note: 'Massive' removed from match_patterns to avoid collision with the original NI Massive (2007)."
fixes[p] = d

# Fix: ni_scarbee_bass.json — remove bare "Scarbee" (too broad)
p = "MCP_Server/midi_cc/ni_scarbee_bass.json"
with open(p, encoding="utf-8") as f: d = json.load(f)
d["match_patterns"] = [x for x in d["match_patterns"] if x != "Scarbee"]
fixes[p] = d

# Fix: arturia_opxa_v.json — remove "Oberheim" (too broad, matches Matrix-12)
p = "MCP_Server/midi_cc/arturia_opxa_v.json"
with open(p, encoding="utf-8") as f: d = json.load(f)
d["match_patterns"] = [x for x in d["match_patterns"] if x != "Oberheim"]
fixes[p] = d

# Fix: arturia_analog_lab.json — Release CC 72 conflicts with Macro 4 CC 72
p = "MCP_Server/midi_cc/arturia_analog_lab.json"
with open(p, encoding="utf-8") as f: d = json.load(f)
if "Release" in d["parameters"] and d["parameters"]["Release"]["cc"] == 72:
    d["parameters"]["Release"]["cc"] = 80
    d["parameters"]["Release"]["description"] += " (remapped from CC 72 to avoid conflict with Macro 4)"
fixes[p] = d

# Fix: arturia_cs80_v.json — Brilliance CC 74 conflicts with Ch1 Filter Cutoff CC 74
p = "MCP_Server/midi_cc/arturia_cs80_v.json"
with open(p, encoding="utf-8") as f: d = json.load(f)
if "Brilliance" in d["parameters"] and d["parameters"]["Brilliance"]["cc"] == 74:
    d["parameters"]["Brilliance"]["cc"] = 75
fixes[p] = d

# Fix: arturia_piano_v.json — EQ High CC 74 conflicts with Bright CC 74
p = "MCP_Server/midi_cc/arturia_piano_v.json"
with open(p, encoding="utf-8") as f: d = json.load(f)
if "EQ High" in d["parameters"] and d["parameters"]["EQ High"]["cc"] == 74:
    d["parameters"]["EQ High"]["cc"] = 75
    d["parameters"]["EQ High"]["description"] = d["parameters"]["EQ High"].get("description","") + " (remapped from CC 74 to avoid conflict with Bright)"
if "Bright" in d["parameters"] and d["parameters"]["Bright"]["cc"] == 74:
    pass  # keep Bright on CC 74, it's the primary brightness control
fixes[p] = d

# Fix: ni_symphony_brass.json — Close Mic CC 15 conflicts with Quick Control 2 CC 15
p = "MCP_Server/midi_cc/ni_symphony_brass.json"
with open(p, encoding="utf-8") as f: d = json.load(f)
if "Close Mic" in d["parameters"] and d["parameters"]["Close Mic"]["cc"] == 15:
    # QC2 (CC 15) IS the close mic — merge the description and remove the duplicate
    d["parameters"]["Quick Control 2"]["description"] = "Close Mic Level — " + d["parameters"]["Quick Control 2"]["description"]
    del d["parameters"]["Close Mic"]
fixes[p] = d

# Write all fixes
for path, data in fixes.items():
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"  Fixed: {os.path.basename(path)}")
PYEOF

echo "→ Committing and pushing..."
git add -A
git commit -m "fix: address CodeRabbit/Qodo review feedback on MIDI CC module

- midi_cc.py: iterate all track devices to find CC map match, not just
  devices[0] — fixes map lookup when plugin is not the first device
- midi_cc.py: validate explicit midi_channel argument is in 1-16 range
- midi_cc.py: add encoding='utf-8' to all JSON file opens
- pyproject.toml: add midi_cc/*.json to existing MCP_Server package-data
  entry (previously in a separate section that didn't merge correctly)
- ni_massive_x.json: remove 'Massive' match pattern (collides with
  original NI Massive plugin, different instrument/parameters)
- ni_scarbee_bass.json: remove bare 'Scarbee' match pattern (too broad,
  can collide with other Scarbee instrument maps)
- arturia_opxa_v.json: remove 'Oberheim' match pattern (too broad,
  would incorrectly match Matrix-12 V)
- arturia_analog_lab.json: remap Release from CC 72 to CC 80 to resolve
  conflict with Macro 4 (also CC 72)
- arturia_cs80_v.json: remap Brilliance from CC 74 to CC 75 to resolve
  conflict with Ch1 Filter Cutoff
- arturia_piano_v.json: remap EQ High from CC 74 to CC 75 to resolve
  conflict with Bright
- ni_symphony_brass.json: remove duplicate Close Mic entry (CC 15 is
  already Quick Control 2); merge description into QC2"

git push "https://${TOKEN}@github.com/jon-wrennall/AbletonBridge.git" "$BRANCH"

echo "→ Opening follow-up PR..."
PR=$(curl -s -X POST \
  -H "Authorization: token $TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/hidingwill/AbletonBridge/pulls" \
  -d "{
    \"title\": \"fix: address CodeRabbit/Qodo review feedback on MIDI CC module\",
    \"head\": \"jon-wrennall:$BRANCH\",
    \"base\": \"main\",
    \"draft\": false,
    \"body\": \"Follow-up to #8 addressing all CodeRabbit and Qodo review comments.\n\n## Changes\n\n### midi_cc.py\n- **Device lookup**: now iterates all devices on a track to find a CC map match — fixes the case where the target instrument is not the first device (e.g. when a MIDI effect or rack precedes it)\n- **Channel validation**: explicit \`midi_channel\` argument is now validated to be in the 1–16 range, consistent with \`assign_cc_channel\` and \`send_raw_cc\`\n- **UTF-8**: all JSON file opens now specify \`encoding='utf-8'\`\n\n### pyproject.toml\n- **Packaging**: \`midi_cc/*.json\` added to the existing \`MCP_Server\` package-data entry so maps are included in wheel/pip distributions\n\n### JSON map fixes (match_patterns)\n- \`ni_massive_x.json\`: removed \`'Massive'\` — would incorrectly match the original NI Massive (2007) plugin\n- \`ni_scarbee_bass.json\`: removed bare \`'Scarbee'\` — too broad, collides with other Scarbee instrument maps\n- \`arturia_opxa_v.json\`: removed \`'Oberheim'\` — too broad, would incorrectly match Matrix-12 V\n\n### JSON map fixes (duplicate CC assignments)\n- \`arturia_analog_lab.json\`: Release remapped CC 72 → CC 80 (conflict with Macro 4)\n- \`arturia_cs80_v.json\`: Brilliance remapped CC 74 → CC 75 (conflict with Ch1 Filter Cutoff)\n- \`arturia_piano_v.json\`: EQ High remapped CC 74 → CC 75 (conflict with Bright)\n- \`ni_symphony_brass.json\`: duplicate Close Mic entry removed (CC 15 = Quick Control 2; description merged)\n\"
  }")

PR_URL=$(echo "$PR" | python3 -c "import sys,json; print(json.load(sys.stdin).get('html_url','(check GitHub)'))" 2>/dev/null || echo "(check GitHub)")
echo ""
echo "✓ Done! PR opened: $PR_URL"
