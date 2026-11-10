"""Resin cure and flow models, calibrated only on the resin's public data sheet [R1].

Cure state: equivalent cure time and gel index.
  The data sheet gives two dip-and-bake schedules that it treats as equivalent: 2 h at 130 C and 1 h at 150 C,
  counted from the moment the part reaches the temperature. If the cure follows one Arrhenius rate law with
  activation energy E, every schedule is equivalent to an isothermal time at a reference temperature:

      t_eq = integral exp(-E/R (1/T - 1/T_ref)) dt                                      (time-temperature superposition)

  and the two schedules fix E = R ln(t130/t150) / (1/T130 - 1/T150) = 49.2 kJ/mol [D]. This is the isoconversional
  ("model-free") view of thermoset kinetics [K1]: at a fixed conversion, t(T) scales as exp(E/RT), whatever the
  reaction model f(alpha). So the requirement "every resin point gets at least the cure of 1 h at 150 C" becomes
  t_eq >= 60 min at T_ref = 150 C, and it does not depend on a reaction model.

  Gelation happens at a fixed conversion, so its time scales with the same law [K1]. With the data sheet's gel time
  at 100 C (7-13 min), the gel index G = integral dt / t_gel(T) reaches 1 at gelation. Using the cure E for the gel
  is an assumption [A], varied in the sensitivity study.

Flow before gelation: Vogel-Fulcher-Tammann fit of the data sheet's viscosity curve (10-80 C) [D]:
  ln eta = A + B / (T - T0). Above 80 C it is an extrapolation [A]; the rise of viscosity with conversion before the
  gel point is ignored, which over-estimates drainage (conservative).

Drainage of the end-winding film: a liquid film on a vertical wall thins as h(x, t) = sqrt(eta x / (rho g t))
(Jeffreys, 1930 [K2]). With a time-varying viscosity the same similarity solution holds with t/eta replaced by the
fluidity integral Phi = integral dt / eta, so the retained film at gelation is h = sqrt(x / (rho g Phi)) [D].
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares

R_GAS = 8.314462618
K0 = 273.15


def activation_energy_from_schedules(schedules: list[list[float]]) -> float:
    """E [J/mol] from two equivalent isothermal schedules [[T1 C, t1], [T2 C, t2]] (any time unit)."""
    (t1c, d1), (t2c, d2) = schedules[:2]
    return R_GAS * math.log(d1 / d2) / (1 / (t1c + K0) - 1 / (t2c + K0))


def arrhenius_factor(t_c, e_j: float, t_ref_c: float):
    """Rate at T relative to the rate at T_ref."""
    return np.exp(-e_j / R_GAS * (1.0 / (np.asarray(t_c) + K0) - 1.0 / (t_ref_c + K0)))


@dataclass
class Resin:
    e_cure: float              # J/mol
    t_ref: float               # C
    gel_t100_s: float          # s, gel time at 100 C
    vft: tuple[float, float, float]   # A, B, T0 (K): ln(eta / Pa s) = A + B / (T - T0)
    rho: float

    @classmethod
    def from_config(cls, rc: dict, t_ref: float, e_override: float | None = None, gel_override_min: float | None = None):
        e = e_override or activation_energy_from_schedules(rc["cure_schedules_h"])
        return cls(e_cure=e, t_ref=t_ref, gel_t100_s=60.0 * (gel_override_min or rc["gel_time_100c_min"]),
                   vft=fit_vft(rc["viscosity_points"]), rho=rc["density"])

    def teq_rate(self, t_c):
        """d t_eq / d t (dimensionless): equivalent seconds at T_ref per real second."""
        return arrhenius_factor(t_c, self.e_cure, self.t_ref)

    def gel_rate(self, t_c):
        """d G / d t [1/s]."""
        return arrhenius_factor(t_c, self.e_cure, 100.0) / self.gel_t100_s

    def viscosity(self, t_c):
        a, b, t0 = self.vft
        return np.exp(a + b / (np.asarray(t_c) + K0 - t0))

    def gel_time_s(self, t_c: float) -> float:
        return 1.0 / float(self.gel_rate(t_c))

    def isothermal_cure_s(self, t_c: float, teq_target_s: float) -> float:
        return teq_target_s / float(self.teq_rate(t_c))


def fit_vft(points: list[list[float]]) -> tuple[float, float, float]:
    t = np.array([p[0] for p in points], float) + K0
    ln_eta = np.log(np.array([p[1] for p in points], float) * 1e-3)

    def res(p):
        a, b, t0 = p
        return a + b / (t - t0) - ln_eta

    sol = least_squares(res, x0=[-8.0, 900.0, 170.0], bounds=([-30, 10, 0], [10, 10000, 270]))
    return tuple(float(x) for x in sol.x)


def retained_film_um(phi: float, height_m: float, rho: float) -> float:
    """Jeffreys film thickness [um] after a fluidity integral phi [s/(Pa s)] on a vertical height."""
    return 1e6 * math.sqrt(height_m / (rho * 9.81 * max(phi, 1e-12)))


def resistivity_arrhenius(points: list[list[float]]) -> tuple[float, float]:
    """Fit log10(rho) = a + b/T to the data-sheet midpoints; returns (activation energy J/mol, log10 rho at 40 C)."""
    t = np.array([p[0] for p in points], float) + K0
    lr = np.array([(p[1] + p[2]) / 2 for p in points], float)
    b, a = np.polyfit(1 / t, lr, 1)
    e = b * math.log(10) * R_GAS
    return float(e), float(a + b / (40 + K0))


def ieee43_kt(t_c):
    """Approximate IEEE 43 temperature correction to 40 C: insulation resistance halves for every 10 K rise [S]."""
    return 0.5 ** ((40.0 - np.asarray(t_c)) / 10.0)
