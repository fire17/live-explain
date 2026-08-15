#!/usr/bin/env bash
# live-explain (/lx) installer — batteries-included.
# Works both ways: run from a clone (./install.sh) OR piped detached
# (curl -fsSL .../install.sh | sh) — in the piped case it fetches the payload itself.
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/fire17/live-explain/main"
REPO_GIT="https://github.com/fire17/live-explain"
SKILLS="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

# Where do the payload files live? Beside this script when run from a clone; otherwise
# (curl | sh) there are no siblings, so materialize them into a cache dir.
SRC="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
if [ -z "$SRC" ] || [ ! -f "$SRC/SKILL.md" ]; then
  SRC="${LIVE_EXPLAIN_SRC:-$HOME/.live-explain/src}"
  mkdir -p "$(dirname "$SRC")"
  if command -v git >/dev/null 2>&1; then
    rm -rf "$SRC"
    git clone --depth 1 "$REPO_GIT" "$SRC" >/dev/null 2>&1 || { echo "✗ git clone failed"; exit 1; }
  else
    # no git — pull the three files directly
    mkdir -p "$SRC/scripts" "$SRC/lx"
    curl -fsSL "$REPO_RAW/SKILL.md"                -o "$SRC/SKILL.md"
    curl -fsSL "$REPO_RAW/scripts/live_explain.py" -o "$SRC/scripts/live_explain.py"
    curl -fsSL "$REPO_RAW/lx/SKILL.md"             -o "$SRC/lx/SKILL.md"
  fi
fi

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
