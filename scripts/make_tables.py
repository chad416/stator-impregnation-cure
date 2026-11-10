"""Write the result tables into DOSSIER.md and README.md between <!-- TABLE:name --> ... <!-- /TABLE --> markers.
Every number comes from results/studies.json; none is typed by hand."""
import json
import re
from pathlib import Path

from cure.plant import load_config

ROOT = Path(__file__).resolve().parents[1]
SIZES = ("S", "M", "L")


def f(x, nd=1):
    return "—" if x is None else f"{x:.{nd}f}"


def t_aoa(d, p):
    rows = ["| | Alternative | Cure time S / M / L (min) | Coldest t_eq S / M / L (min) | Hottest winding (°C) | "
            "End-winding film (µm) | Energy M (kWh) | Positions at 3× | Gates |", "|---|---|---|---|---|---|---|---|---|"]
    for a, per in d["aoa"].items():
        n = {s: per[s]["nominal"] for s in SIZES}
        g = d["gates"][a]
        rows.append(f"| {a} | {p['alternatives'][a]['name']} | " + " / ".join(f(n[s]['cure_min'], 0) for s in SIZES) + " | "
                    + " / ".join(f(n[s]['teq_min_cold']) for s in SIZES) + f" | {f(max(n[s]['t_max_object'] for s in SIZES), 0)} | "
                    f"{f(min(n[s]['film_min_um'] for s in SIZES))} | {f(n['M']['e_total_kwh'])} | "
                    f"{f(d['capacity'][a]['3']['positions'])} | {sum(g.values())}/{len(g)} |")
    return "\n".join(rows)


def t_worst(d):
    rows = ["| | Cure time at h = 15, S / M / L (min) | Coldest t_eq at h = 15, S / M / L (min) | Hottest winding at h = 15 (°C) |",
            "|---|---|---|---|"]
    for a, per in d["aoa"].items():
        w = {s: per[s]["h_worst"] for s in SIZES}
        rows.append(f"| {a} | " + " / ".join(f(w[s]['cure_min'], 0) for s in SIZES) + " | "
                    + " / ".join(f(w[s]['teq_min_cold']) for s in SIZES) + f" | {f(max(w[s]['t_max_object'] for s in SIZES), 0)} |")
    return "\n".join(rows)


def t_gates(d):
    alts = list(d["gates"])
    names = list(next(iter(d["gates"].values())))
    rows = ["| Requirement | " + " | ".join(alts) + " |", "|---|" + "---|" * len(alts)]
    for n in names:
        rows.append(f"| {n} | " + " | ".join("pass" if d["gates"][a][n] else "**fail**" for a in alts) + " |")
    return "\n".join(rows)


def t_capacity(d):
    alts = list(d["capacity"])
    rows = ["| | Today | 2× | 3× | Energy at 3× (MWh/year) | Energy cost at 3× (k€/year) |", "|---|---|---|---|---|---|"]
    for a in alts:
        c = d["capacity"][a]
        rows.append(f"| {a} | {f(c['1']['positions'])} | {f(c['2']['positions'])} | {f(c['3']['positions'])} | "
                    f"{f(c['energy_mwh_year_3x'], 0)} | {f(c['energy_cost_keur_year_3x'], 0)} |")
    return "\n".join(rows)


def t_scores(d):
    s = d["scores"]
    alts = list(s["score"])
    crit = list(s["weights"])
    rows = ["| | " + " | ".join(f"{c} ({s['weights'][c]:.2f})" for c in crit) + " | Weighted score | Won (weight draws) |",
            "|---|" + "---|" * (len(crit) + 2)]
    for a in alts:
        rows.append(f"| {a} | " + " | ".join(f(s["normalised"][c][a], 2) for c in crit) + f" | {f(s['score'][a], 3)} | "
                    f"{100 * s['mc_win_share'][a]:.0f} % |")
    return "\n".join(rows)


def t_sens(d):
    rows = ["| Parameter | Nominal | Low → cure time C2 / C4 (min) | High → cure time C2 / C4 (min) | Coldest t_eq range (min) |",
            "|---|---|---|---|---|"]
    c2, c4 = d["sensitivity"]["C2"], d["sensitivity"]["C4"]
    rows.append(f"| (base) | — | {f(c2['base']['cure_min'], 0)} / {f(c4['base']['cure_min'], 0)} | | |")
    for r2, r4 in zip(c2["rows"], c4["rows"]):
        k = 1e-3 if "(kW)" in r2["label"] else 1.0
        teqs = [r2["low"]["teq_min_cold"], r2["high"]["teq_min_cold"], r4["low"]["teq_min_cold"], r4["high"]["teq_min_cold"]]
        rows.append(f"| {r2['label']} | {r2['nominal']:g} | {r2['low']['value'] * k:g} → {f(r2['low']['cure_min'], 0)} / {f(r4['low']['cure_min'], 0)} | "
                    f"{r2['high']['value'] * k:g} → {f(r2['high']['cure_min'], 0)} / {f(r4['high']['cure_min'], 0)} | {f(min(teqs))}–{f(max(teqs))} |")
    return "\n".join(rows)


def t_mismatch(d):
    rows = ["| True resin | Alternative | Cure time (min) | Controller's t_eq at the probe (min) | True t_eq, coldest (min) | Margin needed instead of 1.10 |",
            "|---|---|---|---|---|---|"]
    for r in d["resin_mismatch"]["rows"]:
        if "e_true_kj" in r:
            rows.append(f"| E = {r['e_true_kj']:g} kJ/mol | {r['alt']} | {f(r['cure_min'], 0)} | {f(r['teq_probe_plc'])} | "
                        f"{f(r['teq_min_cold_true'])} | {f(r['margin_needed'], 2)} |")
        else:
            rows.append(f"| gel time {r['gel_true_min']:g} min at 100 °C | {r['alt']} | {f(r['cure_min'], 0)} | — | "
                        f"{f(r['teq_min_cold_true'])} | film {f(r['film_min_um'])} µm |")
    return "\n".join(rows)


def t_probe(d):
    rows = ["| Slot probe detached at 30 min (M stator) | Alternative | Ends at (min) | Fallback | Coldest t_eq (min) |", "|---|---|---|---|---|"]
    for lab, name in (("plausibility_off", "no plausibility check"), ("plausibility_on", "observer check (as built)")):
        for a, r in d["probe_fault"][lab].items():
            rows.append(f"| {name} | {a} | {f(r['cure_min'], 0)} | {'yes' if r['fallback'] else 'no'} | {f(r['teq_min_cold'])} |")
    return "\n".join(rows)


def t_dc(d):
    rows = ["| Stator | Loop on mean copper only: hottest point (°C) / cure (min) | Loop on max(mean, end-winding probe): hottest (°C) / cure (min) |",
            "|---|---|---|"]
    for s in SIZES:
        a, b = d["dc_control"][s]["mean_only"], d["dc_control"][s]["max_mean_ew"]
        rows.append(f"| {s} | {f(a['t_max_object'], 0)} / {f(a['cure_min'], 0)} | {f(b['t_max_object'], 0)} / {f(b['cure_min'], 0)} |")
    return "\n".join(rows)


def t_air(d):
    rows = ["| Air set point (°C) | C2 cure (min) | C2 hottest (°C) | C2 energy (kWh) | C4 cure (min) | C4 hottest (°C) | C4 energy (kWh) |",
            "|---|---|---|---|---|---|---|"]
    by = {}
    for r in d["air_sweep"]["rows"]:
        by.setdefault(r["t_air"], {})[r["alt"]] = r
    for t, v in by.items():
        rows.append(f"| {t:g} | {f(v['C2']['cure_min'], 0)} | {f(v['C2']['t_max_object'], 0)} | {f(v['C2']['e_total_kwh'])} | "
                    f"{f(v['C4']['cure_min'], 0)} | {f(v['C4']['t_max_object'], 0)} | {f(v['C4']['e_total_kwh'])} |")
    return "\n".join(rows)


def t_cool(d):
    rows = ["| Stator | Heat capacity (kJ/K) | Lumped time constant in the oven (min) | Still-air cooling 150 → 40 °C (h) | Fastest cooling rate (K/min) |",
            "|---|---|---|---|---|"]
    for s in SIZES:
        n = d["aoa"]["C2"][s]["nominal"]
        c = d["cooling"][s]
        rows.append(f"| {s} | {f(n['c_full_kj_k'], 0)} | {f(n['tau_lumped_min'], 0)} | {f(c['hours_to_40c'])} | {f(c['max_rate_k_per_min'], 2)} |")
    return "\n".join(rows)


def t_ir(d):
    di = d["dielectric"]
    rows = [f"| IR test temperature (°C) | IEEE 43 factor to 40 °C | Cured-resin factor (E = {di['e_resin_kj']:.0f} kJ/mol) | Ratio |",
            "|---|---|---|---|"]
    for r in di["rows"]:
        rows.append(f"| {r['t_c']:g} | {f(r['k_ieee'])} | {f(r['k_resin'])} | {f(r['ratio'], 2)} |")
    return "\n".join(rows)


def t_resin(d):
    r = d["resin"]
    return "\n".join([
        "| Quantity | Value | Tag |", "|---|---|---|",
        f"| Activation energy from 2 h/130 °C ≡ 1 h/150 °C | {f(r['e_cure_kj'])} kJ/mol | [D] from [R1] |",
        f"| Gel time at 130 °C / 150 °C | {f(r['gel_time_130c_min'], 2)} / {f(r['gel_time_150c_min'], 2)} min | [D] from [R1] gel time at 100 °C |",
        f"| Viscosity at 100 °C / 150 °C (VFT, extrapolated) | {f(r['eta_100c_mpas'])} / {f(r['eta_150c_mpas'])} mPa s | [D]/[A] |",
        f"| Fixed recipe that covers S, M, L at h = {d['h_worst']:g} W/m²K | {d['fixed_recipe']['fixed_min']} min | [D] |"])


def fill(path: Path, tables: dict):
    txt = path.read_text(encoding="utf-8")
    for name, body in tables.items():
        txt = re.sub(rf"(<!-- TABLE:{name} -->)(.*?)(<!-- /TABLE -->)", lambda m: f"{m.group(1)}\n{body}\n{m.group(3)}", txt, flags=re.S)
    path.write_text(txt, encoding="utf-8")


if __name__ == "__main__":
    p, _ = load_config()
    d = json.loads((ROOT / "results" / "studies.json").read_text())
    tables = {"aoa": t_aoa(d, p), "worst": t_worst(d), "gates": t_gates(d), "capacity": t_capacity(d), "scores": t_scores(d),
              "sens": t_sens(d), "mismatch": t_mismatch(d), "probe": t_probe(d), "dc": t_dc(d), "air": t_air(d),
              "cool": t_cool(d), "ir": t_ir(d), "resin": t_resin(d)}
    for doc in ("DOSSIER.md", "README.md"):
        if (ROOT / doc).exists():
            fill(ROOT / doc, tables)
    print("tables:", ", ".join(tables))
