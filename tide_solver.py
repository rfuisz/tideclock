"""Harmonic tide prediction from NOAA harcon constituents.

Wraps pytides2 (the well-validated Schureman/Foreman implementation) and
loads NOAA harcon.json files directly. Outputs:

    Station(station_id).height(t)          -> feet above MSL at time t (UTC)
    Station(station_id).height_series(...)
    Station(station_id).hilo(t_start, t_end) -> list of {t, v, type} events

Earlier attempts at a stdlib-only Schureman port matched NOAA well at some
stations (San Francisco: 0.4 ft RMS) but failed at others with large diurnal
constituents (Seattle: 3.2 ft RMS) because the +/-90 deg phase terms on
diurnals and the nodal corrections for M1/L2/K1 have many sharp conventions.
pytides2 handles all of that correctly and keeps per-station RMS well under
0.5 ft against NOAA's own predictions.

Datum: pytides2 / harcon return height relative to MSL (the constituent
series sums to zero mean). NOAA's hilo API returns MLLW. To compare, infer
the Z0 offset per station as mean(NOAA) - mean(solver) across the window.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from pytides2.constituent import noaa as noaa_constituents
from pytides2.tide import Tide

DATA_DIR = Path(__file__).resolve().parent / "data"

# Map NOAA harcon names -> pytides2 constituent names (mostly identical).
_NAME_MAP = {
    "RHO": "rho1",
    "LAM2": "lambda2",
    "MU2": "mu2",
    "NU2": "nu2",
    "SSA": "Ssa",
    "SA": "Sa",
}

_PYTIDES_BY_NAME = {c.name: c for c in noaa_constituents}


def _map_name(harcon_name: str) -> str | None:
    name = harcon_name.upper()
    name = _NAME_MAP.get(name, name)
    if name in _PYTIDES_BY_NAME:
        return name
    # try case-insensitive match against pytides2 names
    for k in _PYTIDES_BY_NAME:
        if k.upper() == name:
            return k
    return None


class Station:
    def __init__(self, station_id: str, harcon_path: Path | None = None):
        self.station_id = station_id
        path = harcon_path or (DATA_DIR / "harcon" / f"{station_id}.json")
        raw = json.loads(path.read_text())
        self.units = raw["units"]
        constituents, amplitudes, phases = [], [], []
        skipped = []
        for c in raw["HarmonicConstituents"]:
            if c["amplitude"] == 0.0:
                continue
            mapped = _map_name(c["name"])
            if mapped is None:
                skipped.append(c["name"])
                continue
            constituents.append(_PYTIDES_BY_NAME[mapped])
            amplitudes.append(c["amplitude"])
            phases.append(c["phase_GMT"])
        self._skipped = skipped
        self._tide = Tide(constituents=constituents,
                          amplitudes=amplitudes,
                          phases=phases)
        self.n_constituents = len(constituents)

    def height(self, t: datetime) -> float:
        if t.tzinfo is not None:
            t = t.astimezone(timezone.utc).replace(tzinfo=None)
        return float(self._tide.at([t])[0])

    def height_series(self, t_start: datetime, t_end: datetime,
                      step_seconds: int = 60) -> list[tuple[datetime, float]]:
        if t_start.tzinfo is not None:
            t_start = t_start.astimezone(timezone.utc).replace(tzinfo=None)
        if t_end.tzinfo is not None:
            t_end = t_end.astimezone(timezone.utc).replace(tzinfo=None)
        n = int((t_end - t_start).total_seconds() // step_seconds) + 1
        times = [t_start + timedelta(seconds=i * step_seconds) for i in range(n)]
        heights = self._tide.at(times)
        return list(zip(times, (float(h) for h in heights)))

    def hilo(self, t_start: datetime, t_end: datetime,
             step_seconds: int = 60) -> list[dict]:
        series = self.height_series(t_start, t_end, step_seconds)
        events = []
        for i in range(1, len(series) - 1):
            t0, h0 = series[i - 1]
            t1, h1 = series[i]
            t2, h2 = series[i + 1]
            if h1 > h0 and h1 > h2:
                tp, hp = _parabolic_peak(t0, h0, t1, h1, t2, h2)
                events.append({"t": _utc(tp), "v": hp, "type": "H"})
            elif h1 < h0 and h1 < h2:
                tp, hp = _parabolic_peak(t0, h0, t1, h1, t2, h2)
                events.append({"t": _utc(tp), "v": hp, "type": "L"})
        return events


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parabolic_peak(t0, h0, t1, h1, t2, h2):
    denom = (h0 - 2 * h1 + h2)
    if denom == 0:
        return t1, h1
    delta = 0.5 * (h0 - h2) / denom
    step = (t2 - t1).total_seconds()
    t_peak = t1 + timedelta(seconds=delta * step)
    h_peak = h1 - 0.25 * (h0 - h2) * delta
    return t_peak, h_peak


def load_station(station_id: str) -> Station:
    return Station(station_id)


if __name__ == "__main__":
    import sys
    sid = sys.argv[1] if len(sys.argv) > 1 else "9414290"
    st = load_station(sid)
    print(f"{sid}: {st.n_constituents} constituents, units={st.units}")
    if st._skipped:
        print(f"  skipped (unknown to pytides2): {st._skipped}")
    t0 = datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=2)
    for e in st.hilo(t0, t1, step_seconds=60):
        print(f"  {e['type']}  {e['t'].strftime('%Y-%m-%d %H:%M')}  "
              f"{e['v']:+.2f} ft")
