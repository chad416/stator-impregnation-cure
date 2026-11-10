"""All dossier figures (PNG, docs/img) from results/studies.json and results/traces.json. Nothing is drawn by hand."""
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from cure.plant import load_config  # noqa: E402
from cure.resin import Resin  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "img"
IMG.mkdir(parents=True, exist_ok=True)
# categorical order (validated reference palette, fixed order, never cycled)
C = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
LIMIT = "#e34948"
SEQ = LinearSegmentedColormap.from_list("seq", ["#eef4fc", "#9cc1ee", "#2a78d6", "#0d3a73"])
plt.rcParams.update({"font.size": 9, "axes.edgecolor": INK2, "axes.labelcolor": INK, "xtick.color": INK2,
                     "ytick.color": INK2, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
                     "axes.spines.top": False, "axes.spines.right": False, "lines.linewidth": 1.6,
                     "figure.facecolor": SURF, "axes.facecolor": SURF, "legend.frameon": False})
ALT_COL = {"C0": C[0], "C1": C[1], "C2": C[2], "C3": C[3], "C4": C[4], "C5": C[6]}
SIZES = ("S", "M", "L")


def save(fig, name):
    fig.tight_layout()
    fig.savefig(IMG / name, dpi=150)
    plt.close(fig)


def fig_fields(tr, st):
    f = tr["C2"]["fields"]
    re_, ze, zbe = np.array(f["r_edges"]) * 1000, np.array(f["z_edges"]) * 1000, np.array(f["zb_edges"]) * 1000
    ns = len(f["teq_bundle"])
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.9), sharey=True)
    for ax, key_fe, key_b, title, lab in (
            (axes[0], "t_end_iron", "t_end_bundle", "Temperature at cure end (°C)", "°C"),
            (axes[1], "teq_iron", "teq_bundle", "Equivalent cure t_eq at 150 °C (min)", "min")):
        fe, b = np.array(f[key_fe]), np.array(f[key_b])
        vmin, vmax = min(fe.min(), b.min()), max(fe.max(), b.max())
        m1 = ax.pcolormesh(ze, re_, fe, cmap=SEQ, vmin=vmin, vmax=vmax, shading="flat")
        # winding: in the slot zone draw it as hatched overlay cells on top of the teeth (same colour scale)
        ax.pcolormesh(zbe, re_[: ns + 1], b, cmap=SEQ, vmin=vmin, vmax=vmax, shading="flat",
                      edgecolors=SURF, linewidth=0.3)
        ax.add_patch(Rectangle((ze[0], re_[0]), ze[-1] - ze[0], re_[ns] - re_[0], fill=False, ec=INK2, lw=0.8, ls="--"))
        ax.axvline(ze[-1], color=INK2, lw=0.8)
        ax.text(ze[-1] + 4, re_[-1] - 12, "core end", fontsize=7, color=INK2)
        ax.text(ze[-1] + 4, re_[0] + 4, "end winding", fontsize=7, color=INK)
        ax.plot([ze[0] + (ze[1] - ze[0]) / 2], [(re_[ns - 1] + re_[ns]) / 2], marker="o", ms=8, mfc="none", mec=INK, mew=1.5)
        ax.annotate("slot probe\n(coldest resin)", ((ze[1] - ze[0]) / 2, (re_[ns - 1] + re_[ns]) / 2),
                    xytext=(40, re_[ns] + 30), fontsize=7, color=INK, arrowprops=dict(arrowstyle="-", color=INK2, lw=0.8))
        ax.plot([(zbe[-2] + zbe[-1]) / 2], [(re_[0] + re_[1]) / 2], marker="s", ms=7, mfc="none", mec=INK, mew=1.5)
        ax.set_title(title, fontsize=9, loc="left")
        ax.set_xlabel("axial position from mid-stack (mm)")
        ax.grid(False)
        cb = fig.colorbar(m1, ax=ax, fraction=0.05, pad=0.02)
        cb.set_label(lab)
    axes[0].set_ylabel("radius (mm)")
    fig.suptitle("M stator (800 kVA), C2 cure-index end point, h = 25 W/m²K: winding cells drawn over the slot zone (dashed)",
                 fontsize=8, x=0.01, ha="left", color=INK2)
    save(fig, "fields_M.png")


def fig_cycles(tr, sd):
    fig, axes = plt.subplots(3, 1, figsize=(8.6, 7.6), sharex=True)
    for ax, alt in zip(axes, ("C0", "C2", "C4")):
        t = tr[alt]["trace"]
        ax.plot(t["t_min"], t["t_air"], color=C[0], label="oven air")
        ax.plot(t["t_min"], t["probe"], color=C[2], label="slot probe (coldest resin)")
        ax.plot(t["t_min"], t["t_hot"], color=C[1], label="hottest winding point")
        ax.axhline(150, color=INK2, lw=0.8, ls=":")
        ax.axhline(180, color=LIMIT, lw=1, ls="--")
        ax.text(2, 182, "class H 180 °C (SR-02)", fontsize=7, color=INK2)
        end = t["t_min"][-1]
        ax.axvline(end, color=INK, lw=0.8)
        r = sd["aoa"][alt]["M"]["nominal"]
        ax.text(end + 3, 40, f"cure done {r['cure_min']:.0f} min\nt_eq coldest {r['teq_min_cold']:.1f} min", fontsize=7)
        ax.set_ylabel("°C")
        ax.set_ylim(15, 205)
        ax.set_title(f"{alt}: {load_config()[0]['alternatives'][alt]['name']}", fontsize=9, loc="left")
        if alt == "C4":
            a2 = ax.inset_axes([0.62, 0.08, 0.33, 0.35])
            a2.plot(t["t_min"], np.array(t["i_dc"]) / 1000, color=C[6])
            a2.set_title("DC current (kA)", fontsize=7)
            a2.tick_params(labelsize=6)
    axes[0].legend(fontsize=7, loc="lower right", ncol=3)
    axes[-1].set_xlabel("time from loading (min)")
    axes[-1].set_xlim(0, 400)
    save(fig, "cycle_M.png")


def fig_cure_time(sd):
    alts = list(sd["aoa"])
    fig, ax = plt.subplots(figsize=(8.6, 3.6))
    w = 0.26
    x = np.arange(len(alts))
    for j, s in enumerate(SIZES):
        v = [sd["aoa"][a][s]["nominal"]["cure_min"] for a in alts]
        bars = ax.bar(x + (j - 1) * w, v, w * 0.92, color=C[j], label=f"stator {s}")
        if s == "L":
            for xi, vi in zip(x + (j - 1) * w, v):
                ax.text(xi, vi + 4, f"{vi:.0f}", ha="center", fontsize=7)
    ax.axhline(60, color=INK2, lw=0.8, ls=":")
    ax.text(len(alts) - 0.5, 63, "resin needs 60 min at 150 °C", fontsize=7, ha="right", color=INK2)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{a}\n{sd['capacity'][a]['3']['positions']:.1f} pos. @3×" for a in alts], fontsize=8)
    ax.set_ylabel("oven time to cure (min)")
    ax.legend(fontsize=7, ncol=3, loc="upper right")
    ax.set_title("Cure time per alternative and stator size (h = 25 W/m²K); oven positions needed at 3× output",
                 fontsize=9, loc="left")
    save(fig, "cure_time.png")


def fig_capacity(sd):
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    for a in sd["capacity"]:
        m = [1, 2, 3]
        v = [sd["capacity"][a][str(k)]["positions"] for k in m]
        ax.plot(m, v, marker="o", ms=5, color=ALT_COL[a], label=a)
        ax.text(3.05, v[-1], f"{a} {v[-1]:.1f}", fontsize=7, va="center")
    ax.set_xticks([1, 2, 3])
    ax.set_xticklabels(["today", "2×", "3×"])
    ax.set_xlim(0.9, 3.45)
    ax.set_ylabel("single-stator oven positions")
    ax.set_title("Oven capacity needed vs output", fontsize=9, loc="left")
    ax.legend(fontsize=7, ncol=2, loc="upper left")
    save(fig, "capacity.png")


def fig_resin(p):
    r = Resin.from_config(p["resin"], 150.0)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.2, 3.5))
    pts = np.array(p["resin"]["viscosity_points"], float)
    tt = np.linspace(10, 160, 300)
    a1.semilogy(tt, r.viscosity(tt) * 1e3, color=C[0], label="VFT fit")
    a1.semilogy(pts[:, 0], pts[:, 1], "o", ms=5, color=C[1], label="data sheet, Annex 1")
    a1.axvspan(80, 160, color=GRID, alpha=0.5, lw=0)
    a1.text(85, 3000, "extrapolated", fontsize=7, color=INK2)
    a1.set_xlabel("°C")
    a1.set_ylabel("viscosity (mPa s)")
    a1.set_title("Liquid resin viscosity", fontsize=9, loc="left")
    a1.legend(fontsize=7)
    tc = np.linspace(90, 180, 200)
    a2.semilogy(tc, [r.isothermal_cure_s(t, 3600) / 60 for t in tc], color=C[0], label="time to t_eq = 60 min")
    a2.semilogy(tc, [r.gel_time_s(t) / 60 for t in tc], color=C[2], label="gel time")
    for t_c, h in p["resin"]["cure_schedules_h"]:
        a2.plot(t_c, h * 60, "o", ms=6, color=C[1])
    a2.plot(100, p["resin"]["gel_time_100c_min"], "o", ms=6, color=C[1], label="data-sheet points")
    a2.errorbar(100, 10, yerr=[[3], [3]], color=C[1], lw=1, capsize=3)
    a2.set_xlabel("isothermal object temperature (°C)")
    a2.set_ylabel("min")
    a2.set_title(f"Arrhenius cure, E = {r.e_cure / 1e3:.1f} kJ/mol (from the two schedules)", fontsize=9, loc="left")
    a2.legend(fontsize=7)
    save(fig, "resin.png")


def fig_tornado(sd):
    s = sd["sensitivity"]["C2"]
    base = s["base"]["cure_min"]
    rows = sorted(s["rows"], key=lambda r: abs(r["high"]["cure_min"] - r["low"]["cure_min"]))
    fig, ax = plt.subplots(figsize=(7.4, 3.4))
    for i, r in enumerate(rows):
        lo, hi = r["low"]["cure_min"] - base, r["high"]["cure_min"] - base
        ax.barh(i, lo, color=C[0], height=0.6)
        ax.barh(i, hi, color=C[1], height=0.6)
        k = 1e-3 if "(kW)" in r["label"] else 1.0
        for d, v, default in ((lo, r["low"]["value"], -1), (hi, r["high"]["value"], 1)):
            side = (1 if d >= 0 else -1) if abs(d) >= 1 else default
            ax.text(d + 3 * side, i, f"{v * k:g}", ha="left" if side > 0 else "right", va="center", fontsize=7)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r["label"] for r in rows], fontsize=8)
    ax.axvline(0, color=INK, lw=0.8)
    ax.set_xlabel(f"change of cure time (min) from {base:.0f} min, M stator, C2")
    ax.set_xlim(-75, 90)
    ax.set_title("What sets the cycle (blue: low value, orange: high value)", fontsize=9, loc="left")
    save(fig, "tornado.png")


def fig_probe_fault(tr, sd):
    fig, ax = plt.subplots(figsize=(8.0, 3.5))
    for key, col, lab, lw in (("probe_fault_plausibility_on", C[0], "observer check (as built)", 1.6),
                              ("probe_fault_plausibility_off", C[1], "no plausibility check", 2.6)):
        t = tr[key]
        ax.plot(t["t_min"], t["probe"], color=col, lw=lw * 0.7, ls="--")
        ax.plot(t["t_min"], t["teq_cold"], color=col, lw=lw, label=f"{lab}: t_eq of coldest resin")
        ax.axvline(t["t_min"][-1], color=col, lw=0.8, ls=":")
    ax.axhline(60, color=LIMIT, lw=1, ls="--")
    ax.text(5, 63, "SR-01: 60 min", fontsize=7, color=INK2)
    pf = sd["probe_fault"]
    ax.text(85, 100, f"ends at {pf['plausibility_off']['C2']['cure_min']:.0f} min with t_eq {pf['plausibility_off']['C2']['teq_min_cold']:.1f} min",
            fontsize=7, color=INK)
    ax.text(250, 150, f"falls back to the fixed\nrecipe: t_eq {pf['plausibility_on']['C2']['teq_min_cold']:.0f} min", fontsize=7, color=INK)
    ax.set_xlabel("time from loading (min); dashed = slot probe reading (°C), detached at 30 min")
    ax.set_ylabel("min  /  °C")
    ax.set_title("Detached slot thermocouple, M stator, C2", fontsize=9, loc="left")
    ax.legend(fontsize=7, loc="upper left")
    save(fig, "probe_fault.png")


def fig_dc(tr, sd):
    fig, ax = plt.subplots(figsize=(8.0, 3.5))
    for key, col, lab in (("dc_mean_only", C[1], "PI on mean copper (resistance) only"),
                          ("dc_max_mean_ew", C[0], "PI on max(mean, end-winding probe), as built")):
        t = tr[key]
        ax.plot(t["t_min"], t["t_hot"], color=col, label=f"{lab}: hottest point")
        ax.plot(t["t_min"], t["t_cu"], color=col, lw=1.0, ls="--")
    ax.axhline(180, color=LIMIT, lw=1, ls="--")
    ax.text(150, 183, "class H 180 °C", fontsize=7, color=INK2)
    d = sd["dc_control"]["M"]
    ax.text(5, 230, f"hot spot {d['mean_only']['t_max_object']:.0f} °C vs {d['max_mean_ew']['t_max_object']:.0f} °C (end winding)", fontsize=7)
    ax.set_xlabel("time from loading (min); dashed = mean copper temperature from resistance")
    ax.set_ylabel("°C")
    ax.set_xlim(0, 180)
    ax.set_title("DC heating of the winding (C4, M stator): what the loop must see", fontsize=9, loc="left")
    ax.legend(fontsize=7, loc="lower right")
    save(fig, "dc_heating.png")


def fig_mismatch(sd):
    rows = [r for r in sd["resin_mismatch"]["rows"] if "e_true_kj" in r]
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    for alt, col in (("C2", C[2]), ("C4", C[4])):
        rr = [r for r in rows if r["alt"] == alt]
        ax.plot([r["e_true_kj"] for r in rr], [r["teq_min_cold_true"] for r in rr], marker="o", ms=5, color=col, label=alt)
    e_ds = sd["resin"]["e_cure_kj"]
    ax.axvline(e_ds, color=INK2, lw=0.8, ls=":")
    ax.text(e_ds + 0.7, 92, f"data sheet {e_ds:.1f}", fontsize=7, color=INK2)
    ax.axvspan(29.7, e_ds, color=GRID, alpha=0.6, lw=0)
    ax.text(31, 46, "range the trickle\nschedules allow", fontsize=7, color=INK2)
    ax.axhline(60, color=LIMIT, lw=1, ls="--")
    ax.set_xlabel("true activation energy of the resin (kJ/mol); controller keeps the data-sheet value")
    ax.set_ylabel("true t_eq, coldest resin (min)")
    ax.set_title("Is the 1.10 margin enough if the resin is not the data sheet?", fontsize=9, loc="left")
    ax.legend(fontsize=7)
    save(fig, "resin_mismatch.png")


def fig_dielectric(sd):
    d = sd["dielectric"]
    fig, ax = plt.subplots(figsize=(6.0, 3.3))
    t = [r["t_c"] for r in d["rows"]]
    ax.semilogy(t, [r["k_ieee"] for r in d["rows"]], marker="o", ms=5, color=C[0], label="IEEE 43 rule: ×2 per 10 K")
    ax.semilogy(t, [r["k_resin"] for r in d["rows"]], marker="o", ms=5, color=C[1],
                label=f"cured-resin data, E = {d['e_resin_kj']:.0f} kJ/mol")
    ax.set_xlabel("winding temperature at the IR test (°C)")
    ax.set_ylabel("factor to refer IR to 40 °C")
    ax.set_title("Insulation-resistance temperature correction", fontsize=9, loc="left")
    ax.legend(fontsize=7)
    save(fig, "ir_correction.png")


def main():
    p, _ = load_config()
    sd = json.loads((ROOT / "results" / "studies.json").read_text())
    tr = json.loads((ROOT / "results" / "traces.json").read_text())
    fig_fields(tr, sd)
    fig_cycles(tr, sd)
    fig_cure_time(sd)
    fig_capacity(sd)
    fig_resin(p)
    fig_tornado(sd)
    fig_probe_fault(tr, sd)
    fig_dc(tr, sd)
    fig_mismatch(sd)
    fig_dielectric(sd)
    print("figures in", IMG)


if __name__ == "__main__":
    main()
