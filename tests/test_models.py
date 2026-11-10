"""Model verification: each model is checked against an independent result (closed form, textbook solution,
data-sheet point or a conservation law). DOSSIER §10.2 lists the checks."""
import math

import numpy as np
import pytest
from scipy.optimize import brentq

from cure.plant import load_config
from cure.resin import (Resin, activation_energy_from_schedules, fit_vft, ieee43_kt, resistivity_arrhenius,
                        retained_film_um)
from cure.thermal import Geometry, Model

P, S = load_config()


def resin():
    return Resin.from_config(P["resin"], P["limits"]["t_ref"])


# ---------------------------------------------------------------- resin
def test_activation_energy_reproduces_both_datasheet_schedules():
    r = resin()
    assert 45e3 < r.e_cure < 55e3
    for t_c, hours in P["resin"]["cure_schedules_h"]:
        assert r.isothermal_cure_s(t_c, 3600.0) == pytest.approx(hours * 3600, rel=1e-9)


def test_trickle_schedules_imply_a_lower_or_equal_activation_energy():
    """The trickle ranges (15-30 min at 130 C, 10-15 min at 150 C) bracket E at or below the dip-and-bake value,
    so the dip-and-bake E is the conservative end (cold points count less)."""
    (t1, lo1, hi1), (t2, lo2, hi2) = P["resin"]["trickle_schedules_min"]
    e_lo = activation_energy_from_schedules([[t1, lo1], [t2, hi2]])
    e_hi = activation_energy_from_schedules([[t1, hi1], [t2, lo2]])
    e = resin().e_cure
    assert e_lo <= e + 1 and e <= e_hi + 1


def test_gel_time_at_100c_is_the_datasheet_value():
    assert resin().gel_time_s(100.0) == pytest.approx(600.0, rel=1e-9)


def test_isothermal_equivalent_time_is_real_time_at_tref():
    r = resin()
    assert float(r.teq_rate(150.0)) == pytest.approx(1.0)


def test_vft_fit_reproduces_the_datasheet_viscosity_curve():
    r = resin()
    for t_c, mpas in P["resin"]["viscosity_points"]:
        assert float(r.viscosity(t_c)) * 1e3 == pytest.approx(mpas, rel=0.12)


def test_jeffreys_film_is_the_constant_viscosity_similarity_solution():
    """h = sqrt(eta x / (rho g t)) for constant viscosity [Jeffreys 1930]."""
    eta, x, rho, t = 0.05, 0.1, 1100.0, 600.0
    assert retained_film_um(t / eta, x, rho) == pytest.approx(1e6 * math.sqrt(eta * x / (rho * 9.81 * t)))


def test_ieee43_correction_halves_per_10k():
    assert float(ieee43_kt(50.0)) == pytest.approx(2.0)
    assert float(ieee43_kt(30.0)) == pytest.approx(0.5)


def test_resistivity_fit_is_decreasing_with_temperature():
    e, lr40 = resistivity_arrhenius(P["resin"]["resistivity_points"])
    assert e > 0 and lr40 > 11


# ---------------------------------------------------------------- thermal model
def _iron_core(h_sides, h=25.0, nz=40, kin=1e4):
    mat = {**P["materials"], "steel": {**P["materials"]["steel"], "k_in_plane": kin}}
    g = Geometry.from_config("M", S["M"])
    return Model(g, mat, P["oven"], h=h, no_winding=True, n_z=nz, h_sides=h_sides), mat


def test_lumped_limit_matches_newton_cooling():
    """Very high conductivity everywhere: one time constant C/(hA), T = Tair + (T0 - Tair) exp(-t/tau)."""
    mat = {**P["materials"], "steel": {**P["materials"]["steel"], "k_in_plane": 1e5, "k_through_stack": 1e5}}
    m = Model(Geometry.from_config("M", S["M"]), mat, P["oven"], no_winding=True)
    tau = m.heat_capacity() / m.g_air.sum()
    temp = np.full(m.n, 25.0)
    dt = 5.0
    for _ in range(int(tau / dt)):
        temp = m.step(temp, 160.0, dt)
    expected = 160.0 - 135.0 * math.exp(-1.0)
    assert temp.mean() == pytest.approx(expected, abs=0.6)


def test_axial_slab_matches_heisler_series_solution():
    """Iron core heated only through its end faces (bore and OD adiabatic, radial k very high): a plane wall of
    half-thickness L = l_core/2 with convection at z = L and symmetry at z = 0. The mid-plane temperature follows
    the one-term-plus series solution with zeta_n tan zeta_n = Bi (Incropera, ch. 5)."""
    m, mat = _iron_core({"od": 0.0, "bore": 0.0}, h=40.0, nz=60)
    st = mat["steel"]
    half = S["M"]["l_core"] / 2
    a = st["k_through_stack"] / (st["rho"] * st["cp"])
    bi = 40.0 * half / st["k_through_stack"]
    zetas = [brentq(lambda z: z * math.tan(z) - bi, n * math.pi + 1e-9, n * math.pi + math.pi / 2 - 1e-9) for n in range(30)]
    t_end = 4 * 3600.0
    fo = a * t_end / half ** 2
    temp = np.full(m.n, 25.0)
    dt = 20.0
    for _ in range(int(t_end / dt)):
        temp = m.step(temp, 160.0, dt)
    fe, _ = m.field(temp)
    # node closest to the mid-plane is at z = dz/2; compare with the series at that z
    z0 = m.zc[0]
    theta_z = sum(4 * math.sin(z) / (2 * z + math.sin(2 * z)) * math.exp(-z * z * fo) * math.cos(z * z0 / half) for z in zetas)
    t_series = 160.0 - 135.0 * theta_z
    assert fe[:, 0].mean() == pytest.approx(t_series, abs=1.0)


def test_energy_balance_with_joule_heating():
    g = Geometry.from_config("M", S["M"])
    m = Model(g, P["materials"], P["oven"])
    temp = np.full(m.n, 25.0)
    dt, t_air = 10.0, 120.0
    e_in = 0.0
    for _ in range(600):
        q = m.joule(temp, 1500.0)
        e_in += q.sum() * dt
        t_new = m.step(temp, t_air, dt, q)
        e_in += float(np.sum(m.g_air * (t_air - t_new))) * dt      # backward Euler uses the end-of-step value
        temp = t_new
    du = float(np.sum(m.cap * (temp - 25.0)))
    assert du == pytest.approx(e_in, rel=1e-6)


def test_resistance_thermometer_reads_mean_copper_temperature():
    """IEC 60034-1 rule T = R/R0 (235 + T0) - 235 against the copper-volume mean of the model."""
    g = Geometry.from_config("M", S["M"])
    m = Model(g, P["materials"], P["oven"])
    temp = np.linspace(40.0, 170.0, m.n)
    r0 = m.r_measured(np.full(m.n, 20.0))
    t_est = m.r_measured(temp) / r0 * (235.0 + 20.0) - 235.0
    assert t_est == pytest.approx(m.t_cu_mean(temp), abs=0.5)


def test_series_resistance_matches_hand_calculation():
    """M stator: 48 slots x 6 conductors / 3 = 96 conductors per phase, 4 paths, conductor length
    l_core + l_end_turn = 0.85 m, conductor area 14 x 58 x 0.45 / 6 mm2."""
    g = Geometry.from_config("M", S["M"])
    a_c = 0.014 * 0.058 * 0.45 / 6
    r_path = 1.72e-8 * (96 / 4) * 0.85 / a_c
    assert g.r_series_20(1.72e-8) == pytest.approx(3 * r_path / 4, rel=1e-12)


def test_steady_state_equals_air_temperature_without_sources():
    m = Model(Geometry.from_config("S", S["S"]), P["materials"], P["oven"])
    temp = np.full(m.n, 25.0)
    for _ in range(200):
        temp = m.step(temp, 150.0, 600.0)
    assert np.allclose(temp, 150.0, atol=1e-6)
