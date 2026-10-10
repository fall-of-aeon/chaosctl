import pytest

from chaosctl.probes import classify_fate, fate_table


@pytest.mark.parametrize("wf,ok,rows,fate", [
    ("W1", True, 1, "completed_once"),
    ("W2", True, 1, "completed_once"),
    ("W2", True, 0, "lost_silently"),
    ("W1", True, 2, "duplicated"),
    ("W1", False, 3, "duplicated"),
    ("W1", False, 1, "phantom_success"),
    ("W1", False, 0, "lost_visible"),
    ("W1", True, 0, "success_without_row"),
])
def test_classify(wf, ok, rows, fate):
    assert classify_fate(wf, ok, rows) == fate


def test_fate_table_summary():
    reqs = {
        "a": {"workflow": "W1", "status": 200, "finished_ts": 100},
        "b": {"workflow": "W2", "status": 200, "finished_ts": 101},
        "c": {"workflow": "W1", "status": None, "client_error": "timeout", "finished_ts": 160},
    }
    t = fate_table(reqs, {"a": 1, "b": 0, "c": 1}, t_inject=99)
    assert t["summary"]["completed_once"] == 1
    assert t["summary"]["lost_silently"] == 1
    assert t["summary"]["phantom_success"] == 1
    assert [r["client"] for r in t["rows"]] == ["success", "success", "timeout"]
