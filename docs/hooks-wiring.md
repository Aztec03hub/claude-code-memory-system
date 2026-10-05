# Hook wiring

Five hook entries, all in `~/.claude/settings.json`. Four run the one script
`src/memory-search.py`, which branches on `hook_event_name` from its stdin; the fifth is the
index guard that lives beside the memory files.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {"matcher": "*", "hooks": [
        {"type": "command", "timeout": 10,
         "command": "python3 ~/claude_projects/memory-system/src/memory-search.py"}]}
    ],
    "SessionStart": [
      {"matcher": "*", "hooks": [
        {"type": "command", "timeout": 15,
         "command": "python3 ~/claude_projects/memory-system/src/memory-search.py"}]}
    ],
    "Stop": [
      {"matcher": "*", "hooks": [
        {"type": "command", "timeout": 10,
         "command": "python3 ~/claude_projects/memory-system/src/memory-search.py"}]}
    ],
    "SessionEnd": [
      {"matcher": "*", "hooks": [
        {"type": "command", "timeout": 10,
         "command": "python3 ~/claude_projects/memory-system/src/memory-search.py"}]}
    ],
    "PostToolUse": [
      {"matcher": "*", "hooks": [
        {"type": "command", "timeout": 15,
         "command": "python3 ~/.claude/projects/<slug>/memory/check-memory-index.py --hook"}]}
    ]
  }
}
```

Expand `~` to a real path if your Claude Code version does not, and replace `<slug>` with the
project directory that holds your memory files.

## What each one is for

| Event | Job | Why there |
|---|---|---|
| `UserPromptSubmit` | search the corpus, inject what matches | the only event that sees the prompt. Everything else in the system exists to make this one call good. |
| `SessionStart` | banner: organ health, queued proposals, orphan sweep | the one moment a human reliably reads output. Silent when there is nothing to say. |
| `Stop` | grade the previous injection, accumulate proposals | the turn is over, so whether an injected memory was actually used is now knowable. |
| `SessionEnd` | flush and prune | bounded files, nothing deferred to a cron that may not exist. |
| `PostToolUse` | enforce the two index invariants | fires on the write itself, which is the only point where a new memory is certain to exist. |

## Two things that will bite

**A hook's stdout is a protocol channel, not a log.** Claude Code parses it as one JSON
object. A reused helper that prints one friendly progress line to stdout silently destroys
the message while the work itself still happens, so the hook looks like it never fired. Wrap
anything you call in `contextlib.redirect_stdout(sys.stderr)` and test the hook by parsing
its stdout, never by reading it.

**A hook only protects what is written after it.** The `PostToolUse` guard cannot see a
memory created while it was absent, broken, or bypassed. That is why `SessionStart` repeats
the orphan sweep over the whole directory: a glob and two reads, once per session.
