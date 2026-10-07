"""Renders a harness run as Markdown.

The same text is written to eval/reports/latest.md, printed by `make eval`, and posted to the
GitHub job summary and the PR, so it has to read well in a terminal and on GitHub.
"""

from __future__ import annotations

from typing import Any

DASH = "—"

POLICY_HEADER = (
    "| Policy | Version | Attacks | Catch rate | 95% CI | Safe | FPR | p50 ms |\n"
    "|---|---|---:|---:|:---:|---:|---:|---:|"
)


def _rate(value: float | None) -> str:
    return DASH if value is None else f"{value:.3f}"


def _interval(bounds: list[float] | None) -> str:
    return DASH if not bounds else f"{bounds[0]:.2f}–{bounds[1]:.2f}"


def _ids(case_ids: list[str], limit: int = 10) -> str:
    shown = ", ".join(f"`{i}`" for i in case_ids[:limit])
    extra = len(case_ids) - limit
    return f"{shown} and {extra} more" if extra > 0 else shown


def _policy_table(policies: dict[str, Any]) -> list[str]:
    rows = [POLICY_HEADER]
    for name, s in policies.items():
        rows.append(
            f"| {name} | {s['version']} | {s['attacks']} | {_rate(s['catch_rate'])} "
            f"| {_interval(s['catch_ci'])} | {s['safe']} | {_rate(s['fpr'])} | {s['p50_ms']:.2f} |"
        )
    return rows


def _mistakes(policies: dict[str, Any]) -> list[str]:
    lines = []
    for name, s in policies.items():
        if s["missed"]:
            lines.append(f"- {name} missed {_ids(s['missed'])}")
        if s["false_positives"]:
            lines.append(f"- {name} blocked safe {_ids(s['false_positives'])}")
    return lines


def _end_to_end(e2e: dict[str, Any]) -> str:
    return (
        f"Full pipeline: catch {_rate(e2e['catch_rate'])} on {e2e['attacks']} attacks, "
        f"FPR {_rate(e2e['fpr'])} on {e2e['safe']} safe cases, "
        f"added latency p50 {e2e['added_latency_p50_ms']:.2f} ms / "
        f"p95 {e2e['added_latency_p95_ms']:.2f} ms."
    )


def _suite(title: str, note: str, suite: dict[str, Any]) -> list[str]:
    lines = [f"### {title}", "", f"{suite['cases']} case(s) scored. {note}", ""]
    if suite["skipped"]:
        detail = ", ".join(f"{g} {n}" for g, n in sorted(suite["skipped"].items()))
        total = sum(suite["skipped"].values())
        lines += [f"{total} case(s) skipped, guard not registered yet: {detail}.", ""]
    lines += _policy_table(suite["policies"])
    lines += ["", _end_to_end(suite["end_to_end"])]
    if mistakes := _mistakes(suite["policies"]):
        lines += ["", *mistakes]
    return lines + [""]


def _robustness(variants: dict[str, Any]) -> list[str]:
    lines = [
        "### Robustness",
        "",
        "Dev attacks re-run in disguise. Reported only, never gated.",
        "",
        "| Variant | Attacks | Catch rate |",
        "|---|---:|---:|",
    ]
    for name, v in variants.items():
        lines.append(f"| {name} | {v['attacks']} | {_rate(v['catch_rate'])} |")
    return lines + [""]


def _gate(gate: dict[str, Any] | None) -> list[str]:
    lines = ["### Gate", ""]
    if gate is None:
        return lines + ["Not evaluated."]
    if gate["ok"]:
        return lines + [
            "✅ No policy regressed against `eval/baseline.json` "
            "and all are within `eval/thresholds.yaml`."
        ]
    return lines + ["❌ Eval gate failed:", "", *(f"- {f}" for f in gate["failures"])]


def render_markdown(report: dict[str, Any]) -> str:
    policies = " ".join(f"`{ref}`" for ref in report["policies"]) or "_none_"
    lines = [
        "## Dwarpal eval",
        "",
        f"{policies} · {report['generated_at']} · {report['cache_hits']} cached decision(s) reused",
        "",
        *_suite("Dev set", "This suite gates the merge.", report["suites"]["dev"]),
        *_suite(
            "Holdout set",
            "Written before any guard existed; reported only, never gated.",
            report["suites"]["holdout"],
        ),
        *_robustness(report["robustness"]),
        *_gate(report.get("gate")),
    ]
    return "\n".join(lines).rstrip() + "\n"
