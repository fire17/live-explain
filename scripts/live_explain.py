#!/usr/bin/env python3
"""live-explain — greet the user in a voice persona and recap the last few things
that happened in THIS Claude Code session, as fast as possible.

Speed strategy: the only pre-work before the voice connects is (1) tail-read the current
transcript and (2) write a persona file. Both are cheap and bounded, so we hand off to
`apiplan talk` within a few ms of launch. The recap FACTS are baked into the persona, so
the realtime model just voices them — no reasoning delay, and it can't get them wrong.

Usage: live_explain.py [persona-name-or-theme...]   (all args = optional voice/theme flavor)
"""
from __future__ import annotations  # lazy annotations → runs on the stock 3.7+ python a fresh Mac ships

import glob
import json
import os
import shutil
import sys
import time

T0_MS = int(time.time() * 1000)  # ~ skill-call time; talk.ts reports first-word latency vs this

HOME = os.path.expanduser("~")
VOICE = os.environ.get("LX_VOICE", "cedar")
TAIL_BYTES = 300_000  # enough for many recent records; keeps big transcripts fast


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


def tail_records(path: str) -> list[dict]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        chunk = f.read()
    lines = chunk.decode("utf-8", "ignore").splitlines()
    if size > TAIL_BYTES and lines:
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
    import re
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
                agent.append({"say": blurb, "did": tools})
    return human, agent


def clip(t: str, n: int) -> str:
    t = " ".join(t.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def build_persona(human, agent, theme: str) -> str:
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

## Steering
- Answer the exact thing they ask, briefly, then offer to go deeper.
- If they ask something outside these facts, say you'd have to check rather than guessing.
- Keep it a conversation, not a briefing.
"""


def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    theme = " ".join(args).strip()

    preflight()

    path = find_transcript()
    human, agent = ([], [])
    if path:
        try:
            human, agent = extract(tail_records(path))
        except Exception:
            pass

    persona = build_persona(human, agent, theme)
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".cache")
    os.makedirs(cache, exist_ok=True)
    # Per-session filename — a fixed persona.md collides when two sessions run /lx at once:
    # the last writer wins and apiplan voices the WRONG persona. Key it to this session's id
    # (pid fallback) so each run reads back exactly what it just wrote.
    tag = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip() or str(os.getpid())
    pf = os.path.join(cache, f"persona-{tag}.md")
    with open(pf, "w") as f:
        f.write(persona)

    built_ms = int(time.time() * 1000) - T0_MS
    src = os.path.basename(path) if path else "no transcript found"
    sys.stderr.write(f"live-explain: persona built in {built_ms}ms from {src}; connecting voice…\n")
    sys.stderr.flush()

    env = dict(os.environ, LX_T0_MS=str(T0_MS))
    # exec replaces this process so stdio/signals (Ctrl-C, the 'bye' hangup) pass straight through
    os.execvpe("apiplan", ["apiplan", "talk", "--persona", pf, "--voice", VOICE, "--greet"], env)


if __name__ == "__main__":
    main()
