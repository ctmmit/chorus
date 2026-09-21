#!/usr/bin/env bash
# sync_private_fixtures.sh — copy the real show transcripts from the private
# fixtures repo into fixtures/transcripts/ (gitignored here). Looks for a
# sibling checkout first, then clones via gh.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$PROJECT_DIR/fixtures/transcripts"
SIBLING="$(dirname "$PROJECT_DIR")/chorus-private"

[ -d "$SIBLING" ] || gh repo clone ctmmit/chorus-private "$SIBLING"
cp "$SIBLING"/fixtures/transcripts/*.json "$DEST"/
echo "synced $(ls "$DEST"/*.json | wc -l) transcript(s) into fixtures/transcripts/"
