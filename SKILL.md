---
name: live-explain
description: Instantly spin up a persona that greets you and explains the last few things that happened in the CURRENT Claude Code session — a fast on-demand "where were we?" catch-up voice, which the launching agent then WATCHES LIVE. Reads this session's transcript tail, builds a persona locally (single-digit ms), streams a warm in-character greeting + an accurate recap, and writes a sidecar JSONL of the whole conversation so the agent can follow both sides in real time and know when the call ends. First spoken word lands about as fast as a realtime voice call can connect (~1.8s measured, dominated by the voice link, not local work). Use when the user types /live-explain (optionally /live-explain <persona-name-or-theme>), or asks to "catch me up in a persona", "have someone greet me and explain what just happened", "who's been watching this session", "listen in on the call", or wants a quick spoken-style recap of the last few things. Callable from ANY session.
argument-hint: "[persona-name-or-theme]"
---

# live-explain

A voice colleague that greets you and recaps the last few things in the current session —
**and a live feed of that call back to the agent that launched it.**

Speed is still the point: measure call → first spoken word and keep it minimal.

## Run it — your FIRST action, no preamble

Launch it **in the background** (Bash tool, `run_in_background: true`) so the call runs
while you stay free to watch it:

```
python3 ~/.claude/skills/live-explain/scripts/live_explain.py $ARGUMENTS
```

`$ARGUMENTS` is an optional persona name or theme (e.g. `pirate`, `Jarvis`); with none,
it's a plain warm colleague. Alias: `/lx`.

Do not read the transcript yourself first and do not narrate what you're about to do —
latency is the deliverable. Backgrounding costs nothing: the greeting is already speaking
while your next tool call runs.

## Then WATCH the call — this is half the feature

The call writes every event to a sidecar JSONL as it happens. Follow it with:

```
python3 ~/.claude/skills/live-explain/scripts/lx_monitor.py --wait 30
```

Call that **in a loop** until it exits 0. Each run prints only what is new since the last
run and ends with a status line:

```
17:04:12  ·     │ first word in 1798ms
17:04:14  voice │ Hey — welcome back. You just had me wire the live monitor into /lx.
17:04:21  you   │ what did you do to talk dot ts
17:04:23  voice │ Nothing — that lane owns it. I only touched the skill side.
--- lx-monitor: 9 new event(s), 3 spoken turn(s) · call LIVE (quiet 2s) · …/talk-<sid>-<t0>.jsonl ---
```

| exit code | meaning | what you do |
|---|---|---|
| `0` | the call **ENDED** (goodbye / socket closed / connection failed) | stop looping, summarise the conversation for the user |
| `10` | still live | call it again — `--wait 30` blocks until there is something to see |
| `3` | no log found | the call never started; read the backgrounded task's stderr |

Useful flags: `--from-start` (replay the whole call), `--raw` (every websocket event, not
just speech — this is how you diagnose a silent call), `--json` (machine-readable turns),
`--log <path>` / `--session <id>` (watch a specific call).

You never have to hunt for the log path: it is printed on stderr as
`live-explain: log <path>` and recorded in `.cache/last-call-<session-id>.json`, which is
what the monitor reads by default.

## What it does (so you can explain if asked)

1. **Finds THIS session's transcript by identity** — the `<CLAUDE_CODE_SESSION_ID>.jsonl`
   under `~/.claude/projects/`, never newest-mtime (a parallel session appending its own
   jsonl would otherwise win and the recap would describe someone else's work).
   `LX_TRANSCRIPT` (path or session id) overrides; mtime is a last-resort fallback only.
2. **Tail-reads it** — only the last ~300 KB, so a huge transcript stays fast — and
   extracts the recent human asks + what the agent actually did (tool names + text).
3. **Bakes those facts into a per-session persona file** (`.cache/persona-<session-id>.md`,
   pid fallback — a fixed name would let a parallel `/lx` overwrite it before `apiplan`
   reads it, voicing the wrong persona) and hands off to `apiplan talk --persona … --voice
   cedar --greet --log <sidecar>`. The facts live in the persona, so the realtime model
   just voices them — fast first word, and it can't hallucinate the recap.
4. **Arms the sidecar** — creates the log `0600` first (the conversation lands on disk;
   it should not be world-readable), writes the pointer file, and prunes to the last 20
   call logs. The log name carries the launch timestamp, so two `/lx` runs in one session
   can never truncate each other's live log.
5. **Speaks first**, in short turns; **say "bye"/"goodbye" to end the call** (built-in).

## The voice tools — `scripts/lx_tools.mjs`

The persona is fast because its facts are baked in before the socket opens — which is also
its ceiling. The allow-listed tool bundle lifts it. All three are keys of one `HANDLERS`
object: **allow-listed by construction** (a name that is not a key never reaches a
process), no shell, 4s timeout, output capped. A tool never throws into a live call —
failures come back as a sentence the model can just say. `apiplan talk` JSON-serializes a
tool's structured return before it reaches the model.

- **`reveal_more_context(topic?)`** → `{ topic, matches, context }`. Re-reads **this
  session's** transcript (1.5 MB tail), pulls every turn matching the topic plus the turn
  either side, budgets ~2200 chars from the newest end. Empty topic = more of the recent
  story. Body: `live_explain.py --context "<topic>"`, so tool and greeting never drift on
  what "this session" means.
- **`get_current_time()`** → `{ local, iso, unix, timezone }`. The exact wall clock — a
  voice model otherwise guesses the date. Also the cheapest way to prove a tool's output
  reaches the model (ask the time; a correct answer means the wire is live).
- **`relay(target?, text)`** → delivers `text` to a **named** destination. The model picks
  a name; it never supplies a command or URL. The registry `scripts/relay-targets.json` is
  the entire trust boundary: `kind:"cli"` runs `cmd`+`args` with the text on stdin (execFile,
  no shell — metacharacters in the text are inert) or as the final arg for `stdin:false`
  targets; `kind:"api"` POSTs `{<field>: text}` to `url`. Call with no target to list what
  exists. Add your own targets; keep them to things you'd let a voice command trigger.
  Override the registry path with `RELAY_TARGETS`.
- Full contract, wire shapes and the apiplan-side integration point: **`TOOLS.md`**.
- `LX_TOOLS=1` enables the bundle for `/lx`; the flag passed to `apiplan talk` is `--tools`
  (overridable via `LX_TOOLS_FLAG`). apiplan's tool support has landed and is verified.

Verify without a phone call:

```
node ~/.claude/skills/live-explain/scripts/lx_tools.mjs "the thing you want to look up"
```

## Monitors that speak into the call — `set_monitor` + context injection

`set_monitor` is a generic watcher modelled on Claude Code's `Monitor` (an event = one
detected occurrence), but its notification sink is **the live voice call**: when it fires,
the report is spoken into the conversation. The model picks ONE mechanism:

- **`poll_until`** — a shell test re-run every ~3s; fires the instant it exits 0. The
  general form for any condition: a clock time `[ "$(date +%H:%M)" = "16:20" ]`, a file
  `[ -f /tmp/done ]`, a port `nc -z localhost 8080`, a process `pgrep -x ffmpeg`, disk,
  battery — anything a shell can test.
- **`command`** — a shell command whose stdout LINES are triggers, for streaming/log
  watches (`tail -n0 -F app.log | grep --line-buffered -i error`).

The watcher (`scripts/lx_watch.mjs`) runs detached, self-destructs after `LX_WATCH_TIMEOUT_MS`
(1h default), and appends each trigger to the **inject channel** — `<logFile>.inject`, which
`talk.ts` exports as `APIPLAN_TALK_INJECT`. **Arbitrary shell is intentional** (CC-level
generality, on the user's own machine); the exact armed command is logged to the sidecar
(`tool set_monitor → …`) so the watching operator sees precisely what is running.

**Context injection** is the primitive underneath: any process may append `{text, mode}` to
the inject channel and `talk.ts` speaks it into the call. `mode:"graceful"` waits for the
current sentence to finish; `mode:"interrupt"` barges in (cancel + truncate) so the model
answers on the new context at once. A response is never created while one is active
(the server rejects that) — graceful queues until `response.done`; interrupt cancels first.

Verify the monitor→inject chain without a call:

```
APIPLAN_TALK_INJECT=/tmp/inj.jsonl node -e "import('~/.claude/skills/live-explain/scripts/lx_tools.mjs').then(m=>m.onTool('set_monitor',{poll_until:'[ -f /tmp/go ]'}))"
touch /tmp/go   # → a {text,mode} line appears in /tmp/inj.jsonl
```

## Live word-by-word view — `lx_monitor.py --live`

`--live` is the **human** view (the loop above is the agent's). It follows the sidecar and
streams each side's words inline **as they are transcribed** — the model's from
`model_delta`, the human's from `you_delta` — starting a fresh labelled line whenever the
speaker changes, and returning 0 when the call ends. `--from-start` replays from the top.

```
python3 ~/.claude/skills/live-explain/scripts/lx_monitor.py --log <sidecar> --live
```

## Latency

The script prints `persona built in Nms` (its own pre-work — ~3ms) and the voice layer
prints `first word in Nms` — measured from launch to the first audible byte, via the
`LX_T0_MS` stamp. Measured end-to-end: **3ms local + ~1798ms to first spoken word**. The
~1.8s is the realtime voice link (bun start + OpenAI WebSocket connect + model first
token), not local work — there's little to shave without changing apiplan itself.

## Knobs

- `LX_VOICE=<voice>` — override the default `cedar`.
- `LX_TRANSCRIPT=<path|session-id>` — pin which session gets recapped.
- `LX_LOG=<path>` — pin the sidecar log path (otherwise `.cache/talk-<session>-<t0>.jsonl`).
- `LX_TOOLS=1` — enable `reveal_more_context` (see above). `LX_TOOLS_FLAG` renames the CLI flag.
- Depends on `apiplan` being on PATH (it is: `~/.bun/bin/apiplan`) and `ffmpeg`+`ffplay`
  for mic/speaker. Both are checked up front with the exact install command on failure.
- Everything this skill writes lives in `scripts/../.cache/`, keyed by session id.
