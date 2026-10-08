#!/usr/bin/env python3
"""make bench: Dwarpal's own overhead under load, per scenario and concurrency. (PR-07)

The upstream is the mock (dwarpal/testing/mock_upstream.py) fixed at 300 ms, so the numbers are
Dwarpal's overhead, not Gemini's latency or rate limits. The mock also stands in for the
faithfulness judge: the judge calls the same endpoint, gets a fixed SUPPORTED verdict after the
same 300 ms, and is priced as usual. Each scenario gets a fresh proxy with only its policies,
driven by loadtest/locustfile.py at each concurrency:

  no_guards              no policy: the proxy on its own
  cheap_only             the `cheap` tier (regexes and schema checks)
  all_but_faithfulness   every policy but the judge, on plain chat requests; faithfulness only
                         runs when a request sends context, so this is also what the full
                         policy set costs on such requests
  all                    every policy; each request sends the FAQ as context, so the judge
                         checks every reply and the injection guards also scan the FAQ

Requests/second and client-side latency come from Locust; added latency (total - upstream, as
the proxy measures it) from the proxy's own request log; CPU and peak memory of the proxy
process from psutil. The guard decision cache is off, so every request runs every guard. The
proxy, the mock and Locust share the machine. Cost is not measured here: the mock's token
counts are made up, so cost per request comes from loadtest/real_run.py against Gemini.

  uv run --all-extras --group loadtest python loadtest/bench.py      # what make bench runs
  uv run --all-extras --group loadtest python loadtest/bench.py --duration 10 --users 1 10
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import psutil  # noqa: E402

from dwarpal.policy import load_policies  # noqa: E402
from dwarpal.telemetry.store import RequestStore, percentile, summarize  # noqa: E402

MOCK_PORT, PROXY_PORT = 9100, 8100
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
VERDICT = json.dumps({"claims": [{"claim": "stand-in", "label": "SUPPORTED"}]})
WARMUP = {
    "model": "mock",
    "messages": [{"role": "user", "content": "How do I export last quarter's invoices?"}],
    "mock_response": "Use Reports > Export and pick PDF or CSV.",
}


def scenarios() -> dict[str, tuple[list[str], bool]]:
    """name -> (policies to enable, whether requests send the FAQ as context)."""
    policies = load_policies(ROOT / "policies")
    names = list(dict.fromkeys(p.name for p in policies))
    return {
        "no_guards": ([], False),
        "cheap_only": (list(dict.fromkeys(p.name for p in policies if p.tier == "cheap")), False),
        "all_but_faithfulness": ([n for n in names if n != "faithfulness"], False),
        "all": (names, True),
    }


def server_env(**extra: str) -> dict[str, str]:
    """The servers run in a temp dir (so no local .env applies) but import from this repo."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LANGFUSE_")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH")]))
    return {**env, **extra}


def proxy_env(db: Path, policies: list[str]) -> dict[str, str]:
    return server_env(
        **{
            "UPSTREAM_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}/v1/",
            "UPSTREAM_API_KEY": "mock",  # the judge's client won't call out without a key
            "OVERRIDE_MODEL": "true",
            "POLICY_DIR": str(ROOT / "policies"),
            "ENABLED_POLICIES": ",".join(policies),
            "PRICING_FILE": str(ROOT / "config" / "pricing.yaml"),
            "REQUEST_DB": str(db),
            "PIPELINE_STRATEGY": "tiered",
            "GUARD_CACHE_SIZE": "0",
            "GUARD_LLM_RPM": "0",
            "API_KEYS": "",
            "LOG_PROMPTS": "false",
            "DEMO_ENABLED": "false",
            "RATE_LIMIT_PER_MINUTE": "0",
            "DAILY_REQUEST_CAP": "0",
        }
    )


def uvicorn(app: str, port: int) -> list[str]:
    return [sys.executable, "-m", "uvicorn", app, "--port", str(port), "--no-access-log"]


def start(cmd: list[str], env: dict[str, str], cwd: Path, log: Path) -> subprocess.Popen:
    with log.open("w") as out:
        return subprocess.Popen(cmd, env=env, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()  # uvicorn shuts down cleanly, so the request log is flushed
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_until_up(url: str, proc: subprocess.Popen, log: Path, timeout_s: float = 300) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"{url} exited early:\n{log.read_text()[-2000:]}")
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"{url} not up after {timeout_s:.0f} s; see {log}")


class Sampler(threading.Thread):
    """CPU (% of one core, so 200 = two cores busy) and memory of one process."""

    def __init__(self, pid: int):
        super().__init__(daemon=True)
        self.process = psutil.Process(pid)
        self.process.cpu_percent(None)  # the first reading only sets the baseline
        self.cpu: list[float] = []
        self.rss: list[int] = []
        self.done = threading.Event()

    def run(self) -> None:
        while not self.done.wait(0.5):
            self.cpu.append(self.process.cpu_percent(None))
            self.rss.append(self.process.memory_info().rss)

    def result(self) -> dict[str, float]:
        self.done.set()
        self.join()
        return {
            "cpu_mean_pct": round(sum(self.cpu) / len(self.cpu), 1) if self.cpu else 0.0,
            "cpu_peak_pct": round(max(self.cpu, default=0.0), 1),
            "rss_peak_mb": round(max(self.rss, default=0) / 2**20, 1),
        }


def run_locust(users: int, duration_s: int, prefix: Path, context: bool) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "locust", "-f", str(ROOT / "loadtest" / "locustfile.py")]
    cmd += ["--headless", "--only-summary", "--loglevel", "WARNING", "--csv", str(prefix)]
    cmd += ["--users", str(users), "--spawn-rate", str(users), "--host", PROXY_URL]
    cmd += ["--run-time", f"{duration_s}s", "--stop-timeout", "10"]
    env = {**os.environ, "BENCH_CONTEXT": "1" if context else "0"}
    done = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    stats_csv = Path(f"{prefix}_stats.csv")
    if not stats_csv.is_file():
        raise SystemExit(f"locust did not run:\n{done.stderr[-2000:]}")
    with stats_csv.open(newline="") as f:
        total = next(r for r in csv.DictReader(f) if r["Name"] == "Aggregated")
    return {
        "requests": int(total["Request Count"]),
        "failures": int(total["Failure Count"]),
        "requests_per_s": round(float(total["Requests/s"]), 2),
        "client_latency_p50_ms": float(total["50%"]),
        "client_latency_p99_ms": float(total["99%"]),
    }


def measure(
    store: RequestStore, pid: int, users: int, duration_s: int, prefix: Path, context: bool
) -> dict[str, Any]:
    sampler = Sampler(pid)
    sampler.start()
    t0 = time.time()
    load = run_locust(users, duration_s, prefix, context)
    t1 = time.time()
    usage = sampler.result()
    time.sleep(1.5)  # the proxy writes its request log in the background
    rows = store.since(t0, t1)
    stats = summarize(rows)
    added = [r["added_ms"] for r in rows]
    return {
        "users": users,
        **load,
        "logged_requests": len(rows),
        "blocked": stats["blocked"],
        "added_latency_p50_ms": percentile(added, 0.5),
        "added_latency_p99_ms": percentile(added, 0.99),
        **usage,
        "guards": {
            ref: {**g["latency_ms"], "errors": g["errors"], "blocks": g["blocks"]}
            for ref, g in stats["guards"].items()
        },
    }


def machine() -> dict[str, Any]:
    cpu = platform.processor() or platform.machine()
    if sys.platform == "darwin":
        brand = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True
        )
        cpu = brand.stdout.strip() or cpu
    return {
        "platform": platform.platform(),
        "cpu": cpu,
        "cores_logical": os.cpu_count(),
        "cores_physical": psutil.cpu_count(logical=False),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "python": platform.python_version(),
    }


def main(args) -> dict[str, Any]:
    plan = {k: v for k, v in scenarios().items() if k in args.scenarios}
    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": machine(),
        "setup": {
            "mock_upstream_latency_ms": args.mock_latency_ms,
            "judge": "the mock upstream, fixed SUPPORTED verdict, same latency",
            "duration_s_per_run": args.duration,
            "traffic": "dev-set safe cases, round-robin (loadtest/locustfile.py)",
            "guard_decision_cache": "off",
            "pipeline_strategy": "tiered",
        },
        "scenarios": {},
    }
    with tempfile.TemporaryDirectory(prefix="dwarpal-bench-") as tmp:
        workdir = Path(tmp)  # also the servers' cwd, so no local .env leaks in
        mock_env = server_env(MOCK_LATENCY_MS=str(args.mock_latency_ms), MOCK_RESPONSE=VERDICT)
        mock = start(
            uvicorn("dwarpal.testing.mock_upstream:app", MOCK_PORT),
            mock_env,
            workdir,
            workdir / "mock.log",
        )
        try:
            wait_until_up(f"http://127.0.0.1:{MOCK_PORT}/openapi.json", mock, workdir / "mock.log")
            for name, (policies, context) in plan.items():
                print(f"{name}: {len(policies)} policies, context={context}", flush=True)
                db = workdir / f"{name}.db"
                log = workdir / f"{name}.log"
                proxy = start(
                    uvicorn("dwarpal.app:app", PROXY_PORT), proxy_env(db, policies), workdir, log
                )
                try:
                    wait_until_up(f"{PROXY_URL}/healthz", proxy, log)
                    store = RequestStore(db)
                    warmup = {**WARMUP, "dwarpal": {"context": ["warm-up"]}} if context else WARMUP
                    for _ in range(5):  # the first inference of each model is slow
                        httpx.post(f"{PROXY_URL}/v1/chat/completions", json=warmup, timeout=60)
                    runs = []
                    for users in args.users:
                        prefix = workdir / f"{name}_{users}"
                        run = measure(store, proxy.pid, users, args.duration, prefix, context)
                        print(
                            f"  {users:>3} users: {run['requests_per_s']:7.1f} req/s, added "
                            f"p50 {run['added_latency_p50_ms']} ms / p99 "
                            f"{run['added_latency_p99_ms']} ms, CPU {run['cpu_mean_pct']}%",
                            flush=True,
                        )
                        runs.append(run)
                finally:
                    stop(proxy)
                report["scenarios"][name] = {
                    "policies": policies,
                    "context": context,
                    "runs": runs,
                }
        finally:
            stop(mock)
    return report


def num(x: float | None, digits: int = 1) -> str:
    return "—" if x is None else f"{x:.{digits}f}"


def render(report: dict[str, Any]) -> str:
    m, setup = report["machine"], report["setup"]
    lines = [
        "# Benchmarks",
        "",
        f"`make bench` · {report['generated_at']} · {m['cpu']}, {m['cores_logical']} cores, "
        f"{m['ram_gb']} GB RAM, {m['platform']}, Python {m['python']}",
        "",
        f"Mock upstream fixed at {setup['mock_upstream_latency_ms']:.0f} ms (it also answers the "
        "faithfulness judge, in the same time). Each run is "
        f"{setup['duration_s_per_run']} s of Locust traffic: the dev set's safe cases, "
        "round-robin, each user sending its next request as soon as the last one returns. "
        "Guard decision cache off. *Added* latency is total minus upstream, from the proxy's "
        "request log; *client* latency includes the mock's 300 ms (twice with the judge). "
        "CPU is % of one core, for the proxy process only.",
        "",
        "| Scenario | Users | Req/s | Added p50 ms | Added p99 ms | Client p50 ms "
        "| Client p99 ms | CPU % mean | Peak RAM MB | Blocked | HTTP errors |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, scenario in report["scenarios"].items():
        for r in scenario["runs"]:
            lines.append(
                f"| {name} | {r['users']} | {r['requests_per_s']:.1f} "
                f"| {num(r['added_latency_p50_ms'])} | {num(r['added_latency_p99_ms'])} "
                f"| {r['client_latency_p50_ms']:.0f} | {r['client_latency_p99_ms']:.0f} "
                f"| {r['cpu_mean_pct']:.0f} | {r['rss_peak_mb']:.0f} "
                f"| {r['blocked']}/{r['logged_requests']} | {r['failures']} |"
            )
    lines += [
        "",
        "All traffic is safe, so *Blocked* counts guards that failed closed: a guard that "
        "overruns its policy's `timeout_ms` (or crashes) is decided by `on_error`, and the "
        "injection guards fail closed.",
    ]
    lines += ["", "Policies per scenario:", ""]
    for name, scenario in report["scenarios"].items():
        context = " (requests send the FAQ as context)" if scenario["context"] else ""
        lines.append(f"- `{name}`{context}: {', '.join(scenario['policies']) or 'none'}")
    for name, scenario in report["scenarios"].items():
        first = scenario["runs"][0] if scenario["runs"] else None
        if not first or not first["guards"]:
            continue
        lines += [
            "",
            f"Per-guard latency, `{name}`, {first['users']} user(s):",
            "",
            "| Guard | p50 ms | p99 ms |",
            "|---|---:|---:|",
        ]
        for ref, g in sorted(first["guards"].items(), key=lambda kv: -(kv[1]["p99"] or 0)):
            lines.append(f"| {ref} | {num(g['p50'])} | {num(g['p99'])} |")
        if any(ref.startswith("faithfulness@") for ref in first["guards"]):
            lines += ["", "`faithfulness` here is the mock standing in for the judge, not Gemini."]
    timeouts = [
        f"- `{name}`, {r['users']} users: "
        + ", ".join(f"{ref} {g['errors']}" for ref, g in r["guards"].items() if g["errors"])
        for name, scenario in report["scenarios"].items()
        for r in scenario["runs"]
        if any(g["errors"] for g in r["guards"].values())
    ]
    if timeouts:
        lines += ["", "Guard errors (timeouts), by run:", "", *timeouts]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--duration", type=int, default=30, help="seconds per run")
    parser.add_argument("--users", type=int, nargs="+", default=[1, 10, 50])
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=["no_guards", "cheap_only", "all_but_faithfulness", "all"],
    )
    parser.add_argument("--mock-latency-ms", type=float, default=300.0)
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "benchmarks.json")
    args = parser.parse_args()

    report = main(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown = render(report)
    args.out.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print("\n" + markdown)
    print(f"wrote {args.out} and {args.out.with_suffix('.md')}")
