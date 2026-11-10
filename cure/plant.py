"""Closed loop: the compiled ST controller (OVEN_CURE) drives the oven and DC supply models; the finite-volume
stator model returns the temperatures; the resin model tracks cure, gel and drainage at every winding node.

Only the half core is modelled (symmetry at mid-stack). Heat flows and powers are reported for the whole stator.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from cure.resin import Resin, retained_film_um
from cure.thermal import Geometry, Model

ROOT = Path(__file__).resolve().parents[1]


def load_config():
    p = yaml.safe_load((ROOT / "config" / "process.yaml").read_text(encoding="utf-8"))
    s = yaml.safe_load((ROOT / "config" / "stators.yaml").read_text(encoding="utf-8"))["stators"]
    return p, s


@dataclass
class Scenario:
    h: float | None = None                 # oven heat-transfer coefficient override
    probe_detach_min: float | None = None  # slot thermocouple falls out and reads the air from this time
    e_override: float | None = None        # resin activation energy override (J/mol)
    gel_override_min: float | None = None
    mat_over: dict | None = None           # {"steel.k_through_stack": 2.0, ...}
    oven_over: dict | None = None
    t_start: float = 25.0                  # stator temperature at loading
    dc_mean_only: bool = False             # DC heating controlled on the mean copper temperature only (D6)


def _apply(d: dict, over: dict | None):
    d = copy.deepcopy(d)
    for k, v in (over or {}).items():
        node = d
        parts = k.split(".")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = v
    return d


def recipe(alt: dict, model: Model, p: dict, resin: Resin, fixed_min: float) -> dict:
    """Recipe values for one alternative and one stator type (the observer data come from the FV model)."""
    lim, dc, pr = p["limits"], p["dc_heating"], p["probes"]
    c_full = 2 * model.heat_capacity()
    tau = model.heat_capacity() / model.g_air.sum()
    return dict(rcMode=alt["mode"], rcTAir=alt["t_air"], rcTAirBoost=alt.get("t_air_boost", alt["t_air"]),
                rcBoostEnd=alt.get("boost_end", 0.0), rcFixedMin=fixed_min, rcSoakMin=alt.get("soak_min", 60.0),
                rcSoakBand=alt.get("soak_band", 5.0), rcTref=lim["t_ref"], rcEa=resin.e_cure,
                rcTeqTarget=lim["teq_target_min"], rcMargin=alt.get("margin", 1.0),
                rcGelT100=resin.gel_t100_s / 60.0, rcTcuSet=dc["t_cu_set"], rcTcuMax=lim["t_max_object"] - 5.0,
                rcIdcMax=dc["i_max"], rcDcOffBand=dc["probe_off_band"], rcTauObs=tau, rcCthObs=c_full,
                rcPlausBand=pr["plausibility_band"], rcFallbackMin=fixed_min, rcTAirMax=p["oven"]["t_air_max"])


def run_cycle(plc, size: str, alt_key: str, fixed_min: float, sc: Scenario | None = None, dt: float = 10.0,
              t_end_h: float = 12.0, keep_trace: bool = True, keep_fields: bool = False) -> dict:
    sc = sc or Scenario()
    p, stators = load_config()
    mat = _apply(p["materials"], sc.mat_over)
    oven = _apply(p["oven"], sc.oven_over)
    alt = p["alternatives"][alt_key]
    geo = Geometry.from_config(size, stators[size])
    model = Model(geo, mat, oven, h=sc.h)
    # the plant follows the "true" resin (overrides = resin unlike its data sheet); the controller recipe always
    # carries the data-sheet values, as it would in a plant
    resin = Resin.from_config(p["resin"], p["limits"]["t_ref"], sc.e_override, sc.gel_override_min)
    resin_ds = Resin.from_config(p["resin"], p["limits"]["t_ref"])
    lim, dcp = p["limits"], p["dc_heating"]

    temp = np.full(model.n, sc.t_start)
    b = model.bundle_nodes
    teq = np.zeros(model.n)
    gel = np.zeros(model.n)
    # drip over the tank at 25 C before loading: fluidity integral starts there
    phi = np.full(model.n, lim["drip_min"] * 60.0 / float(resin.viscosity(25.0)))
    t_max_b = np.full(model.n, sc.t_start)
    t_air = sc.t_start
    probe = probe_ew = sc.t_start
    e_heat = e_dc = 0.0
    i_dc = u_dc = 0.0
    r0 = model.r_measured(temp)
    plc.reset()
    plc.set(**recipe(alt, model, p, resin_ds, fixed_min), rcDcMeanOnly=sc.dc_mean_only)
    plc.set(cycleTime=dt, doorClosed=True, fanOk=True, mTAir=t_air, mTProbe=probe, mTProbeEw=probe_ew,
            rcR0=r0, rcT0=sc.t_start, mRWind=0.0, mIdc=0.0, mUdc=0.0, cmdStart=True)
    plc.scan()
    plc.set(cmdStart=False)
    tr = {k: [] for k in ("t_min", "t_air", "air_set", "probe", "probe_ew", "t_cold", "t_hot", "t_cu", "i_dc",
                          "teq_probe", "teq_cold", "state", "p_heat", "p_dc")}
    t = 0.0
    done = fault = False
    gel_time_probe = None
    a_tau = dt / p["probes"]["tau"]
    while t < t_end_h * 3600:
        out = plc.outputs()
        # ---- DC supply: current set point, limited by current and voltage
        r_now = model.r_measured(temp)
        i_dc = min(out["QIDCSET"], dcp["i_max"], dcp["v_max"] / r_now) if out["QIDCSET"] > 0 else 0.0
        u_dc = i_dc * r_now
        p_dc = i_dc * u_dc
        q = None
        if p_dc > 0:
            qj = model.joule(temp, i_dc)
            q = qj * (p_dc / 2) / qj.sum()          # half model carries half the power
        # ---- oven air: first-order inner loop with a power limit (heating only)
        q_obj = 2 * model.q_conv(temp, t_air)        # W from air into the whole stator
        q_wall = oven["ua_wall"] * (t_air - oven["t_ambient"])
        if out["QHEATERENABLE"]:
            target = t_air + (out["QAIRSET"] - t_air) * min(1.0, dt / oven["tau_air"])
            p_req = oven["c_air"] * (target - t_air) / dt + q_obj + q_wall
            p_heat = float(np.clip(p_req, 0.0, oven["p_heater_max"]))
        else:
            p_heat = 0.0
        # ---- stator step (air at the start of the step)
        temp = model.step(temp, t_air, dt, q)
        t_air = t_air + (p_heat - q_obj - q_wall) * dt / oven["c_air"]
        if not out["QHEATERENABLE"] and out["STATE"] == 3:
            t_air = max(t_air, oven["t_ambient"])
        e_heat += p_heat * dt
        e_dc += p_dc * dt
        t += dt
        # ---- resin state at every winding node
        teq += resin.teq_rate(temp) * dt
        liquid = gel < 1.0
        phi[liquid] += dt / resin.viscosity(temp[liquid])
        gel += resin.gel_rate(temp) * dt
        t_max_b = np.maximum(t_max_b, temp)
        # ---- probes (first-order lag); a detached slot probe reads the air
        src_probe = t_air if (sc.probe_detach_min is not None and t >= sc.probe_detach_min * 60) else temp[model.node_probe_slot]
        probe += (src_probe - probe) * a_tau
        probe_ew += (temp[model.node_probe_ew] - probe_ew) * a_tau
        if gel_time_probe is None and gel[model.node_probe_slot] >= 1.0:
            gel_time_probe = t / 60
        plc.set(mTAir=t_air, mTProbe=probe, mTProbeEw=probe_ew, mRWind=r_now if i_dc > 0 else 0.0, mIdc=i_dc,
                mUdc=u_dc)
        plc.scan()
        out = plc.outputs()
        if keep_trace and (int(t / dt) % 6 == 0):
            tb = temp[b]
            tr["t_min"].append(t / 60)
            tr["t_air"].append(t_air)
            tr["air_set"].append(out["QAIRSET"])
            tr["probe"].append(probe)
            tr["probe_ew"].append(probe_ew)
            tr["t_cold"].append(float(tb.min()))
            tr["t_hot"].append(float(tb.max()))
            tr["t_cu"].append(model.t_cu_mean(temp))
            tr["i_dc"].append(i_dc)
            tr["teq_probe"].append(out["TEQPROBE"])
            tr["teq_cold"].append(float(teq[b].min() / 60))
            tr["state"].append(out["STATE"])
            tr["p_heat"].append(p_heat)
            tr["p_dc"].append(p_dc)
        if out["CUREDONE"]:
            done = True
            break
        if out["STATE"] == 9:
            fault = True
            break
    film = np.array([retained_film_um(phi[k], lim["drain_height_m"], resin.rho) for k in b])
    ew = model.is_ew[b]
    k_cold = b[int(np.argmin(teq[b]))]
    res = {
        "size": size, "alt": alt_key, "done": done, "fault": fault, "fallback": bool(plc.get("FALLBACK")),
        "alarms": int(plc.get("ALARMS")),
        "cure_min": t / 60, "cycle_min": t / 60 + oven["load_unload_min"],
        "teq_min_cold": float(teq[b].min() / 60), "teq_probe_plc": float(plc.get("TEQPROBE")),
        "teq_probe_true": float(teq[model.node_probe_slot] / 60),
        "cold_node_is_probe": bool(k_cold == model.node_probe_slot), "cold_node_is_ew": bool(model.is_ew[k_cold]),
        "t_max_object": float(t_max_b[b].max()), "hot_node_is_ew": bool(model.is_ew[b[int(np.argmax(t_max_b[b]))]]),
        "hot_node_rz": [int(x) for x in np.argwhere(model.idx_b == b[int(np.argmax(t_max_b[b]))])[0]], "t_max_iron": float(t_max_b.max()),
        "film_min_um": float(film[ew].min()), "film_slot_min_um": float(film[~ew].min()),
        "gel_min_probe": gel_time_probe, "gelled_all": bool((gel[b] >= 1).all()),
        "e_heat_kwh": e_heat / 3.6e6, "e_dc_kwh": e_dc / 3.6e6, "e_total_kwh": (e_heat + e_dc) / 3.6e6,
        "e_stator_kwh": 2 * float(np.sum(model.cap * (temp - sc.t_start))) / 3.6e6,
        "mass_kg_equiv_steel": 2 * model.heat_capacity() / (p["materials"]["steel"]["cp"]),
        "c_full_kj_k": 2 * model.heat_capacity() / 1e3, "tau_lumped_min": model.heat_capacity() / model.g_air.sum() / 60,
        "r_series_20_mohm": model.r_series_20 * 1e3,
    }
    if keep_trace:
        res["trace"] = tr
    if keep_fields:
        fe, bb = model.field(teq / 60)
        res["fields"] = {"teq_iron": fe.tolist(), "teq_bundle": bb.tolist(), "r_edges": model.r_edges.tolist(),
                         "z_edges": model.z_edges.tolist(), "zb_edges": model.zb_edges.tolist(),
                         "t_end_iron": model.field(temp)[0].tolist(), "t_end_bundle": model.field(temp)[1].tolist()}
    return res
