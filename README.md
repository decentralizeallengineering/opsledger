# opsledger

[![tests](https://github.com/decentralizeallengineering/opsledger/actions/workflows/tests.yml/badge.svg)](https://github.com/decentralizeallengineering/opsledger/actions/workflows/tests.yml) ![license](https://img.shields.io/badge/license-Apache--2.0-blue)

Turns an "operation" (a large, multi-part request from a person) into tracked asks and a percent complete.

Big requests get lost in chat. opsledger keeps the person's own words in an append-only log, splits the request into workstreams and asks, dedupes and deconflicts them (newest wording wins, old rows are kept with dates), and scores progress with an evidence trail. Python standard library only; storage is one JSON file plus generated markdown.

## Install

    pip install .        # or run `python3 opsledger.py` directly

## Configure

- `--dir` or `OPSLEDGER_DIR`: data directory (default `./ops`). It holds `log.md`, `ledger.json`, `ledger.md`, `report.md`.
- `OPSLEDGER_MODEL` (default `default`) and `OPSLEDGER_LLM_KEY` (optional bearer token) for `--llm`.
- `OPSLEDGER_NOW`: pin the clock (ISO 8601) for reproducible runs.

## Use

    opsledger intake notes.txt                  # or `-` for stdin; one verbatim, timestamped entry
    opsledger intake --transcripts ~/transcripts # person-typed messages from *.jsonl transcripts
    opsledger op new move plan.txt              # headings/bullets -> workstreams/asks
    opsledger op new move plan.txt --llm http://127.0.0.1:8080   # model splits and dedupes (falls back to rules)
    opsledger ask add move Reports "Send Ann Example the weekly report"
    opsledger ask merge A1 A3                   # newest wording wins; A3 stays, marked merged
    opsledger ask supersede A2 "Archive folders older than a year"
    opsledger status A1 DONE --evidence "commit abc123"
    opsledger score                             # prints and writes report.md

Scoring: DONE 1, BUILT 0.6, PARTIAL 0.4, OPEN 0. BLOCKED asks are left out of "in hand" and count as 0 in "overall". Superseded and merged asks are not scored. The report gives both percentages per operation and per workstream.

Rule splitter: a markdown heading, a short line ending in `:`, or a short plain line followed by a bullet starts a workstream; bullets and other lines are asks; indented lines continue the previous bullet.

## Test

    python3 -m pytest

Licensed under Apache-2.0.
