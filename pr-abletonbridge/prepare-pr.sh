#!/usr/bin/env bash
# prepare-pr.sh — forks hidingwill/AbletonBridge, applies CC maps + tool module, pushes PR branch.
#
# Run from your Mac (requires network access and a GitHub token with repo scope):
#   chmod +x prepare-pr.sh && ./prepare-pr.sh
#
# What it does:
#   1. Forks hidingwill/AbletonBridge into your GitHub account
#   2. Clones the fork
#   3. Copies the midi_cc/ maps from ableton-mcp-extended
#   4. Copies MCP_Server/tools/midi_cc.py
#   5. Patches MCP_Server/server.py to register the new tools
#   6. Updates pyproject.toml with optional mido dependency
#   7. Pushes the branch and opens a draft PR

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────

# Your GitHub personal access token (repo scope required).
# Set it in your shell before running:  export GITHUB_TOKEN=ghp_...
# Or retrieve it from your git remote:
#   git -C ~/Documents/Claude/Projects/Music/ableton-mcp-extended remote get-url origin | grep -o 'ghp_[^@]*'
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
if [ -z "$GITHUB_TOKEN" ]; then
  echo "ERROR: GITHUB_TOKEN is not set. Export it first:"
  echo "  export GITHUB_TOKEN=\$(git -C ~/Documents/Claude/Projects/Music/ableton-mcp-extended remote get-url origin | grep -o 'ghp_[^@]*')"
  exit 1
fi
GITHUB_USER="jon-wrennall"
UPSTREAM="hidingwill/AbletonBridge"
BRANCH="feature/midi-cc-plugin-maps"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"   # ableton-mcp-extended root
WORK_DIR="$(mktemp -d)/AbletonBridge-pr"

# ── Step 1: Fork ──────────────────────────────────────────────────────────────

echo "→ Forking $UPSTREAM into $GITHUB_USER/AbletonBridge..."
FORK_RESPONSE=$(curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/$UPSTREAM/forks" \
  -d '{}')

FORK_URL=$(echo "$FORK_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('clone_url',''))" 2>/dev/null || true)

if [ -z "$FORK_URL" ]; then
  # Fork may already exist
  FORK_URL="https://github.com/$GITHUB_USER/AbletonBridge.git"
  echo "  Fork may already exist — using $FORK_URL"
else
  echo "  Fork created: $FORK_URL"
  echo "  Waiting 5s for GitHub to initialise the fork..."
  sleep 5
fi

FORK_URL_WITH_TOKEN="${FORK_URL/https:\/\//https://${GITHUB_TOKEN}@}"

# ── Step 2: Clone ─────────────────────────────────────────────────────────────

echo "→ Cloning fork to $WORK_DIR..."
mkdir -p "$WORK_DIR"
git clone "$FORK_URL_WITH_TOKEN" "$WORK_DIR"
cd "$WORK_DIR"

git config user.email "jon@remote-eyes.co.uk"
git config user.name "Jon Wrennall"
git checkout -b "$BRANCH"

# ── Step 3: Copy midi_cc maps ─────────────────────────────────────────────────

echo "→ Copying 100 midi_cc JSON maps..."
mkdir -p midi_cc
cp "$SOURCE_REPO/midi_cc/"*.json midi_cc/
echo "  $(ls midi_cc/*.json | wc -l | tr -d ' ') maps copied."

# ── Step 4: Copy tool module ──────────────────────────────────────────────────

echo "→ Copying MCP_Server/tools/midi_cc.py..."
cp "$SCRIPT_DIR/MCP_Server/tools/midi_cc.py" MCP_Server/tools/midi_cc.py

# ── Step 5: Patch MCP_Server/tools/__init__.py ───────────────────────────────
#
# AbletonBridge registers tools via register_all_tools() in tools/__init__.py.
# We add midi_cc to the module imports and one register_tools(mcp) call.

echo "→ Patching MCP_Server/tools/__init__.py..."

INIT="MCP_Server/tools/__init__.py"

if grep -q "midi_cc" "$INIT"; then
  echo "  __init__.py already patched — skipping."
else
  python3 - <<PYEOF
with open("$INIT") as f:
    src = f.read()

# 1. Add midi_cc to the import tuple
src = src.replace(
    "    session, tracks, clips, devices, browser, mixer,\n"
    "    automation, arrangement, scenes, creative, m4l_tools,\n"
    "    snapshots, audio, grid, workflows,",
    "    session, tracks, clips, devices, browser, mixer,\n"
    "    automation, arrangement, scenes, creative, m4l_tools,\n"
    "    snapshots, audio, grid, workflows,\n"
    "    midi_cc,"
)

# 2. Add registration call at the end of register_all_tools()
src = src.replace(
    "    workflows.register_tools(mcp)\n",
    "    workflows.register_tools(mcp)\n"
    "    midi_cc.register_tools(mcp)\n"
)

with open("$INIT", "w") as f:
    f.write(src)

print("  __init__.py patched successfully.")
PYEOF
fi

# ── Step 6: Update pyproject.toml ────────────────────────────────────────────

echo "→ Adding optional mido dependency to pyproject.toml..."

if grep -q "mido" pyproject.toml; then
  echo "  mido already present — skipping."
else
  python3 - <<'PYEOF'
import re

with open("pyproject.toml") as f:
    src = f.read()

# Add after the last dependency line in [project.optional-dependencies] or [project]
# Try to add under [project] dependencies list
if '[project.optional-dependencies]' in src:
    # Add a 'midi_cc' group
    idx = src.index('[project.optional-dependencies]')
    end = src.index('\n', idx) + 1
    new_section = 'midi_cc = [\n    "mido>=1.3",\n    "python-rtmidi>=1.5",\n]\n'
    src = src[:end] + new_section + src[end:]
else:
    # Append optional-dependencies section at end
    src += '\n[project.optional-dependencies]\nmidi_cc = [\n    "mido>=1.3",\n    "python-rtmidi>=1.5",\n]\n'

with open("pyproject.toml", "w") as f:
    f.write(src)

print("  pyproject.toml updated.")
PYEOF
fi

# ── Step 7: Commit and push ───────────────────────────────────────────────────

echo "→ Committing..."
git add midi_cc/ MCP_Server/tools/midi_cc.py MCP_Server/tools/__init__.py pyproject.toml
git commit -m "feat: add MIDI CC plugin control — 100 maps for NI Komplete + Arturia V Collection 11

Adds a new tools/midi_cc.py module and a midi_cc/ directory containing 100 JSON
parameter maps covering:

  NI Komplete Collector's Edition (55 maps):
  - All Abbey Road Drummer series (50s/60s/70s/80s/Modern)
  - Studio Drummer, Session Strings/Horns/Guitarist/Bassist/Percussionist
  - Symphony Series (Strings/Brass/Woodwinds/Percussion)
  - Emotive Strings, Analog Dreams, Choir Omnia
  - All Scarbee instruments (Mark I, A-200, Clavinet Pianet, Bass series)
  - Piano instruments (The Giant, Grandeur, Gentleman, Maverick, Noire, Una Corda)
  - Vintage Organs, Kontakt 8 / Factory Library
  - Reaktor synths (Monark, Super 8, Rounds, Form, Retro Machines Mk2)
  - Cinematic: Damage 2, Action Strikes, Thrill, Straylight, Pharlight, Mysteria
  - Kinetic Toys/Metal, Cuba, West Africa, George Duke Soul Treasures
  - Battery 4, Massive X, FM8, Guitar Rig 7, Replika XT, Raum

  Arturia V Collection 11 Pro (45 maps — full collection):
  - All analog synths: ARP 2600, Mini V, CS-80, Prophet-5, OB-Xa, Jup-8,
    Jun-6, Jup-8000, SEM, Matrix-12, MS-20, Modular, MiniBrute, Synthi, Synthx
  - Digital synths: DX7, SQ80, Synclavier, CMI, CZ, Emulator II, Prophet VS
  - Keys/organs: B-3, Farfisa, Vox Continental, Mellotron, Solina,
    Stage-73, Wurli, Clavinet, CP-70, Piano V
  - Augmented Series: Strings, Brass, Woodwinds, Grand Piano, Voices, Mallets, Yangtze
  - Buchla Easel, MiniFreak, Pure Lo-Fi, Vocoder V, Analog Lab Pro

Four new tools:
  set_plugin_parameter_cc(track, param_name, value)  — set by name via CC
  get_cc_map(track)                                  — show full map for loaded plugin
  list_cc_maps(manufacturer='all')                   — list all 100 maps
  assign_cc_channel(track, channel)                  — override default MIDI channel
  send_raw_cc(channel, cc_number, value)             — raw CC without map lookup

Requires mido + python-rtmidi (optional — tools report unavailable if missing).
In Ableton: set plugin track MIDI input to 'AbletonBridge' on track_index+1 channel.

This fills the one significant gap in AbletonBridge: Kontakt and Arturia VSTs
expose almost nothing through VST3 Configure mode. MIDI CC is the reliable
real-time control path for these instruments."

echo "→ Pushing branch $BRANCH..."
git push origin "$BRANCH"

# ── Step 8: Create draft PR ───────────────────────────────────────────────────

echo "→ Opening draft PR..."
PR_RESPONSE=$(curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/$UPSTREAM/pulls" \
  -d "{
    \"title\": \"feat: MIDI CC plugin control — 100 maps for NI Komplete + Arturia V Collection 11\",
    \"head\": \"$GITHUB_USER:$BRANCH\",
    \"base\": \"main\",
    \"draft\": true,
    \"body\": \"## What this adds\\n\\nA new \`tools/midi_cc.py\` module and \`midi_cc/\` directory with **100 JSON parameter maps** covering the two most common plugin suites that VST3 Configure mode can't meaningfully control.\\n\\n### The problem\\n\\nKontakt instruments (NI Komplete) and Arturia V Collection expose almost nothing through Live's Configure mode — typically 0–8 parameters vs the 30–100+ you actually want to control. MIDI CC is the reliable real-time path for these instruments.\\n\\n### What's included\\n\\n**NI Komplete Collector's Edition — 55 maps**\\n- All Abbey Road Drummer series (50s/60s/70s/80s/Modern)\\n- Studio Drummer (The Fab/Beast/Classic)\\n- Full Session Series (Strings/Horns/Guitarist×7/Bassist×3/Percussionist)\\n- Symphony Series (Strings/Brass/Woodwinds/Percussion) + Emotive Strings\\n- All Scarbee instruments (Mark I Rhodes, A-200 Wurlitzer, Clavinet Pianet, MM/Pre/Jay/Rickenbacker Bass)\\n- Piano collection (The Giant, Grandeur, Gentleman, Maverick, Noire, Una Corda, Alicia's Keys, Electric Keys)\\n- Organs, Kontakt 8/Factory Library, Massive X, FM8, Guitar Rig 7\\n- Reaktor synths: Monark (Moog), Super 8, Rounds, Form, Retro Machines Mk2\\n- Cinematic: Damage 2, Action Strikes, Thrill, Straylight, Pharlight, Mysteria, Kinetic Toys/Metal\\n- World: Cuba, West Africa, George Duke Soul Treasures\\n- Battery 4, Analog Dreams, Choir Omnia, Replika XT, Raum\\n\\n**Arturia V Collection 11 Pro — 45 maps (full collection)**\\n- All 45 instruments: ARP 2600, CS-80, Prophet-5, OB-Xa, Jupiter-8, Juno-6, Jup-8000, SEM, Matrix-12, MS-20, Modular V, MiniBrute, Buchla Easel, Synthi, Synthx, DX7, SQ80, Synclavier, CMI (Fairlight), CZ, Emulator II, Prophet VS, B-3, Farfisa, Vox Continental, Mellotron, Solina, Stage-73, Wurli, Clavinet, CP-70, Piano V, all 7 Augmented Series, MiniFreak, Pure Lo-Fi, Vocoder V, Analog Lab Pro\\n\\n### New tools\\n\\n| Tool | Description |\\n|---|---|\\n| \`set_plugin_parameter_cc\` | Set a parameter by name via CC — auto-matches plugin to map |\\n| \`get_cc_map\` | Show the full CC map for the plugin on a given track |\\n| \`list_cc_maps\` | List all 100 maps, filterable by manufacturer |\\n| \`assign_cc_channel\` | Override the default MIDI channel for a track |\\n| \`send_raw_cc\` | Send a raw CC without map lookup |\\n\\n### Setup\\n\\n1. \`pip install mido python-rtmidi\` (optional — tools report unavailable if missing)\\n2. In Ableton: set the plugin track's MIDI input to **'AbletonBridge'** on channel **track_index + 1**\\n\\n### Design notes\\n\\n- All 100 maps are pure JSON in \`midi_cc/\` — easy to add/edit without touching Python\\n- Map matching uses \`match_patterns\` for flexible plugin name matching\\n- Graceful fallback: if mido isn't installed, all 5 tools return a clear install message rather than crashing\\n- No changes to existing tool modules, connections, or Remote Script\\n- Follows the existing \`register_tools(mcp)\` pattern exactly\\n\"
  }")

PR_URL=$(echo "$PR_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('html_url','(check GitHub)'))" 2>/dev/null || echo "(check GitHub)")
echo ""
echo "✓ Done! Draft PR opened: $PR_URL"
echo ""
echo "Branch is at: https://github.com/$GITHUB_USER/AbletonBridge/tree/$BRANCH"
echo "Working copy at: $WORK_DIR"
