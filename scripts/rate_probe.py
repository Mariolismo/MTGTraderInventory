#!/usr/bin/env python3
"""Phase 0: measure safe sustained RPS for CardTrader marketplace products.

Issues requests on a timed schedule with concurrent in-flight HTTP calls so
latency does not cap the measured issue rate (serial GETs were ~1.5 RPS).

Usage:
  set CARDTRADER_JWT=...
  python scripts/rate_probe.py
  python scripts/rate_probe.py --write-docs
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Allow running without editable install: add src/ to path.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cardtrader_inventory.config import API_BASE_URL, ConfigError, load_api_token

# Stepped ramp rates (request *starts* per second) for short windows.
RAMP_RATES: tuple[float, ...] = (1.0, 2.0, 5.0, 8.0, 10.0, 12.0)
RAMP_WINDOW_SECONDS = 8.0
SUSTAINED_WINDOW_SECONDS = 45.0
WARMUP_CALLS = 3
# Margin below highest non-429 *issue* rate for production limiter.
SAFE_RPS_MARGIN = 0.75
TARGET_LISTINGS = 10_000
LAMBDA_HEADROOM_SECONDS = 12 * 60  # leave headroom under 15-minute max
# Enough workers to keep up with schedule when latency is high (~0.5–2s).
MAX_WORKERS = 32


@dataclass
class RequestResult:
    status: int
    body: str
    elapsed_ms: float
    ok: bool

    @property
    def body_preview(self) -> str:
        return self.body[:500]


@dataclass
class RateWindowResult:
    target_rps: float
    duration_s: float
    attempts: int
    successes: int
    status_counts: dict[str, int] = field(default_factory=dict)
    first_429_body: str | None = None
    issued_rps: float = 0.0
    completed_rps: float = 0.0
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    max_in_flight: int = 0
    hit_429: bool = False

    @property
    def achieved_rps(self) -> float:
        """Back-compat alias: completion throughput."""
        return self.completed_rps


@dataclass
class ProbeReport:
    probe_date_utc: str
    base_url: str
    expansion_id: int
    expansion_name: str
    blueprint_id: int
    warmup_ok: bool
    ramp_results: list[RateWindowResult]
    first_429_at_rps: float | None
    first_429_body: str | None
    sustained: RateWindowResult | None
    highest_non_429_rps: float | None
    production_safe_rps: float | None
    implied_10k_seconds: float | None
    recommended_chunk_size: int | None
    notes: list[str] = field(default_factory=list)


def _request(
    token: str,
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> RequestResult:
    url = f"{API_BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "cardtrader-inventory-rate-probe/0.1",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            elapsed_ms = (time.perf_counter() - started) * 1000
            return RequestResult(
                status=resp.status,
                body=raw,
                elapsed_ms=elapsed_ms,
                ok=200 <= resp.status < 300,
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        elapsed_ms = (time.perf_counter() - started) * 1000
        return RequestResult(
            status=exc.code,
            body=raw,
            elapsed_ms=elapsed_ms,
            ok=False,
        )
    except urllib.error.URLError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return RequestResult(
            status=0,
            body=str(exc.reason),
            elapsed_ms=elapsed_ms,
            ok=False,
        )


def resolve_probe_target(token: str) -> tuple[int, str, int]:
    """Return (expansion_id, expansion_name, blueprint_id) for marketplace GETs.

    Phase 1 prices by blueprint; probing with blueprint_id matches that path and
    keeps payloads small enough that concurrency can reach the rate limit.
    """
    result = _request(token, "GET", "/expansions", timeout=60.0)
    if not result.ok:
        raise RuntimeError(
            f"Failed to list expansions: HTTP {result.status} {result.body_preview}"
        )
    try:
        expansions = json.loads(result.body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from /expansions: {exc}") from exc

    if not isinstance(expansions, list):
        raise RuntimeError("Unexpected /expansions response shape")

    mtg: list[dict] = []
    for item in expansions:
        if not isinstance(item, dict) or "id" not in item:
            continue
        game = item.get("game") or item.get("game_id") or item.get("game_name") or ""
        name = str(item.get("name") or "")
        blob = f"{game} {name}".lower()
        if "magic" in blob or "mtg" in blob or game == 1 or game == "1":
            mtg.append(item)

    candidates = mtg or [e for e in expansions if isinstance(e, dict) and "id" in e]
    if not candidates:
        raise RuntimeError("No expansions returned from API")

    chosen = candidates[0]
    expansion_id = int(chosen["id"])
    expansion_name = str(chosen.get("name") or f"id={expansion_id}")

    market = _request(
        token,
        "GET",
        "/marketplace/products",
        query={"expansion_id": str(expansion_id)},
        timeout=60.0,
    )
    if not market.ok:
        raise RuntimeError(
            f"Failed to sample marketplace for expansion {expansion_id}: "
            f"HTTP {market.status} {market.body_preview}"
        )
    try:
        payload = json.loads(market.body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from marketplace sample: {exc}") from exc

    if not isinstance(payload, dict) or not payload:
        raise RuntimeError("Marketplace sample returned no blueprint keys")

    # Keys are blueprint ids (string); pick the first stable one.
    blueprint_id = int(next(iter(payload.keys())))
    return expansion_id, expansion_name, blueprint_id


def marketplace_call(token: str, blueprint_id: int) -> RequestResult:
    return _request(
        token,
        "GET",
        "/marketplace/products",
        query={"blueprint_id": str(blueprint_id)},
        timeout=30.0,
    )


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    # Nearest-rank style.
    k = min(len(sorted_values) - 1, max(0, int(round((pct / 100.0) * (len(sorted_values) - 1)))))
    return sorted_values[k]


def run_rate_window(
    token: str,
    blueprint_id: int,
    target_rps: float,
    duration_s: float,
) -> RateWindowResult:
    """Issue marketplace GETs at target_rps using concurrent in-flight requests.

    Scheduling is based on request *start* times so high latency cannot
    collapse the measured issue rate the way a serial loop does.
    """
    interval = 1.0 / target_rps if target_rps > 0 else 1.0
    status_counts: dict[str, int] = {}
    successes = 0
    attempts = 0
    first_429_body: str | None = None
    hit_429 = False
    latencies_ms: list[float] = []
    max_in_flight = 0

    window_start = time.perf_counter()
    next_at = window_start
    stop_issuing = False
    pending: set[Future[RequestResult]] = set()

    workers = min(MAX_WORKERS, max(4, int(target_rps * 4) + 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            now = time.perf_counter()
            elapsed = now - window_start

            # Collect finished work.
            done = {f for f in pending if f.done()}
            for fut in done:
                pending.discard(fut)
                result = fut.result()
                key = str(result.status)
                status_counts[key] = status_counts.get(key, 0) + 1
                latencies_ms.append(result.elapsed_ms)
                if result.ok:
                    successes += 1
                if result.status == 429:
                    hit_429 = True
                    if first_429_body is None:
                        first_429_body = result.body_preview
                    stop_issuing = True

            max_in_flight = max(max_in_flight, len(pending))

            if elapsed >= duration_s or stop_issuing:
                break

            if now >= next_at:
                attempts += 1
                pending.add(pool.submit(marketplace_call, token, blueprint_id))
                next_at += interval
                # Do not reschedule from "now" — that silently drops intended RPS
                # when the dispatch loop is briefly busy. Catch up with multiple
                # submits if we fell behind slightly.
                while next_at <= time.perf_counter() and (
                    time.perf_counter() - window_start
                ) < duration_s and not stop_issuing:
                    attempts += 1
                    pending.add(pool.submit(marketplace_call, token, blueprint_id))
                    next_at += interval
                continue

            # Sleep until next schedule tick or a short poll for completions.
            sleep_for = min(0.01, max(0.0, next_at - time.perf_counter()))
            if sleep_for > 0:
                time.sleep(sleep_for)

        # Drain in-flight after stop (needed for accurate counts / 429 body).
        if pending:
            wait(pending)
            for fut in pending:
                result = fut.result()
                key = str(result.status)
                status_counts[key] = status_counts.get(key, 0) + 1
                latencies_ms.append(result.elapsed_ms)
                if result.ok:
                    successes += 1
                if result.status == 429:
                    hit_429 = True
                    if first_429_body is None:
                        first_429_body = result.body_preview

    wall = max(time.perf_counter() - window_start, 1e-9)
    latencies_ms.sort()
    return RateWindowResult(
        target_rps=target_rps,
        duration_s=wall,
        attempts=attempts,
        successes=successes,
        status_counts=status_counts,
        first_429_body=first_429_body,
        issued_rps=attempts / wall,
        completed_rps=successes / wall,
        latency_p50_ms=_percentile(latencies_ms, 50),
        latency_p95_ms=_percentile(latencies_ms, 95),
        max_in_flight=max_in_flight,
        hit_429=hit_429,
    )


def compute_chunk_guidance(safe_rps: float) -> tuple[float, int]:
    """Return (seconds for 10k lookups, recommended generate-chunk size)."""
    implied_10k = TARGET_LISTINGS / safe_rps if safe_rps > 0 else float("inf")
    effective_rps = safe_rps * 0.8
    max_calls = int(effective_rps * LAMBDA_HEADROOM_SECONDS)
    chunk = max(50, min(max_calls, 2000))
    return implied_10k, chunk


def run_probe(token: str) -> ProbeReport:
    notes: list[str] = []
    expansion_id, expansion_name, blueprint_id = resolve_probe_target(token)
    notes.append(
        f"Using expansion_id={expansion_id} ({expansion_name}), "
        f"blueprint_id={blueprint_id} (concurrent issue-rate probe)"
    )

    warmup_ok = True
    warmup_latencies: list[float] = []
    for i in range(WARMUP_CALLS):
        result = marketplace_call(token, blueprint_id)
        if not result.ok:
            warmup_ok = False
            raise RuntimeError(
                f"Warm-up call {i + 1}/{WARMUP_CALLS} failed: "
                f"HTTP {result.status} {result.body_preview}"
            )
        warmup_latencies.append(result.elapsed_ms)
        time.sleep(1.0)
    notes.append(
        f"Warm-up latency p50={statistics.median(warmup_latencies):.0f}ms "
        f"(serial throughput would be ~{1000 / statistics.median(warmup_latencies):.2f} RPS)"
    )

    ramp_results: list[RateWindowResult] = []
    first_429_at_rps: float | None = None
    first_429_body: str | None = None
    highest_non_429: float | None = None

    for rate in RAMP_RATES:
        print(f"Ramp: targeting {rate:g} issue-RPS for ~{RAMP_WINDOW_SECONDS:g}s ...")
        window = run_rate_window(token, blueprint_id, rate, RAMP_WINDOW_SECONDS)
        ramp_results.append(window)
        p50 = f"{window.latency_p50_ms:.0f}ms" if window.latency_p50_ms else "n/a"
        print(
            f"  issued={window.attempts} ok={window.successes} "
            f"issued_rps={window.issued_rps:.2f} completed_rps={window.completed_rps:.2f} "
            f"p50={p50} max_in_flight={window.max_in_flight} "
            f"statuses={window.status_counts} hit_429={window.hit_429}"
        )
        if window.hit_429:
            first_429_at_rps = rate
            first_429_body = window.first_429_body
            notes.append(f"First 429 during ramp at {rate:g} issue-RPS")
            break
        # Require issued rate within 20% of target so we don't claim we tested
        # a rate we never actually reached.
        if window.issued_rps < rate * 0.8:
            notes.append(
                f"Could not sustain issue rate at {rate:g} RPS "
                f"(issued {window.issued_rps:.2f}); stopping ramp"
            )
            break
        highest_non_429 = rate
        time.sleep(2.0)

    sustained: RateWindowResult | None = None
    if highest_non_429 is not None:
        print(
            f"Sustained retest: {highest_non_429:g} issue-RPS "
            f"for ~{SUSTAINED_WINDOW_SECONDS:g}s ..."
        )
        time.sleep(3.0)
        sustained = run_rate_window(
            token, blueprint_id, highest_non_429, SUSTAINED_WINDOW_SECONDS
        )
        p50 = f"{sustained.latency_p50_ms:.0f}ms" if sustained.latency_p50_ms else "n/a"
        print(
            f"  issued={sustained.attempts} ok={sustained.successes} "
            f"issued_rps={sustained.issued_rps:.2f} completed_rps={sustained.completed_rps:.2f} "
            f"p50={p50} max_in_flight={sustained.max_in_flight} "
            f"statuses={sustained.status_counts} hit_429={sustained.hit_429}"
        )
        if sustained.hit_429:
            notes.append(
                f"Sustained window hit 429 at {highest_non_429:g} RPS; "
                "stepping down one ramp level for production safe RPS"
            )
            idx = list(RAMP_RATES).index(highest_non_429)
            if idx > 0:
                highest_non_429 = RAMP_RATES[idx - 1]
            else:
                highest_non_429 = max(0.5, highest_non_429 * 0.5)
            if first_429_at_rps is None:
                first_429_at_rps = sustained.target_rps
            if first_429_body is None:
                first_429_body = sustained.first_429_body
        elif sustained.issued_rps < highest_non_429 * 0.8:
            notes.append(
                f"Sustained issued_rps {sustained.issued_rps:.2f} below target "
                f"{highest_non_429:g}; using measured issued rate for safe RPS base"
            )
            highest_non_429 = round(sustained.issued_rps, 2)
    else:
        notes.append("All ramp rates hit 429 or failed to issue; floor 0.5 RPS")
        highest_non_429 = 0.5

    # Safe RPS is derived from issue-rate ceiling, NOT completion throughput
    # (completion is latency-bound and understates the API limit).
    production_safe = round(highest_non_429 * SAFE_RPS_MARGIN, 2)
    production_safe = max(0.5, production_safe)
    if first_429_at_rps is None:
        notes.append(
            f"No 429 observed up to {highest_non_429:g} issue-RPS; "
            f"safe RPS uses {SAFE_RPS_MARGIN}× that ceiling (limit may be higher)"
        )

    implied_10k, chunk = compute_chunk_guidance(production_safe)

    return ProbeReport(
        probe_date_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        base_url=API_BASE_URL,
        expansion_id=expansion_id,
        expansion_name=expansion_name,
        blueprint_id=blueprint_id,
        warmup_ok=warmup_ok,
        ramp_results=ramp_results,
        first_429_at_rps=first_429_at_rps,
        first_429_body=first_429_body,
        sustained=sustained,
        highest_non_429_rps=highest_non_429,
        production_safe_rps=production_safe,
        implied_10k_seconds=round(implied_10k, 1),
        recommended_chunk_size=chunk,
        notes=notes,
    )


def format_report_text(report: ProbeReport) -> str:
    lines = [
        "=== CardTrader marketplace rate probe ===",
        f"Date (UTC): {report.probe_date_utc}",
        f"Base URL:   {report.base_url}",
        f"Expansion:  {report.expansion_id} ({report.expansion_name})",
        f"Blueprint:  {report.blueprint_id}",
        f"Warm-up OK: {report.warmup_ok}",
        "",
        "Ramp results (concurrent issue-rate):",
    ]
    for w in report.ramp_results:
        p50 = f"{w.latency_p50_ms:.0f}ms" if w.latency_p50_ms is not None else "n/a"
        lines.append(
            f"  {w.target_rps:g} RPS: issued={w.attempts} ok={w.successes} "
            f"issued_rps={w.issued_rps:.2f} completed_rps={w.completed_rps:.2f} "
            f"p50={p50} in_flight≤{w.max_in_flight} "
            f"statuses={w.status_counts} hit_429={w.hit_429}"
        )
    lines.append("")
    lines.append(f"First 429 at RPS: {report.first_429_at_rps}")
    lines.append(f"First 429 body:   {report.first_429_body}")
    if report.sustained:
        s = report.sustained
        p50 = f"{s.latency_p50_ms:.0f}ms" if s.latency_p50_ms is not None else "n/a"
        lines.append(
            f"Sustained:        target={s.target_rps:g} issued_rps={s.issued_rps:.2f} "
            f"completed_rps={s.completed_rps:.2f} p50={p50} "
            f"hit_429={s.hit_429} statuses={s.status_counts}"
        )
    lines.append(f"Highest non-429:  {report.highest_non_429_rps} issue-RPS")
    lines.append(
        f"Production safe:  {report.production_safe_rps} RPS "
        f"(margin={SAFE_RPS_MARGIN} on issue-rate ceiling)"
    )
    lines.append(
        f"Implied 10k:      {report.implied_10k_seconds}s "
        f"(~{report.implied_10k_seconds / 60:.1f} min) at safe RPS"
    )
    lines.append(f"Recommend chunk:  {report.recommended_chunk_size} listings/generate")
    if report.notes:
        lines.append("")
        lines.append("Notes:")
        for n in report.notes:
            lines.append(f"  - {n}")
    return "\n".join(lines)


def write_docs(report: ProbeReport) -> Path:
    docs_path = _ROOT / "docs" / "rate-probe-results.md"
    first_429_body = report.first_429_body or "_none observed_"
    first_429_cell = first_429_body.replace("|", "\\|").replace("\n", " ")
    sustained_s = (
        f"{report.sustained.duration_s:.1f}" if report.sustained else "_n/a_"
    )
    row_parts: list[str] = []
    for w in report.ramp_results:
        p50 = f"{w.latency_p50_ms:.0f}" if w.latency_p50_ms is not None else "n/a"
        row_parts.append(
            f"| {w.target_rps:g} | {w.attempts} | {w.issued_rps:.2f} | {w.successes} | "
            f"{w.completed_rps:.2f} | {p50} | {w.max_in_flight} | "
            f"`{w.status_counts}` | {w.hit_429} |"
        )
    ramp_lines = "\n".join(row_parts)
    notes_md = "\n".join(f"- {n}" for n in report.notes) or "- _(none)_"
    implied_min = (
        report.implied_10k_seconds / 60 if report.implied_10k_seconds is not None else 0
    )

    content = f"""# Marketplace rate probe results

Status: **complete** (live run).

Re-run:

```powershell
python scripts/rate_probe.py --write-docs
```

## Summary

| Field | Value |
|-------|-------|
| Probe date (UTC) | {report.probe_date_utc} |
| Endpoint | `GET /api/v2/marketplace/products?blueprint_id=...` |
| Expansion used | `{report.expansion_id}` ({report.expansion_name}) |
| Blueprint used | `{report.blueprint_id}` |
| First 429 at (attempted issue-RPS) | {report.first_429_at_rps if report.first_429_at_rps is not None else "_none_"} |
| First 429 body | `{first_429_cell}` |
| Highest sustained non-429 issue-RPS | {report.highest_non_429_rps} |
| Sustained window (s) | {sustained_s} |
| **Production safe RPS** | **{report.production_safe_rps}** |
| Implied wall-clock for 10k blueprint lookups | {report.implied_10k_seconds}s (~{implied_min:.1f} min) |
| Recommended generate-chunk size | {report.recommended_chunk_size} |

## Ramp detail

| Target RPS | Issued | Issued RPS | OK | Completed RPS | p50 ms | Max in-flight | Status counts | Hit 429 |
|------------|--------|------------|----|---------------|--------|---------------|---------------|---------|
{ramp_lines}

## Method

1. Authenticate with Bearer token.
2. Resolve one MTG `expansion_id`, sample marketplace, pick a `blueprint_id`.
3. Warm-up calls ({WARMUP_CALLS}) on that blueprint.
4. Stepped ramp with **concurrent** request starts: {", ".join(str(int(r) if r == int(r) else r) for r in RAMP_RATES)} issue-RPS (~{RAMP_WINDOW_SECONDS:g}s each).
5. On first `429`, record payload and rate.
6. Sustained retest at highest non-429 issue rate (~{SUSTAINED_WINDOW_SECONDS:g}s).
7. Apply margin {SAFE_RPS_MARGIN} to the **issue-rate** ceiling (not latency-bound completion RPS).

## Notes

{notes_md}

Docs conflict on marketplace limits (prose “1/s” vs error “10/s”). This probe is the source of truth for Phase 1 rate limiting and chunk sizing.
"""
    docs_path.write_text(content, encoding="utf-8")

    raw_path = _ROOT / "docs" / "rate-probe-raw.json"
    raw_path.write_text(
        json.dumps(asdict(report), indent=2),
        encoding="utf-8",
    )
    return docs_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 0 CardTrader marketplace rate-limit probe"
    )
    parser.add_argument(
        "--write-docs",
        action="store_true",
        help="Overwrite docs/rate-probe-results.md with measured results",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        token = load_api_token()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        report = run_probe(token)
    except Exception as exc:  # noqa: BLE001 — CLI top-level
        print(f"error: probe failed: {exc}", file=sys.stderr)
        return 1

    print(format_report_text(report))
    if args.write_docs:
        path = write_docs(report)
        print(f"\nWrote {path}")
        print(f"Wrote {_ROOT / 'docs' / 'rate-probe-raw.json'} (gitignored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
