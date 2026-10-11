"""Thin Mistral wrapper: structured JSON out, cached, bounded, never hangs.

Callers use generate_json(purpose, prompt, Model) and must catch LLMUnavailable,
report it with alerts.llm_fallback() (never silently) and then fall back to their
deterministic path. Pass meta={} to learn whether the answer came from the API or
the local cache. Nothing here makes safety decisions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable, TypeVar

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

T = TypeVar("T", bound=BaseModel)

STATE_DIR = Path(os.environ.get("CHAOSCTL_STATE_DIR", "state"))
TIMEOUT_S = 30
MAX_RETRIES = 3

_SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|encryption[_-]?key)\s*[=:]\s*\S+"),
]


class LLMUnavailable(Exception):
    """Raised when no model produced a valid answer; callers fall back."""


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(0).split("=")[0].split(":")[0] + "=<redacted>", text)
    return text


class _Budget:
    def __init__(self) -> None:
        self.limit = int(os.environ.get("CHAOSCTL_LLM_BUDGET", "30"))
        self.used = 0

    def take(self) -> None:
        if self.used >= self.limit:
            raise LLMUnavailable(f"call budget of {self.limit} exhausted")
        self.used += 1

    def allow(self, n: int) -> None:
        """Long-lived processes (the dashboard's Agent 2 watcher): grant n more calls for one bounded task."""
        self.limit = max(self.limit, self.used + n)


budget = _Budget()


def _cache() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DIR / "llm_cache.sqlite")
    conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, model TEXT, output TEXT, ts REAL)")
    return conn


def _key(model: str, prompt: str, schema: dict, seed: int = 7, temperature: float = 0.1) -> str:
    extra = {**({"seed": seed} if seed != 7 else {}), **({"t": temperature} if temperature != 0.1 else {})}
    blob = json.dumps({"m": model, "p": prompt, "s": schema, **extra}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"


class MistralHTTPError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"HTTP {status_code}: {body[:200]}")
        self.status_code = status_code


def _strict(node):
    """Mistral strict json_schema mode wants additionalProperties: false on every object."""
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for v in node.values():
            _strict(v)
    elif isinstance(node, list):
        for v in node:
            _strict(v)
    return node


def _call_mistral(model: str, prompt: str, schema: dict, timeout_s: float = TIMEOUT_S, seed: int = 7,
                  temperature: float = 0.1) -> str:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "random_seed": seed,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema.get("title", "Output"), "schema": _strict(json.loads(json.dumps(schema))),
                            "strict": True},
        },
    }
    resp = httpx.post(MISTRAL_URL, json=body, timeout=timeout_s,
                      headers={"Authorization": f"Bearer {os.environ['MISTRAL_API_KEY']}"})
    if resp.status_code != 200:
        raise MistralHTTPError(resp.status_code, resp.text)
    return resp.json()["choices"][0]["message"]["content"] or ""


def _retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError)) or "timeout" in type(exc).__name__.lower()


def generate_json(
    purpose: str,
    prompt: str,
    model_cls: type[T],
    journal: Callable[[dict], None] | None = None,
    call: Callable[[str, str, dict], str] | None = None,
    timeout_s: float = TIMEOUT_S,
    seed: int = 7,
    temperature: float = 0.1,
    meta: dict | None = None,
) -> T:
    """Return a validated model_cls instance or raise LLMUnavailable (its message says why). A different seed
    (with a temperature above ~0.3) is an independent sample. meta, if given, is filled with
    {"source": "api"|"cache", "model": ...}."""
    real = call is None
    call = call or (lambda m, p, s: _call_mistral(m, p, s, timeout_s, seed, temperature))
    prompt = redact(prompt)
    schema = model_cls.model_json_schema()
    models = [m for m in (os.environ.get("MISTRAL_MODEL"), os.environ.get("MISTRAL_FALLBACK_MODEL")) if m]
    log = journal or (lambda _e: None)
    meta = meta if meta is not None else {}
    errors: list[str] = []

    if not models:
        log({"event": "llm", "purpose": purpose, "result": "no_model"})
        raise LLMUnavailable("MISTRAL_MODEL not set")
    if not os.environ.get("MISTRAL_API_KEY") and real:
        log({"event": "llm", "purpose": purpose, "result": "no_api_key"})
        raise LLMUnavailable("MISTRAL_API_KEY not set")

    conn = _cache()
    try:
        for model in models:
            key = _key(model, prompt, schema, seed, temperature)
            row = conn.execute("SELECT output FROM cache WHERE key=?", (key,)).fetchone()
            if row:
                log({"event": "llm", "purpose": purpose, "model": model, "prompt_hash": key[:16], "source": "cache"})
                meta.update(source="cache", model=model)
                return model_cls.model_validate_json(row[0])

            for attempt in range(MAX_RETRIES):
                t0 = time.monotonic()
                try:
                    budget.take()
                    text = call(model, prompt, schema)
                    obj = model_cls.model_validate_json(text)
                except LLMUnavailable:
                    raise
                except ValidationError as exc:
                    log({"event": "llm", "purpose": purpose, "model": model, "result": "invalid_json", "err": str(exc)[:200]})
                    errors.append(f"{model}: invalid JSON ({str(exc).splitlines()[0][:80]})")
                    break  # same model will likely repeat; go to fallback model
                except Exception as exc:  # network, quota, server
                    log({"event": "llm", "purpose": purpose, "model": model, "result": "error",
                         "err": f"{type(exc).__name__}: {str(exc)[:200]}"})
                    if _retryable(exc) and attempt < MAX_RETRIES - 1:
                        time.sleep(2 ** attempt)
                        continue
                    errors.append(f"{model}: {type(exc).__name__}: {str(exc)[:120]}")
                    break
                conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)", (key, model, obj.model_dump_json(), time.time()))
                conn.commit()
                log({"event": "llm", "purpose": purpose, "model": model, "prompt_hash": key[:16], "source": "api",
                     "latency_s": round(time.monotonic() - t0, 2)})
                meta.update(source="api", model=model)
                return obj
    finally:
        conn.close()
    raise LLMUnavailable(f"no valid response for {purpose}: " + ("; ".join(errors) or "unknown error"))
