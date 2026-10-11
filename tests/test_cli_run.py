"""`chaosctl run --max-experiments` caps the plan (with --exp, Agent 1 may plan the same fault several times)."""
from typer.testing import CliRunner

import chaosctl.agents.campaign as campaign
import chaosctl.runner as runner_mod
from chaosctl import cli


class _FakeRunner:
    run_id = "rtest"

    def __init__(self, *a, **k):
        import pathlib
        import tempfile
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.journal = None
        self.loadgen = type("L", (), {"stop": lambda self: None})()
        self.ctl = type("C", (), {"stop": lambda self: None})()

    def start_safety(self):
        pass


def _invoke(monkeypatch, args):
    seen = {}

    class _Camp:
        def invoke(self, state, cfg):
            seen.update(state)
            return {"results": [{"exp_id": "exp-01", "fault": "worker_pod_kill", "target": "node:n8n-worker",
                                 "status": "completed"}]}

    monkeypatch.setattr(runner_mod, "Runner", _FakeRunner)
    monkeypatch.setattr(campaign, "build_campaign", lambda *a, **k: _Camp())
    monkeypatch.setattr(cli, "_print_plan", lambda state: None)
    res = CliRunner().invoke(cli.app, ["run", *args])
    return res, seen


def test_max_experiments_is_passed_to_the_campaign(monkeypatch):
    res, seen = _invoke(monkeypatch, ["--exp", "E1", "--max-experiments", "1"])
    assert res.exit_code == 0, res.output
    assert seen["max_experiments"] == 1
    assert seen["only"] == ["worker_pod_kill"]


def test_default_is_unlimited(monkeypatch):
    res, seen = _invoke(monkeypatch, ["--exp", "E1"])
    assert res.exit_code == 0, res.output
    assert seen["max_experiments"] is None
