"""Two-medium axisymmetric (r, z) finite-volume thermal model of a wound stator core in a curing oven.

Media
  iron    laminated core: yoke + teeth. In-plane conductivity k_in (radial), through-stack k_ax (axial). In the slot
          zone only the tooth share f_t(r) = 1 - Q w_s / (2 pi r) of each ring is iron.
  bundle  the winding: copper + insulation + resin. It fills the slot share f_s = 1 - f_t of the slot zone and goes on
          into the end winding beyond the core. Along the conductors it conducts like copper (fill * k_cu); across
          them only like an impregnated bundle (k_tr). The end winding carries k_ew = l_end_turn / l_ew_axial more
          conductor per axial length (the coils run round the circumference), so its heat capacity is k_ew times,
          and its axial conductance 1/k_ew times, that of a slot cell.
  The two media exchange heat through the slot walls: U = 1 / (t_liner / k_liner + (w_s/2) / (3 k_tr)) per wall
  area, two side walls per slot plus the slot bottom [D] (conduction from the bundle mean to the wall of a slab
  with uniform heating has the resistance (w/2)/(3k)).

Boundaries (one half of the core length, symmetry at mid-stack z = 0)
  convection h to the oven air on the outer diameter, the bore (teeth; the slot mouths only through the wedges),
  the core end face, and every end-winding surface (envelope area times a wetting factor for the gaps).

Sources: Joule heating of the copper when a DC current flows through the series-connected phases; the copper
resistivity rises with temperature (IEC 60028 alpha20) [S].

Time integration: backward Euler with one sparse LU factorisation per run (constant conductances); the Joule source
is taken at the start of each step.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla


@dataclass
class Geometry:
    name: str
    d_bore: float
    d_out: float
    l_core: float
    slots: int
    slot_w: float
    slot_d: float
    fill: float
    cond_per_slot: int
    paths: int
    l_end_turn: float
    l_ew_axial: float
    rating_kva: float = 0.0

    @classmethod
    def from_config(cls, name: str, d: dict) -> "Geometry":
        return cls(name=name, **d)

    @property
    def k_ew(self) -> float:
        return self.l_end_turn / self.l_ew_axial

    @property
    def a_cond(self) -> float:
        """Copper area of one conductor [m2]."""
        return self.slot_w * self.slot_d * self.fill / self.cond_per_slot

    def r_series_20(self, rho20: float) -> float:
        """DC resistance at 20 C of the three phases in series [ohm]."""
        z_phase = self.slots * self.cond_per_slot / 3          # conductors (turn sides) per phase
        l_path = z_phase / self.paths * (self.l_core + self.l_end_turn)
        r_path = rho20 * l_path / self.a_cond
        return 3 * r_path / self.paths


@dataclass
class Model:
    geo: Geometry
    mat: dict
    oven: dict
    h: float | None = None
    n_slot: int = 6
    n_yoke: int = 8
    n_z: int = 12
    n_ew: int = 5
    no_winding: bool = False                 # iron-only core (verification tests)
    h_sides: dict | None = None              # per-surface h override (verification tests)
    nodes: dict = field(default_factory=dict)

    def __post_init__(self):
        self.build()

    # ------------------------------------------------------------------ assembly
    def build(self):
        g, m, ov = self.geo, self.mat, self.oven
        h = self.h if self.h is not None else ov["h_conv"]
        hs = {"od": h, "bore": h, "end": h, "ew": h}
        hs.update(self.h_sides or {})
        rb, ro = g.d_bore / 2, g.d_out / 2
        rs = rb + g.slot_d
        r_edges = np.concatenate([np.linspace(rb, rs, self.n_slot + 1), np.linspace(rs, ro, self.n_yoke + 1)[1:]])
        z_edges = np.linspace(0.0, g.l_core / 2, self.n_z + 1)
        ze = np.linspace(g.l_core / 2, g.l_core / 2 + g.l_ew_axial, self.n_ew + 1)[1:]
        rc = 0.5 * (r_edges[1:] + r_edges[:-1])
        dr = np.diff(r_edges)
        zc = 0.5 * (z_edges[1:] + z_edges[:-1])
        dz = np.diff(z_edges)
        zb_edges = np.concatenate([z_edges, ze])
        zbc = 0.5 * (zb_edges[1:] + zb_edges[:-1])
        dzb = np.diff(zb_edges)
        nr, nz, ns = len(rc), len(zc), self.n_slot
        nzb = len(zbc)
        self.rc, self.zc, self.zbc, self.r_edges, self.z_edges, self.zb_edges = rc, zc, zbc, r_edges, z_edges, zb_edges

        def f_slot(r):
            return 0.0 if self.no_winding else g.slots * g.slot_w / (2 * math.pi * r)

        st, cu, ins = m["steel"], m["copper"], m["insulation"]
        rc_fe = st["rho"] * st["cp"]
        rc_b = g.fill * cu["rho"] * cu["cp"] + (1 - g.fill) * ins["rho"] * ins["cp"]
        k_tr = m["winding_k_transverse"]
        u_wall = 1.0 / (m["liner"]["thickness"] / m["liner"]["k"] + (g.slot_w / 2) / (3 * k_tr))
        self.u_wall = u_wall

        idx_fe = -np.ones((nr, nz), int)
        n = 0
        for i in range(nr):
            for j in range(nz):
                idx_fe[i, j] = n
                n += 1
        idx_b = -np.ones((ns, nzb), int)
        if not self.no_winding:
            for i in range(ns):
                for j in range(nzb):
                    idx_b[i, j] = n
                    n += 1
        self.n = n
        self.idx_fe, self.idx_b = idx_fe, idx_b
        cap = np.zeros(n)
        rows, cols, vals = [], [], []
        g_air = np.zeros(n)               # conductance of each node to the oven air
        v_cu = np.zeros(n)                # copper volume in each node (Joule source distribution)
        self.is_ew = np.zeros(n, bool)

        def couple(a, b, gab):
            rows.extend([a, b, a, b])
            cols.extend([a, b, b, a])
            vals.extend([gab, gab, -gab, -gab])

        two_pi = 2 * math.pi
        # iron
        for i in range(nr):
            ft = 1.0 - (f_slot(rc[i]) if i < ns else 0.0)
            for j in range(nz):
                v = two_pi * rc[i] * dr[i] * dz[j]
                cap[idx_fe[i, j]] = rc_fe * ft * v
                if i + 1 < nr:
                    rf = r_edges[i + 1]
                    ff = 1.0 - f_slot(rf) if i < ns else 1.0     # up to the slot bottom only the teeth conduct
                    gr = st["k_in_plane"] * two_pi * rf * dz[j] * ff / (rc[i + 1] - rc[i])
                    couple(idx_fe[i, j], idx_fe[i + 1, j], gr)
                if j + 1 < nz:
                    gz = st["k_through_stack"] * two_pi * rc[i] * dr[i] * ft / (zc[j + 1] - zc[j])
                    couple(idx_fe[i, j], idx_fe[i, j + 1], gz)
            # end face of the core (iron share only; the bundle carries on into the end winding)
            g_air[idx_fe[i, nz - 1]] += hs["end"] * two_pi * rc[i] * dr[i] * ft
        for j in range(nz):
            g_air[idx_fe[nr - 1, j]] += hs["od"] * two_pi * r_edges[-1] * dz[j]
            g_air[idx_fe[0, j]] += hs["bore"] * two_pi * r_edges[0] * dz[j] * (1.0 - f_slot(r_edges[0]))
        # bundle
        if not self.no_winding:
            kew = g.k_ew
            for i in range(ns):
                fs = f_slot(rc[i])
                for j in range(nzb):
                    ew = j >= nz
                    a = idx_b[i, j]
                    self.is_ew[a] = ew
                    v = two_pi * rc[i] * dr[i] * dzb[j] * fs * (kew if ew else 1.0)
                    cap[a] = rc_b * v
                    v_cu[a] = g.fill * v
                    if j + 1 < nzb:
                        # along the conductors: series of two half cells, each with its own path length factor
                        k1 = kew if ew else 1.0
                        k2 = kew if (j + 1) >= nz else 1.0
                        area = two_pi * rc[i] * dr[i] * fs * g.fill
                        res = (dzb[j] / 2 * k1 + dzb[j + 1] / 2 * k2) / (m["copper"]["k"] * area)
                        couple(a, idx_b[i, j + 1], 1.0 / res)
                    if i + 1 < ns:
                        rf = r_edges[i + 1]
                        gr = k_tr * two_pi * rf * dzb[j] * f_slot(rf) * (kew if ew else 1.0) / (rc[i + 1] - rc[i])
                        couple(a, idx_b[i + 1, j], gr)
                    if not ew:
                        # slot walls: two sides per slot over dr, plus the slot bottom for the deepest cell
                        area_w = 2 * g.slots * dr[i] * dzb[j]
                        if i == ns - 1:
                            area_w += g.slots * g.slot_w * dzb[j]
                        couple(a, idx_fe[i, j], u_wall * area_w)
                        if i == 0:
                            g_air[a] += hs["bore"] * two_pi * r_edges[0] * dzb[j] * f_slot(r_edges[0]) * ov["bore_wedge_factor"]
            # end-winding envelope: inner and outer cylinder + end cap, times the wetting factor; shared by cells
            r_in, r_out = r_edges[0], r_edges[ns]
            env = (two_pi * (r_in + r_out) * g.l_ew_axial + math.pi * (r_out ** 2 - r_in ** 2)) * ov["ew_surface_factor"]
            ew_nodes = [idx_b[i, j] for i in range(ns) for j in range(nz, nzb)]
            vol = np.array([cap[k] for k in ew_nodes])
            for k, w in zip(ew_nodes, vol / vol.sum()):
                g_air[k] += hs["ew"] * env * w
        k_mat = sp.csr_matrix((vals, (rows, cols)), shape=(n, n))
        self.K = k_mat + sp.diags(g_air)
        self.cap, self.g_air, self.v_cu = cap, g_air, v_cu
        self.area_conv = g_air.sum() / h if h else 0.0
        # probes and reporting nodes
        if not self.no_winding:
            self.node_probe_slot = idx_b[ns - 1, 0]                 # slot bottom, mid-stack
            self.node_probe_ew = idx_b[0, nzb - 1]                  # end-winding tip, bore-side layer (the hot spot under DC heating, D7)
            self.bundle_nodes = idx_b[idx_b >= 0].ravel()
        self.r_series_20 = g.r_series_20(m["copper"]["rho_e20"])
        self.j_per_amp = 1.0 / (g.paths * g.a_cond)
        self._lu_dt = None

    # ------------------------------------------------------------------ physics helpers
    def joule(self, temp: np.ndarray, i_dc: float) -> np.ndarray:
        cu = self.mat["copper"]
        rho = cu["rho_e20"] * (1 + cu["alpha20"] * (temp - 20.0))
        j = i_dc * self.j_per_amp
        return j * j * rho * self.v_cu

    def r_measured(self, temp: np.ndarray) -> float:
        """Series DC resistance seen by the supply: copper-volume average of the local resistivity."""
        if self.no_winding:
            return 0.0
        cu = self.mat["copper"]
        w = self.v_cu / self.v_cu.sum()
        return self.r_series_20 * float(np.sum(w * (1 + cu["alpha20"] * (temp - 20.0))))

    def t_cu_mean(self, temp: np.ndarray) -> float:
        w = self.v_cu / self.v_cu.sum()
        return float(np.sum(w * temp))

    def heat_capacity(self) -> float:
        return float(self.cap.sum())

    # ------------------------------------------------------------------ stepping
    def step(self, temp: np.ndarray, t_air: float, dt: float, q: np.ndarray | None = None) -> np.ndarray:
        if self._lu_dt != dt:
            a = sp.diags(self.cap / dt) + self.K
            self._lu = spla.splu(a.tocsc())
            self._lu_dt = dt
        rhs = self.cap / dt * temp + self.g_air * t_air
        if q is not None:
            rhs = rhs + q
        return self._lu.solve(rhs)

    def q_conv(self, temp: np.ndarray, t_air: float) -> float:
        """Heat flow from the air into the object [W]."""
        return float(np.sum(self.g_air * (t_air - temp)))

    def field(self, temp: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Iron (nr x nz) and bundle (ns x nzb) arrays of a node vector."""
        fe = temp[self.idx_fe]
        b = temp[self.idx_b] if not self.no_winding else None
        return fe, b
