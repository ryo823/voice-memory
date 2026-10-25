import json
import subprocess
from types import SimpleNamespace

import pytest

from voice_memory import database as db, suggest
from test_database import _days_ago, save


def stream(ids, *, send="ok", result="RESULT: sent"):
    ev = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__voice-memory__get_pending_action_memories", "input": {}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": json.dumps({"results": [{"id": i} for i in ids]})}]}]}},
    ]
    if send:
        ev += [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t2", "name": "mcp__claude_ai_Gmail__send_message", "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "x", "is_error": send == "error"}]}},
        ]
    ev.append({"type": "result", "is_error": False, "result": result})
    return "\n".join(json.dumps(e) for e in ev)


def runner(out, rc=0):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")
    run.calls = calls
    return run


def test_sent_marks_only_fetched_ids(db_path):
    a = save(db_path, recorded_at=_days_ago(1))
    b = save(db_path, recorded_at=_days_ago(1))
    assert suggest.run(runner=runner(stream([a["id"]])), db_path=db_path) == 1
    assert [m["id"] for m in db.pending_memories(db_path=db_path, exclude_suggested=True)] == [b["id"]]


def test_no_new_memos_skips_claude(db_path):
    r = runner(stream([]))
    assert suggest.run(runner=r, db_path=db_path) == 0
    assert r.calls == []


@pytest.mark.parametrize("send,result", [("error", "RESULT: sent"), (None, "RESULT: sent"), (None, "done")])
def test_unconfirmed_mail_marks_nothing(db_path, send, result):
    a = save(db_path, recorded_at=_days_ago(1))
    with pytest.raises(suggest.SuggestError):
        suggest.run(runner=runner(stream([a["id"]], send=send, result=result)), db_path=db_path)
    assert len(db.pending_memories(db_path=db_path, exclude_suggested=True)) == 1


def test_all_excluded_marks_without_mail(db_path):
    a = save(db_path, recorded_at=_days_ago(1))
    assert suggest.run(runner=runner(stream([a["id"]], send=None, result="x\nRESULT: none")), db_path=db_path) == 1


def test_dry_run_and_nonzero_exit_mark_nothing(db_path, capsys):
    a = save(db_path, recorded_at=_days_ago(1))
    suggest.run(dry_run=True, runner=runner(stream([a["id"]], send=None, result="body")), db_path=db_path)
    assert capsys.readouterr().out.strip() == "body"
    with pytest.raises(suggest.SuggestError):
        suggest.run(runner=runner("", rc=1), db_path=db_path)
    assert len(db.pending_memories(db_path=db_path, exclude_suggested=True)) == 1
