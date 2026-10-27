#!/usr/bin/env python3
"""opsledger: turn an operation (a large multi-part request) into tracked asks and a percent complete.

Storage is one JSON file plus generated markdown (ledger, report) and an append-only message log.
Everything lives in a data directory: --dir, else $OPSLEDGER_DIR, else ./ops.
Python standard library only.
"""
import argparse
import difflib
import glob
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

STATUSES = ("DONE", "BUILT", "PARTIAL", "OPEN", "BLOCKED")
WEIGHTS = {"DONE": 1.0, "BUILT": 0.6, "PARTIAL": 0.4, "OPEN": 0.0}  # BLOCKED is excluded from "in hand"
NEEDS_EVIDENCE = ("DONE", "BUILT")
SIMILAR = 0.82  # difflib ratio at which two asks count as the same ask
DEFAULT_WS = "General"

BULLET = re.compile(r"^(\s*)(?:[-*+\u2022]|\d+[.)])\s+(.*\S)\s*$")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")
SYSREM = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
SKIP_PREFIX = ("This session is being continued", "<task-notification", "<local-command",
               "[Request interrupted", "Caveat:")


class OpsError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def now():
    """Current time (UTC). $OPSLEDGER_NOW (ISO 8601) pins it for reproducible runs."""
    fixed = os.environ.get("OPSLEDGER_NOW")
    if fixed:
        return parse_ts(fixed)
    return datetime.now(timezone.utc)


def parse_ts(ts):
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def today():
    return now().strftime("%Y-%m-%d")


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def similar(a, b):
    na, nb = norm(a), norm(b)
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= SIMILAR


def data_dir(args):
    return getattr(args, "dir", None) or os.environ.get("OPSLEDGER_DIR") or "ops"


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write_atomic(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- store

def load(d):
    path = os.path.join(d, "ledger.json")
    if not os.path.exists(path):
        return {"next_id": 1, "operations": {}, "asks": []}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(d, store):
    write_atomic(os.path.join(d, "ledger.json"), json.dumps(store, indent=2, ensure_ascii=False) + "\n")
    write_atomic(os.path.join(d, "ledger.md"), render_ledger(store))


def active(ask):
    return not ask.get("superseded_by") and not ask.get("merged_into")


def find(store, ident):
    ident = str(ident).strip().upper().lstrip("A")
    for ask in store["asks"]:
        if ask["id"] == "A" + ident:
            return ask
    raise OpsError(f"no ask with id {ident!r}")


def get_op(store, name):
    if name not in store["operations"]:
        raise OpsError(f"no operation named {name!r}")
    return store["operations"][name]


def new_ask(store, op, ws, text):
    ask = {"id": f"A{store['next_id']}", "op": op, "workstream": ws, "text": text,
           "status": "OPEN", "evidence": "", "created": today(), "updated": today(),
           "history": [{"date": today(), "event": "created", "text": text}]}
    store["next_id"] += 1
    store["asks"].append(ask)
    ops = store["operations"][op]
    if ws not in ops["workstreams"]:
        ops["workstreams"].append(ws)
    return ask


def find_similar(store, op, text):
    for ask in store["asks"]:
        if ask["op"] == op and active(ask) and similar(ask["text"], text):
            return ask
    return None


# ---------------------------------------------------------------- intake

def log_path(d):
    return os.path.join(d, "log.md")


def append_log(d, entries):
    """entries: list of (when_datetime, source, text, key). Append-only, deduplicated by key."""
    seen_path = os.path.join(d, "seen.json")
    seen = set(read_text(seen_path) and json.loads(read_text(seen_path))) if os.path.exists(seen_path) else set()
    out, added = "", 0
    for when, source, text, key in entries:
        if key in seen:
            continue
        seen.add(key)
        added += 1
        lines = text.split("\n")
        out += "- {} [{}] {}\n".format(when.strftime("%Y-%m-%d %H:%M UTC"), source, lines[0])
        for ln in lines[1:]:
            out += ("    " + ln.rstrip() + "\n") if ln.strip() else "\n"
    if added:
        os.makedirs(d, exist_ok=True)
        fresh = not os.path.exists(log_path(d))
        with open(log_path(d), "a", encoding="utf-8") as fh:
            if fresh:
                fh.write("# Message log\n\nMessages as typed, with timestamps (UTC). Append-only; do not edit.\n\n")
            fh.write(out)
        write_atomic(seen_path, json.dumps(sorted(seen)))
    return added


def blocks_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return ""
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def human_message(rec):
    """Return the text of a person-typed message from one transcript record, else None."""
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
        return None
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    text = SYSREM.sub("", blocks_text((rec.get("message") or {}).get("content"))).strip("\n").rstrip()
    if not text.strip() or text.lstrip().startswith(SKIP_PREFIX):
        return None
    return text


def transcript_entries(directory):
    found = []
    for path in sorted(glob.glob(os.path.join(directory, "**", "*.jsonl"), recursive=True)):
        label = "transcript:" + os.path.basename(path)[:-6]
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                text = human_message(rec) if isinstance(rec, dict) else None
                if text and rec.get("timestamp"):
                    when = parse_ts(rec["timestamp"])
                    key = hashlib.sha1((rec["timestamp"] + "\0" + text).encode()).hexdigest()
                    found.append((when, label, text, key))
    found.sort(key=lambda e: e[0])
    return found


def cmd_intake(args):
    d = data_dir(args)
    if args.transcripts:
        added = append_log(d, transcript_entries(args.transcripts))
    else:
        if not args.source:
            raise OpsError("give a text file, '-' for stdin, or --transcripts DIR")
        if args.source == "-":
            text, label = sys.stdin.read(), "stdin"
        else:
            with open(args.source, encoding="utf-8") as fh:
                text = fh.read()
            label = "file:" + os.path.basename(args.source)
        text = text.strip("\n").rstrip()
        if not text.strip():
            raise OpsError("input is empty")
        added = append_log(d, [(now(), label, text, hashlib.sha1(text.encode()).hexdigest())])
    print(f"intake: {added} new message(s) appended to {log_path(d)}")


# ---------------------------------------------------------------- operation split

def split_rules(text):
    """Rule-based split: headings, short lines ending in ':', and short plain lines followed by a bullet
    start workstreams; bullets and other plain lines are asks."""
    plan, ws, last = {}, DEFAULT_WS, None
    order = []

    def bucket(name):
        if name not in plan:
            plan[name] = []
            order.append(name)
        return plan[name]

    lines = text.splitlines()
    for i, raw in enumerate(lines):
        if not raw.strip():
            last = None
            continue
        h = HEADING.match(raw)
        if h:
            ws, last = h.group(1).strip().rstrip(":").strip(), None
            bucket(ws)
            continue
        b = BULLET.match(raw)
        if b:
            bucket(ws).append(b.group(2).strip())
            last = plan[ws]
            continue
        stripped = raw.strip()
        if raw[:1].isspace() and last:
            last[-1] += " " + stripped  # indented continuation of the previous bullet
            continue
        nxt = next((x for x in lines[i + 1:] if x.strip()), "")
        if (stripped.endswith(":") and len(stripped) <= 60) or (BULLET.match(nxt) and len(stripped) <= 60):
            ws, last = stripped.rstrip(":").strip(), None
            bucket(ws)
            continue
        bucket(ws).append(stripped)
        last = plan[ws]
    return [(name, plan[name]) for name in order]


def split_llm(text, url, model):
    endpoint = url if url.rstrip("/").endswith("/chat/completions") else url.rstrip("/") + "/v1/chat/completions"
    prompt = ("Split the following operation into workstreams and discrete asks, merging duplicates. "
              "Keep each ask in the author's own words. Reply with JSON only, shaped as "
              '{"workstreams":[{"name":"...","asks":["..."]}]}.\n\n' + text)
    body = json.dumps({"model": model, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    headers = {"Content-Type": "application/json"}
    if os.environ.get("OPSLEDGER_LLM_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["OPSLEDGER_LLM_KEY"]
    with urllib.request.urlopen(urllib.request.Request(endpoint, body, headers), timeout=120) as resp:
        reply = json.load(resp)["choices"][0]["message"]["content"]
    data = json.loads(reply[reply.index("{"):reply.rindex("}") + 1])
    return [(w["name"], [str(a) for a in w["asks"]]) for w in data["workstreams"]]


def cmd_op_new(args):
    d = data_dir(args)
    store = load(d)
    if args.name in store["operations"]:
        raise OpsError(f"operation {args.name!r} already exists")
    text = sys.stdin.read() if args.file == "-" else read_text(args.file)
    plan = None
    if args.llm:
        try:
            plan = split_llm(text, args.llm, args.model)
        except Exception as exc:  # noqa: BLE001 - network, parse or shape problems all fall back to the rules
            print(f"opsledger: model split failed ({exc}); using rules", file=sys.stderr)
    if plan is None:
        plan = split_rules(text)
    store["operations"][args.name] = {"created": today(), "workstreams": []}
    count = skipped = 0
    for ws, asks in plan:
        if ws not in store["operations"][args.name]["workstreams"]:
            store["operations"][args.name]["workstreams"].append(ws)
        for a in asks:
            if find_similar(store, args.name, a):
                skipped += 1
                continue
            new_ask(store, args.name, ws, a)
            count += 1
    save(d, store)
    nws = len(store["operations"][args.name]["workstreams"])
    print(f"op {args.name}: {nws} workstream(s), {count} ask(s), {skipped} duplicate(s) skipped")


# ---------------------------------------------------------------- asks

def cmd_ask_add(args):
    d = data_dir(args)
    store = load(d)
    get_op(store, args.op)
    text = " ".join(args.text).strip()
    dup = find_similar(store, args.op, text)
    if dup and not args.force:
        raise OpsError("similar to {} ({!r}); use merge or supersede, or --force".format(dup["id"], dup["text"]))
    ask = new_ask(store, args.op, args.workstream, text)
    save(d, store)
    print("added {}".format(ask["id"]))


def rank(status):
    return STATUSES.index(status)


def cmd_ask_merge(args):
    """Fold the other asks into the first. The newest wording becomes the text; the folded rows stay, marked merged."""
    d = data_dir(args)
    store = load(d)
    keep = find(store, args.keep)
    group = [keep] + [find(store, i) for i in args.drop]
    for a in group:
        if not active(a):
            raise OpsError("{} is already superseded or merged".format(a["id"]))
        if a["op"] != keep["op"]:
            raise OpsError("{} is in a different operation".format(a["id"]))
    if len({a["id"] for a in group}) != len(group):
        raise OpsError("an ask cannot be merged with itself")
    newest = max(group, key=lambda a: (a["created"], int(a["id"][1:])))
    best = min(group, key=lambda a: rank(a["status"]))  # DONE outranks BUILT ... BLOCKED
    if newest["text"] != keep["text"]:
        keep["history"].append({"date": today(), "event": "reworded", "text": keep["text"]})
        keep["text"] = newest["text"]
    keep["status"], keep["evidence"] = best["status"], best["evidence"]
    keep["updated"] = today()
    for a in group[1:]:
        a["merged_into"], a["merged_on"], a["updated"] = keep["id"], today(), today()
        a["history"].append({"date": today(), "event": "merged into " + keep["id"], "text": a["text"]})
    keep["history"].append({"date": today(), "event": "merged " + ", ".join(a["id"] for a in group[1:])})
    save(d, store)
    print("merged {} into {}".format(", ".join(a["id"] for a in group[1:]), keep["id"]))


def cmd_ask_supersede(args):
    """Replace an ask with newer wording (new text, or an existing ask via --by). The old row is kept with its date."""
    d = data_dir(args)
    store = load(d)
    old = find(store, args.old)
    if not active(old):
        raise OpsError("{} is already superseded or merged".format(old["id"]))
    if args.by:
        new = find(store, args.by)
        if new["id"] == old["id"] or new["op"] != old["op"] or not active(new):
            raise OpsError("--by must be a different, active ask in the same operation")
    else:
        text = " ".join(args.text).strip()
        if not text:
            raise OpsError("give the new wording, or --by ID")
        new = new_ask(store, old["op"], old["workstream"], text)
    old["superseded_by"], old["superseded_on"], old["updated"] = new["id"], today(), today()
    old["history"].append({"date": today(), "event": "superseded by " + new["id"], "text": old["text"]})
    save(d, store)
    print("{} superseded by {}".format(old["id"], new["id"]))


def cmd_ask_list(args):
    store = load(data_dir(args))
    for a in store["asks"]:
        if args.op and a["op"] != args.op:
            continue
        if not active(a) and not args.all:
            continue
        note = ""
        if a.get("superseded_by"):
            note = " (superseded by {} on {})".format(a["superseded_by"], a["superseded_on"])
        if a.get("merged_into"):
            note = " (merged into {} on {})".format(a["merged_into"], a["merged_on"])
        print("{}\t{}\t{}\t{}/{}\t{}{}".format(a["id"], a["status"], a["updated"], a["op"], a["workstream"], a["text"], note))


def cmd_status(args):
    d = data_dir(args)
    store = load(d)
    ask = find(store, args.id)
    if not active(ask):
        raise OpsError("{} is superseded or merged; set the status on its replacement".format(ask["id"]))
    evidence = (args.evidence or "").strip()
    if args.status in NEEDS_EVIDENCE and not evidence:
        raise OpsError(f"{args.status} needs --evidence (a commit, file or note)")
    ask["history"].append({"date": today(), "event": "{} -> {}".format(ask["status"], args.status), "evidence": evidence})
    ask["status"], ask["updated"] = args.status, today()
    if evidence or args.status not in NEEDS_EVIDENCE:
        ask["evidence"] = evidence
    save(d, store)
    print("{} is now {}".format(ask["id"], args.status))


# ---------------------------------------------------------------- score

def tally(asks):
    counts = {s: 0 for s in STATUSES}
    for a in asks:
        counts[a["status"]] += 1
    total = len(asks)
    points = sum(WEIGHTS[s] * counts[s] for s in WEIGHTS)
    hand = total - counts["BLOCKED"]
    return {"asks": total, "counts": counts, "points": points,
            "in_hand": round(100 * points / hand, 1) if hand else None,
            "overall": round(100 * points / total, 1) if total else None}


def score(store):
    result = {}
    for op, meta in store["operations"].items():
        asks = [a for a in store["asks"] if a["op"] == op and active(a)]
        wss = {ws: tally([a for a in asks if a["workstream"] == ws]) for ws in meta["workstreams"]}
        result[op] = {"total": tally(asks), "workstreams": wss}
    return result


def pct(v):
    return "n/a" if v is None else f"{v:.1f}%"


def render_report(result):
    out = ["# Operation score", "",
           ("Weights: DONE 1, BUILT 0.6, PARTIAL 0.4, OPEN 0. In hand leaves BLOCKED asks out of the count; "
           "overall counts them as 0. Superseded and merged asks are not scored."), ""]
    for op, r in result.items():
        t = r["total"]
        out += [f"## {op}", "", f"In hand: **{pct(t['in_hand'])}**. Overall: **{pct(t['overall'])}**. Asks scored: {t['asks']}.", "",
                "| workstream | asks | DONE | BUILT | PARTIAL | OPEN | BLOCKED | in hand | overall |",
                "|---|---|---|---|---|---|---|---|---|"]
        for ws, w in list(r["workstreams"].items()) + [("Total", t)]:
            c = w["counts"]
            out.append(f"| {ws} | {w['asks']} | {c['DONE']} | {c['BUILT']} | {c['PARTIAL']} | {c['OPEN']} "
                       f"| {c['BLOCKED']} | {pct(w['in_hand'])} | {pct(w['overall'])} |")
        out.append("")
    if not result:
        out.append("No operations yet.")
    return "\n".join(out) + "\n"


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_ledger(store):
    out = ["# Ledger", "", "Generated by opsledger from ledger.json. Do not edit by hand.", ""]
    for op, meta in store["operations"].items():
        out += [f"## {op}", "", "Created {}.".format(meta["created"]), ""]
        for ws in meta["workstreams"]:
            rows = [a for a in store["asks"] if a["op"] == op and a["workstream"] == ws and active(a)]
            out += [f"### {ws}", "", "| id | ask | status | evidence | updated |", "|---|---|---|---|---|"]
            out += ["| {} | {} | {} | {} | {} |".format(a["id"], cell(a["text"]), a["status"], cell(a["evidence"]), a["updated"]) for a in rows]
            out.append("")
        gone = [a for a in store["asks"] if a["op"] == op and not active(a)]
        if gone:
            out += ["### Superseded and merged", "", "| id | old wording | outcome | date |", "|---|---|---|---|"]
            for a in gone:
                if a.get("superseded_by"):
                    how, when = "superseded by " + a["superseded_by"], a["superseded_on"]
                else:
                    how, when = "merged into " + a["merged_into"], a["merged_on"]
                out.append("| {} | {} | {} | {} |".format(a["id"], cell(a["text"]), how, when))
            out.append("")
    return "\n".join(out) + "\n"


def cmd_score(args):
    d = data_dir(args)
    result = score(load(d))
    report = render_report(result)
    path = args.out or os.path.join(d, "report.md")
    write_atomic(path, report)
    print(report, end="")
    print(f"report written to {path}")


# ---------------------------------------------------------------- cli

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=argparse.SUPPRESS, help="data directory (default $OPSLEDGER_DIR or ./ops)")
    p = argparse.ArgumentParser(prog="opsledger", parents=[common],
                                description="Track a multi-part operation as asks and a percent complete.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("intake", parents=[common], help="append typed messages to the log")
    s.add_argument("source", nargs="?", help="text file, or - for stdin")
    s.add_argument("--transcripts", metavar="DIR", help="read person-typed messages from *.jsonl transcripts under DIR")
    s.set_defaults(fn=cmd_intake)

    s = sub.add_parser("op", parents=[common], help="operations")
    osub = s.add_subparsers(dest="opcmd", required=True)
    n = osub.add_parser("new", parents=[common], help="split a file into workstreams and asks")
    n.add_argument("name")
    n.add_argument("file", help="text file, or - for stdin")
    n.add_argument("--llm", metavar="URL", help="OpenAI-compatible base URL used to split and dedupe")
    n.add_argument("--model", default=os.environ.get("OPSLEDGER_MODEL", "default"))
    n.set_defaults(fn=cmd_op_new)

    s = sub.add_parser("ask", parents=[common], help="add, merge, supersede or list asks")
    asub = s.add_subparsers(dest="askcmd", required=True)
    a = asub.add_parser("add", parents=[common], help="add an ask (refuses near-duplicates)")
    a.add_argument("op")
    a.add_argument("workstream")
    a.add_argument("text", nargs="+")
    a.add_argument("--force", action="store_true", help="add even if a similar ask exists")
    a.set_defaults(fn=cmd_ask_add)
    a = asub.add_parser("merge", parents=[common], help="merge ask(s) into the first id; newest wording wins")
    a.add_argument("keep")
    a.add_argument("drop", nargs="+")
    a.set_defaults(fn=cmd_ask_merge)
    a = asub.add_parser("supersede", parents=[common], help="replace an ask with new wording; the old row is kept")
    a.add_argument("old")
    a.add_argument("text", nargs="*")
    a.add_argument("--by", metavar="ID", help="use an existing ask as the replacement")
    a.set_defaults(fn=cmd_ask_supersede)
    a = asub.add_parser("list", parents=[common], help="list asks")
    a.add_argument("op", nargs="?")
    a.add_argument("--all", action="store_true", help="include superseded and merged rows")
    a.set_defaults(fn=cmd_ask_list)

    s = sub.add_parser("status", parents=[common], help="set an ask's status")
    s.add_argument("id")
    s.add_argument("status", type=str.upper, choices=STATUSES)
    s.add_argument("--evidence", help="commit, file or note (required for DONE and BUILT)")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("score", parents=[common], help="print and write the percent-complete report")
    s.add_argument("--out", help="report path (default <dir>/report.md)")
    s.set_defaults(fn=cmd_score)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except OpsError as exc:
        print(f"opsledger: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"opsledger: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
