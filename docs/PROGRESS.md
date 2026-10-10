# Progress log

## Gates
| # | Status |
|---|---|
| G1 n8n license | CLOSED: organizers confirmed n8n is fine. List Sustainable Use License in README + team form. |
| G2 K8s go/no-go | OPEN: clock starts at T1a. Team chose the full Kind plan despite 8 GB host RAM (see G3). |
| G3 Resources | Host: macOS 15.7.1, Apple Silicon (arm64), **8 GB RAM total**; Docker Desktop 28.1.1 with 8 CPUs and **3.8 GB memory** at start. Below the 8-12 GB the plan assumes. Decision (2026-10-10): proceed with full Kind plan; raise Docker Desktop memory; trim monitoring (no Alertmanager, short retention). Watch for OOM kills; Compose fallback remains the escape hatch. |
| G5 n8n version | OPEN (T1b). |
| G7 Hosted LLM OK? | OPEN: ask organizers whether Gemini (hosted) is acceptable vs "open-source model". |
| G8 Gemini quota | OPEN: needs API key; check RPM/RPD in AI Studio. |

## Phase 0 (2026-10-10)
- Tools: kubectl v1.32.2 (client), kind v0.33.0 and Helm v4.3.0 installed via Homebrew (plan said Helm >= 3.12; Helm 4 used), uv 0.12.9.
- Python: project venv via uv. Plan suggested 3.11+; all deps install on the host's Python 3.14.7, so 3.14 is used (`requires-python >=3.12`).
- Deps: langgraph 1.2.14, google-genai 2.29.0, pydantic 2.14.0, typer 0.27.3, kubernetes 37.0.1, httpx 0.28.1, redis 8.1.0, psycopg 3.3.6, pytest 9.1.1.
- `chaosctl/llm.py` written: cache, retries, fallback model, call budget, redaction. `tests/test_llm.py` 7/7 pass (offline, fake transport).
- Gemini live smoke test: PASSED with gemini-3.8-flash (structured JSON validated, 8.9 s first call). Run: `PYTHONPATH=. uv run python scripts/gemini_smoke.py`.

## T1a (2026-10-10) DONE
- `scripts/cluster-up.sh`: Kind `chaos` (k8s 1.37.0) with NodePorts on 127.0.0.1: n8n 15678, redis 16379, postgres 15432, mock-api 18080 (1xxxx host ports avoid clashing with a local Homebrew Postgres). kube-prometheus-stack 92.3.0 (no Alertmanager, 6 h retention, trimmed requests), Chaos Mesh 2.8.4 (containerd, FilterNamespace, 1 replica, no leader election, no DNS server). Namespace `n8n-chaos` annotated.
- Acceptance: all pods Running; Chaos dashboard HTTP 200; Grafana healthy; pod-failure on a dummy Deployment injected and auto-restored after 20 s; un-annotated namespace not selected. Memory after install: ~3.1 GiB of 5.8 GiB.
- Slow part: in-cluster image pulls from ghcr.io (~10 min).

## T2/T3/T4b/T5/T6 code (written ahead of the cluster, against fixtures)
- graph.py + overlay.yaml (T2), catalog.yaml + chaos/chaosmesh.py (T3), safety.py (T4b), loadgen.py/probes.py (T4a), agents/injection.py (T5/T6), runner.py, cli.py. 55 unit tests pass. Live acceptance pending on the n8n stack.
- Decision: the injection agent's guard re-checks the tier rule at run time (`runtime_guard`), so E2 is only approved after E1 actually passed.
