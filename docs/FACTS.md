# Verified facts (filled in as [E]/[H] items are checked)

| Item | Value | How verified | Date |
|---|---|---|---|
| google-genai SDK surface | `client.interactions.create(**body)` exists (untyped body); `client.models.generate_content` with `GenerateContentConfig(temperature, seed, response_mime_type, response_json_schema)` exists | introspected google-genai 2.29.0 | 2026-10-10 |
| Gemini temperature/seed | exposed by SDK config fields; server-side honoring not yet tested | SDK introspection | 2026-10-10 |
| Helm major version | 4.3.0 | `helm version` | 2026-10-10 |
| Interactions vs generate_content live behavior | Both return valid structured JSON with gemini-3.8-flash. Interactions: 45.3 s for a one-field schema (exceeds 30 s timeout); generate_content: 6.5 s. **llm.py uses generate_content only.** | live calls | 2026-10-10 |
| Chaos Mesh version | chart/app 2.8.4; values keys `chaosDaemon.runtime`, `chaosDaemon.socketPath`, `controllerManager.enableFilterNamespace`, `controllerManager.leaderElection.enabled`, `controllerManager.replicaCount` (default 3, set 1) exist | `helm show values` | 2026-10-10 |
| Chaos Mesh on Kind containerd | `runtime=containerd`, `socketPath=/run/containerd/containerd.sock` works: pod-failure injected and auto-restored at `duration` | live test on dummy Deployment | 2026-10-10 |
| `PodChaos.spec.gracePeriod` | exists, integer | `kubectl explain` | 2026-10-10 |
| Namespace filtering | PodChaos in an un-annotated namespace: "Failed to select targets: no pod is selected" | live test | 2026-10-10 |
| pod-failure mechanics | replaces container image with `gcr.io/google-containers/pause:latest`; a pod **without a readiness probe stays Ready=true** during the fault | live test | 2026-10-10 |
| Kind image loading | `kind load docker-image` fails with Docker Desktop's containerd image store (attestation manifests: "content digest ... not found"), even for local builds. Workaround in `scripts/up.sh`: `docker save --platform linux/arm64 IMG \| docker exec -i chaos-control-plane ctr -n k8s.io images import --snapshotter=overlayfs -` | live | 2026-10-10 |
| Kind node image | kindest/node:v1.37.0 (kind v0.33.0) | cluster-up log | 2026-10-10 |
| kube-prometheus-stack | chart 92.3.0 (operator v0.94.1), Grafana 13.2.3 | helm / Grafana /api/health | 2026-10-10 |
| n8n chart | 1.15.0, appVersion 2.42.4 (pinned image tag 2.42.4). Queue mode default; PDB **enabled by default** (`pdb.enabled`, minAvailable 1); `redis.prefix` default "" | `helm show chart/values` | 2026-10-10 |
| n8n pod labels | `app.kubernetes.io/name=n8n, app.kubernetes.io/instance=n8n, app.kubernetes.io/component=main|worker` | `helm template` | 2026-10-10 |
| n8n extra env keys | `config.extraEnv` (all pods), `queueMode.workerExtraEnv` (workers), `webhook.extraEnv`; Redis/DB host env come from ConfigMap `n8n` via `configMapKeyRef` | `helm template` | 2026-10-10 |
| n8n worker probes | readiness and liveness are `exec: pgrep -f 'n8n worker'` (process only, not Redis connectivity) | chart values | 2026-10-10 |
| Chart still sets `QUEUE_WORKER_MAX_STALLED_COUNT` | yes (value 1), although docs say removed in n8n 2.0 | `helm template` | 2026-10-10 |
| Bull active-list key | PENDING (T1b) | | |
| Mistral structured output | `POST https://api.mistral.ai/v1/chat/completions` with `response_format: {type: json_schema, json_schema: {name, schema, strict: true}}`; strict mode needs `additionalProperties: false` on every object (as the official SDK does) | mistralai 3.2.0 SDK source + live calls | 2026-10-10 |
| Mistral model access (team key) | 0 req/min on mistral-small/medium-latest and magistral-*; ministral-14b-latest 30 req/min, ministral-8b-latest 188 req/min, ministral-3b-latest 750 req/min (`x-ratelimit-limit-req-minute` header) | live calls | 2026-10-10 |
