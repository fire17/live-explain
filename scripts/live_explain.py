#!/usr/bin/env python3
"""live-explain — greet the user in a voice persona and recap the last few things
that happened in THIS Claude Code session, as fast as possible.

Speed strategy: the only pre-work before the voice connects is (1) tail-read the current
transcript and (2) write a persona file. Both are cheap and bounded, so we hand off to
`apiplan talk` within a few ms of launch. The recap FACTS are baked into the persona, so
the realtime model just voices them — no reasoning delay, and it can't get them wrong.

Two more jobs beyond speaking:

* **Live monitoring.** Every call writes a sidecar JSONL of the WHOLE conversation
  (`apiplan talk --log …`), flushed per line. The launching agent runs /lx backgrounded
  and tails that file with `lx_monitor.py`, so it sees both sides as they happen and
  knows when the call ended. The path is deterministic-per-call and recorded in a
  pointer file, so nothing has to be scraped out of stderr.
* **Digging deeper mid-call.** `--context <topic>` re-reads this session's transcript and
  prints a bounded JSON slice about that topic. That is the body of the one allow-listed
  voice tool, `reveal_more_context` (see `lx_tools.mjs`).

Usage:
  live_explain.py [persona-name-or-theme...]     # normal: build persona, exec the call
  live_explain.py --context [topic] [--max-chars N]   # print a JSON context slice, exit
"""
from __future__ import annotations  # lazy annotations → runs on the stock 3.7+ python a fresh Mac ships

import glob
import json
import os
import re
import shutil
import sys
import time

T0_MS = int(time.time() * 1000)  # ~ skill-call time; talk.ts reports first-word latency vs this

HOME = os.path.expanduser("~")
VOICE = os.environ.get("LX_VOICE", "cedar")
TAIL_BYTES = 300_000  # enough for many recent records; keeps big transcripts fast
CONTEXT_TAIL_BYTES = 1_500_000  # --context digs deeper than the greeting does
KEEP_LOGS = 20  # sidecar logs retained in .cache before the oldest are pruned

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "..", ".cache")


def _apt_or_brew() -> str:
    if shutil.which("brew"):
        return "brew install ffmpeg"
    if shutil.which("apt"):
        return "sudo apt install -y ffmpeg"
    return "install ffmpeg from https://ffmpeg.org/download.html"


def preflight() -> None:
    """Batteries-included: fail fast with the EXACT fix if a runtime dep is absent,
    instead of an execvpe traceback on a fresh machine."""
    missing = []
    if not shutil.which("apiplan"):
        missing.append(
            "apiplan (the voice CLI) — install (also pulls bun):\n"
            "    curl -fsSL https://raw.githubusercontent.com/fire17/apiplan/main/install.sh | sh"
        )
    # apiplan needs ffmpeg (mic capture) + ffplay (playback); both ship with ffmpeg.
    if not (shutil.which("ffmpeg") and shutil.which("ffplay")):
        missing.append(f"ffmpeg + ffplay (mic in / audio out) — install:\n    {_apt_or_brew()}")
    if missing:
        sys.stderr.write("live-explain can't run — missing dependencies:\n\n")
        for m in missing:
            sys.stderr.write(f"  • {m}\n\n")
        sys.stderr.write("Install the above, then re-run /lx.\n")
        sys.exit(1)


def session_tag() -> str:
    """Per-session key for every file this skill writes. Session id when the harness
    gives one; pid otherwise — never a fixed name, which two parallel /lx runs would
    fight over."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip() or str(os.getpid())


def _by_session_id(sid: str) -> str | None:
    """Resolve a bare session id to its transcript file, anywhere under projects/."""
    if not sid:
        return None
    hits = glob.glob(os.path.join(HOME, ".claude", "projects", "*", f"*{sid}*.jsonl"))
    return hits[0] if hits else None


def find_transcript() -> str | None:
    """THIS session's own transcript — resolved by identity, never by mtime.

    Newest-mtime is wrong the moment two sessions share a project bucket: a parallel
    session actively appending its jsonl always looks "newest", so the recap would
    describe someone else's work. So we key on the current session's id instead.

    Order: LX_TRANSCRIPT (explicit pin, path or session id) → CLAUDE_CODE_SESSION_ID
    (the harness-set id of THIS session, the robust default) → newest-mtime, but only
    as a last resort when no session id is available (old harness / detached run)."""
    pin = os.environ.get("LX_TRANSCRIPT", "").strip()
    if pin:
        if os.path.isfile(pin):
            return pin
        hit = _by_session_id(pin)
        if hit:
            return hit

    own = _by_session_id(os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip())
    if own:
        return own

    # Last resort only — mtime is unreliable across concurrent sessions.
    slug = os.getcwd().replace("/", "-")
    proj = os.path.join(HOME, ".claude", "projects", slug)
    cands = glob.glob(os.path.join(proj, "*.jsonl"))
    if not cands:
        cands = glob.glob(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl"))
    return max(cands, key=os.path.getmtime) if cands else None


def tail_records(path: str, tail_bytes: int = TAIL_BYTES) -> list[dict]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - tail_bytes))
        chunk = f.read()
    lines = chunk.decode("utf-8", "ignore").splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]  # drop the partial first line
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def _text_blocks(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""


def _strip_noise(t: str) -> str:
    # Injected reminders/hook context arrive as user-role text but aren't the human talking.
    t = re.sub(r"<system-reminder>.*?</system-reminder>", "", t, flags=re.S)
    t = re.sub(r"UserPromptSubmit hook.*", "", t, flags=re.S)
    return t.strip()


# Text that is the harness talking, not the human: slash-command echoes, command output,
# interrupts, hook notices, and the compaction summary preamble.
_NOISE_PREFIXES = (
    "<command-", "<local-command-", "[request interrupted", "goal set:",
    "a session-scoped stop hook", "this session is being continued", "caveman mode",
)


def _is_human(record: dict, text: str) -> bool:
    if record.get("isMeta") or record.get("isCompactSummary"):
        return False
    low = text.lstrip().lower()
    return bool(low) and not low.startswith(_NOISE_PREFIXES)


def _agent_turn(content) -> dict | None:
    """One assistant record → {"say": text, "did": [tool blurbs]} or None if it is empty."""
    texts, tools = [], []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and b.get("text", "").strip():
            texts.append(b["text"].strip())
        elif b.get("type") == "tool_use":
            name = b.get("name", "tool")
            inp = b.get("input", {}) or {}
            hint = inp.get("file_path") or inp.get("command") or inp.get("description") or inp.get("pattern") or ""
            tools.append(f"{name}({str(hint)[:60]})" if hint else name)
    blurb = " ".join(texts)[:280]
    if blurb or tools:
        return {"say": blurb, "did": tools}
    return None


def extract(records: list[dict]):
    """Return (human_asks[oldest..newest], agent_actions[oldest..newest])."""
    human, agent = [], []
    for r in records:
        msg = r.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            # skip pure tool_result turns (no human text)
            if isinstance(content, list) and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                continue
            txt = _strip_noise(_text_blocks(content))
            if len(txt.split()) >= 2 and _is_human(r, txt):  # real human sentence
                human.append(txt)
        elif role == "assistant" and isinstance(content, list):
            turn = _agent_turn(content)
            if turn:
                agent.append(turn)
    return human, agent


def timeline(records: list[dict]) -> list[dict]:
    """The same extraction, but INTERLEAVED and untruncated-ish — what `--context` searches.

    The greeting only needs "the last few things"; a follow-up question needs the order and
    the surroundings of a match, so this keeps user and agent turns in one sequence."""
    out = []
    for r in records:
        msg = r.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, list) and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                continue
            txt = _strip_noise(_text_blocks(content))
            if len(txt.split()) >= 2 and _is_human(r, txt):
                out.append({"who": "user", "text": txt})
        elif role == "assistant" and isinstance(content, list):
            turn = _agent_turn(content)
            if turn:
                line = turn["say"]
                if turn["did"]:
                    tail = "[" + ", ".join(turn["did"][:6]) + "]"
                    line = (line + " " + tail) if line else tail
                out.append({"who": "agent", "text": line})
    return out


def clip(t: str, n: int) -> str:
    t = " ".join(t.split())
    return t if len(t) <= n else t[: n - 1] + "…"


# ── the reveal_more_context tool body ────────────────────────────────────────────────
# Kept HERE, not reimplemented in JS, so the voice tool and the greeting can never drift
# apart on what "this session" means or how a transcript is parsed.

_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "what", "was",
    "were", "is", "are", "did", "do", "does", "about", "that", "this", "it", "me", "you",
    "my", "our", "we", "i", "tell", "more", "again", "please", "can", "could", "how",
}


def _terms(topic: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9_.\-/]{3,}", topic.lower()) if w not in _STOP]


def context_slice(topic: str, max_chars: int = 2500) -> dict:
    """A bigger / targeted slice of THIS session's transcript.

    Empty topic → simply MORE of the recent story than the greeting carried.
    Non-empty   → every turn matching the topic's words, each with the turn before and
                  after it for sense, newest matches kept first, budget-bounded."""
    path = find_transcript()
    if not path:
        return {"ok": False, "topic": topic, "reason": "no transcript for this session", "context": ""}
    try:
        tl = timeline(tail_records(path, CONTEXT_TAIL_BYTES))
    except Exception as e:  # a truncated/locked transcript must not kill a live call
        return {"ok": False, "topic": topic, "reason": f"could not read transcript: {e}", "context": ""}
    if not tl:
        return {"ok": False, "topic": topic, "reason": "transcript has no readable turns yet", "context": ""}

    terms = _terms(topic or "")
    if terms:
        hit = [i for i, e in enumerate(tl) if any(t in e["text"].lower() for t in terms)]
        keep = sorted({j for i in hit for j in (i - 1, i, i + 1) if 0 <= j < len(tl)})
        picked = [tl[j] for j in keep]
        matched = len(hit)
    else:
        picked = tl[-24:]
        matched = len(picked)

    if not picked:
        return {"ok": True, "topic": topic, "matches": 0, "source": os.path.basename(path),
                "context": "Nothing in this session's transcript mentions that."}

    # Budget from the NEWEST end backwards — a live caller cares about recent first.
    lines, used = [], 0
    for e in reversed(picked):
        one = ("They asked: " if e["who"] == "user" else "You did: ") + clip(e["text"], 400)
        if used + len(one) > max_chars:
            break
        lines.append(one)
        used += len(one) + 1
    lines.reverse()
    return {"ok": True, "topic": topic, "matches": matched, "source": os.path.basename(path),
            "context": "\n".join(lines)}


# ── persona ──────────────────────────────────────────────────────────────────────────

_TOOL_HINT = """
## Digging deeper (you have one tool)
- `reveal_more_context(topic)` re-reads the session transcript and hands back more of it.
  Call it the moment they ask about something outside the facts below — a file, a bug, a
  name, "what happened before that" — instead of saying you'd have to check.
- Say one short bridging word ("one sec…") before you call it, then answer from what
  comes back. Never read the raw result aloud verbatim; summarize it in a sentence.
"""


def build_persona(human, agent, theme: str, tools: bool = False) -> str:
    last_ask = clip(human[-1], 400) if human else ""
    prior = [clip(h, 160) for h in human[-4:-1]] if len(human) > 1 else []
    recent_actions = []
    for a in agent[-6:]:
        line = clip(a["say"], 180) if a["say"] else ""
        if a["did"]:
            line += (" [" + ", ".join(a["did"][:5]) + "]") if line else "[" + ", ".join(a["did"][:5]) + "]"
        if line:
            recent_actions.append(line)

    name_line = f'You are "{theme}". ' if theme else ""
    facts = []
    if last_ask:
        facts.append(f"- The MOST RECENT thing the user asked you to do:\n  \"{last_ask}\"")
    if recent_actions:
        facts.append("- What you (the agent) actually did most recently, newest last:\n  - " + "\n  - ".join(recent_actions))
    if prior:
        facts.append("- A few things the user asked before that, oldest first:\n  - " + "\n  - ".join(prior))
    facts_block = "\n".join(facts) if facts else "- (This session has almost no history yet — say so honestly and briefly.)"

    steering_tail = (
        "- If they ask something outside these facts, call `reveal_more_context` and answer from it.\n"
        if tools else
        "- If they ask something outside these facts, say you'd have to check rather than guessing.\n"
    )

    return f"""{name_line}You are a warm, sharp colleague on a quick voice call. You were watching this Claude Code session and the person just asked you to catch them up on what was going on.

## Speak
- Begin speaking IMMEDIATELY. Your first word should land in your first breath — open with something like "Hey — welcome back." Do not pause to think.
- Very short turns: one or two sentences, then stop and let them react. Never monologue.
- Plain speech, contractions. No lists read aloud, no "great question", no filler.
- After the opener, give the ONE headline of the last thing that happened, then ask if they want more detail or the step before it.
- If they say bye or goodbye, the call ends on its own — just a warm one-line sign-off.

## Opening (say this first, in your own words, ~10 seconds)
A quick hello, then the single most recent thing: what they last asked and where it landed. Then: "want the details, or what came before it?"

## What actually happened in the session (ground truth — never invent beyond this)
{facts_block}
{_TOOL_HINT if tools else ""}
## Steering
- Answer the exact thing they ask, briefly, then offer to go deeper.
{steering_tail}- Keep it a conversation, not a briefing.
"""


# ── sidecar wiring ───────────────────────────────────────────────────────────────────

def _prune(pattern: str, keep: int) -> None:
    old = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)[keep:]
    for p in old:
        try:
            os.remove(p)
        except OSError:
            pass


def sidecar_paths(tag: str) -> tuple:
    """(log_path, pointer_path) for this call.

    The log name carries T0 so two /lx runs — even inside one session — can never
    truncate each other's live log. The POINTER is the stable, per-session name a
    monitoring agent looks up, and it always names the current call."""
    pin = os.environ.get("LX_LOG", "").strip()
    log = pin or os.path.join(CACHE, f"talk-{tag}-{T0_MS}.jsonl")
    return log, os.path.join(CACHE, f"last-call-{tag}.json")


def arm_sidecar(tag: str, persona_file: str, transcript: str | None) -> str:
    """Create the log 0600-first (so the conversation on disk isn't world-readable) and
    write the pointer the monitor reads. Returns the log path."""
    log, ptr = sidecar_paths(tag)
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        fd = os.open(log, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        os.close(fd)
    except OSError:
        pass
    meta = {"log": log, "persona": persona_file, "session": tag, "transcript": transcript or "",
            "started_ms": T0_MS, "pid": os.getpid()}
    try:
        with open(ptr, "w") as f:
            json.dump(meta, f)
        _prune(os.path.join(CACHE, "talk-*.jsonl"), KEEP_LOGS)
    except OSError:
        pass
    return log


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main():
    argv = sys.argv[1:]

    # `--context [topic]` — the body of the reveal_more_context voice tool. Checked before
    # anything else, and only as the FIRST argument, so the free-form `/lx <theme words>`
    # contract is untouched (a theme never starts with --context).
    if argv and argv[0] == "--context":
        rest = argv[1:]
        max_chars = 2500
        if "--max-chars" in rest:
            i = rest.index("--max-chars")
            try:
                max_chars = max(200, min(20000, int(rest[i + 1])))
            except (IndexError, ValueError):
                pass
            rest = rest[:i] + rest[i + 2:]
        sys.stdout.write(json.dumps(context_slice(" ".join(rest).strip(), max_chars)) + "\n")
        return

    args = [a for a in argv if a.strip()]
    theme = " ".join(args).strip()

    preflight()

    path = find_transcript()
    human, agent = ([], [])
    if path:
        try:
            human, agent = extract(tail_records(path))
        except Exception:
            pass

    # Tools stay OFF until the apiplan side lands them (see lx_tools.mjs) — promising the
    # model a tool it cannot call is worse than not having one. LX_TOOLS=1 flips it.
    tools = _truthy("LX_TOOLS")
    persona = build_persona(human, agent, theme, tools)
    os.makedirs(CACHE, exist_ok=True)
    # Per-session filename — a fixed persona.md collides when two sessions run /lx at once:
    # the last writer wins and apiplan voices the WRONG persona. Key it to this session's id
    # (pid fallback) so each run reads back exactly what it just wrote.
    tag = session_tag()
    pf = os.path.join(CACHE, f"persona-{tag}.md")
    with open(pf, "w") as f:
        f.write(persona)

    log = arm_sidecar(tag, pf, path)

    built_ms = int(time.time() * 1000) - T0_MS
    src = os.path.basename(path) if path else "no transcript found"
    sys.stderr.write(f"live-explain: persona built in {built_ms}ms from {src}; connecting voice…\n")
    # Machine-readable so a launcher can grep one line instead of parsing prose.
    sys.stderr.write(f"live-explain: log {log}\n")
    sys.stderr.write(f"live-explain: monitor python3 {os.path.join(HERE, 'lx_monitor.py')} --wait 30\n")
    sys.stderr.flush()

    env = dict(os.environ, LX_T0_MS=str(T0_MS), LX_LOG=log)
    if path:
        # Pin the transcript for the child: the tool handler must dig in the SAME session's
        # history, and it does not inherit our resolution work otherwise.
        env["LX_TRANSCRIPT"] = path
    cmd = ["apiplan", "talk", "--persona", pf, "--voice", VOICE, "--greet", "--log", log]
    if tools:
        # apiplan's tool flag (Phase 3). Name is overridable so this keeps working if the
        # CLI lands it under a different spelling.
        cmd += [os.environ.get("LX_TOOLS_FLAG", "--tools"), os.path.join(HERE, "lx_tools.mjs")]
    # exec replaces this process so stdio/signals (Ctrl-C, the 'bye' hangup) pass straight through
    os.execvpe("apiplan", cmd, env)


if __name__ == "__main__":
    main()
