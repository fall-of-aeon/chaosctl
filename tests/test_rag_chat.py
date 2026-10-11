"""Ask Agent 2 chat: the selected run's records come first, docs are labelled historical, history is used."""
from chaosctl import rag

CHUNKS = [
    {"id": "doc:JUDGES_GUIDE.md#The injections", "title": f"{rag.HISTORICAL} The injections",
     "text": "Redis restart: 14 phantom successes, 10 lost, 60 s outage"},
    {"id": "run:r1011-052657/exp-01:result", "title": "redis_pod_kill on node:redis (aborted)",
     "text": "Run r1011-052657 exp-01: fault redis_pod_kill. Fates: completed_once 5, phantom_success 3"},
    {"id": "run:r1011-052657/exp-01:diagnosis", "title": "Agent 2 diagnosis r1011-052657",
     "text": "Root cause: Redis pod restart caused n8n-main and n8n-worker restarts"},
    {"id": "report:r1011-052657/exp-01", "title": "Findings report on r1011-052657/exp-01",
     "text": "Report analysis: Redis pod kill score 37.2"},
    {"id": "run:r1011-053323/exp-01:result", "title": "postgres_pod_kill on node:postgres (aborted)",
     "text": "Run r1011-053323 exp-01: fault postgres_pod_kill. Fates: lost_silently 1, success_without_row 1"},
    {"id": "playbook:redis_persistence", "title": "Enable Redis AOF persistence", "text": "Fix redis_persistence"},
]


def test_run_ref():
    assert rag.run_ref("r1011-052657") == ("r1011-052657", None)
    assert rag.run_ref("r1011-052657/exp-01") == ("r1011-052657", "exp-01")
    assert rag.run_ref("live") is None and rag.run_ref(None) is None and rag.run_ref("../etc") is None


def test_selected_run_records_come_first_even_if_the_question_does_not_match_them():
    hits = rag.select_context("what happened to in-flight orders?", CHUNKS, run="r1011-053323/exp-01")
    assert hits[0]["id"] == "run:r1011-053323/exp-01:result" and hits[0]["pinned"]
    assert not any(h.get("pinned") for h in hits[1:])


def test_pinned_order_result_report_diagnosis():
    hits = rag.select_context("redis", CHUNKS, run="r1011-052657")
    pinned = [h["id"] for h in hits if h.get("pinned")]
    assert pinned == ["run:r1011-052657/exp-01:result", "report:r1011-052657/exp-01", "run:r1011-052657/exp-01:diagnosis"]
    assert any(h["id"].startswith("playbook:") for h in hits)   # BM25 hits still follow


def test_no_run_is_plain_bm25():
    assert rag.select_context("redis restart phantom", CHUNKS)[0].get("pinned") is None


def test_prompt_marks_this_run_and_history(monkeypatch):
    seen = {}

    def fake(purpose, prompt, model_cls, meta=None, **kw):
        seen["prompt"] = prompt
        return model_cls(answer="ok", cited=["run:r1011-052657/exp-01:result"])

    monkeypatch.setattr(rag, "build_corpus", lambda: CHUNKS)
    monkeypatch.setattr(rag.llm, "generate_json", fake)
    out = rag.ask("and the fix?", [{"q": "what broke?", "a": "redis"}], run="r1011-052657/exp-01")
    p = seen["prompt"]
    assert "[run:r1011-052657/exp-01:result] [this run]" in p
    assert "looking at run r1011-052657/exp-01" in p and "historical docs" in p
    assert "Q: what broke?\nA: redis" in p
    assert out["sources"][0]["this_run"] is True and out["llm_used"] is True


def test_result_records_explain_fate_names():
    assert "phantom_success = client got an error but the row WAS saved" in rag.FATES
    assert all(c["text"].count("fate meanings") == 1 for c in rag.build_corpus() if c["id"].endswith(":result"))
