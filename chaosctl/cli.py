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


@app.callback()
def _main(require_llm: bool = typer.Option(False, "--require-llm",
                                           help="fail instead of falling back when the LLM cannot be used "
                                                "(same as CHAOSCTL_REQUIRE_LLM=1)")):
    """chaosctl: AI-driven chaos engineering for n8n. Any fallback or harness error is printed when it
    happens and again in a summary at exit."""
    import atexit
    import os

    from chaosctl import alerts
    if require_llm:
        os.environ["CHAOSCTL_REQUIRE_LLM"] = "1"
    atexit.register(alerts.print_summary)


def main() -> None:
    from chaosctl.alerts import LLMRequired
    try:
        app()
    except LLMRequired as exc:
        Console(stderr=True).print(f"[bold red]STOPPED: {exc}[/bold red]")
        raise SystemExit(2) from None


def _print_plan(state: dict) -> None:
    """Where the plan came from, said out loud (a fallback was already alerted; repeat it here)."""
    src = state.get("plan_source") or "?"
    color = "green" if src.startswith("llm:api") else "yellow" if src.startswith("llm:cache") else "bold red"
    label = {"green": "LLM (live call)", "yellow": "LLM (cached answer from an earlier identical prompt)"}.get(
        color, "DETERMINISTIC FALLBACK - the LLM did not choose this plan")
    console.print(f"[{color}]Agent 1 plan source: {label}[/{color}] [dim]({src})[/dim]")
    for e in (state.get("plan").experiments if state.get("plan") else []):
        console.print(f"  {e.id} {e.fault} -> {e.target} ({e.rung})")


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
         llm: bool = typer.Option(True, "--llm/--no-llm", help="use Mistral (falls back automatically)"),
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
    _print_plan({"plan_source": out["source"], "plan": None})
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
def run(exp: list[str] = typer.Option(None, help="fault alias (E1, E2, ... see `chaosctl kb`) or catalog fault name; repeatable"),
        auto: bool = typer.Option(False, help="run the whole planned campaign"),
        approve: bool = typer.Option(False, help="ask before each injection"),
        llm: bool = typer.Option(True, "--llm/--no-llm"),
        queue_abort: int = typer.Option(None, help="override queue_waiting abort threshold (stricter only)")):
    """Run experiments through the campaign graph under the safety controller."""
    from chaosctl.agents.campaign import build_campaign
    from chaosctl.runner import Runner, load_policy

    from chaosctl.fault_kb import load_kb

    if not exp and not auto:
        raise typer.BadParameter("give --exp E1/E2 (alias or catalog fault name) or --auto")
    kb = load_kb()
    only = None if auto else [kb.resolve(e) for e in exp]
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
    _print_plan(state)
    if not state.get("results"):
        console.print("[bold red]No experiment ran (empty plan). See the warnings above.[/bold red]")
        raise typer.Exit(1)
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


@app.command()
def demo(queue_abort: int = typer.Option(None, help="strict queue limit to show the automatic stop (e.g. 20)"),
         llm: bool = typer.Option(True, "--llm/--no-llm")):
    """Judge demo in one command: Agent 1 picks ONE experiment, it runs once under the safety controller,
    then Agent 2 diagnoses it blind. Watch http://127.0.0.1:8787 while it runs."""
    from chaosctl.agents.campaign import build_campaign
    from chaosctl.runner import Runner, load_policy

    policy = load_policy()
    if queue_abort is not None:
        policy["defaults"]["abort"]["queue_waiting"] = min(queue_abort, policy["defaults"]["abort"]["queue_waiting"])
    runner = Runner()
    runner.start_safety()
    console.rule("[bold]1-3 · Agent 1 reads the map and picks one experiment[/bold]")
    camp = build_campaign(runner, _live_graph, policy, runner.journal)
    try:
        state = camp.invoke({"only": None, "max_experiments": 1, "use_llm": llm}, {"recursion_limit": 50})
    finally:
        runner.loadgen.stop()
        runner.ctl.stop()
    (runner.dir / "results.json").write_text(json.dumps(state.get("results", []), indent=2, default=str))
    _print_plan(state)
    res = (state.get("results") or [None])[0]
    if not res:
        console.print("[red]Agent 1 produced no runnable experiment.[/red]")
        raise typer.Exit(1)
    console.rule(f"[bold]4-6 · result: {res['fault']} -> {res['target']}: {res['status']}[/bold]")
    for k in ("skip_reason", "abort_reason", "recovery_time_s", "manual_action_needed"):
        if res.get(k) is not None:
            console.print(f"  {k}: {res[k]}")
    if res.get("fates"):
        console.print(f"  in-flight orders: {res['fates']['summary']}")
    if res["status"] == "skipped":
        raise typer.Exit(0)
    console.rule("[bold]7 · Agent 2 diagnoses, blind[/bold]")
    diagnose(run=runner.run_id, exp=res["exp_id"], llm=llm, pace=0.8)


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
def dashboard(port: int = 8787,
              replay: str = typer.Option(None, help="serve a recorded run from reports/<run-id>/ instead of live data")):
    """Live impact dashboard (open http://127.0.0.1:8787 next to the terminal). Read-only."""
    from chaosctl.dash.server import serve

    serve(port, replay)


@app.command()
def diagnose(run: str = typer.Option(None, help="run id (default: newest)"), exp: str = "exp-01",
             llm: bool = typer.Option(True, "--llm/--no-llm"), pace: float = typer.Option(0.8, help="seconds per thought")):
    """Agent 2: blind diagnosis of an experiment's evidence, thinking out loud, with ranked fix recommendations."""
    import time

    from chaosctl.agents.diagnosis import PLAYBOOK, diagnose as run_diag, grade
    from chaosctl.journal import Journal

    rdir = ROOT / "reports" / run if run else max((p for p in (ROOT / "reports").glob("r*") if p.is_dir()),
                                                 key=lambda p: p.stat().st_mtime)
    bundle = json.loads((rdir / "evidence" / f"{exp}.json").read_text())
    console.rule(f"[bold]Agent 2 · blind diagnosis · {rdir.name}/{exp}[/bold]")
    console.print("[dim]Input: metrics trace, k8s events, logs, fate table. NOT told what was injected.[/dim]\n")
    out = run_diag(bundle, journal=Journal(rdir / "journal.jsonl"), use_llm=llm)
    (rdir / "evidence" / f"{exp}.diagnosis.json").write_text(json.dumps(out, indent=2))

    console.print("[bold]1. Deterministic detectors fired[/bold]")
    for h in out["detectors"]:
        console.print(f"   [{'red' if h['severity'] in ('Critical', 'High') else 'yellow'}]{h['severity']:<8}[/] "
                      f"{h['id']}: {h['detail']}")
        time.sleep(pace / 3)
    if out["llm_used"]:
        console.print(f"\n[bold]2. Reasoning[/bold] [green](LLM: {out['source']})[/green]")
    else:
        console.print(f"\n[bold]2. Reasoning[/bold] [bold red](NOT the LLM - pre-written template built from the "
                      f"detector hits; {out['source']})[/bold red]")
    colors = {"observe": "cyan", "hypothesize": "magenta", "check": "yellow", "conclude": "green"}
    for i, t in enumerate(out["thoughts"], 1):
        console.print(f"   [{colors.get(t['step'], 'white')}]{t['step']:>11}[/] │ {t['text']}")
        time.sleep(pace)
    console.print(f"\n[bold]3. Root cause:[/bold] {out['root_cause']}")
    console.print("[bold]   Failure chain:[/bold] " + "  →  ".join(out["failure_chain"]))
    console.print("\n[bold]4. Recommended fixes (from the vetted playbook; not applied)[/bold]")
    for i, f in enumerate(out["fixes"], 1):
        pb = PLAYBOOK[f["playbook_id"]]
        console.print(f"   {i}. [bold]{pb['title']}[/bold]  [dim](effort {pb['effort']}, impact {pb['impact']})[/dim]")
        console.print(f"      why: {f['why']}")
        console.print(f"      change: {pb['change']}")
        console.print(f"      verify: {pb['verify']}")
    plan_p = rdir / "evidence" / f"{exp}.plan.json"
    if plan_p.exists():
        g = grade(out, json.loads(plan_p.read_text()), bundle)
        shown = {"CORRECT": "[green]CORRECT[/green]", "MISSED": "[red]MISSED[/red]",
                 "NO_IMPACT": "[yellow]NOT GRADED: the harness saw no measurable impact, so there was no failure "
                              "to find[/yellow]"}[g["verdict"]]
        console.print(f"\n[bold]5. Grading (after the fact):[/bold] injected {g['fault']} on {g['actual_target']} → "
                      f"blind root cause {shown}"
                      + ("" if out["llm_used"] else " [bold red](graded the TEMPLATE diagnosis, not the LLM: does not "
                                                    "count as an Agent 2 result)[/bold red]"))
    _write_fix_summary(rdir, exp, out, llm)


def _write_fix_summary(rdir: Path, exp: str, diag: dict, use_llm: bool) -> dict:
    """Fix summary card (chaosctl/fix_summary.py): blind, playbook-only fixes, stored for the dashboard."""
    from chaosctl import fix_summary
    from chaosctl.journal import Journal

    out = fix_summary.build(diag, use_llm=use_llm, journal=Journal(rdir / "journal.jsonl"))
    path = fix_summary.write(rdir, exp, out)
    color = "green" if out["source"].startswith("llm") else "bold red"
    console.print(f"\n[bold]6. Fix summary[/bold] [{color}]({out['source']})[/{color}] -> {path.relative_to(ROOT)}")
    console.print(f"   what broke: {out['what_broke']}\n   why: {out['why']}")
    for i, f in enumerate(out["fixes"], 1):
        console.print(f"   {i}. {f['action']} [dim]({f['playbook_id']}; verify: {f['verify']})[/dim]")
    return out


@app.command("fix-summary")
def fix_summary_cmd(run: str = typer.Option(..., help="run id"), exp: str = "exp-01",
                    llm: bool = typer.Option(True, "--llm/--no-llm")):
    """Regenerate the fix summary of a diagnosed experiment (what broke, why, fix, verify, rollback)."""
    rdir = ROOT / "reports" / run
    diag_p = rdir / "evidence" / f"{exp}.diagnosis.json"
    if not diag_p.exists():
        raise typer.BadParameter(f"{diag_p} not found: run `chaosctl diagnose --run {run} --exp {exp}` first")
    _write_fix_summary(rdir, exp, json.loads(diag_p.read_text()), llm)


@app.command()
def report(run: list[str] = typer.Option(None, help="run id(s); default: newest run with evidence"),
           all_runs: bool = typer.Option(False, "--all", help="every run in reports/"),
           llm: bool = typer.Option(True, "--llm/--no-llm", help="AI-written (default) or deterministic draft")):
    """Findings report written by Agent 2: experiments, expected vs observed, weaknesses, fixes, resilience score."""
    from chaosctl import report as rep

    reports = ROOT / "reports"
    dirs = rep.run_dirs(reports) if all_runs else [reports / r for r in run] if run else rep.run_dirs(reports)[-1:]
    if not dirs:
        console.print("[red]no run with evidence in reports/ (run an experiment first)[/red]")
        raise typer.Exit(1)
    exps = [e for d in dirs for e in rep.load_run(d)]
    out = dirs[0] / "findings.md" if len(dirs) == 1 else reports / "findings.md"
    console.print(f"Agent 2 is writing the report for {len(exps)} experiment(s) ..." if llm else "deterministic draft ...")
    data = rep.write(exps, out, use_llm=llm)
    if data["source"] != "llm":
        console.print(f"[yellow]{data['source']}[/yellow]")
    if o := data["overall"]:
        console.print(f"[bold]Resilience score{' (Agent 2)' if data['source'] == 'llm' else ''}: "
                      f"{o['score']} / 100 ({o['verdict']})[/bold]  ranking, least resilient first:")
        for e in data["ranking"]["experiments"]:
            console.print(f"  {e['rank'] or '-'}. {e['exp_ref']} {e['alias'] or ''} {e['title']}: "
                          f"{'n/a' if e['score'] is None else e['score']} ({e['verdict'] or 'not scored'})")
    if data["source"] == "llm":
        for w in data["summary"]["weaknesses"][:5]:
            console.print(f"  {w['severity']:<8} {w['title']}")
        if data["unverified_numbers"]:
            console.print(f"[yellow]fact-check flagged numbers: {data['unverified_numbers']}[/yellow]")
    console.print(f"written: {out} and {out.with_suffix('.json')}")


@app.command()
def kb():
    """Fault knowledge base: what each catalog fault means, and consistency problems."""
    from chaosctl.agents.diagnosis import DETECTOR_IDS
    from chaosctl.fault_kb import load_kb

    k = load_kb()
    for f, entry in k.catalog["faults"].items():
        e = k.get(f)
        state = "enabled" if entry.get("enabled") else "test only" if entry.get("test_only") else "disabled"
        console.print(f"[bold]{e.alias or '--'}[/bold] {f:<22} tier {entry.get('tier')} {state:<9} "
                      f"{e.title}{'' if e.documented else '  [yellow](no knowledge file)[/yellow]'}")
    problems = k.check(DETECTOR_IDS)
    console.print("[green]knowledge base consistent[/green]" if not problems else "[yellow]problems:[/yellow]")
    for p in problems:
        console.print(f"  - {p}")


if __name__ == "__main__":
    main()
