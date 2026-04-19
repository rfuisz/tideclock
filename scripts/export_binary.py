#!/usr/bin/env python3
"""Export tide data as flat binary files for on-device (ESP32) use.

For each reference station, compute 50 years of hi/lo events and write a
fixed-width binary file the MCU can binary-search in place. Also writes a
compact station index and subordinate offsets table.

Binary format (per-station <station_id>.dat):

    Header (16 bytes, little-endian):
        magic[4]      = "TIDE"
        version: u16  = 1
        flags:   u16  = 0
        count:   u32  (number of records)
        reserved[4]

    Record (8 bytes each, sorted ascending by ts):
        ts:      u32  epoch seconds since 2000-01-01T00:00:00Z (UTC)
        height:  i16  hundredths of foot relative to MSL
        type:    u8   ascii 'H' (0x48) or 'L' (0x4C)
        pad:     u8   = 0

    File size per station: 16 + 8 * count. ~50 yr -> ~570 KB.

Supporting files:

    stations.bin  -- fixed-width station index (see STATION_RECORD_FMT)
    offsets.bin   -- subordinate station offsets (see OFFSETS_FMT)
"""
import json
import struct
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool, cpu_count, get_context
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from tide_solver import Station  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
OUT_DIR = REPO_ROOT / "data" / "sdcard"

EPOCH = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
YEARS = 50
START = datetime(2025, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(days=YEARS * 365 + 13)  # +13 leap days margin

# --- Station index record ---
# 72 bytes per station:
#   id:    10 bytes ASCII (null padded)
#   parent:10 bytes ASCII ('' if reference)
#   state:  2 bytes ASCII
#   type:   1 byte 'R' or 'S'
#   _pad:   1 byte
#   lat:    f32 (deg)
#   lng:    f32 (deg)
#   name:  40 bytes UTF-8 (null padded / truncated)
# total: 10+10+2+1+1+4+4+40 = 72
STATION_RECORD_FMT = "<10s10s2scxff40s"
STATION_RECORD_SIZE = struct.calcsize(STATION_RECORD_FMT)
assert STATION_RECORD_SIZE == 72, STATION_RECORD_SIZE

# --- Offsets record (one per subordinate) ---
# 32 bytes per record:
#   sub_id:    10 bytes ASCII
#   ref_id:    10 bytes ASCII
#   t_hi:      i16  minutes
#   t_lo:      i16  minutes
#   h_hi:      i16  hundredths of ft (additive offset)
#   h_lo:      i16  hundredths of ft
#   _pad:       4 bytes
OFFSETS_FMT = "<10s10shhhh4x"
OFFSETS_SIZE = struct.calcsize(OFFSETS_FMT)
assert OFFSETS_SIZE == 32

# --- Per-station hilo file ---
HEADER_FMT = "<4sHHI4x"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
assert HEADER_SIZE == 16
RECORD_FMT = "<IhBB"
RECORD_SIZE = struct.calcsize(RECORD_FMT)
assert RECORD_SIZE == 8


def _find_peaks(times, heights):
    """Return (idx, type) for local extrema. type='H' or 'L'."""
    peaks = []
    # Use simple 3-point local extrema on the coarse sample.
    for i in range(1, len(heights) - 1):
        h0, h1, h2 = heights[i - 1], heights[i], heights[i + 1]
        if h1 > h0 and h1 > h2:
            peaks.append((i, "H"))
        elif h1 < h0 and h1 < h2:
            peaks.append((i, "L"))
    return peaks


def _parabolic_refine(t0, h0, t1, h1, t2, h2):
    denom = h0 - 2 * h1 + h2
    if denom == 0:
        return t1, h1
    delta = 0.5 * (h0 - h2) / denom
    step = (t2 - t1).total_seconds()
    t_peak = t1 + timedelta(seconds=delta * step)
    h_peak = h1 - 0.25 * (h0 - h2) * delta
    return t_peak, h_peak


def export_station(station_id):
    try:
        return _export_station_inner(station_id)
    except Exception as e:
        return (station_id, f"error: {e}: {traceback.format_exc()[:200]}", 0)


def _export_station_inner(station_id):
    out_path = OUT_DIR / "hilo" / f"{station_id}.dat"
    if out_path.exists():
        return (station_id, "skip", 0)
    try:
        st = Station(station_id)
    except FileNotFoundError:
        return (station_id, "no_harcon", 0)
    if st.n_constituents < 3:
        return (station_id, "too_few_constituents", 0)

    # Sample the full window once at 5-min resolution (vectorized numpy
    # inside pytides2), then pick local extrema and parabolic-refine
    # each one for sub-minute peak-time accuracy.
    step = 300
    series = st.height_series(START, END, step_seconds=step)
    hs = [h for _, h in series]
    records = []
    epoch_naive = EPOCH.replace(tzinfo=None)
    for i in range(1, len(series) - 1):
        h0, h1, h2 = hs[i - 1], hs[i], hs[i + 1]
        if h1 > h0 and h1 > h2:
            typ = "H"
        elif h1 < h0 and h1 < h2:
            typ = "L"
        else:
            continue
        t0, _ = series[i - 1]
        t1, _ = series[i]
        t2, _ = series[i + 1]
        tp, hp = _parabolic_refine(t0, h0, t1, h1, t2, h2)
        ts = int((tp - epoch_naive).total_seconds())
        if ts < 0:
            continue
        height_cft = int(round(hp * 100))
        if height_cft > 32767:
            height_cft = 32767
        elif height_cft < -32768:
            height_cft = -32768
        records.append((ts, height_cft, ord(typ)))

    records.sort()
    data = bytearray()
    data += struct.pack(HEADER_FMT, b"TIDE", 1, 0, len(records))
    for ts, h, t in records:
        data += struct.pack(RECORD_FMT, ts, h, t, 0)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(data))
    return (station_id, "ok", len(records))


def write_index(stations):
    path = OUT_DIR / "stations.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        # Header: magic 'STNS', version 1, count
        f.write(struct.pack("<4sHHI4x", b"STNS", 1, 0, len(stations)))
        for s in stations:
            f.write(struct.pack(
                STATION_RECORD_FMT,
                s["id"].encode("ascii")[:10],
                (s["reference_id"] or "").encode("ascii")[:10],
                s["state"].encode("ascii")[:2].ljust(2, b"\x00"),
                s["type"].encode("ascii")[:1],
                float(s["lat"]),
                float(s["lng"]),
                s["name"].encode("utf-8")[:40].ljust(40, b"\x00"),
            ))
    print(f"wrote {path}  ({len(stations)} stations, "
          f"{path.stat().st_size} bytes)")


def write_offsets():
    src = json.load(open(DATA_DIR / "subordinate_offsets.json"))
    path = OUT_DIR / "offsets.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(struct.pack("<4sHHI4x", b"OFST", 1, 0, len(src)))
        for sub_id, off in sorted(src.items()):
            ref = (off.get("ref") or "").encode("ascii")[:10]
            t_hi = int(off.get("t_hi") or 0)
            t_lo = int(off.get("t_lo") or 0)
            h_hi = int(round((off.get("h_hi") or 0) * 100))
            h_lo = int(round((off.get("h_lo") or 0) * 100))
            # Many offsets are published as height ADDENDS (type 'R' add),
            # but a handful use multiplicative type. We only support additive
            # here; firmware can fall back to the parent if flags indicate.
            f.write(struct.pack(
                OFFSETS_FMT,
                sub_id.encode("ascii")[:10].ljust(10, b"\x00"),
                ref.ljust(10, b"\x00"),
                t_hi, t_lo, h_hi, h_lo,
            ))
    print(f"wrote {path}  ({len(src)} subordinates, "
          f"{path.stat().st_size} bytes)")


def main():
    stations = json.load(open(DATA_DIR / "stations.json"))["stations"]
    refs = [s for s in stations if s["type"] == "R"]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "hilo").mkdir(exist_ok=True)

    # Index + offsets
    write_index(stations)
    write_offsets()

    # Per-station hilo files, in parallel.
    # Use fork on macOS so workers inherit imports without re-importing
    # pytides2 (which is slow and brittle under spawn).
    workers = max(1, cpu_count() - 1)
    ctx = get_context("fork")
    print(f"Exporting {len(refs)} stations x {YEARS} yr "
          f"on {workers} fork workers...")
    t0 = time.time()
    ok = skip = fail = 0
    total_events = 0
    with ctx.Pool(workers) as pool:
        for i, (sid, status, nrec) in enumerate(
                pool.imap_unordered(export_station,
                                    [s["id"] for s in refs]), 1):
            if status == "ok":
                ok += 1
                total_events += nrec
            elif status == "skip":
                skip += 1
            else:
                fail += 1
            if status not in ("ok", "skip") and not status.startswith("no_"):
                print(f"  FAIL {sid}: {status[:100]}", flush=True)
            if i % 20 == 0 or i == len(refs):
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(refs) - i) / rate if rate > 0 else 0
                print(f"  [{i:4d}/{len(refs)}] ok={ok} skip={skip} "
                      f"fail={fail}  {rate:.1f} sta/s  ETA {eta:.0f}s",
                      flush=True)

    print(f"\nDone. ok={ok} skip={skip} fail={fail}  "
          f"total events={total_events:,}")

    # Total size
    total = sum(f.stat().st_size for f in OUT_DIR.rglob("*") if f.is_file())
    print(f"Total SD payload: {total / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
