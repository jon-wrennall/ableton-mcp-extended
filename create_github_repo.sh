#!/bin/bash
# Push all files to the already-created GitHub repo.
# Run from inside the ableton-mcp-extended directory:
#   cd "/Users/jonwrennall/Documents/Claude/Projects/Music/ableton-mcp-extended"
#   bash create_github_repo.sh

set -e

echo "→ Initialising git..."
git init
git config user.name "Jon Wrennall"
git config user.email "jon@remote-eyes.co.uk"

git add .
git commit -m "Initial commit — ableton-mcp-extended

Extended fork of ahujasid/ableton-mcp with:
- Fuzzy browser search (search_browser)
- Deep plugin parameter control (get/set_device_parameters)
- Device snapshots (save/recall_device_snapshot)
- Type coercion fixes for small LLMs (Llama 3.x)
- Fixed get_browser_tree recursive traversal
- Fixed _find_browser_item_by_uri for VST/AU plugins
"

echo "→ Setting remote and pushing..."
git remote add origin "https://github.com/jon-wrennall/ableton-mcp-extended.git"
git branch -M main
git push -u origin main

echo ""
echo "✓ Done! https://github.com/jon-wrennall/ableton-mcp-extended"
