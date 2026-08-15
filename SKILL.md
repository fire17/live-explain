---
name: live-explain
description: Instantly spin up a persona that greets you and explains the last few things that happened in the CURRENT Claude Code session — a fast on-demand "where were we?" catch-up voice. Reads this session's transcript tail, builds a persona locally (single-digit ms), and streams a warm in-character greeting + an accurate recap of recent work; first spoken word lands about as fast as a realtime voice call can connect (~1.8s measured, dominated by the voice link, not local work). Use when the user types /live-explain (optionally /live-explain <persona-name-or-theme>), or asks to "catch me up in a persona", "have someone greet me and explain what just happened", "who's been watching this session", or wants a quick spoken-style recap of the last few things. Callable from ANY session.
argument-hint: "[persona-name-or-theme]"
---

# live-explain

A voice colleague that greets you and recaps the last few things in the current session.
Speed is the point — measure call → first spoken word and keep it minimal.

## Run it — your FIRST action, no preamble

```
python3 ~/.claude/skills/live-explain/scripts/live_explain.py $ARGUMENTS
```

Run that immediately, then show its streamed output as-is. Do not read the transcript
yourself first, do not narrate what you're about to do — latency is the deliverable.
`$ARGUMENTS` is an optional persona name or theme (e.g. `pirate`, `Jarvis`); with none,
it's a plain warm colleague. Alias: `/lx`.

## What it does (so you can explain if asked)

1. **Finds THIS session's transcript by identity** — the `<CLAUDE_CODE_SESSION_ID>.jsonl`
   under `~/.claude/projects/`, never newest-mtime (a parallel session appending its own
   jsonl would otherwise win and the recap would describe someone else's work).
   `LX_TRANSCRIPT` (path or session id) overrides; mtime is a last-resort fallback only.
2. **Tail-reads it** — only the last ~300 KB, so a huge transcript stays fast — and
   extracts the recent human asks + what the agent actually did (tool names + text).
3. **Bakes those facts into a per-session persona file** (`.cache/persona-<session-id>.md`,
   pid fallback — a fixed name would let a parallel `/lx` overwrite it before `apiplan`
   reads it, voicing the wrong persona) and hands off to `apiplan talk --persona …
   --voice cedar --greet`. The facts live in the persona, so the realtime model just
   voices them — fast first word, and it can't hallucinate the recap.
4. **Speaks first**, in short turns; **say "bye"/"goodbye" to end the call** (built-in).

## Latency

The script prints `persona built in Nms` (its own pre-work — ~3ms) and the voice layer
prints `first word in Nms` — measured from launch to the first audible byte, via the
`LX_T0_MS` stamp. Measured end-to-end: **3ms local + ~1798ms to first spoken word**. The
~1.8s is the realtime voice link (bun start + OpenAI WebSocket connect + model first
token), not local work — there's little to shave without changing apiplan itself.

## Knobs

- `LX_VOICE=<voice>` env overrides the default `cedar`.
- Depends on `apiplan` being on PATH (it is: `~/.bun/bin/apiplan`) and `ffmpeg` for mic/speaker.
