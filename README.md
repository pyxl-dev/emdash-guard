# emdash-guard

A Hermes Agent plugin that removes em dashes (`U+2014` —), en dashes (`U+2013` –) and
horizontal bars (`U+2015` ―) from everything the agent writes.

Not a style instruction the model can forget: a `transform_llm_output` hook rewrites the
final assistant response deterministically, **before** it is stored in the transcript and
delivered to the user (CLI, gateway, desktop). No extra inference tokens, no prompt
engineering.

```
model output:   Bonjour — voici la synthèse. Plage 2018–2020.
what you get:   Bonjour - voici la synthèse. Plage 2018-2020.
```

## Why

Language models emit em dashes constantly, and they are the single most reliable tell of
machine-written text. Deleting them by asking nicely does not hold: the model regresses
as soon as the instruction leaves the recent context window. This plugin makes it a
mechanical, testable guarantee instead of a preference.

## What it does

| Character | Code point | Rewritten to |
|---|---|---|
| em dash | `U+2014` | ` - ` (or a glued `-` inside a numeric range) |
| en dash | `U+2013` | same |
| horizontal bar | `U+2015` | same |

Rules, in order:

1. A dash standing between two digits is a range separator and becomes a bare hyphen:
   `2018–2020` -> `2018-2020`.
2. Everywhere else it is a punctuation break and becomes a spaced hyphen:
   `word—word` -> `word - word`, `a – b` -> `a - b`.
3. Lines that were rewritten get their trailing whitespace trimmed, so a line-final dash
   does not leave a dangling space.

The transform is idempotent (running it twice changes nothing the second time) and only
touches the current turn's final text: the system prompt, earlier turns and the prompt
cache are never rewritten.

## Loud on purpose

A silent fixer cannot be trusted, so every interception is recorded:

- `WARNING` line in the Hermes logs,
- banner on stderr,
- one JSONL line in `$HERMES_HOME/logs/emdash-guard.log` that keeps the **original**
  snippet as evidence of what was rewritten,
- a counter in `$HERMES_HOME/state/emdash-guard.json`,
- optional macOS notification (rate limited to one per minute).

## Install

```bash
# 1. drop the plugin into the profile that needs it
git clone https://github.com/pyxl-dev/emdash-guard /tmp/emdash-guard
mkdir -p "$HERMES_HOME/plugins/emdash-guard"
cp /tmp/emdash-guard/{plugin.yaml,__init__.py,dashes.py} "$HERMES_HOME/plugins/emdash-guard/"

# 2. enable it (the running gateway is nudged over the control socket, so it goes live now)
hermes plugins enable emdash-guard

# 3. verify
hermes plugins doctor emdash-guard
hermes chat -q 'Reply exactly: Hello — test'   # must come back as "Hello - test"
```

`$HERMES_HOME` defaults to `~/.hermes`; with profiles it is
`~/.hermes/profiles/<name>`. Only hooks and slash commands go live on `enable` — a
plugin's *tools* and prompt sections wait for the next session (Hermes prompt-cache
invariant).

## Usage

Inside a Hermes session:

```
/dashes                  counters (corrected + interim leaks), last interception, log path
/dashes log 20           last 20 interceptions
/dashes fix <path>...    scan and rewrite files (directories are walked recursively)
/dashes check <path>...  read-only scan
```

From a terminal, on any text file or tree (useful as a release gate for documents):

```bash
python3 dashes.py --check docs/        # exit code 1 if a dash is still there
python3 dashes.py --fix docs/          # rewrite in place
echo "word — word" | python3 dashes.py --stdin
```

## Settings

| Variable | Default | Effect |
|---|---|---|
| `HERMES_EMDASH_NOTIFY` | off | `1` enables the macOS notification on interception |
| `HERMES_EMDASH_ANNOUNCE` | off | `1` appends a visible marker to the rewritten response |

## Known limits

- **No syntax exemption.** The rewrite is global: fenced code blocks, inline code and YAML front
  matter are rewritten exactly like prose. That is deliberate (a stray dash in a code comment is
  still a stray dash), and the one document to keep out of its way is a page whose subject *is*
  typography, where the rule would rewrite the very character under discussion.
- **Interim text is out of reach.** Hook payloads for streaming (`on_stream_delta`,
  `on_interim_message`) are immutable by design, so any text Hermes emits as an *interim*
  message never passes through the transform. There are two paths into that state:
  1. commentary written between two tool calls (visible when
     `display.interim_assistant_messages` is on) - keep a style rule for it, or turn that
     setting off;
  2. **the answer itself**, reclassified as interim by the verify-on-stop stop gate
     (`agent/turn_stop_gates.py::_append_interim_answer`, `finish_reason=verification_required`)
     when a turn edited files without verification evidence. Set `agent.verify_on_stop: false`
     to remove that path; otherwise a file-editing turn can show its answer untransformed.

  This hole is **measured, not fixed**. The plugin subscribes to the `on_interim_message`
  observer: every interim message that carries a dash adds to a separate `interims`
  counter (the `dashes` counter only counts real corrections) and writes one line with
  `"leak": true` to `$HERMES_HOME/logs/emdash-guard.log`, with the original `sample` and
  the usual `pid`/`home`/`turn_id` diagnostics. `/dashes` shows both counters and
  `/dashes log` tags those lines as interim. Nothing is rewritten: the text has already
  reached the user, so a non-zero `interims` count means dashes were shown, and the only
  levers are the two settings above.
- **Text files only.** Non-UTF-8/binary files are skipped; `dashes.py --fix` never touches
  them.
- **`config.yaml` shell hooks cannot do this.** The shell-hook response parser only
  understands `block`/`modify`/`context` directives, so a rewrite has to be a Python
  plugin returning a string from `transform_llm_output`.
- Interceptions are counted per profile, from the process that owns the turn. One host
  process can serve many profiles with the launcher's home in its environment, so the
  journal resolves its home from the plugin's own location and stamps `pid`/`turn_id` on
  every entry.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers the rewrite rules, idempotence, numeric ranges, byte-identical handling of clean
and binary files, the CLI contract (`--check` exit code, `--stdin`), hook registration,
the interim leak counter and journal, and the notification default (off unless
explicitly opted in).

## License

MIT
