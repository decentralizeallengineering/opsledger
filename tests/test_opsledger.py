import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import opsledger


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPSLEDGER_DIR", str(tmp_path / "ops"))
    monkeypatch.setenv("OPSLEDGER_NOW", "2026-01-05T09:00:00Z")
    return tmp_path


def run(*argv):
    assert opsledger.main(list(argv)) == 0


def ledger(env):
    return json.loads((env / "ops" / "ledger.json").read_text())


PLAN = """\
Reports:
- Ann Example wants a weekly sales report
- Email the report to ann@example.invalid
Cleanup
* Archive the old folders
* archive the OLD folders!
"""


def test_intake_text_and_transcripts_verbatim_and_append_only(env):
    note = env / "note.txt"
    note.write_text("Please plan the move.\n\n  keep this indent\n")
    run("intake", str(note))
    run("intake", str(note))  # same text again: no second entry
    tdir = env / "tr"
    tdir.mkdir()
    lines = [
        {"type": "user", "timestamp": "2026-01-04T10:30:00Z", "origin": {"kind": "human"},
         "message": {"content": "Ann Example here <system-reminder>noise</system-reminder>"}},
        {"type": "user", "timestamp": "2026-01-04T10:31:00Z",
         "message": {"content": [{"type": "tool_result", "content": "tool output"}]}},
        {"type": "user", "timestamp": "2026-01-04T10:32:00Z", "origin": {"kind": "automation"},
         "message": {"content": "scheduled prompt"}},
        {"type": "assistant", "timestamp": "2026-01-04T10:33:00Z", "message": {"content": "reply"}},
    ]
    (tdir / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    run("intake", "--transcripts", str(tdir))
    run("intake", "--transcripts", str(tdir))
    log = (env / "ops" / "log.md").read_text()
    assert log.count("Please plan the move.") == 1
    assert "    keep this indent" in log
    assert "- 2026-01-04 10:30 UTC [transcript:s1] Ann Example here" in log
    assert "noise" not in log and "tool output" not in log and "scheduled prompt" not in log and "reply" not in log


def test_op_new_rule_split_and_dedupe(env):
    f = env / "plan.txt"
    f.write_text(PLAN)
    run("op", "new", "move", str(f))
    led = ledger(env)
    assert led["operations"]["move"]["workstreams"] == ["Reports", "Cleanup"]
    texts = [(a["workstream"], a["text"]) for a in led["asks"]]
    assert texts == [("Reports", "Ann Example wants a weekly sales report"),
                     ("Reports", "Email the report to ann@example.invalid"),
                     ("Cleanup", "Archive the old folders")]
    assert "| A1 |" in (env / "ops" / "ledger.md").read_text()


def test_op_new_llm_split_and_fallback(env):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            reply = {"workstreams": [{"name": "Sales", "asks": ["Weekly report for Ann Example"]}]}
            body = json.dumps({"choices": [{"message": {"content": "Here: " + json.dumps(reply)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    f = env / "plan.txt"
    f.write_text(PLAN)
    run("op", "new", "viallm", str(f), "--llm", f"http://127.0.0.1:{srv.server_port}")
    run("op", "new", "fallback", str(f), "--llm", "http://127.0.0.1:1")  # unreachable: rules used
    srv.shutdown()
    led = ledger(env)
    assert [a["text"] for a in led["asks"] if a["op"] == "viallm"] == ["Weekly report for Ann Example"]
    assert len([a for a in led["asks"] if a["op"] == "fallback"]) == 3


def test_ask_add_merge_supersede_keep_dated_rows(env, monkeypatch):
    f = env / "plan.txt"
    f.write_text("- Send Ann Example the weekly report\n- Archive old folders\n")
    run("op", "new", "move", str(f))
    assert opsledger.main(["ask", "add", "move", "Reports", "send ann example the weekly report!"]) == 1  # duplicate
    monkeypatch.setenv("OPSLEDGER_NOW", "2026-01-09T09:00:00Z")
    run("ask", "add", "move", "Reports", "Send Ann Example the weekly report by Friday", "--force")
    run("ask", "merge", "A1", "A3")
    run("ask", "supersede", "A2", "Archive", "folders", "older", "than", "a", "year")
    led = {a["id"]: a for a in ledger(env)["asks"]}
    assert led["A1"]["text"] == "Send Ann Example the weekly report by Friday"  # newest wording wins
    assert led["A3"]["merged_into"] == "A1" and led["A3"]["merged_on"] == "2026-01-09"
    assert led["A3"]["text"] == "Send Ann Example the weekly report by Friday"
    assert led["A2"]["superseded_by"] == "A4" and led["A2"]["superseded_on"] == "2026-01-09"
    assert led["A2"]["text"] == "Archive old folders"  # old row kept as written
    assert any(h["text"] == "Send Ann Example the weekly report" for h in led["A1"]["history"])
    md = (env / "ops" / "ledger.md").read_text()
    assert "superseded by A4 | 2026-01-09" in md and "merged into A1 | 2026-01-09" in md


def test_status_and_score_in_hand_vs_overall(env):
    f = env / "plan.txt"
    f.write_text("A\n- one\n- two\n- three\n- four\nB\n- five\n- six\n")
    run("op", "new", "move", str(f))
    assert opsledger.main(["status", "A1", "DONE"]) == 1  # DONE needs evidence
    run("status", "A1", "DONE", "--evidence", "commit abc123")
    run("status", "A2", "built", "--evidence", "tool.py")
    run("status", "A3", "PARTIAL")
    run("status", "A5", "BLOCKED", "--evidence", "waiting on Ann Example")
    run("score")
    # Workstream A: 1 + .6 + .4 + 0 = 2.0 of 4 -> 50.0 both ways.
    # Workstream B: 0 of 1 in hand (one blocked) -> 0.0; 0 of 2 overall.
    # Total: 2.0 of 5 in hand = 40.0; 2.0 of 6 overall = 33.3.
    res = opsledger.score(opsledger.load(str(env / "ops")))["move"]
    assert res["workstreams"]["A"]["in_hand"] == 50.0 and res["workstreams"]["A"]["overall"] == 50.0
    assert res["workstreams"]["B"]["in_hand"] == 0.0 and res["total"]["overall"] == 33.3
    assert res["total"]["in_hand"] == 40.0
    report = (env / "ops" / "report.md").read_text()
    assert "In hand: **40.0%**. Overall: **33.3%**" in report


def test_help_runs(capsys):
    with pytest.raises(SystemExit) as e:
        opsledger.main(["--help"])
    assert e.value.code == 0
    assert "intake" in capsys.readouterr().out
