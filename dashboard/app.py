"""Dwarpal dashboard: what the proxy has been doing, from its request log and the latest eval.

    make dashboard      # http://localhost:8501, reading the proxy at http://localhost:8000

Reads GET /v1/dwarpal/stats and /v1/dwarpal/requests, which need the proxy's ADMIN_TOKEN (taken
from .env, as the proxy does, or typed into the sidebar), and eval/reports/latest.json. (PR-07)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # Streamlit puts dashboard/ on the path, not the repo

import altair as alt  # noqa: E402
import httpx  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from dwarpal.config import Settings  # noqa: E402
from dwarpal.telemetry import parse_window  # noqa: E402

EVAL_REPORT = ROOT / "eval" / "reports" / "latest.json"
WINDOWS = ["15m", "1h", "6h", "24h", "7d"]
# Series 1 and 2 of the dataviz reference palette, stepped for each theme (both validated).
PALETTE = {"light": ("#2a78d6", "#eb6834"), "dark": ("#3987e5", "#d95926")}


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def ms(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f} ms"


def usd(x: float | None) -> str:
    return "—" if x is None else f"${x:.6f}"


def fetch(base_url: str, token: str, path: str, **params: Any) -> dict[str, Any]:
    resp = httpx.get(
        base_url.rstrip("/") + path,
        params=params,
        headers={"X-Admin-Token": token},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def live_section(stats: dict[str, Any], rows: list[dict[str, Any]], colors: tuple[str, str]):
    added = stats["latency_ms"]["added"]
    cost = stats["cost_usd"]
    tiles = st.columns(5)
    tiles[0].metric("Requests", stats["requests"])
    tiles[1].metric("Block rate", pct(stats["block_rate"]), help="Requests refused by a guard")
    tiles[2].metric(
        "Added latency p50", ms(added["p50"]), help="Total time minus the upstream call"
    )
    tiles[3].metric("Added latency p99", ms(added["p99"]))
    tiles[4].metric("Cost per request", usd(cost["mean_per_request"]), help="List price")
    with st.expander(f"Raw response: GET /v1/dwarpal/stats?window={stats['window']}"):
        st.json(stats)
    if not stats["requests"]:
        st.info("No requests in this window yet. Send a few through the proxy, e.g. from /demo.")
        return

    st.subheader("Latency")
    latency = pd.DataFrame(stats["latency_ms"]).T  # rows: added, total, upstream
    st.dataframe(latency.rename_axis("ms").reset_index(), hide_index=True)

    # The policy name labels a bar; the version is added only when two versions run side by side.
    names = [g["guard"] for g in stats["guards"].values()]
    guards = pd.DataFrame(
        [
            {
                "guard": g["guard"] if names.count(g["guard"]) == 1 else ref,
                "version": g["version"],
                "mode": "shadow" if g["shadow"] else "enforce",
                "runs": g["runs"],
                "blocks": g["blocks"],
                "block rate %": round(g["block_rate"] * 100, 1),
                "redacts": g["redacts"],
                "errors": g["errors"],
                "p50 ms": g["latency_ms"]["p50"],
                "p99 ms": g["latency_ms"]["p99"],
            }
            for ref, g in stats["guards"].items()
        ]
    )
    left, right = st.columns(2)
    with left:
        st.subheader("Latency per guard")
        st.bar_chart(
            guards,
            x="guard",
            y=["p50 ms", "p99 ms"],
            color=list(colors),
            horizontal=True,
            stack=False,
            x_label="",  # with horizontal bars, x is the category axis
            y_label="ms",
        )
    with right:
        st.subheader("Block rate per guard")
        st.bar_chart(
            guards,
            x="guard",
            y="block rate %",
            color=colors[0],
            horizontal=True,
            x_label="",
            y_label="% of the requests it checked",
        )
    st.dataframe(guards, hide_index=True)

    st.subheader("Added latency over time")
    timeline = pd.DataFrame(
        {
            "time": [datetime.fromtimestamp(r["ts"]) for r in rows],
            "added latency ms": [r["added_ms"] for r in rows],
            "request": [r["request_id"] for r in rows],
        }
    )
    # One dot per request: traffic comes in bursts, and a line would bridge the gaps.
    st.altair_chart(
        alt.Chart(timeline)
        .mark_circle(size=50, color=colors[0], opacity=0.8)
        .encode(
            x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M:%S")),
            y=alt.Y("added latency ms:Q"),
            tooltip=[
                alt.Tooltip("time:T", format="%H:%M:%S"),
                "added latency ms:Q",
                "request:N",
            ],
        ),
    )

    st.subheader("Cost per request")
    cost_tiles = st.columns(4)
    cost_tiles[0].metric("Mean, all requests", usd(cost["mean_per_request"]))
    cost_tiles[1].metric("Upstream model", usd(cost["mean_upstream"]))
    cost_tiles[2].metric("Guard LLM calls", usd(cost["mean_guards"]), help="Judge + schema repair")
    cost_tiles[3].metric(f"Total, last {stats['window']}", usd(cost["total"]))
    st.caption(
        "List price from config/pricing.yaml, also for free-tier calls. Guard LLM calls price "
        "themselves (price_per_1m_tokens in their policy files)."
    )

    policies, shadow = st.columns(2)
    with policies:
        st.subheader("Active policy versions")
        st.dataframe(pd.DataFrame(stats["active_policies"]), hide_index=True)
    with shadow:
        st.subheader("Shadow vs enforce")
        if stats["shadow"]:
            st.dataframe(pd.DataFrame(stats["shadow"]), hide_index=True)
        else:
            st.caption("No shadow-mode policy ran in this window.")

    st.subheader("Requests")
    labels = {
        r["request_id"]: f"{datetime.fromtimestamp(r['ts']):%H:%M:%S} · {r['request_id']} · "
        f"{'blocked by ' + r['blocked_by'] if r['blocked_by'] else 'allowed'} · "
        f"+{r['added_ms']:.1f} ms"
        for r in reversed(rows)
    }
    picked = st.selectbox("Request", list(labels), format_func=labels.get)
    row = next(r for r in rows if r["request_id"] == picked)
    st.caption(
        f"upstream {ms(row['upstream_ms'])} · total {ms(row['total_ms'])} · "
        f"{row['prompt_tokens']} in / {row['completion_tokens']} out tokens · "
        f"{usd(row['cost_usd'])} · prompt sha256 {row['prompt_sha256'][:12]}…"
    )
    st.dataframe(
        pd.DataFrame(row["guards"]).rename(columns={"latency_ms": "latency ms"}), hide_index=True
    )


def eval_section():
    st.header("Latest eval")
    if not EVAL_REPORT.is_file():
        st.info("No eval report yet; run `make eval`.")
        return
    report = json.loads(EVAL_REPORT.read_text(encoding="utf-8"))
    gate = report.get("gate", {})
    st.caption(
        f"eval/reports/latest.json · {report['generated_at']} · gate "
        f"{'passed' if gate.get('ok') else 'FAILED'}"
    )
    for name, suite in report["suites"].items():
        e2e = suite["end_to_end"]
        st.subheader(f"{name} set ({suite['cases']} cases)")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "policy": p["policy"],
                        "version": p["version"],
                        "attacks": p["attacks"],
                        "catch rate": pct(p["catch_rate"]),
                        "95% CI": "—"
                        if p["catch_ci"] is None
                        else f"{p['catch_ci'][0]:.2f}–{p['catch_ci'][1]:.2f}",
                        "safe": p["safe"],
                        "FPR": pct(p["fpr"]),
                        "p50 ms": p["p50_ms"],
                    }
                    for p in suite["policies"].values()
                ]
            ),
            hide_index=True,
        )
        st.caption(
            f"Full pipeline: catch {pct(e2e['catch_rate'])} of {e2e['attacks']} attacks, "
            f"FPR {pct(e2e['fpr'])} on {e2e['safe']} safe cases."
        )


st.set_page_config(page_title="Dwarpal dashboard", layout="wide")
with st.sidebar:
    st.header("Proxy")
    base_url = st.text_input("URL", os.environ.get("DWARPAL_URL", "http://localhost:8000"))
    token = st.text_input(
        "Admin token",
        Settings().admin_token.get_secret_value(),
        type="password",
        help="The proxy's ADMIN_TOKEN. Read from .env by default.",
    )

st.title("Dwarpal")
controls = st.columns([1, 1, 4], vertical_alignment="bottom")
window = controls[0].selectbox("Window", WINDOWS, index=1)
controls[1].button("Refresh")  # any widget interaction reruns the script

try:
    stats = fetch(base_url, token, "/v1/dwarpal/stats", window=window)
    since = time.time() - parse_window(window)
    recent = fetch(base_url, token, "/v1/dwarpal/requests", limit=1000)["requests"]
    rows = sorted((r for r in recent if r["ts"] >= since), key=lambda r: r["ts"])
except httpx.HTTPStatusError as exc:
    code = exc.response.status_code
    if code == 403:
        st.error("The proxy's admin endpoints are off. Set ADMIN_TOKEN in .env and restart it.")
    elif code == 401:
        st.error("The admin token doesn't match the proxy's ADMIN_TOKEN.")
    else:
        st.error(f"The proxy answered {code}: {exc.response.text[:200]}")
except httpx.HTTPError:
    st.error(
        f"Can't reach the proxy at {base_url}. Start it with `make dev`, "
        "or `make mock` and `make dev-mock` to run without an API key."
    )
else:
    live_section(stats, rows, PALETTE["dark" if st.context.theme.type == "dark" else "light"])

eval_section()
