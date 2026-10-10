import pytest
from pydantic import BaseModel

from chaosctl import llm


class Pick(BaseModel):
    choice: str
    reason: str


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "STATE_DIR", tmp_path)
    monkeypatch.setenv("MISTRAL_MODEL", "primary")
    monkeypatch.setenv("MISTRAL_FALLBACK_MODEL", "fallback")
    monkeypatch.setattr(llm, "budget", llm._Budget())
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)


def test_cache_hit_returns_identical_output():
    calls = []

    def fake(model, prompt, schema):
        calls.append(model)
        return '{"choice": "E1", "reason": "r%d"}' % len(calls)

    a = llm.generate_json("t", "p", Pick, call=fake)
    b = llm.generate_json("t", "p", Pick, call=fake)
    assert a == b and len(calls) == 1


def test_retry_then_fallback_model():
    seen = []

    class Quota(Exception):
        code = 429

    def fake(model, prompt, schema):
        seen.append(model)
        if model == "primary":
            raise Quota("rate limited")
        return '{"choice": "E2", "reason": "ok"}'

    out = llm.generate_json("t", "p", Pick, call=fake)
    assert out.choice == "E2"
    assert seen == ["primary"] * llm.MAX_RETRIES + ["fallback"]


def test_invalid_json_everywhere_raises_unavailable():
    with pytest.raises(llm.LLMUnavailable):
        llm.generate_json("t", "p", Pick, call=lambda *a: "not json")


def test_timeout_raises_unavailable():
    def fake(*a):
        raise TimeoutError()

    with pytest.raises(llm.LLMUnavailable):
        llm.generate_json("t", "p", Pick, call=fake)


def test_budget_exhaustion(monkeypatch):
    monkeypatch.setenv("CHAOSCTL_LLM_BUDGET", "0")
    monkeypatch.setattr(llm, "budget", llm._Budget())
    with pytest.raises(llm.LLMUnavailable):
        llm.generate_json("t", "p", Pick, call=lambda *a: '{"choice":"x","reason":"y"}')


def test_missing_key_raises_unavailable(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    with pytest.raises(llm.LLMUnavailable):
        llm.generate_json("t", "p", Pick)


def test_redact():
    out = llm.redact("N8N_ENCRYPTION_KEY=abc123 password: hunter2 ok")
    assert "abc123" not in out and "hunter2" not in out and "ok" in out


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code, self._payload, self.text = status_code, payload, str(payload)

    def json(self):
        return self._payload


def test_mistral_request_is_strict_json_schema(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    sent = {}

    def fake_post(url, json, timeout, headers):
        sent.update(url=url, body=json, headers=headers)
        return _Resp(200, {"choices": [{"message": {"content": '{"choice": "E1", "reason": "r"}'}}]})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    out = llm.generate_json("t", "p", Pick)
    assert out.choice == "E1"
    rf = sent["body"]["response_format"]
    assert sent["url"] == llm.MISTRAL_URL and sent["headers"]["Authorization"] == "Bearer k"
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["additionalProperties"] is False
    assert sent["body"]["model"] == "primary"


def test_mistral_429_retries_then_falls_back(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    seen = []

    def fake_post(url, json, timeout, headers):
        seen.append(json["model"])
        if json["model"] == "primary":
            return _Resp(429, {"message": "rate limited"})
        return _Resp(200, {"choices": [{"message": {"content": '{"choice": "E2", "reason": "ok"}'}}]})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.generate_json("t", "p", Pick).choice == "E2"
    assert seen == ["primary"] * llm.MAX_RETRIES + ["fallback"]
