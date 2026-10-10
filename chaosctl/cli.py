"""chaosctl command line (BUILD_PLAN section 13)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()
ROOT = Path(__file__).parents[1]


def _script(name: str, *args: str) -> None:
    raise typer.Exit(subprocess.call([str(ROOT / "scripts" / name), *args]))


@app.command("cluster-up")
def cluster_up():
    """Kind + kube-prometheus-stack + Chaos Mesh + annotated namespace."""
    _script("cluster-up.sh")


@app.command("cluster-down")
def cluster_down():
    _script("cluster-down.sh")


@app.command()
def up(profile: str = typer.Option("baseline", help="baseline|hardened")):
    """Deploy postgres, redis, mock-api, n8n."""
    _script("up.sh", profile)


@app.command()
def down():
    _script("down.sh")


@app.command()
def bootstrap():
    """Import + publish workflows and check health."""
    _script("bootstrap.sh")


@app.command()
def graph(fmt: str = typer.Option("table", "--format", help="table|mermaid|json"),
          offline: bool = typer.Option(False, help="use recorded fixture manifests instead of the cluster")):
    """Build graph.json / graph.mmd and print SPOFs, blast radius, criticality."""
    from chaosctl.graph import Graph, live_manifests
    from chaosctl.kube import NAMESPACE, api_client

    if offline:
        import yaml
        docs = []
        for f in sorted((ROOT / "tests" / "fixtures" / "manifests").glob("*.yaml")):
            docs += [d for d in yaml.safe_load_all(f.read_text()) if d]
    else:
        api_client()  # context check
        from chaosctl.kube import EXPECTED_CONTEXT
        docs = live_manifests(NAMESPACE, EXPECTED_CONTEXT)
    g = Graph.build(docs)
    g.write_json(ROOT / "graph.json")
    (ROOT / "graph.mmd").write_text(g.mermaid())

    if fmt == "json":
        console.print_json(json.dumps(g.to_dict(), sort_keys=True))
    elif fmt == "mermaid":
        print(g.mermaid())
    else:
        t = Table(title="Dependency graph (ranked by criticality)")
        for col in ("node", "kind", "replicas", "SPOF", "criticality", "blast radius", "workflows affected", "gaps"):
            t.add_column(col)
        for n in g.ranked():
            a = g.nodes[n]
            if a["kind"] == "obs":
                continue
            br = g.blast_radius(n)
            t.add_row(f"node:{n}", a["kind"], str(a["replicas"]), "YES" if a["spof"] else "",
                      f"{a['criticality']:.2f}",
                      ", ".join(br["nodes"]) + (" +client" if br["client_impact"] else ""),
                      ", ".join(f"{k}:{v}" for k, v in g.workflows_affected(n).items()),
                      ", ".join(a["redundancy_gap"]))
        console.print(t)
        console.print("Edges (A depends on B): " + ", ".join(e["id"] for e in g.edges))
        console.print(f"wrote {ROOT / 'graph.json'} and {ROOT / 'graph.mmd'}")


@app.command()
def kill():
    """Kill switch: stop the runner (via ./KILL), delete all managed chaos, restore workloads."""
    from chaosctl.runner import kill_switch

    (ROOT / "KILL").touch()
    result = kill_switch(reason="cli")
    console.print(result)


@app.command()
def recover(ack: bool = typer.Option(False, help="clear LOCKED state after manual verification")):
    """Clean up chaos resources (also after a crashed runner); --ack clears LOCKED."""
    from chaosctl.runner import kill_switch
    from chaosctl import safety

    result = kill_switch(reason="recover")
    console.print(result)
    if ack and safety.LOCK_FILE.exists():
        safety.LOCK_FILE.unlink()
        console.print("LOCKED cleared")
    (ROOT / "KILL").unlink(missing_ok=True)


def _live_graph():
    from chaosctl.graph import Graph, live_manifests
    from chaosctl.kube import EXPECTED_CONTEXT, NAMESPACE, api_client

    api_client()
    return Graph.build(live_manifests(NAMESPACE, EXPECTED_CONTEXT))


@app.command()
def baseline(duration: int = typer.Option(120, help="seconds of steady load"),
             rate: float = typer.Option(None, help="req/s (default: steady profile)")):
    """Run steady load and capture baseline SLIs to state/baseline.json."""
    from chaosctl.runner import capture_baseline

    console.print(f"steady load for {duration}s ...")
    console.print_json(json.dumps(capture_baseline(duration, rate)))


@app.command()
def plan(dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run"),
         llm: bool = typer.Option(True, "--llm/--no-llm", help="use Gemini (falls back automatically)"),
         offline: bool = typer.Option(False, help="graph from fixtures instead of the cluster")):
    """Injection agent: ranked plan with graph-cited reasons and guard verdicts. Never executes."""
    import yaml

    from chaosctl.agents.campaign import load_history
    from chaosctl.agents.injection import build_injection_agent
    from chaosctl.chaos.chaosmesh import load_catalog
    from chaosctl.journal import Journal
    from chaosctl.runner import load_policy

    if offline:
        from chaosctl.graph import Graph
        docs = []
        for f in sorted((ROOT / "tests" / "fixtures" / "manifests").glob("*.yaml")):
            docs += [d for d in yaml.safe_load_all(f.read_text()) if d]
        g = Graph.build(docs)
    else:
        g = _live_graph()
    j = Journal(ROOT / "reports" / "plans" / "journal.jsonl")
    out = build_injection_agent(j).invoke({"graph": g, "catalog": load_catalog(), "policy": load_policy(),
                                           "history": load_history(), "use_llm": llm})
    console.print(f"[bold]Plan source:[/bold] {out['source']}")
    for n in out.get("notes", []):
        console.print(f"[yellow]guard:[/yellow] {n}")
    verdicts = {v.exp_id: v for v in out["verdicts"]}
    for e in out["plan"].experiments:
        v = verdicts[e.id]
        console.rule(f"{e.id}  {e.fault} -> {e.target} ({e.rung})")
        console.print(f"[bold]reason:[/bold] {e.reason}")
        console.print(f"[bold]hypothesis [H]:[/bold] {e.hypothesis}")
        console.print(f"[bold]start when:[/bold] {e.start_when}   cooldown {e.cooldown_s}s   cites {e.cites}")
        console.print(f"[bold]guard:[/bold] {v.verdict} - {v.rationale}"
                      + (f" (runs after {v.order_after})" if v.order_after else ""))
        console.print(f"  blast radius {v.blast_radius}; workflows {v.workflows_affected}")
        console.print(f"  abort {v.abort}")


@app.command()
def run(exp: list[str] = typer.Option(None, help="E1 (worker kill) / E2 (redis kill); repeatable"),
        auto: bool = typer.Option(False, help="run the whole planned campaign"),
        approve: bool = typer.Option(False, help="ask before each injection"),
        llm: bool = typer.Option(True, "--llm/--no-llm"),
        queue_abort: int = typer.Option(None, help="override queue_waiting abort threshold (stricter only)")):
    """Run experiments through the campaign graph under the safety controller."""
    from chaosctl.agents.campaign import build_campaign
    from chaosctl.runner import Runner, load_policy

    names = {"E1": "worker_pod_kill", "E2": "redis_pod_kill"}
    if not exp and not auto:
        raise typer.BadParameter("give --exp E1/E2 or --auto")
    only = None if auto else [names.get(e, e) for e in exp]
    policy = load_policy()
    if queue_abort is not None:
        policy["defaults"]["abort"]["queue_waiting"] = min(queue_abort, policy["defaults"]["abort"]["queue_waiting"])

    runner = Runner()
    runner.start_safety()
    approve_fn = None
    if approve:
        approve_fn = lambda item, v: typer.confirm(f"Inject {item.fault} on {item.target}? ({v.rationale})")  # noqa: E731
    camp = build_campaign(runner, _live_graph, policy, runner.journal, approve_fn=approve_fn)
    console.print(f"run id {runner.run_id}; journal {runner.dir / 'journal.jsonl'}")
    try:
        state = camp.invoke({"only": only, "use_llm": llm}, {"recursion_limit": 100})
    finally:
        runner.loadgen.stop()
        runner.ctl.stop()
    (runner.dir / "results.json").write_text(json.dumps(state.get("results", []), indent=2, default=str))
    for r in state.get("results", []):
        console.rule(f"{r['exp_id']} {r['fault']} -> {r['target']}: {r['status']}")
        for k in ("skip_reason", "abort_reason", "recovery_time_s", "manual_action_needed", "restarts_delta"):
            if r.get(k) is not None:
                console.print(f"  {k}: {r[k]}")
        if r.get("timeline"):
            console.print(f"  timeline (s after inject): {r['timeline']}")
        if r.get("fates"):
            console.print(f"  fates: {r['fates']['summary']}")
    if state.get("stop_reason"):
        console.print(f"[red]campaign stopped: {state['stop_reason']}[/red]")


@app.command("ks-hold", hidden=True)
def ks_hold(run_id: str = typer.Option(...), hold_s: int = 60):
    """Child process for killswitch-test: inject reversible pod_failure on the worker and wait under the controller."""
    import signal
    import time

    from chaosctl.chaos.chaosmesh import ChaosMesh
    from chaosctl.journal import Journal
    from chaosctl.safety import SafetyController

    j = Journal(ROOT / "reports" / run_id / "journal.jsonl")
    cm = ChaosMesh(journal=j)
    from chaosctl import safety
    safety.save_active_run({"run_id": run_id, "fault": "pod_failure", "target": "n8n-worker"})
    ctl = SafetyController(lambda: {}, lambda reason: cm.delete_all(run_id, wait_s=10), j, kill_file=ROOT / "KILL")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, f: ctl.kill(f"signal:{signal.Signals(s).name}"))
    cm.create("pod_failure", "n8n-worker", "d60", run_id, allow_test_only=True)
    print("INJECTED", flush=True)
    ctl.start()
    t0 = time.time()
    while time.time() - t0 < hold_s and not ctl.aborted:
        time.sleep(0.2)
    time.sleep(1)
    ctl.stop()
    print("ABORTED" if ctl.aborted else "TIMEOUT", ctl.abort_reason, flush=True)


@app.command("killswitch-test")
def killswitch_test(via: list[str] = typer.Option(["cli", "file", "signal"]), repeat: int = 3,
                    trigger_after_s: float = 10):
    """Kill-switch acceptance: pod_failure (60 s) on the worker, trigger at t=10 s, expect immediate restore."""
    import os
    import signal
    import time

    from chaosctl.chaos.chaosmesh import ChaosMesh
    from chaosctl.probes import K8sProbe

    k8s, cm = K8sProbe(), ChaosMesh()
    results = []
    for mode in via:
        for i in range(repeat):
            (ROOT / "KILL").unlink(missing_ok=True)
            run_id = f"ks-{mode}-{i + 1}-{int(time.time()) % 100000}"
            child = subprocess.Popen(["uv", "run", "chaosctl", "ks-hold", "--run-id", run_id],
                                     cwd=ROOT, stdout=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == "INJECTED", "child failed to inject"
            t_inj = time.time()
            # wait until the fault is visible (worker unready), max trigger_after_s
            while time.time() - t_inj < trigger_after_s:
                time.sleep(0.5)
            unready_seen = not k8s.pods()["n8n-worker"]["ready"]
            t_trig = time.time()
            if mode == "cli":
                subprocess.run(["uv", "run", "chaosctl", "kill"], cwd=ROOT, capture_output=True)
            elif mode == "file":
                (ROOT / "KILL").touch()
            else:
                child.send_signal(signal.SIGINT)
            # restored = no managed CR left and worker Ready
            t_gone = t_ready = None
            while time.time() - t_trig < 120:
                if t_gone is None and not cm.list_managed(run_id):
                    t_gone = time.time()
                if t_gone and k8s.pods()["n8n-worker"]["ready"]:
                    t_ready = time.time()
                    break
                time.sleep(0.5)
            child.wait(timeout=30)
            (ROOT / "KILL").unlink(missing_ok=True)
            ok = bool(t_gone and t_ready and t_ready - t_inj < 60)
            r = {"via": mode, "try": i + 1, "fault_visible_before_trigger": unready_seen,
                 "cr_deleted_s": round(t_gone - t_trig, 1) if t_gone else None,
                 "worker_ready_s": round(t_ready - t_trig, 1) if t_ready else None, "pass": ok}
            results.append(r)
            console.print(r)
            time.sleep(5)
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "killswitch-test.json").write_text(json.dumps(results, indent=2))
    console.print(f"[bold]{sum(r['pass'] for r in results)}/{len(results)} passed[/bold]")


@app.command()
def dashboard(port: int = 8787):
    """Live impact dashboard (open http://127.0.0.1:8787 next to the terminal)."""
    from chaosctl.dashboard import serve

    serve(port)


if __name__ == "__main__":
    app()
