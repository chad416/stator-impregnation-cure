"""Tests of the compiled ST controller (build/plc/libplc.so), alone and in the closed loop.
Skipped when the library is not built (Windows without WSL); CI builds it with matiec."""
import math

import pytest

from cure import plc as plcmod
from cure.plant import Scenario, run_cycle

pytestmark = pytest.mark.skipif(not plcmod.available(), reason="build/plc/libplc.so not built (scripts/build_plc.sh)")

FIXED = 365.0


@pytest.fixture(scope="module")
def plc():
    return plcmod.Plc()


def _start(plc, **over):
    plc.reset()
    base = dict(cycleTime=10.0, doorClosed=True, fanOk=True, mTAir=25.0, mTProbe=25.0, mTProbeEw=25.0, rcMode=2,
                rcTAir=160.0, rcTref=150.0, rcEa=49157.6, rcTeqTarget=60.0, rcMargin=1.1, rcGelT100=10.0,
                rcFixedMin=FIXED, rcFallbackMin=FIXED, rcTauObs=4700.0, rcCthObs=516e3, rcPlausBand=25.0,
                rcTAirMax=200.0, rcR0=4.32e-3, rcT0=25.0, rcTcuSet=160.0, rcTcuMax=175.0, rcIdcMax=2000.0,
                rcDcOffBand=5.0, rcSoakMin=60.0, rcSoakBand=5.0)
    base.update(over)
    plc.set(**base, cmdStart=True)
    plc.scan()
    plc.set(cmdStart=False)


def test_isothermal_cure_index_is_real_time_at_tref(plc):
    """Probe held at 150 C: t_eq grows 1 min per min; the cycle ends at 60 x 1.10 = 66 min (gel long reached)."""
    _start(plc, mTAir=150.0, mTProbe=150.0, mTProbeEw=150.0, rcTauObs=1.0)
    n = 0
    while not plc.get("CUREDONE") and n < 2000:
        plc.scan()
        n += 1
    assert plc.get("ELAPSEDMIN") == pytest.approx(66.0, abs=10 / 60 + 1e-3)
    assert plc.get("TEQPROBE") == pytest.approx(66.0, abs=0.2)


def test_cure_index_at_130c_follows_the_datasheet_equivalence(plc):
    """2 h at 130 C is 1 h at 150 C (E from the data sheet): the end comes at 2 h x 1.10."""
    _start(plc, mTAir=130.0, mTProbe=130.0, mTProbeEw=130.0, rcTauObs=1.0)
    n = 0
    while not plc.get("CUREDONE") and n < 5000:
        plc.scan()
        n += 1
    assert plc.get("ELAPSEDMIN") == pytest.approx(132.0, rel=0.003)


def test_resistance_thermometer(plc):
    _start(plc, mIdc=500.0, mUdc=2.0, mRWind=4.32e-3 * (235.0 + 140.0) / (235.0 + 25.0), rcMode=3)
    plc.scan()
    assert plc.get("TCUMEAN") == pytest.approx(140.0, abs=1e-3)


def test_door_open_trips_to_fault(plc):
    _start(plc)
    plc.set(doorClosed=False)
    plc.scan()
    assert plc.get("STATE") == 9 and plc.get("ALARMS") & (1 << plc.constants["A_DOOR"])
    assert not plc.get("QHEATERENABLE") and plc.get("QIDCSET") == 0.0


def test_fixed_mode_ends_at_the_fixed_time(plc):
    _start(plc, rcMode=0, rcFixedMin=30.0)
    for _ in range(int(30 * 6) + 1):
        plc.scan()
    assert plc.get("CUREDONE") and math.isclose(plc.get("ELAPSEDMIN"), 30.0, abs_tol=0.2)


def test_closed_loop_online_index_matches_offline_integral(plc):
    """The ST controller integrates t_eq from the (lagged) probe; the plant integrates it from the true node
    temperature with numpy. Two independent implementations must agree within 1 %."""
    r = run_cycle(plc, "M", "C2", FIXED, keep_trace=False)
    assert r["done"] and not r["fallback"]
    assert r["teq_probe_plc"] == pytest.approx(r["teq_probe_true"], rel=0.01)
    assert r["cold_node_is_probe"]


def test_detached_probe_falls_back_to_the_safe_recipe(plc):
    r = run_cycle(plc, "M", "C2", FIXED, Scenario(probe_detach_min=30.0), keep_trace=False)
    assert r["fallback"] and r["teq_min_cold"] >= 60.0


def test_dc_heating_keeps_the_end_winding_below_class_h(plc):
    r = run_cycle(plc, "S", "C4", FIXED, keep_trace=False)
    assert r["done"] and r["t_max_object"] <= 180.0 and r["teq_min_cold"] >= 60.0
