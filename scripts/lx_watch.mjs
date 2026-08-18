#!/usr/bin/env node
// lx_watch.mjs — ONE background monitor for the voice `set_monitor` tool.
//
// It mirrors Claude Code's Monitor model — an event is one detected occurrence — but the
// notification sink is the live voice call: on a trigger it appends a {text, mode} line to
// the inject channel (LX_WATCH_INJECT), which talk.ts speaks into the conversation.
//
// GENERIC by design, like CC's Monitor. Four kinds, from most to least general:
//   command      LX_WATCH_TARGET is a shell command; each stdout LINE it prints is a trigger
//                (CC-Monitor streaming — e.g. `tail -n0 -F app.log | grep --line-buffered ERROR`).
//   poll_until   LX_WATCH_TARGET is a shell TEST re-run every ~3s; it fires once it exits 0
//                (the natural fit for arbitrary conditions — `[ "$(date +%H:%M)" = "16:20" ]`,
//                 `[ "$(pmset -g batt | grep -o '[0-9]*%')" \\> "80%" ]`, `nc -z localhost 8080`).
//   file_appears LX_WATCH_TARGET is a path; fires when it exists.
//   file_contains LX_WATCH_TARGET is a path; fires on a new line matching LX_WATCH_PATTERN.
//
// Arbitrary shell is intentional (the user asked for CC-level generality on their own
// machine); set_monitor logs the exact command to the sidecar so the operator sees it.
//
// Other env: LX_WATCH_LABEL, LX_WATCH_MODE (graceful|interrupt), LX_WATCH_INJECT,
//   LX_WATCH_ONCE ("1" default = stop after first trigger), LX_WATCH_TIMEOUT_MS (default 1h).
import { appendFileSync, existsSync, statSync, readFileSync } from "node:fs";
import { spawn } from "node:child_process";

const e = process.env;
const kind = e.LX_WATCH_KIND, target = e.LX_WATCH_TARGET, inject = e.LX_WATCH_INJECT;
const label = e.LX_WATCH_LABEL || target, mode = e.LX_WATCH_MODE === "interrupt" ? "interrupt" : "graceful";
const once = e.LX_WATCH_ONCE !== "0";
const TIMEOUT = Number(e.LX_WATCH_TIMEOUT_MS || 3600000);
const POLL = 800;
if (!inject || !target || !kind) process.exit(1);

const started = Date.now();
const expired = () => Date.now() - started > TIMEOUT;
function fire(detail) {
  const text = `A monitor you set${label ? ` ("${label}")` : ""} just triggered: ${detail}. Report this to me now, briefly.`;
  try { appendFileSync(inject, JSON.stringify({ text, mode }) + "\n"); } catch {}
}
function done() { process.exit(0); }
setTimeout(done, TIMEOUT);   // hard self-destruct so nothing orphans forever

if (kind === "command") {
  // CC-Monitor streaming: run the command, each stdout line is a trigger event.
  const child = spawn("sh", ["-c", target], { stdio: ["ignore", "pipe", "ignore"] });
  let buf = "";
  child.stdout.on("data", (d) => {
    buf += d.toString("utf8");
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
      if (line) { fire(line.slice(0, 200)); if (once) { try { child.kill(); } catch {} done(); } }
    }
  });
  child.on("exit", done);
} else if (kind === "poll_until") {
  // Re-run a shell test until it succeeds (exit 0). The workhorse for arbitrary conditions.
  const tick = () => {
    if (expired()) return done();
    const child = spawn("sh", ["-c", target], { stdio: "ignore" });
    child.on("exit", (code) => {
      if (code === 0) { fire("the condition is now true"); if (once) return done(); }
      setTimeout(tick, 3000);
    });
    child.on("error", () => setTimeout(tick, 3000));
  };
  tick();
} else if (kind === "file_appears") {
  const iv = setInterval(() => {
    if (existsSync(target)) { fire(`${target} now exists`); if (once) { clearInterval(iv); return done(); } }
    if (expired()) { clearInterval(iv); done(); }
  }, POLL);
} else if (kind === "file_contains") {
  const re = e.LX_WATCH_PATTERN ? new RegExp(e.LX_WATCH_PATTERN, "i") : null;
  let off = existsSync(target) ? statSync(target).size : 0;   // only NEW lines from arm time
  const iv = setInterval(() => {
    try {
      if (existsSync(target)) {
        const size = statSync(target).size;
        if (size < off) off = 0;
        if (size > off) {
          const buf = readFileSync(target).slice(off, size).toString("utf8");
          off = size;
          for (const ln of buf.split("\n")) {
            const s = ln.trim();
            if (s && (!re || re.test(s))) { fire(`${target}: ${s.slice(0, 160)}`); if (once) { clearInterval(iv); return done(); } }
          }
        }
      }
    } catch {}
    if (expired()) { clearInterval(iv); done(); }
  }, POLL);
} else {
  process.exit(1);
}
