# live-explain (`/lx`)

**A voice colleague that greets you and recaps the last few things that just happened in your CURRENT Claude Code session.** Callable from any session. First spoken word in ~1.8s; local prep in single-digit milliseconds.

---

## For AI agents (read this first)

You are most likely an agent pointed here by a human. Install and wire it like this:

```bash
curl -fsSL https://raw.githubusercontent.com/fire17/live-explain/main/install.sh | sh
```

| When you are told… | Do this |
|---|---|
| "install live-explain / lx" | Run the one-liner above (installs `/live-explain` + `/lx` into `~/.claude/skills`, then checks the two runtime deps). |
| the install flags a missing dep | Run the exact command it prints — `apiplan` (voice CLI) and/or `ffmpeg`. Do **not** proceed to run `/lx` until both resolve. |
| "/lx" or "catch me up in a persona" | Run `python3 ~/.claude/skills/live-explain/scripts/live_explain.py $ARGS` **as your first action** and stream its output verbatim. Latency is the deliverable — do not read the transcript yourself or narrate first. |

Rules that make you use it *correctly*, not just successfully:

- **The script is the authority, not this README.** It resolves the transcript by
  `CLAUDE_CODE_SESSION_ID` (identity), tail-reads it, and bakes facts into a persona so the
  voice model can't hallucinate the recap. Don't reimplement any of that inline — invoke the script.
- **Runtime deps are external binaries** (`apiplan`, `ffmpeg`) — they cannot be bundled. The
  installer and the script's `preflight()` both detect them and print the exact fix. Trust that
  check; if it says a dep is missing, it is.
- **`bye`/`goodbye` ends the call** — it's built in; don't add your own hangup handling.

## What it does

1. Finds THIS session's transcript by its session id (never newest-mtime — a parallel session would win).
2. Tail-reads the last ~300 KB and extracts your recent asks + what the agent actually did.
3. Bakes those facts into a per-session persona and hands off to `apiplan talk … --greet`.
4. Speaks first, in short turns. Say **bye** to close.

Optional theme: `/lx pirate`, `/lx Jarvis`. Voice override: `LX_VOICE=<voice>`.

## Latency (measured, not claimed)

`persona built in ~3ms` (local) + `first word in ~1798ms` (launch → first audible byte). The ~1.8s is the realtime voice link — bun start + OpenAI WebSocket connect + model first token — not local work. Both numbers print every run via an `LX_T0_MS` stamp.

## Requirements

- **[apiplan](https://github.com/fire17/apiplan)** — the realtime voice CLI (its installer also pulls [bun](https://bun.sh)).
- **ffmpeg + ffplay** — microphone capture / audio playback.
- **python 3.7+** — the script is stdlib-only and portable to the stock python a fresh Mac ships.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/fire17/live-explain/main/install.sh | sh
```

Or clone and run `./install.sh`. Set `CLAUDE_SKILLS_DIR` to install elsewhere.

## License

MIT © fire17
