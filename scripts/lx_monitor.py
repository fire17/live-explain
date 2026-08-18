#!/usr/bin/env python3
"""lx_monitor — watch a live /lx voice call from the agent side.

`apiplan talk --log <file>` appends one JSON line per event, flushed as it happens.
This turns that firehose into the two things a watching agent actually needs: the
conversation as readable lines, and an unambiguous answer to "is the call over?".

It is INCREMENTAL by design. Each run prints only what is new since the last run (a
byte offset is remembered per log), so an agent can poll it in a loop without re-reading
the transcript and without blocking on `tail -f`.

  python3 lx_monitor.py                 # what's new since last time + status
  python3 lx_monitor.py --wait 30       # …but block up to 30s for new lines / the end
  python3 lx_monitor.py --from-start     # the whole call so far
  python3 lx_monitor.py --raw            # every websocket event too, not just speech
  python3 lx_monitor.py --json           # machine-readable, for scripting
  python3 lx_monitor.py --live           # HUMAN view: words stream in as spoken, both sides

Exit codes:  0 = the call has ENDED · 10 = still live · 3 = no log found.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "..", ".cache")
POLL_S = 0.4

# The end of a call, in the order we trust them. talk.ts writes these as `info` events.
END_MARKERS = ("call ended", "socket closed", "connection failed")
# Websocket events worth showing even without --raw: the ones that explain a silence.
LOUD_WS = ("error", "response.done", "session.updated", "session.created",
           "input_audio_buffer.speech_started", "conversation.item.input_audio_transcription.failed")


def resolve_log(explicit: str, tag: str) -> str:
    """--log → LX_LOG → this session's pointer file → newest sidecar in the cache."""
    if explicit:
        return explicit
    env = os.environ.get("LX_LOG", "").strip()
    if env:
        return env
    ptr = os.path.join(CACHE, f"last-call-{tag}.json")
    if os.path.isfile(ptr):
        try:
            with open(ptr) as f:
                p = json.load(f).get("log", "")
            if p:
                return p
        except (OSError, ValueError):
            pass
    cands = glob.glob(os.path.join(CACHE, "talk-*.jsonl"))
    return max(cands, key=os.path.getmtime) if cands else ""


def _offset_file(log: str) -> str:
    return os.path.join(CACHE, "monitor-" + os.path.basename(log) + ".offset")


def read_offset(log: str) -> int:
    try:
        with open(_offset_file(log)) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_offset(log: str, off: int) -> None:
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(_offset_file(log), "w") as f:
            f.write(str(off))
    except OSError:
        pass


def read_new(log: str, start: int):
    """Return (records, new_offset). Only whole lines are consumed, so a half-written
    line is left for the next poll instead of being parsed as garbage."""
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if start > size:      # the log was replaced by a newer call — start over
                start = 0
            f.seek(start)
            chunk = f.read()
    except OSError:
        return [], start
    text = chunk.decode("utf-8", "ignore")
    cut = text.rfind("\n")
    if cut < 0:
        return [], start
    consumed = start + len(text[: cut + 1].encode("utf-8"))
    out = []
    for ln in text[:cut].splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out, consumed


def is_end(rec: dict) -> bool:
    if rec.get("ev") != "info":
        return False
    low = str(rec.get("text", "")).lower()
    return any(m in low for m in END_MARKERS)


def fmt(rec: dict, raw: bool) -> str | None:
    t = rec.get("t")
    stamp = time.strftime("%H:%M:%S", time.localtime(t / 1000.0)) if isinstance(t, (int, float)) else "--:--:--"
    ev = rec.get("ev")
    if ev == "you":
        return f"{stamp}  you   │ {rec.get('text', '')}"
    if ev == "model":
        return f"{stamp}  voice │ {rec.get('text', '')}"
    if ev == "info":
        return f"{stamp}  ·     │ {rec.get('text', '')}"
    ws = rec.get("ws")
    if ws and (raw or ws in LOUD_WS or rec.get("error")):
        err = f"  error={rec['error']}" if rec.get("error") else ""
        return f"{stamp}  ws    │ {ws}{err}"
    return None


def run_live(log: str, from_start: bool) -> int:
    """Continuous human view: stream each side's words as they are transcribed, inline,
    word-by-word. Model words arrive as `model_delta`; human words as `you_delta` when the
    transcription model streams them, else the whole utterance lands as a `you` line. A new
    labelled line starts whenever the speaker changes; the call's end returns 0."""
    off = 0 if from_start else (os.path.getsize(log) if os.path.isfile(log) else 0)
    cur = None  # speaker currently mid-line: None | "you" | "voice"

    def switch(to: str) -> None:
        nonlocal cur
        if cur == to:
            return
        if cur is not None:
            sys.stdout.write("\n")
        stamp = time.strftime("%H:%M:%S")
        sys.stdout.write(f"{stamp}  {'you  ' if to == 'you' else 'voice'} │ ")
        cur = to

    def finish() -> None:
        nonlocal cur
        if cur is not None:
            sys.stdout.write("\n")
            cur = None
        sys.stdout.flush()

    sys.stdout.write(f"--- lx-monitor --live · {log} · Ctrl-C to stop ---\n")
    sys.stdout.flush()
    try:
        while True:
            batch, off = read_new(log, off)
            for r in batch:
                ev = r.get("ev")
                if ev == "model_delta":
                    switch("voice"); sys.stdout.write(str(r.get("text", ""))); sys.stdout.flush()
                elif ev == "you_delta":
                    switch("you"); sys.stdout.write(str(r.get("text", ""))); sys.stdout.flush()
                elif ev == "model":
                    if cur != "voice":                       # no deltas streamed — show it whole
                        switch("voice"); sys.stdout.write(str(r.get("text", "")))
                    finish()
                elif ev == "you":
                    if cur != "you":
                        switch("you"); sys.stdout.write(str(r.get("text", "")))
                    finish()
                elif is_end(r):
                    finish()
                    sys.stdout.write(f"--- call ended: {r.get('text', '')} ---\n")
                    return 0
            time.sleep(0.12)   # tighter than the agent poll — this is a human watching live
    except KeyboardInterrupt:
        finish()
        return 0


def main() -> int:
    a = sys.argv[1:]

    def val(flag, default=""):
        return a[a.index(flag) + 1] if flag in a and a.index(flag) + 1 < len(a) else default

    raw = "--raw" in a
    as_json = "--json" in a
    tag = val("--session") or os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip() or str(os.getppid())
    log = resolve_log(val("--log"), tag)
    if not log or not os.path.isfile(log):
        msg = f"no /lx call log found (looked for {log or 'the pointer + ' + CACHE})"
        sys.stdout.write(json.dumps({"ok": False, "reason": msg}) + "\n" if as_json else msg + "\n")
        return 3

    if "--live" in a:
        return run_live(log, "--from-start" in a)

    try:
        wait_s = float(val("--wait", "0"))
    except ValueError:
        wait_s = 0.0
    off = 0 if "--from-start" in a else read_offset(log)

    deadline = time.time() + wait_s
    recs, ended, lines = [], False, []
    while True:
        batch, off = read_new(log, off)
        for r in batch:
            recs.append(r)
            line = fmt(r, raw)
            if line:
                lines.append(line)
            if is_end(r):
                ended = True
        if ended or lines or time.time() >= deadline:
            break
        time.sleep(POLL_S)

    write_offset(log, off)
    age = time.time() - os.path.getmtime(log)
    said = [r for r in recs if r.get("ev") in ("you", "model")]

    if as_json:
        sys.stdout.write(json.dumps({
            "ok": True, "log": log, "ended": ended, "new_events": len(recs),
            "new_turns": len(said), "idle_s": round(age, 1),
            "turns": [{"who": r.get("ev"), "text": r.get("text", "")} for r in said],
        }) + "\n")
    else:
        for ln in lines:
            sys.stdout.write(ln + "\n")
        state = "ENDED" if ended else f"LIVE (quiet {age:.0f}s)"
        sys.stdout.write(f"--- lx-monitor: {len(recs)} new event(s), {len(said)} spoken turn(s) · call {state} · {log} ---\n")
    return 0 if ended else 10


if __name__ == "__main__":
    sys.exit(main())
