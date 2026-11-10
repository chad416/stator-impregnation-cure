(*
  OVEN_CURE: cure-cycle controller of a stator impregnation oven.

  IEC 61131-3 Structured Text. Compiled with matiec (iec2c) and run once per scan against the
  finite-volume stator and oven model (cure/plant.py), so the closed-loop results come from this code.

  End criteria (rcMode)
    0  FIXED     air set point for a fixed time from the start
    1  SOAK      timer starts when the slot probe is within rcSoakBand of rcTref
    2  CUREIDX   equivalent cure time at the slot probe, t_eq = integral exp(-E/R (1/T - 1/Tref)) dt,
                 reaches rcTeqTarget * rcMargin (and the end-winding probe has gelled)
    3  JOULE     as 2, plus DC heating of the winding during heat-up. The mean copper temperature comes from
                 the winding resistance: T = R / R0 * (235 + T0) - 235 (IEC 60034-1 copper rule)
    4  BOOST     as 2, with a high air set point until the slot probe reaches rcBoostEnd

  Probe plausibility: a first-order observer of the slot temperature, driven by the air temperature
  and the DC power, with the stator time constant and heat capacity from the recipe. A probe that reads
  more than rcPlausBand above the observer has left its slot (it reads air). The controller then raises
  an alarm and falls back to the fixed time rcFallbackMin from the start: never an early end.

  Conventions: rc* recipe, m* measurements, q* commands. Times in minutes unless named *S.
*)

FUNCTION ARRH : REAL
VAR_INPUT
  tC : REAL;      (* temperature, C *)
  eJ : REAL;      (* activation energy, J/mol *)
  tRefC : REAL;   (* reference temperature, C *)
END_VAR
  ARRH := EXP(-eJ / 8.314462 * (1.0 / (tC + 273.15) - 1.0 / (tRefC + 273.15)));
END_FUNCTION

FUNCTION_BLOCK OVEN_CURE
VAR_INPUT
  cycleTime : REAL;          (* scan time, s *)
  cmdStart : BOOL;
  cmdAbort : BOOL;
  cmdReset : BOOL;
  doorClosed : BOOL;
  fanOk : BOOL;
  mTAir : REAL;              (* oven air, C *)
  mTProbe : REAL;            (* thermocouple at the slot bottom, mid-stack, C *)
  mTProbeEw : REAL;          (* thermocouple on the end winding, C *)
  mRWind : REAL;             (* series winding resistance from the DC supply, ohm (0 = not measured) *)
  mIdc : REAL;               (* DC supply current, A *)
  mUdc : REAL;               (* DC supply voltage, V *)
  rcMode : INT;
  rcTAir : REAL;
  rcTAirBoost : REAL;
  rcBoostEnd : REAL;
  rcFixedMin : REAL;
  rcSoakMin : REAL;
  rcSoakBand : REAL;
  rcTref : REAL;
  rcEa : REAL;
  rcTeqTarget : REAL;        (* min at rcTref *)
  rcMargin : REAL;
  rcGelT100 : REAL;          (* gel time at 100 C, min *)
  rcR0 : REAL;               (* cold winding resistance, ohm *)
  rcT0 : REAL;               (* temperature of the cold measurement, C *)
  rcTcuSet : REAL;           (* mean copper set point during DC heating, C *)
  rcTcuMax : REAL;           (* mean copper trip, C *)
  rcIdcMax : REAL;
  rcDcOffBand : REAL;
  rcTauObs : REAL;           (* stator time constant for the observer, s *)
  rcCthObs : REAL;           (* stator heat capacity for the observer, J/K *)
  rcPlausBand : REAL;
  rcFallbackMin : REAL;
  rcTAirMax : REAL;
  rcDcMeanOnly : BOOL;       (* study switch: control the DC heating on the mean copper temperature only (D6) *)
END_VAR
VAR_OUTPUT
  qAirSet : REAL;
  qHeaterEnable : BOOL;
  qIdcSet : REAL;
  qRotate : BOOL;
  qDoorLock : BOOL;
  state : INT;
  elapsedMin : REAL;
  teqProbe : REAL;           (* min at rcTref *)
  gelProbe : REAL;
  gelEw : REAL;
  tCuMean : REAL;
  tObs : REAL;
  cureDone : BOOL;
  fallback : BOOL;
  alarms : DWORD;
END_VAR
VAR CONSTANT
  S_IDLE : INT := 0;
  S_HEAT : INT := 1;
  S_CURE : INT := 2;
  S_DONE : INT := 3;
  S_FAULT : INT := 9;
  A_PROBE : INT := 0;
  A_CU_OVERTEMP : INT := 1;
  A_AIR_OVERTEMP : INT := 2;
  A_DOOR : INT := 3;
  A_FAN : INT := 4;
  A_ABORT : INT := 5;
END_VAR
VAR
  soakMin : REAL;
  dtMin : REAL;
  iInt : REAL;
  iCmd : REAL;
  pDc : REAL;
  endReached : BOOL;
  probeOk : BOOL;
  boostOn : BOOL;
  dcPhase : BOOL;
  tDcCtl : REAL;
END_VAR

  dtMin := cycleTime / 60.0;

  (* resistance thermometer: mean copper temperature *)
  IF mRWind > 0.0 AND rcR0 > 0.0 THEN
    tCuMean := mRWind / rcR0 * (235.0 + rcT0) - 235.0;
  ELSE
    tCuMean := mTProbe;
  END_IF;

  IF cmdReset AND state <> S_HEAT AND state <> S_CURE THEN
    state := S_IDLE;
    alarms := DWORD#0;
    cureDone := FALSE;
    fallback := FALSE;
  END_IF;

  CASE state OF
    0: (* S_IDLE *)
      qAirSet := 25.0;
      qHeaterEnable := FALSE;
      qIdcSet := 0.0;
      qRotate := FALSE;
      qDoorLock := FALSE;
      IF cmdStart AND doorClosed AND fanOk THEN
        elapsedMin := 0.0;
        teqProbe := 0.0;
        gelProbe := 0.0;
        gelEw := 0.0;
        soakMin := 0.0;
        iInt := 0.0;
        tObs := mTProbe;
        cureDone := FALSE;
        fallback := FALSE;
        alarms := DWORD#0;
        boostOn := rcMode = 4;
        dcPhase := rcMode = 3;
        state := S_HEAT;
      END_IF;

    1, 2: (* S_HEAT, S_CURE: one block, the state only reports whether the cure index has started to count *)
      elapsedMin := elapsedMin + dtMin;
      qHeaterEnable := TRUE;
      qDoorLock := TRUE;

      (* air set point *)
      IF boostOn AND mTProbe >= rcBoostEnd THEN
        boostOn := FALSE;
      END_IF;
      IF boostOn THEN
        qAirSet := rcTAirBoost;
      ELSE
        qAirSet := rcTAir;
      END_IF;

      (* DC heating of the winding: PI on the mean copper temperature, until the slot probe is near Tref *)
      IF dcPhase AND mTProbe >= rcTref - rcDcOffBand THEN
        dcPhase := FALSE;
      END_IF;
      (* controlled temperature: the hotter of the mean copper (resistance) and the end-winding probe. The mean
         alone hides the end winding, which has no iron to lose its heat to (decision D6). *)
      IF rcDcMeanOnly THEN
        tDcCtl := tCuMean;
      ELSE
        tDcCtl := MAX(tCuMean, mTProbeEw);
      END_IF;
      IF dcPhase THEN
        iInt := iInt + 15.0 * (rcTcuSet - tDcCtl) * dtMin;
        (* anti-windup: the integral never exceeds the current the supply actually delivers (voltage limit) *)
        iInt := LIMIT(0.0, iInt, MIN(rcIdcMax, mIdc + 50.0));
        iCmd := LIMIT(0.0, 30.0 * (rcTcuSet - tDcCtl) + iInt, rcIdcMax);
        qIdcSet := iCmd;
      ELSE
        qIdcSet := 0.0;
        iInt := 0.0;
      END_IF;

      (* cure and gel indices at the probes *)
      teqProbe := teqProbe + dtMin * ARRH(tC := mTProbe, eJ := rcEa, tRefC := rcTref);
      gelProbe := gelProbe + dtMin / rcGelT100 * ARRH(tC := mTProbe, eJ := rcEa, tRefC := 100.0);
      gelEw := gelEw + dtMin / rcGelT100 * ARRH(tC := mTProbeEw, eJ := rcEa, tRefC := 100.0);
      qRotate := gelEw < 1.0 OR gelProbe < 1.0;     (* rotate until the resin has gelled at both probes *)
      IF teqProbe > 0.5 THEN
        state := S_CURE;
      END_IF;

      (* probe plausibility observer *)
      pDc := mIdc * mUdc;
      tObs := tObs + cycleTime * ((mTAir - tObs) / rcTauObs + pDc / rcCthObs);
      probeOk := mTProbe <= tObs + rcPlausBand;
      IF NOT probeOk AND NOT fallback THEN
        fallback := TRUE;
        alarms := alarms OR SHL(DWORD#1, A_PROBE);
      END_IF;

      (* end criterion *)
      IF fallback THEN
        endReached := elapsedMin >= rcFallbackMin;
      ELSIF rcMode = 0 THEN
        endReached := elapsedMin >= rcFixedMin;
      ELSIF rcMode = 1 THEN
        IF mTProbe >= rcTref - rcSoakBand THEN
          soakMin := soakMin + dtMin;
        END_IF;
        endReached := soakMin >= rcSoakMin;
      ELSE
        endReached := teqProbe >= rcTeqTarget * rcMargin AND gelEw >= 1.0;
      END_IF;
      IF endReached THEN
        cureDone := TRUE;
        state := S_DONE;
      END_IF;

      (* protection *)
      IF tCuMean > rcTcuMax THEN
        alarms := alarms OR SHL(DWORD#1, A_CU_OVERTEMP);
        dcPhase := FALSE;
        qIdcSet := 0.0;
      END_IF;
      IF mTAir > rcTAirMax THEN
        alarms := alarms OR SHL(DWORD#1, A_AIR_OVERTEMP);
        state := S_FAULT;
      END_IF;
      IF NOT doorClosed THEN
        alarms := alarms OR SHL(DWORD#1, A_DOOR);
        state := S_FAULT;
      END_IF;
      IF NOT fanOk THEN
        alarms := alarms OR SHL(DWORD#1, A_FAN);
        state := S_FAULT;
      END_IF;
      IF cmdAbort THEN
        alarms := alarms OR SHL(DWORD#1, A_ABORT);
        state := S_FAULT;
      END_IF;

    3: (* S_DONE: heaters off, unload *)
      qHeaterEnable := FALSE;
      qAirSet := 25.0;
      qIdcSet := 0.0;
      qRotate := FALSE;
      qDoorLock := FALSE;

    9: (* S_FAULT: safe state, operator reset *)
      qHeaterEnable := FALSE;
      qAirSet := 25.0;
      qIdcSet := 0.0;
      qRotate := FALSE;
      qDoorLock := FALSE;
  END_CASE;

  (* output stage: a transition to DONE or FAULT takes the heaters off in the same scan *)
  IF state = S_DONE OR state = S_FAULT OR state = S_IDLE THEN
    qHeaterEnable := FALSE;
    qIdcSet := 0.0;
    qRotate := FALSE;
    qAirSet := 25.0;
  END_IF;

END_FUNCTION_BLOCK
