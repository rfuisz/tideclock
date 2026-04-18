#!/usr/bin/env python3
"""Download NOAA tide data for US coastal stations.

Pulls the station index, harmonic constants, subordinate offsets, and a
small validation corpus of NOAA hi/lo predictions. All outputs go under
`data/`. Idempotent with --resume.
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

MDAPI = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi"
DATAGETTER = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

COAST_STATES = {
    "east": ["ME", "NH", "MA", "RI", "CT", "NY", "NJ", "DE", "MD", "DC",
             "VA", "NC", "SC", "GA", "FL"],
    "west": ["CA", "OR", "WA"],
    "gulf": ["FL", "AL", "MS", "LA", "TX"],
    "ak":   ["AK"],
    "hi":   ["HI"],
    "pr":   ["PR", "VI"],
}

USER_AGENT = "tideclock-downloader/1.0 (github.com/rfuisz/tideclock)"


def http_get(url, retries=3, timeout=30):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                raise
            time.sleep(1.5 ** attempt + random.random())
    raise last_err


def http_get_json(url, **kw):
    return json.loads(http_get(url, **kw))


def filtered_states(coasts):
    states = set()
    for c in coasts:
        states.update(COAST_STATES[c])
    return states


def fetch_stations(states):
    print(f"Fetching station index ({len(states)} states)...", flush=True)
    data = http_get_json(f"{MDAPI}/stations.json?type=tidepredictions")
    all_stations = data["stations"]
    filtered = [s for s in all_stations if s.get("state") in states]
    slim = []
    for s in filtered:
        slim.append({
            "id": s["id"],
            "name": s["name"],
            "state": s["state"],
            "lat": s["lat"],
            "lng": s["lng"],
            "type": s["type"],
            "reference_id": s.get("reference_id") or None,
            "timezonecorr": s.get("timezonecorr"),
            "timemeridian": s.get("timemeridian"),
        })
    slim.sort(key=lambda r: (r["state"], r["id"]))
    print(f"  {len(slim)} stations total  "
          f"({sum(1 for s in slim if s['type'] == 'R')} reference, "
          f"{sum(1 for s in slim if s['type'] == 'S')} subordinate)", flush=True)
    return slim


def fetch_harcon(stations, rate_limit, resume):
    target = [s for s in stations if s["type"] == "R"]
    out_dir = DATA_DIR / "harcon"
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = skipped = missing = failed = 0
    for i, s in enumerate(target):
        path = out_dir / f"{s['id']}.json"
        if resume and path.exists():
            skipped += 1
            continue
        url = f"{MDAPI}/stations/{s['id']}/harcon.json"
        try:
            data = http_get_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                missing += 1
                continue
            failed += 1
            print(f"  FAIL {s['id']}: {e}", flush=True)
            continue
        except Exception as e:
            failed += 1
            print(f"  FAIL {s['id']}: {e}", flush=True)
            continue
        path.write_text(json.dumps(data, indent=2))
        ok += 1
        if (i + 1) % 50 == 0:
            print(f"  harcon {i+1}/{len(target)}  ok={ok} skipped={skipped} "
                  f"missing={missing} failed={failed}", flush=True)
        time.sleep(rate_limit)
    print(f"harcon done: ok={ok} skipped={skipped} missing={missing} "
          f"failed={failed}", flush=True)


def fetch_subordinate_offsets(stations, rate_limit, resume):
    out_path = DATA_DIR / "subordinate_offsets.json"
    existing = {}
    if resume and out_path.exists():
        existing = json.loads(out_path.read_text())
    target = [s for s in stations if s["type"] == "S"]
    ok = skipped = missing = failed = 0
    result = dict(existing)
    for i, s in enumerate(target):
        if resume and s["id"] in existing:
            skipped += 1
            continue
        url = f"{MDAPI}/stations/{s['id']}/tidepredoffsets.json"
        try:
            data = http_get_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                missing += 1
                continue
            failed += 1
            continue
        except Exception:
            failed += 1
            continue
        result[s["id"]] = {
            "ref": data.get("refStationId"),
            "t_hi": data.get("timeOffsetHighTide"),
            "t_lo": data.get("timeOffsetLowTide"),
            "h_hi": data.get("heightOffsetHighTide"),
            "h_lo": data.get("heightOffsetLowTide"),
            "h_type": data.get("heightAdjustedType"),
        }
        ok += 1
        if (i + 1) % 200 == 0:
            out_path.write_text(json.dumps(result, indent=2, sort_keys=True))
            print(f"  offsets {i+1}/{len(target)}  ok={ok} skipped={skipped} "
                  f"missing={missing} failed={failed}", flush=True)
        time.sleep(rate_limit)
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"offsets done: ok={ok} skipped={skipped} missing={missing} "
          f"failed={failed}  total={len(result)}", flush=True)


def pick_validation_sample(stations, sample_size):
    ref_stations = [s for s in stations if s["type"] == "R"]
    by_state = {}
    for s in ref_stations:
        by_state.setdefault(s["state"], []).append(s)
    picks = []
    states = sorted(by_state.keys())
    rng = random.Random(42)
    i = 0
    while len(picks) < sample_size and i < sample_size * 5:
        state = states[i % len(states)]
        pool = by_state[state]
        if pool:
            chosen = rng.choice(pool)
            if chosen not in picks:
                picks.append(chosen)
        i += 1
    return picks[:sample_size]


def fetch_validation(sample, years, start_year, rate_limit, resume):
    out_dir = DATA_DIR / "validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    begin = f"{start_year}0101"
    end = f"{start_year + years}0101"
    ok = skipped = failed = 0
    for i, s in enumerate(sample):
        path = out_dir / f"{s['id']}.json"
        if resume and path.exists():
            skipped += 1
            continue
        params = {
            "begin_date": begin,
            "end_date": end,
            "station": s["id"],
            "product": "predictions",
            "interval": "hilo",
            "datum": "MLLW",
            "time_zone": "gmt",
            "units": "english",
            "format": "json",
        }
        url = DATAGETTER + "?" + urllib.parse.urlencode(params)
        try:
            data = http_get_json(url)
        except Exception as e:
            failed += 1
            print(f"  FAIL {s['id']}: {e}", flush=True)
            continue
        if "error" in data:
            failed += 1
            print(f"  FAIL {s['id']}: {data['error']}", flush=True)
            continue
        payload = {
            "station_id": s["id"],
            "name": s["name"],
            "state": s["state"],
            "begin_date": begin,
            "end_date": end,
            "datum": "MLLW",
            "units": "english",
            "time_zone": "gmt",
            "predictions": data.get("predictions", []),
        }
        path.write_text(json.dumps(payload))
        ok += 1
        print(f"  validation {i+1}/{len(sample)}  {s['id']} {s['state']} "
              f"{s['name'][:40]}  n={len(payload['predictions'])}", flush=True)
        time.sleep(rate_limit)
    print(f"validation done: ok={ok} skipped={skipped} failed={failed}",
          flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coasts", default="east,west,gulf",
                    help="comma list from: east,west,gulf,ak,hi,pr")
    ap.add_argument("--validation-years", type=int, default=2)
    ap.add_argument("--validation-start-year", type=int, default=2026)
    ap.add_argument("--validation-sample", type=int, default=40)
    ap.add_argument("--rate-limit", type=float, default=0.4,
                    help="seconds between NOAA requests")
    ap.add_argument("--resume", action="store_true",
                    help="skip outputs that already exist")
    ap.add_argument("--skip-harcon", action="store_true")
    ap.add_argument("--skip-offsets", action="store_true")
    ap.add_argument("--skip-validation", action="store_true")
    args = ap.parse_args()

    coasts = [c.strip() for c in args.coasts.split(",") if c.strip()]
    for c in coasts:
        if c not in COAST_STATES:
            sys.exit(f"unknown coast: {c}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stations_path = DATA_DIR / "stations.json"
    if args.resume and stations_path.exists():
        stations = json.loads(stations_path.read_text())["stations"]
        print(f"Using cached station index ({len(stations)} stations)",
              flush=True)
    else:
        stations = fetch_stations(filtered_states(coasts))
        stations_path.write_text(json.dumps(
            {"coasts": coasts, "stations": stations}, indent=2))

    if not args.skip_harcon:
        fetch_harcon(stations, args.rate_limit, args.resume)
    if not args.skip_offsets:
        fetch_subordinate_offsets(stations, args.rate_limit, args.resume)
    if not args.skip_validation:
        sample = pick_validation_sample(stations, args.validation_sample)
        fetch_validation(sample, args.validation_years,
                         args.validation_start_year, args.rate_limit,
                         args.resume)

    print("done.", flush=True)


if __name__ == "__main__":
    main()
