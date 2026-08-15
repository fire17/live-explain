#!/usr/bin/env bash
# live-explain (/lx) installer — batteries-included.
# Installs the skill + alias into ~/.claude/skills and checks the two runtime deps.
set -euo pipefail
SKILLS="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
SRC="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$SKILLS/live-explain/scripts" "$SKILLS/lx"
cp "$SRC/SKILL.md"                 "$SKILLS/live-explain/SKILL.md"
cp "$SRC/scripts/live_explain.py"  "$SKILLS/live-explain/scripts/live_explain.py"
cp "$SRC/lx/SKILL.md"              "$SKILLS/lx/SKILL.md"
echo "✓ installed /live-explain + /lx into $SKILLS"

miss=0
if ! command -v apiplan >/dev/null 2>&1; then
  echo "⚠ apiplan not found — the voice CLI. Install (also pulls bun):"
  echo "    curl -fsSL https://raw.githubusercontent.com/fire17/apiplan/main/install.sh | sh"
  miss=1
fi
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffplay >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then hint="brew install ffmpeg";
  elif command -v apt >/dev/null 2>&1; then hint="sudo apt install -y ffmpeg";
  else hint="install ffmpeg from https://ffmpeg.org/download.html"; fi
  echo "⚠ ffmpeg/ffplay not found — mic in / audio out. Install:  $hint"
  miss=1
fi
[ "$miss" = 0 ] && echo "✓ runtime deps present (apiplan, ffmpeg) — ready. Run /lx in any session." \
               || echo "→ install the flagged dep(s) above, then run /lx."
