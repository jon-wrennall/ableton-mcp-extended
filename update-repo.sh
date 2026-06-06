#!/bin/bash
# update-repo.sh — stage, commit, and push all changes to origin/main
# Usage: ./update-repo.sh "your commit message"

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

MSG="${1:-Update midi_cc maps and config}"

echo "→ Staging all changes..."
git add -A

if git diff --cached --quiet; then
  echo "Nothing to commit — working tree clean."
  exit 0
fi

echo "→ Committing: $MSG"
git commit -m "$MSG"

echo "→ Pushing to origin/main..."
git push origin main

echo "✓ Done."
