#!/usr/bin/env python3
"""Validate tide_solver.py against NOAA's own hi/lo predictions.

For each station with both a harcon file and a validation corpus of NOAA
hi/lo events, compute predictions with the solver over the same window,
match each NOAA event to the nearest predicted event, and report error
distributions.

Datum offset (MSL vs MLLW) is station-specific and not available in
harcon.json, so we infer it per station as the mean difference between
NOAA heights and solver heights across the window and subtract before
comparing.
"""
import json
import statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from tide_solver import Station  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
VALIDATION_DIR = DATA_DIR / "validation"
HARCON_DIR = DATA_DIR / "harcon"
REPORT_JSON = DATA_DIR / "validation_report.json"
REPORT_MD = DATA_DIR / "validation_report.md"

MATCH_WINDOW_MINUTES = 120


def parse_noaa(rec):
    t = datetime.strptime(rec["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return {"t": t, "v": float(rec["v"]), "type": rec["type"]}


def match_events(noaa_events, solver_events, window_min):
    """Greedy nearest-time match; returns (matched_pairs, unmatched_noaa)."""
    used = [False] * len(solver_events)
    matches = []
    unmatched = []
    for n in noaa_events:
        best_j, best_dt = None, None
        for j, s in enumerate(solver_events):
            if used[j]:
                continue
            dt = abs((s["t"] - n["t"]).total_seconds())
            if dt > window_min * 60:
                continue
            if s["type"] != n["type"]:
                continue
            if best_dt is None or dt < best_dt:
                best_dt, best_j = dt, j
        if best_j is None:
            unmatched.append(n)
        else:
            used[best_j] = True
            matches.append((n, solver_events[best_j]))
    return matches, unmatched


def validate_station(station_id, validation_path):
    data = json.loads(validation_path.read_text())
    noaa = [parse_noaa(r) for r in data["predictions"]]
    if not noaa:
        return None

    t_start = noaa[0]["t"] - timedelta(hours=6)
    t_end = noaa[-1]["t"] + timedelta(hours=6)

    try:
        station = Station(station_id)
    except FileNotFoundError:
        return {"station_id": station_id, "error": "no harcon file"}
    if station.n_constituents == 0:
        return {"station_id": station_id, "error": "no usable constituents"}

    solver_raw = station.hilo(t_start, t_end, step_seconds=60)

    datum_offset = statistics.mean(n["v"] for n in noaa) - statistics.mean(
        s["v"] for s in solver_raw)
    solver = [{**s, "v": s["v"] + datum_offset} for s in solver_raw]

    matches, unmatched = match_events(noaa, solver, MATCH_WINDOW_MINUTES)

    if not matches:
        return {
            "station_id": station_id,
            "name": data.get("name"),
            "state": data.get("state"),
            "n_noaa": len(noaa),
            "n_matched": 0,
            "error": "no matches",
        }

    time_errs_min = [(s["t"] - n["t"]).total_seconds() / 60 for n, s in matches]
    height_errs_ft = [s["v"] - n["v"] for n, s in matches]

    return {
        "station_id": station_id,
        "name": data.get("name"),
        "state": data.get("state"),
        "n_constituents": station.n_constituents,
        "n_noaa": len(noaa),
        "n_matched": len(matches),
        "n_unmatched": len(unmatched),
        "match_pct": 100.0 * len(matches) / len(noaa),
        "datum_offset_ft": datum_offset,
        "time_err_median_min": statistics.median(
            abs(e) for e in time_errs_min),
        "time_err_mean_min": statistics.mean(abs(e) for e in time_errs_min),
        "time_err_max_min": max(abs(e) for e in time_errs_min),
        "height_err_median_ft": statistics.median(
            abs(e) for e in height_errs_ft),
        "height_err_mean_ft": statistics.mean(abs(e) for e in height_errs_ft),
        "height_err_max_ft": max(abs(e) for e in height_errs_ft),
    }


def aggregate(results):
    ok = [r for r in results if "error" not in r]
    if not ok:
        return {}
    return {
        "n_stations": len(ok),
        "n_failed": len(results) - len(ok),
        "total_events_matched": sum(r["n_matched"] for r in ok),
        "total_events_noaa": sum(r["n_noaa"] for r in ok),
        "overall_match_pct": (
            100.0 * sum(r["n_matched"] for r in ok)
            / max(1, sum(r["n_noaa"] for r in ok))
        ),
        "median_time_err_min": statistics.median(
            r["time_err_median_min"] for r in ok),
        "median_height_err_ft": statistics.median(
            r["height_err_median_ft"] for r in ok),
        "p95_time_err_min": _percentile(
            [r["time_err_median_min"] for r in ok], 95),
        "p95_height_err_ft": _percentile(
            [r["height_err_median_ft"] for r in ok], 95),
        "max_time_err_min": max(r["time_err_max_min"] for r in ok),
        "max_height_err_ft": max(r["height_err_max_ft"] for r in ok),
    }


def _percentile(values, pct):
    if not values:
        return None
    s = sorted(values)
    k = (pct / 100) * (len(s) - 1)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def write_report(results, agg):
    REPORT_JSON.write_text(json.dumps(
        {"summary": agg, "per_station": results}, indent=2))

    PASS = (agg.get("overall_match_pct", 0) >= 99
            and agg.get("median_time_err_min", 999) < 5
            and agg.get("median_height_err_ft", 999) < 0.1)

    lines = []
    lines.append("# Tide solver validation report\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
    lines.append("## Pass criteria\n")
    lines.append("- match_pct >= 99%")
    lines.append("- median time error < 5 min")
    lines.append("- median height error < 0.1 ft\n")
    lines.append(f"**Result: {'PASS' if PASS else 'FAIL'}**\n")
    lines.append("## Aggregate\n")
    if agg:
        lines.append(f"- stations validated: {agg['n_stations']}")
        lines.append(f"- stations failed: {agg['n_failed']}")
        lines.append(f"- events matched: {agg['total_events_matched']} / "
                     f"{agg['total_events_noaa']} "
                     f"({agg['overall_match_pct']:.2f}%)")
        lines.append(f"- median time error: "
                     f"{agg['median_time_err_min']:.2f} min")
        lines.append(f"- median height error: "
                     f"{agg['median_height_err_ft']:.3f} ft")
        lines.append(f"- p95 time error: {agg['p95_time_err_min']:.2f} min")
        lines.append(f"- p95 height error: {agg['p95_height_err_ft']:.3f} ft")
        lines.append(f"- max time error: {agg['max_time_err_min']:.1f} min")
        lines.append(f"- max height error: {agg['max_height_err_ft']:.2f} ft\n")
    lines.append("## Per station\n")
    lines.append("| id | state | name | match% | t_err_med (min) | "
                 "h_err_med (ft) | h_err_max (ft) | Z0 (ft) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in sorted(results,
                    key=lambda r: r.get("height_err_max_ft", 99),
                    reverse=True):
        if "error" in r:
            lines.append(f"| {r['station_id']} | - | - | ERROR: "
                         f"{r['error']} | | | | |")
            continue
        lines.append(
            f"| {r['station_id']} | {r['state']} | {r['name'][:35]} | "
            f"{r['match_pct']:.1f}% | {r['time_err_median_min']:.1f} | "
            f"{r['height_err_median_ft']:.2f} | "
            f"{r['height_err_max_ft']:.2f} | "
            f"{r['datum_offset_ft']:+.2f} |"
        )
    REPORT_MD.write_text("\n".join(lines))
    return PASS


def main():
    paths = sorted(VALIDATION_DIR.glob("*.json"))
    if not paths:
        sys.exit(f"No validation files in {VALIDATION_DIR}. "
                 "Run scripts/download_tides.py first.")
    print(f"Validating {len(paths)} stations...", flush=True)
    results = []
    for i, path in enumerate(paths, 1):
        sid = path.stem
        r = validate_station(sid, path)
        if r is None:
            continue
        results.append(r)
        if "error" in r:
            print(f"  [{i}/{len(paths)}] {sid}  ERROR: {r['error']}",
                  flush=True)
        else:
            print(f"  [{i}/{len(paths)}] {sid} {r['state']:>2s} "
                  f"{r['name'][:30]:30s}  matched {r['n_matched']}/{r['n_noaa']}  "
                  f"t_err_med={r['time_err_median_min']:5.1f}m  "
                  f"h_err_med={r['height_err_median_ft']:.2f}ft  "
                  f"h_err_max={r['height_err_max_ft']:.2f}ft", flush=True)

    agg = aggregate(results)
    passed = write_report(results, agg)
    print(f"\nReport: {REPORT_MD}")
    print(f"Overall: {'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    main()
