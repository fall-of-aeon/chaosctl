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
| Catalog CRs accepted by the API | all 26 rendered (fault, rung) CRs pass `kubectl create --dry-run=server` (schema + Chaos Mesh webhooks) | dry run | 2026-10-11 |
| HTTPChaos on Kind containerd | replace code 500 (got 500, 4.1 s), abort (`UND_ERR_SOCKET` after 0.1 s), delay +2 s (6.1 s vs 4.1 s) on mock-api :8080 `/work`; restored on delete | `scripts/chaos_smoke.py`, probe from a worker pod | 2026-10-11 |
| NetworkChaos delay | redis 100 ms: PING 21 ms -> 234 ms; postgres 100 ms: TCP connect 11 ms -> 119 ms; restored on delete | chaos_smoke | 2026-10-11 |
| NetworkChaos partition (worker <-> redis, `mode: one`) | exactly one worker times out to redis, the other is unaffected; records list the worker and the redis pod | chaos_smoke | 2026-10-11 |
| StressChaos on Kind (cgroup v2) | first injection on a pod works; recovery leaves the container's processes in a cgroup shown as `... .scope (deleted)` in `/proc/<pid>/cgroup`; every later Stress/TimeChaos on that pod fails (`attach process to cgroup ... Syntax error: "(" unexpected`). Fixed by restarting the pod. Faults disabled in the catalog | chaos_smoke + `/proc` on the Kind node | 2026-10-11 |
| TimeChaos on n8n (Node.js) | injects on a fresh pod; afterwards often `ptrace ... operation not permitted`; one worker exited 139 (SIGSEGV) when TimeChaos was removed right after a StressChaos on the same pod. Fault disabled | chaos_smoke | 2026-10-11 |
| n8n Bull lock settings | `QUEUE_WORKER_LOCK_DURATION=60000`, `QUEUE_WORKER_LOCK_RENEW_TIME=10000`, `QUEUE_WORKER_STALLED_INTERVAL=30000` | ConfigMap `n8n` | 2026-10-11 |
| W1/W2 HTTP node | `timeout: 30000`, no `retryOnFail` | workflows/*.json | 2026-10-11 |
| IOChaos on Kind | does not inject: chaos-daemon starts `toda` in the target's namespaces and kills it at once (`toda update RPC failed: context canceled`), every retry; CR stays AllInjected=False and blocks on its finalizer when deleted (pause + clear finalizers) | live, postgres-0 | 2026-10-11 |
| Postgres PV location | hostPath `/var/local-path-provisioner/pvc-..._data-postgres-0` on the Kind node = the laptop disk (~940 GB free): never fill it directly | `kubectl get pv`, `df` | 2026-10-11 |
| Small postgres disk | `scripts/pg_small_disk.sh`: 512 MiB ext4 (`-m 0`) loop file `/var/lib/chaosctl/pgdisk.img` mounted over the PV dir; data copied (3715 orders intact); n8n kept running through the ~30 s postgres restart | live | 2026-10-11 |
| Disk-full behaviour | with the disk full, an insert fails with `could not extend file ...: No space left on device`; postgres did not PANIC/restart; writes work again once the ballast is deleted | chaos_smoke `postgres_disk_full full` | 2026-10-11 |
| pgbench in postgres:17.6-alpine | present (`/usr/local/bin/pgbench`); `hold85` holds exactly 85 connections (max_connections=100, n8n uses ~10) | chaos_smoke | 2026-10-11 |
| NetworkChaos loss 30 % on redis | PING from a worker 15 ms -> ~1.5 s (TCP retransmits) | chaos_smoke | 2026-10-11 |
| container-kill | PodChaos `container-kill` restarts the container in place (restart count +1, same pod); mock-api: ECONNREFUSED during, 200 after | chaos_smoke | 2026-10-11 |
| Chaos Mesh record fields for verification | `status.conditions[type=AllInjected]`, `status.experiment.containerRecords[].{id,phase,injectedCount,events[].type/message}`; used by `ChaosMesh.verify_injected` | live | 2026-10-11 |
