"""All closed-loop studies of DOSSIER §6-§9 -> results/studies.json (and results/traces.json for the figures).

Every cycle is run by the compiled ST controller (build/plc/libplc.so) against the finite-volume stator model.

    bash scripts/build_plc.sh && python -m scripts.run_studies [--quick]
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

from cure.plant import Scenario, load_config, run_cycle
from cure.plc import Plc
from cure.resin import Resin, ieee43_kt, resistivity_arrhenius
from cure.thermal import Geometry, Model

ROOT = Path(__file__).resolve().parents[1]
SIZES = ("S", "M", "L")
EXTRA_TRACES: dict = {}       # traces kept for the figures (results/traces.json)


def size_fixed_recipe(plc, p, h_worst: float, margin: float) -> dict:
    """Shortest fixed air-temperature/time recipe that gives every stator size t_eq >= target * margin at the worst
    oven heat-transfer coefficient (bisection on whole minutes, rounded up to 5 min)."""
    target = p["limits"]["teq_target_min"] * margin

    def ok(fixed):
        return all(run_cycle(plc, s, "C0", fixed, Scenario(h=h_worst), keep_trace=False)["teq_min_cold"] >= target
                   for s in SIZES)

    lo, hi = 60.0, 900.0
    while hi - lo > 1.0:
        mid = 0.5 * (lo + hi)
        lo, hi = (lo, mid) if ok(mid) else (mid, hi)
    fixed = 5 * math.ceil(hi / 5)
    return {"fixed_min": fixed, "h_worst": h_worst, "teq_target_with_margin": target}


def metrics(r: dict) -> dict:
    keys = ("done", "fault", "fallback", "alarms", "cure_min", "cycle_min", "teq_min_cold", "teq_probe_plc",
            "teq_probe_true", "cold_node_is_probe", "cold_node_is_ew", "t_max_object", "hot_node_is_ew",
            "film_min_um", "film_slot_min_um", "gel_min_probe", "e_heat_kwh", "e_dc_kwh", "e_total_kwh",
            "e_stator_kwh", "c_full_kj_k", "tau_lumped_min", "r_series_20_mohm")
    return {k: r[k] for k in keys}


def capacity(p, aoa: dict) -> dict:
    d = p["demand"]
    hours = d["oven_hours_per_day"] * d["oven_availability"]
    out = {}
    for alt, per in aoa.items():
        row = {}
        for mult in d["multipliers"]:
            load_h = sum(d["today_per_day"][s] * mult * per[s]["nominal"]["cycle_min"] / 60 for s in SIZES)
            row[str(mult)] = {"oven_hours": load_h, "positions": load_h / hours, "positions_int": math.ceil(load_h / hours - 1e-9)}
        e_day = sum(d["today_per_day"][s] * 3 * per[s]["nominal"]["e_total_kwh"] for s in SIZES)
        row["energy_mwh_year_3x"] = e_day * 250 / 1000
        row["energy_cost_keur_year_3x"] = e_day * 250 * d["energy_price_eur_kwh"] / 1000
        out[alt] = row
    return out


def gates(p, aoa: dict, cap: dict, pf: dict | None) -> dict:
    """SR-01 cure, SR-02 temperature, SR-03 retention (S, M, L at nominal and worst h); SR-04 a detached slot probe
    does not end the cycle under-cured (M stator); SR-05 oven positions at 3x output."""
    lim = p["limits"]
    g = {}
    for alt, per in aoa.items():
        sr01 = all(per[s][c]["teq_min_cold"] >= lim["teq_target_min"] and per[s][c]["done"] and not per[s][c]["fallback"]
                   for s in SIZES for c in ("nominal", "h_worst"))
        sr02 = all(per[s][c]["t_max_object"] <= lim["t_max_object"] for s in SIZES for c in ("nominal", "h_worst"))
        sr03 = all(per[s]["nominal"]["film_min_um"] >= lim["retention_min_um"] for s in SIZES)
        g[alt] = {"SR-01": sr01, "SR-02": sr02, "SR-03": sr03}
        if pf is not None:
            g[alt]["SR-04"] = pf["plausibility_on"][alt]["teq_min_cold"] >= lim["teq_target_min"]
        g[alt]["SR-05"] = cap[alt]["3"]["positions"] <= p["demand"]["max_positions_3x"]
    return g


def scores(p, aoa: dict, cap: dict, n_mc: int = 4000, seed: int = 1) -> dict:
    """Weighted scores on 0..1 per criterion; the weights are assumed and varied by a Dirichlet Monte Carlo."""
    alts = list(aoa)
    lim = p["limits"]
    crit = {
        "throughput": {a: 1.0 / cap[a]["3"]["positions"] for a in alts},
        "energy": {a: 1.0 / cap[a]["energy_mwh_year_3x"] for a in alts},
        "cure_margin": {a: min(aoa[a][s]["h_worst"]["teq_min_cold"] for s in SIZES) / lim["teq_target_min"] for a in alts},
        "temp_margin": {a: (lim["t_max_object"] - max(aoa[a][s]["nominal"]["t_max_object"] for s in SIZES)) for a in alts},
        "retention": {a: min(aoa[a][s]["nominal"]["film_min_um"] for s in SIZES) for a in alts},
        "simplicity": {"C0": 1.0, "C1": 0.9, "C2": 0.8, "C3": 0.7, "C4": 0.5, "C5": 0.5},   # [A] capex / integration
    }
    # over-cure is not a merit: cap the cure margin at 1.1 (the target margin) so a long fixed recipe gains nothing
    crit["cure_margin"] = {a: min(v, 1.1) for a, v in crit["cure_margin"].items()}
    norm = {}
    for c, v in crit.items():
        lo, hi = min(v.values()), max(v.values())
        norm[c] = {a: (v[a] - lo) / (hi - lo) if hi > lo else 1.0 for a in alts}
    w0 = {"throughput": 0.30, "energy": 0.15, "cure_margin": 0.20, "temp_margin": 0.10, "retention": 0.10, "simplicity": 0.15}
    base = {a: sum(w0[c] * norm[c][a] for c in w0) for a in alts}
    rng = np.random.default_rng(seed)
    wins = {a: 0 for a in alts}
    names = list(w0)
    for _ in range(n_mc):
        w = rng.dirichlet(np.array([w0[c] for c in names]) * 20)
        s = {a: sum(w[i] * norm[c][a] for i, c in enumerate(names)) for a in alts}
        wins[max(s, key=s.get)] += 1
    return {"weights": w0, "raw": crit, "normalised": norm, "score": base, "mc_win_share": {a: wins[a] / n_mc for a in alts}}


def sensitivity(plc, p, fixed: float) -> dict:
    """One-at-a-time on the M stator for C2 (oven only) and C4 (DC heating): cure time, coldest t_eq, hottest T."""
    cases = [
        ("h", "oven h (W/m2K)", 25.0, [("h", 15.0), ("h", 40.0)]),
        ("k_ax", "stack k through (W/mK)", 1.0, [("mat", {"steel.k_through_stack": 0.5}), ("mat", {"steel.k_through_stack": 3.0})]),
        ("k_in", "stack k in plane (W/mK)", 28.0, [("mat", {"steel.k_in_plane": 20.0}), ("mat", {"steel.k_in_plane": 35.0})]),
        ("k_tr", "winding k across (W/mK)", 0.7, [("mat", {"winding_k_transverse": 0.35}), ("mat", {"winding_k_transverse": 1.5})]),
        ("liner", "liner k (W/mK)", 0.15, [("mat", {"liner.k": 0.10}), ("mat", {"liner.k": 0.25})]),
        ("ew", "end-winding wetted area factor", 2.0, [("oven", {"ew_surface_factor": 1.0}), ("oven", {"ew_surface_factor": 3.0})]),
        ("pmax", "heater power (kW)", 60.0, [("oven", {"p_heater_max": 40000.0}), ("oven", {"p_heater_max": 90000.0})]),
        ("t0", "stator start temperature (C)", 25.0, [("t0", 15.0), ("t0", 40.0)]),
    ]
    out = {}
    for alt in ("C2", "C4"):
        base = metrics(run_cycle(plc, "M", alt, fixed, keep_trace=False))
        rows = []
        for key, label, nominal, variants in cases:
            vals = []
            for kind, v in variants:
                sc = Scenario()
                if kind == "h":
                    sc.h = v
                elif kind == "mat":
                    sc.mat_over = v
                elif kind == "oven":
                    sc.oven_over = v
                elif kind == "t0":
                    sc.t_start = v
                r = metrics(run_cycle(plc, "M", alt, fixed, sc, keep_trace=False))
                vals.append({"value": v if not isinstance(v, dict) else list(v.values())[0], "cure_min": r["cure_min"],
                             "teq_min_cold": r["teq_min_cold"], "t_max_object": r["t_max_object"]})
            rows.append({"key": key, "label": label, "nominal": nominal, "low": vals[0], "high": vals[1]})
        out[alt] = {"base": base, "rows": rows}
    return out


def resin_mismatch(plc, p, fixed: float) -> dict:
    """The controller uses the data-sheet E (49.2 kJ/mol) and gel time; the real resin may differ. The plant
    computes the true t_eq with the true E. Does the 1.10 margin cover it?"""
    rows = []
    for e_kj in (30.0, 40.0, 60.0, 80.0):
        for alt in ("C2", "C4"):
            r = run_cycle(plc, "M", alt, fixed, Scenario(e_override=e_kj * 1e3), keep_trace=False)
            rows.append({"e_true_kj": e_kj, "alt": alt, "teq_min_cold_true": r["teq_min_cold"],
                         "margin_needed": p["alternatives"][alt]["margin"] * p["limits"]["teq_target_min"] / r["teq_min_cold"],
                         "teq_probe_plc": r["teq_probe_plc"], "cure_min": r["cure_min"]})
    for gel in (7.0, 13.0):
        r = run_cycle(plc, "M", "C2", fixed, Scenario(gel_override_min=gel), keep_trace=False)
        rows.append({"gel_true_min": gel, "alt": "C2", "film_min_um": r["film_min_um"], "cure_min": r["cure_min"],
                     "teq_min_cold_true": r["teq_min_cold"]})
    return {"rows": rows}


def probe_fault(plc, p, fixed: float) -> dict:
    out = {}
    for label, band in (("plausibility_on", None), ("plausibility_off", 1e6)):
        rows = {}
        for alt in (p["alternatives"] if band is None else ("C1", "C2", "C4")):
            sc = Scenario(probe_detach_min=30.0)
            r = run_cycle(plc, "M", alt, fixed, sc, keep_trace=True) if band is None else _run_no_plaus(plc, alt, fixed, sc)
            if alt == "C2":
                EXTRA_TRACES[f"probe_fault_{label}"] = r["trace"]
            rows[alt] = {k: r[k] for k in ("done", "fallback", "alarms", "cure_min", "teq_min_cold")}
        out[label] = rows
    return out


def _run_no_plaus(plc, alt, fixed, sc):
    # run_cycle writes the recipe at start; override the band afterwards through a wrapper that re-sets it each scan
    class P:
        def __init__(self, inner):
            self.i = inner

        def reset(self):
            self.i.reset()

        def set(self, **kw):
            if "rcPlausBand" in kw:
                kw["rcPlausBand"] = 1e6
            self.i.set(**kw)

        def scan(self):
            self.i.scan()

        def outputs(self):
            return self.i.outputs()

        def get(self, n):
            return self.i.get(n)

    return run_cycle(P(plc), "M", alt, fixed, sc, keep_trace=True)


def dc_control(plc, p, fixed: float) -> dict:
    """DC heating closed on the mean copper temperature (resistance) only, vs on MAX(mean, end-winding probe)."""
    out = {}
    for s in SIZES:
        a = run_cycle(plc, s, "C4", fixed, Scenario(dc_mean_only=True), keep_trace=(s == "M"))
        b = run_cycle(plc, s, "C4", fixed, Scenario(), keep_trace=(s == "M"))
        if s == "M":
            EXTRA_TRACES["dc_mean_only"], EXTRA_TRACES["dc_max_mean_ew"] = a["trace"], b["trace"]
        out[s] = {"mean_only": {"t_max_object": a["t_max_object"], "cure_min": a["cure_min"], "hot_node_is_ew": a["hot_node_is_ew"]},
                  "max_mean_ew": {"t_max_object": b["t_max_object"], "cure_min": b["cure_min"], "hot_node_is_ew": b["hot_node_is_ew"]}}
    return out


def air_sweep(plc, p, fixed: float) -> dict:
    rows = []
    for t_air in (140.0, 150.0, 160.0, 170.0, 180.0):
        for alt in ("C2", "C4"):
            r = _run_with_air(plc, alt, fixed, t_air)
            rows.append({"t_air": t_air, "alt": alt, "cure_min": r["cure_min"], "t_max_object": r["t_max_object"],
                         "e_total_kwh": r["e_total_kwh"], "teq_min_cold": r["teq_min_cold"]})
    return {"rows": rows}


def _run_with_air(plc, alt, fixed, t_air):
    class P:
        def __init__(self, inner):
            self.i = inner

        def reset(self):
            self.i.reset()

        def set(self, **kw):
            if "rcTAir" in kw:
                kw["rcTAir"] = t_air
            self.i.set(**kw)

        def scan(self):
            self.i.scan()

        def outputs(self):
            return self.i.outputs()

        def get(self, n):
            return self.i.get(n)

    return run_cycle(P(plc), "M", alt, fixed, keep_trace=False)


def cooling(p) -> dict:
    """Passive cooling in still air after unloading (h = 8 W/m2K), from a uniform 150 C: time until the hottest node
    is at 40 C (an IR/PI test at 40 C needs no temperature correction) and the fastest surface cooling rate."""
    _, st = load_config()
    out = {}
    for s in SIZES:
        m = Model(Geometry.from_config(s, st[s]), p["materials"], p["oven"], h=p["oven"]["h_cooling_buffer"])
        temp = np.full(m.n, 150.0)
        t, dt, rate = 0.0, 60.0, 0.0
        while temp.max() > 40.0 and t < 72 * 3600:
            new = m.step(temp, 25.0, dt)
            rate = max(rate, float(np.max(temp - new)) / dt * 60)
            temp, t = new, t + dt
        out[s] = {"hours_to_40c": t / 3600, "max_rate_k_per_min": rate}
    return out


def dielectric(p) -> dict:
    """Temperature correction of insulation resistance: IEEE 43 rule (halving per 10 K) vs the cured resin's own
    volume-resistivity data [R1] Table 2 (Arrhenius through the midpoints of 155, 180, 200 C)."""
    e, lr40 = resistivity_arrhenius(p["resin"]["resistivity_points"])
    rows = []
    for t_c in (40.0, 60.0, 80.0, 100.0):
        k_ieee = float(ieee43_kt(t_c))
        k_resin = math.exp(e / 8.314462 * (1 / 313.15 - 1 / (t_c + 273.15)))
        rows.append({"t_c": t_c, "k_ieee": k_ieee, "k_resin": k_resin, "ratio": k_resin / k_ieee})
    e_ieee = 8.314462 * math.log(2) / (1 / 313.15 - 1 / 323.15)
    return {"e_resin_kj": e / 1e3, "e_ieee_rule_kj_at_40c": e_ieee / 1e3, "rows": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="CI: fewer runs (no sensitivity, sweeps or Monte Carlo)")
    a = ap.parse_args()
    t0 = time.time()
    p, _ = load_config()
    plc = Plc()
    res = Resin.from_config(p["resin"], p["limits"]["t_ref"])
    h_nom, h_worst = p["oven"]["h_conv"], 15.0
    fixed = size_fixed_recipe(plc, p, h_worst, margin=1.10)
    aoa, traces = {}, {}
    for alt in p["alternatives"]:
        aoa[alt] = {}
        for s in SIZES:
            rn = run_cycle(plc, s, alt, fixed["fixed_min"], Scenario(h=h_nom), keep_trace=True,
                           keep_fields=(s == "M"))
            rw = run_cycle(plc, s, alt, fixed["fixed_min"], Scenario(h=h_worst), keep_trace=False)
            aoa[alt][s] = {"nominal": metrics(rn), "h_worst": metrics(rw)}
            if s == "M":
                traces[alt] = {"trace": rn["trace"], "fields": rn.get("fields")}
    cap = capacity(p, aoa)
    out = {
        "resin": {"e_cure_kj": res.e_cure / 1e3, "vft": res.vft, "gel_time_150c_min": res.gel_time_s(150.0) / 60,
                  "gel_time_130c_min": res.gel_time_s(130.0) / 60, "eta_150c_mpas": float(res.viscosity(150.0)) * 1e3,
                  "eta_100c_mpas": float(res.viscosity(100.0)) * 1e3},
        "h_nominal": h_nom, "h_worst": h_worst, "fixed_recipe": fixed, "aoa": aoa, "capacity": cap,
    }
    if not a.quick:
        out["scores"] = scores(p, aoa, cap)
        out["sensitivity"] = sensitivity(plc, p, fixed["fixed_min"])
        out["resin_mismatch"] = resin_mismatch(plc, p, fixed["fixed_min"])
        out["probe_fault"] = probe_fault(plc, p, fixed["fixed_min"])
        out["air_sweep"] = air_sweep(plc, p, fixed["fixed_min"])
        out["dc_control"] = dc_control(plc, p, fixed["fixed_min"])
        out["cooling"] = cooling(p)
        out["dielectric"] = dielectric(p)
    out["gates"] = gates(p, aoa, cap, out.get("probe_fault"))
    out["runtime_s"] = time.time() - t0
    name = "studies_quick.json" if a.quick else "studies.json"
    (ROOT / "results" / name).write_text(json.dumps(out, indent=1, default=float))
    if not a.quick:
        traces.update(EXTRA_TRACES)
        (ROOT / "results" / "traces.json").write_text(json.dumps(traces, default=float))
    print(f"fixed recipe {fixed['fixed_min']} min; runtime {out['runtime_s']:.0f} s")
    for alt in aoa:
        print(alt, " ".join(f"{s}:{aoa[alt][s]['nominal']['cure_min']:.0f}min/{aoa[alt][s]['nominal']['teq_min_cold']:.0f}eq"
                            for s in SIZES), "positions@3x", round(cap[alt]["3"]["positions"], 1), out["gates"][alt])


if __name__ == "__main__":
    main()
