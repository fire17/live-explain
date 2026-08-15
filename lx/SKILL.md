---
name: lx
description: Alias for /live-explain — instantly spin up a persona that greets you and recaps the last few things that happened in the current Claude Code session (voice; first word ~1.8s). Use on /lx [persona-or-theme].
argument-hint: "[persona-name-or-theme]"
---

# lx — alias for /live-explain

Run immediately as your first action (latency is the point):

```
python3 ~/.claude/skills/live-explain/scripts/live_explain.py $ARGUMENTS
```

Show the streamed greeting as printed. See the `live-explain` skill for details.
