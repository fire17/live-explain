// lx_tools.mjs — the one allow-listed tool the /lx voice persona may call.
//
// Why it exists: the greeting persona is FAST because the recap facts are baked into it
// before the socket opens. That is also its ceiling — it only knows the last few turns.
// When the caller asks about something older or more specific ("what did you do to
// talk.ts?", "what came before the reconcile?"), the model should be able to go and look
// instead of saying "I'd have to check".
//
// Shape: an OpenAI realtime function tool. `tools` goes into the session config,
// `onTool(name, args)` is invoked when the server emits a function_call, and whatever it
// returns becomes the `function_call_output` sent back before the next response.create.
//
// Safety properties that matter here:
//   * ALLOW-LIST BY CONSTRUCTION — a name that is not a key of HANDLERS never reaches any
//     process. There is no dynamic dispatch on the model's string.
//   * NO SHELL — execFile with an argv array, so a topic full of shell metacharacters is
//     just a topic.
//   * BOUNDED — hard timeout and output cap, because the caller is sitting in silence on
//     a live phone call while this runs.
//   * READ-ONLY — the handler reads this session's own transcript and nothing else.
import { execFile, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { readFileSync } from "node:fs";

const HERE = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(HERE, "live_explain.py");
const WATCH = join(HERE, "lx_watch.mjs");
// Relay's trust boundary: a human-authored registry of named targets. The model chooses a
// NAME from here; it never supplies a command or URL. Override the file with RELAY_TARGETS.
const RELAY_REGISTRY = process.env.RELAY_TARGETS || join(HERE, "relay-targets.json");

const TIMEOUT_MS = 4000;   // a voice call cannot wait longer than this without feeling broken
const MAX_BUFFER = 1 << 20;
const MAX_TOPIC = 200;     // the model is describing a subject, not pasting a document
const MAX_CHARS = 2200;    // ~ what a voice model can usefully hold and summarise

export const tools = [
  {
    type: "function",
    name: "reveal_more_context",
    description:
      "Look deeper into the Claude Code session you are recapping and get more of its " +
      "history. Use it whenever the person asks about something you were not told about " +
      "up front — an older step, a specific file, a bug, a name, or 'what happened before " +
      "that'. Returns plain text describing what the user asked and what the agent did. " +
      "Call it instead of guessing or saying you would have to check.",
    parameters: {
      type: "object",
      properties: {
        topic: {
          type: "string",
          description:
            "What to look for, in a few words — a file name, a feature, a person, an error, " +
            "or a phrase the user just used. Leave empty to get more of the recent story.",
        },
      },
      required: [],
      additionalProperties: false,
    },
  },
  {
    type: "function",
    name: "get_current_time",
    description:
      "Get the exact current date and time on the user's machine. Call it whenever the " +
      "person asks what time or date it is, or how long ago something happened — never " +
      "guess the time, always call this. Returns the local time, ISO timestamp, weekday, " +
      "and timezone.",
    parameters: { type: "object", properties: {}, required: [], additionalProperties: false },
  },
  {
    type: "function",
    name: "relay",
    description:
      "Send a piece of text to a named external destination — a note file, a spoken voice, " +
      "a webhook, and so on. Use it when the person asks you to send, save, note, post, or " +
      "relay something somewhere. You may only use a destination that already exists; call " +
      "with no target to get the list of what is available. Returns whether it was delivered.",
    parameters: {
      type: "object",
      properties: {
        target: {
          type: "string",
          description: "The named destination (e.g. 'note', 'say'). Leave empty to list what exists.",
        },
        text: { type: "string", description: "The text to send." },
      },
      required: [],
      additionalProperties: false,
    },
  },
  {
    type: "function",
    name: "set_monitor",
    description:
      "Watch for ANYTHING to happen and report it out loud the moment it does — you don't " +
      "check again yourself, you're handed the news to speak when it fires. Use when the " +
      "person says 'let me know when…', 'tell me if…', 'watch for…', 'monitor…'. It is fully " +
      "general, like a shell watcher. Choose ONE mechanism:\n" +
      "• poll_until — a shell test re-run every few seconds; fires when it succeeds. The best " +
      "choice for almost anything: a clock time '[ \"$(date +%H:%M)\" = \"16:20\" ]', a file " +
      "'[ -f /tmp/done ]', a port 'nc -z localhost 8080', a process 'pgrep -x ffmpeg', disk, " +
      "battery, the output of any command.\n" +
      "• command — a shell command whose printed output lines are triggers, for streaming/log " +
      "watches, e.g. 'tail -n0 -F /var/log/app.log | grep --line-buffered -i error'.\n" +
      "Write a real POSIX shell one-liner. Prefer poll_until unless you need streaming lines.",
    parameters: {
      type: "object",
      properties: {
        poll_until: { type: "string", description: "A shell test command; the monitor fires as soon as it exits 0. The general way to watch for a condition." },
        command: { type: "string", description: "A shell command whose stdout lines are triggers (streaming/log watches)." },
        label: { type: "string", description: "A short human name for this monitor, used when reporting." },
        mode: { type: "string", enum: ["graceful", "interrupt"], description: "graceful = finish your current sentence first (default); interrupt = break in immediately." },
        repeat: { type: "boolean", description: "true = report every time it triggers; false (default) = report once, then stop." },
      },
      required: [],
      additionalProperties: false,
    },
  },
];

/** The model may hand arguments over as a JSON string, an object, or nothing at all. */
function coerce(args) {
  if (args == null) return {};
  if (typeof args === "string") {
    const s = args.trim();
    if (!s) return {};
    try { return JSON.parse(s); } catch { return { topic: s }; }
  }
  return typeof args === "object" ? args : {};
}

function run(argv) {
  return new Promise((resolve) => {
    execFile("python3", argv, { timeout: TIMEOUT_MS, maxBuffer: MAX_BUFFER },
      (err, stdout) => {
        if (err && !stdout) return resolve({ ok: false, reason: `lookup failed: ${err.message}` });
        try { resolve(JSON.parse(String(stdout))); }
        catch { resolve({ ok: false, reason: "lookup returned nothing readable" }); }
      });
  });
}

async function reveal_more_context(a) {
  const topic = String(a.topic ?? "").replace(/\s+/g, " ").trim().slice(0, MAX_TOPIC);
  const out = await run([SCRIPT, "--context", topic, "--max-chars", String(MAX_CHARS)]);
  if (!out.ok) {
    // Never throw into a live call: give the model a sentence it can simply say.
    return { context: `I couldn't dig that up — ${out.reason || "nothing matched"}.`, matches: 0 };
  }
  return { topic: out.topic, matches: out.matches, context: out.context };
}

/** Exact wall-clock time on this machine — a deterministic tool for verifying that
 *  tool output actually reaches the model (a voice model guesses the date otherwise). */
function get_current_time() {
  const now = new Date();
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "local";
  return {
    local: now.toLocaleString("en-US", { weekday: "long", year: "numeric", month: "long",
      day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true }),
    iso: now.toISOString(),
    unix: Math.floor(now.getTime() / 1000),
    timezone: tz,
  };
}

function loadRelayTargets() {
  try { return JSON.parse(readFileSync(RELAY_REGISTRY, "utf8")).targets ?? {}; }
  catch { return {}; }
}

/** Run a registered CLI target. The command + args come ONLY from the registry; the
 *  model's text is passed on stdin (or as the final arg for stdin:false targets) with
 *  execFile — an argv array, never a shell — so metacharacters in the text are inert. */
function runRelayCli(t, text) {
  const useStdin = t.stdin !== false;
  const args = [...(Array.isArray(t.args) ? t.args : []), ...(useStdin ? [] : [text])];
  return new Promise((resolve) => {
    const child = execFile(t.cmd, args, { timeout: TIMEOUT_MS, maxBuffer: MAX_BUFFER },
      (err, stdout, stderr) =>
        resolve(err && !stdout ? `error: ${err.message}` : (String(stdout || stderr || "").trim() || "ok")));
    if (useStdin) { try { child.stdin?.end(text); } catch {} }
  });
}

/** relay(target, text) — deliver text to a named registry destination (CLI or API). */
async function relay(a) {
  const name = String(a.target ?? "").trim();
  const text = String(a.text ?? "");
  const targets = loadRelayTargets();
  if (!name) return { ok: true, targets: Object.keys(targets), hint: "call relay again with one of these as target" };
  const t = targets[name];
  if (!t) return { ok: false, error: `no destination named "${name}". Available: ${Object.keys(targets).join(", ") || "none"}` };
  if (!text) return { ok: false, error: "nothing to send — text was empty" };
  try {
    if (t.kind === "cli") return { ok: true, target: name, result: (await runRelayCli(t, text)).slice(0, 500) };
    if (t.kind === "api") {
      const field = t.field || "text";
      const r = await fetch(t.url, {
        method: t.method || "POST",
        headers: { "content-type": "application/json", ...(t.headers || {}) },
        body: JSON.stringify({ [field]: text }),
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      return { ok: r.ok, target: name, status: r.status, result: (await r.text()).slice(0, 500) };
    }
    return { ok: false, error: `destination "${name}" has an unsupported kind "${t.kind}"` };
  } catch (e) {
    return { ok: false, error: `could not reach "${name}": ${String(e?.message ?? e).slice(0, 150)}` };
  }
}

/** set_monitor — arm a background watcher that speaks INTO the call when it triggers.
 *  The watcher is a fixed script (lx_watch.mjs); the model only chooses a bounded kind and
 *  a target, never a command — same trust model as relay. It writes triggers to the inject
 *  channel talk.ts exported as APIPLAN_TALK_INJECT. */
function set_monitor(a) {
  const inject = process.env.APIPLAN_TALK_INJECT;
  if (!inject) return { ok: false, error: "monitoring is not available in this call." };
  // Pick the mechanism. poll_until/command are the general shell forms; file_* are sugar.
  let kind, target;
  if (a.command && String(a.command).trim()) { kind = "command"; target = String(a.command).trim(); }
  else if (a.poll_until && String(a.poll_until).trim()) { kind = "poll_until"; target = String(a.poll_until).trim(); }
  else if (a.kind && a.target) { kind = String(a.kind).trim(); target = String(a.target).trim(); }
  else return { ok: false, error: "tell me what to watch — a poll_until test, a command, or a kind+target." };
  const label = String(a.label ?? target).slice(0, 60);
  const mode = a.mode === "interrupt" ? "interrupt" : "graceful";
  try {
    const child = spawn(process.execPath, [WATCH], {
      detached: true, stdio: "ignore",
      env: { ...process.env,
        LX_WATCH_KIND: kind, LX_WATCH_TARGET: target, LX_WATCH_PATTERN: String(a.pattern ?? ""),
        LX_WATCH_LABEL: label, LX_WATCH_MODE: mode, LX_WATCH_INJECT: inject,
        LX_WATCH_ONCE: a.repeat ? "0" : "1" },
    });
    child.unref();
    return { ok: true, monitoring: kind, target, label, note: "armed — I will speak up the moment it triggers." };
  } catch (err) {
    return { ok: false, error: `could not start the monitor: ${String(err?.message ?? err).slice(0, 150)}` };
  }
}

// The allow-list IS this object. Nothing else is callable, by construction.
const HANDLERS = { reveal_more_context, get_current_time, relay, set_monitor };

export async function onTool(name, args) {
  const fn = HANDLERS[name];
  if (!fn) return { error: `no such tool: ${String(name).slice(0, 60)}` };
  try {
    return await fn(coerce(args));
  } catch (e) {
    return { error: `tool failed: ${String(e?.message ?? e).slice(0, 200)}` };
  }
}

export default { tools, onTool };

// Self-test: `bun scripts/lx_tools.mjs "talk.ts"` (or node) prints exactly what the model
// would receive. Verifying the tool should never require making a phone call.
if (process.argv[1] && process.argv[1].endsWith("lx_tools.mjs")) {
  onTool("reveal_more_context", { topic: process.argv.slice(2).join(" ") })
    .then((r) => process.stdout.write(JSON.stringify(r, null, 2) + "\n"));
}
