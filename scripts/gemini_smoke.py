"""Phase 0: one structured-output call against GEMINI_MODEL. Prints result, never the key."""
import os
import time

from pydantic import BaseModel

from chaosctl import llm


class Smoke(BaseModel):
    service: str
    depends_on: list[str]


t0 = time.monotonic()
out = llm.generate_json(
    "phase0_smoke",
    "n8n main depends on redis and postgres. Return JSON with service='n8n-main' and its dependencies.",
    Smoke,
    journal=print,
)
print("model:", os.environ.get("GEMINI_MODEL"), "| ok:", out.model_dump(), "| %.1fs" % (time.monotonic() - t0))
