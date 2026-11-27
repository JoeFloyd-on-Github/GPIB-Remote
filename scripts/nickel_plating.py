#!/usr/bin/env python3
"""Bright nickel plating experiment: a guided, automated test procedure.

Built on the test-script API (cli/gpibctl/api.py, docs/api.md). The instruments are configured
instances on the GPIB-Remote server, named in a bench file (JSON) by role:

    supply       the plating supply (constant current, voltage as compliance)
    ohmmeter     the 7.5-digit DMM: 4-wire resistance of the copper cladding (Kelvin probe)
    thermometer  bath temperature: an instrument with a thermometer role, or a DMM reading a
                 type K thermocouple on DC volts (cold-junction temperature entered at calibration)
    shunt        optional: a DMM across a current shunt (otherwise the supply's current readback)
    cell         optional: a DMM across the cell (otherwise the supply's voltage readback)

    python scripts/nickel_plating.py --setup              # scan the bus, assign handles, write the bench file
    python scripts/nickel_plating.py                      # run the procedure
    python scripts/nickel_plating.py --bench my-bench.json --log-dir runs

The procedure:
 1. Check every instrument answers and is what its handle says; lock them for the run.
 2. Calibrate: ohmmeter self-cal (optional), 4-wire setup, probe zero on a shorting bar,
    optional check on a reference resistor; thermometer against a reference; current zero.
 3. Sample: ID, weight, size, sides; initial resistance at N probe positions; copper estimate.
 4. Bath: pH; wait until the temperature is in the window; choose the current (30 mA/cm2).
 5. Plate: output on only after confirmation; log time code, elapsed time, current, cell and
    supply voltage, cell resistance, charge and temperature; alarms; output off at the end,
    on Ctrl-C or on any error.
 6. After: final weight, final resistance at the same positions.
 7. Report: Faraday prediction vs measured mass, plating efficiency, nickel thickness, predicted
    vs measured resistance (2D sheet and 1D strip models). Saved next to the CSV log.

Standard library only; Python 3.6 or newer.
"""

import argparse
import datetime
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli"))
from gpibctl.api import ApiError, Bench, DataLog, ServerError, Stats  # noqa: E402

# -- Physical constants ---------------------------------------------------------------------------
FARADAY = 96485.0          # C/mol
NI_MOLAR_MASS = 58.69      # g/mol
NI_VALENCE = 2
NI_DENSITY = 8.90          # g/cm3
RHO_NI = 6.99e-6           # ohm cm (bulk; bright nickel deposits run somewhat higher)
RHO_CU = 1.72e-6           # ohm cm
C2D = math.pi / math.log(2)  # 4.53236: sheet resistance / (V/I) for a collinear 4-point probe
CU_UM_PER_OZ = 35.0        # 1 oz/ft2 copper foil is ~35 um
PROBE_SPACING_CM = 2.0     # Kelvin probe voltage-sense spacing
TARGET_J = 0.030           # A/cm2 (30 mA/cm2; the useful window is about 20-50)
J_WINDOW = (0.020, 0.050)
R_CELL_MIN_I = 1e-4        # only report R_cell above 0.1 mA

DEFAULT_BENCH = "plating-bench.json"
ROLES = ("supply", "ohmmeter", "thermometer", "shunt", "cell")
OPTIONAL = ("shunt", "cell")


# -- Calculations ---------------------------------------------------------------------------------
def copper_estimate(r_init, width_cm, f2=1.0, probe_cm=PROBE_SPACING_CM):
    """Copper thickness (cm) from the initial Kelvin resistance, by the 2D and 1D models."""
    rs_cu = r_init * C2D * f2
    return {"rs_cu": rs_cu, "t_cu_2d": RHO_CU / rs_cu, "t_cu_1d": RHO_CU * probe_cm / (width_cm * r_init)}


def nickel_from_charge(q, efficiency=1.0):
    """Nickel mass (g) deposited by q coulombs (Faraday's law)."""
    return q * efficiency * NI_MOLAR_MASS / (NI_VALENCE * FARADAY)


def predict_resistance(t_ni, r_init, width_cm, f2=1.0, probe_cm=PROBE_SPACING_CM):
    """Probe resistance after plating t_ni (cm) of nickel on the probed face, by both models."""
    cu = copper_estimate(r_init, width_cm, f2, probe_cm)
    rs_ni = RHO_NI / t_ni
    rs_comp = cu["rs_cu"] * rs_ni / (cu["rs_cu"] + rs_ni)
    squares = probe_cm / width_cm
    r1d_ni = rs_ni * squares
    return {"rs_ni": rs_ni, "rs_comp": rs_comp, "r_2d": rs_comp / (C2D * f2),
            "r_1d": r_init * r1d_ni / (r_init + r1d_ni), "squares": squares}


def plating_summary(q, area_cm2, w0_g, r_init, width_cm, f2=1.0, efficiency=1.0, probe_cm=PROBE_SPACING_CM):
    """Prediction from the charge: nickel mass and thickness, final weight, final resistance."""
    mass = nickel_from_charge(q, efficiency)
    t_ni = mass / NI_DENSITY / area_cm2
    out = {"q": q, "mass": mass, "final_weight": w0_g + mass, "t_ni": t_ni}
    if t_ni > 0:
        out.update(predict_resistance(t_ni, r_init, width_cm, f2, probe_cm))
    return out


# -- Operator interaction -------------------------------------------------------------------------
class Abort(Exception):
    pass


class Operator:
    def __init__(self, inp=input, out=sys.stdout):
        self.inp, self.out = inp, out

    def say(self, text=""):
        print(text, file=self.out)
        self.out.flush()

    def step(self, title):
        self.say("\n== %s %s" % (title, "=" * max(0, 70 - len(title))))

    def _line(self, prompt):
        try:
            return self.inp(prompt).strip()
        except EOFError:
            raise Abort("input ended")

    def ask(self, prompt, default=None, cast=str, check=None, optional=False):
        """A value; Enter takes the default (or None when optional and there is no default)."""
        while True:
            shown = "%s [%s]: " % (prompt, default) if default is not None else (
                "%s (Enter to skip): " % prompt if optional else "%s: " % prompt)
            text = self._line(shown)
            if not text:
                if default is not None:
                    text = str(default)
                elif optional:
                    return None
            try:
                value = cast(text)
            except ValueError:
                self.say("  not a valid value")
                continue
            msg = check(value) if check else None
            if msg:
                self.say("  " + msg)
                continue
            return value

    def yes(self, prompt, default="n"):
        return self.ask(prompt + " (y/n)", default, str.lower).startswith("y")

    def pause(self, instruction):
        self._line("  >> %s  [Enter when done] " % instruction)


def positive(v):
    return None if v > 0 else "must be greater than 0"


def fmt_ohm(r):
    return "%.4f mohm" % (r * 1e3) if abs(r) < 1 else "%.5f ohm" % r


def thickness_line(t_cm, oz=True):
    um = t_cm * 1e4
    if not oz:
        return "%.3f um  (%.4f mil)" % (um, um / 25.4)
    return "%.2f um  (%.3f mil, %.2f oz/ft2)" % (um, um / 25.4, um / CU_UM_PER_OZ)


def now_code():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# -- Bench file -----------------------------------------------------------------------------------
def load_bench_file(path):
    if not os.path.exists(path):
        raise Abort("no bench file %s: run with --setup first (or give --bench FILE)" % path)
    with open(path) as fh:
        cfg = json.load(fh)
    for role in ROLES:
        if role not in OPTIONAL and not (cfg.get(role) or {}).get("id"):
            raise Abort("%s: no %s instrument" % (path, role))
    return cfg


def setup_bench_file(args, op):
    """Scan the bus, show what is there, and assign an instrument handle to each role."""
    bench = Bench(args.host, args.port, client="nickel_plating")
    try:
        op.step("Instruments on the bus")
        found = bench.scan(board=args.board)
        for f in found:
            if f.instance:
                op.say("  GPIB %2d  %-16s %s" % (f.pad, f.instance, f.type))
            else:
                op.say("  GPIB %2d  %-16s %r%s" % (f.pad, "(not configured)", f.response,
                                                    " -> " + ", ".join(f.types) if f.types else ""))
        cfg = {"host": bench.host, "port": bench.port}
        old = {}
        if os.path.exists(args.bench):
            with open(args.bench) as fh:
                old = json.load(fh)
        ids = [d.id for d in bench.devices()]
        descriptions = {
            "supply": "plating supply", "ohmmeter": "7.5-digit DMM for 4-wire resistance",
            "thermometer": "bath temperature (thermocouple DMM)", "shunt": "DMM across a current shunt",
            "cell": "DMM across the cell"}
        for role in ROLES:
            op.say("\n%s: %s. Configured: %s" % (role, descriptions[role], ", ".join(ids) or "none"))
            default = (old.get(role) or {}).get("id")
            prompt = "  instance id, 'add' for a new one" + (", or 'none'" if role in OPTIONAL else "")
            while True:
                choice = op.ask(prompt, default or ("none" if role in OPTIONAL else None))
                if choice == "none" and role in OPTIONAL:
                    cfg[role] = None
                    break
                if choice == "add":
                    pad = op.ask("  GPIB address", None, int, lambda v: None if 1 <= v <= 30 else "1-30")
                    guess = next((f.types[0] for f in found if f.pad == pad and f.types), None)
                    tref = op.ask("  device type", guess)
                    choice = op.ask("  handle (instance id)", "%s-%s" % (role, pad))
                    try:
                        bench.add(choice, tref, pad, board=args.board, name=op.ask("  name", choice),
                                  off_on_disconnect=(role == "supply"))
                    except ServerError as exc:
                        op.say("  not added: %s" % exc)
                        continue
                    ids.append(choice)
                if choice not in ids:
                    op.say("  no instance %r" % choice)
                    continue
                entry = dict(old.get(role) or {}, id=choice)
                try:
                    check_role(bench, role, entry)
                except ApiError as exc:
                    if role == "thermometer" and "no thermometer role" in str(exc):
                        entry["thermocouple"] = op.ask("  thermocouple type on DC volts", "K")
                    else:
                        op.say("  %s" % exc)
                        continue
                if role == "shunt":
                    entry["ohms"] = op.ask("  shunt resistance (ohm)", entry.get("ohms", 0.1), float, positive)
                if role == "supply" and bench.device(choice).channels:
                    entry["channel"] = op.ask("  channel", entry.get("channel") or bench.supply(choice).channel)
                cfg[role] = entry
                break
        cfg["bath"] = old.get("bath") or {"temp_min_c": 45.0, "temp_max_c": 60.0}
        cfg["bath"]["temp_min_c"] = op.ask("\nBath temperature window, low (C)", cfg["bath"]["temp_min_c"], float)
        cfg["bath"]["temp_max_c"] = op.ask("Bath temperature window, high (C)", cfg["bath"]["temp_max_c"], float)
        with open(args.bench, "w") as fh:
            json.dump(cfg, fh, indent=2)
        op.say("\nWrote %s" % args.bench)
        return cfg
    finally:
        bench.close()


def check_role(bench, role, entry):
    """The API view for a role entry (raises ApiError if the instrument can't fill the role)."""
    if role == "supply":
        return bench.supply(entry["id"], entry.get("channel"))
    if role == "thermometer":
        return bench.thermometer(entry["id"], entry.get("thermocouple"), entry.get("cjc", 25.0))
    return bench.meter(entry["id"])


# -- The procedure --------------------------------------------------------------------------------
class PlatingRun:
    def __init__(self, args, op, cfg, clock=time.monotonic, sleep=time.sleep):
        self.args, self.op, self.cfg = args, op, cfg
        self.clock, self.sleep = clock, sleep
        self.bench = None
        self.log = None
        self.result = {}

    # 1 ------------------------------------------------------------------------------------------
    def connect(self):
        op, cfg = self.op, self.cfg
        op.step("1. Instruments")
        self.bench = b = Bench(self.args.host or cfg.get("host"), self.args.port or cfg.get("port"),
                               client="nickel_plating")
        s = cfg["supply"]
        self.supply = b.supply(s["id"], s.get("channel"))
        self.ohm = b.meter(cfg["ohmmeter"]["id"])
        th = cfg["thermometer"]
        self.therm = b.thermometer(th["id"], th.get("thermocouple"), 25.0)
        self.shunt = b.meter(cfg["shunt"]["id"]) if cfg.get("shunt") else None
        self.shunt_ohms = cfg["shunt"]["ohms"] if cfg.get("shunt") else None
        self.cell = b.meter(cfg["cell"]["id"]) if cfg.get("cell") else None
        views = [("supply", self.supply), ("ohmmeter", self.ohm), ("thermometer", self.therm),
                 ("shunt", self.shunt), ("cell", self.cell)]
        devices = {}
        for role, v in views:
            if v is None:
                op.say("  %-11s none (%s)" % (role, "supply current readback" if role == "shunt" else "supply voltage readback"))
                continue
            d = v.device
            ident = d.identify()
            if not ident.get("match"):
                raise Abort("%s %s answered %r, not a %s" % (role, d.id, ident.get("response"), d.type))
            op.say("  %-11s %-14s %-22s GPIB %d:%-2d %s" % (role, d.id, d.type, d.address[0], d.address[1],
                                                           (ident.get("response") or "").strip()))
            devices[d.id] = d
        for d in devices.values():
            try:
                d.lock("nickel plating run")
            except ServerError as exc:
                raise Abort("cannot take control of %s: %s" % (d.id, exc))
        self.supply.off()
        op.say("  All locked for this run. Supply output is OFF; max %.2f V / %.3f A."
               % (self.supply.max_voltage, self.supply.max_current))

    # 2 ------------------------------------------------------------------------------------------
    def calibrate(self):
        op, a = self.op, self.args
        op.step("2. Calibration")
        cal = self.result.setdefault("calibration", {"time": now_code()})
        # ohmmeter
        if self.ohm._has("autocal") and op.yes("Run the ohmmeter's self-calibration (about %d s)?" % a.autocal_wait):
            op.say("  self-calibrating...")
            self.ohm.autocal(wait=a.autocal_wait)
            cal["autocal"] = True
        self.configure_ohmmeter()
        op.pause("Put the Kelvin probe on the shorting bar (all four points on solid copper)")
        zero = self.ohm.null(a.readings)
        cal["ohm_zero"] = zero
        op.say("  probe zero %s (subtracted from every resistance reading)" % fmt_ohm(zero))
        ref = op.ask("Reference resistor value in ohm, to check the ohmmeter", None, float, positive, optional=True)
        if ref:
            op.pause("Put the probe on the reference resistor")
            st = self.ohm.measure(a.readings)
            err = (st.mean - ref) / ref * 100
            cal["reference"] = {"nominal": ref, "measured": st.mean, "error_pct": err}
            op.say("  measured %s (sd %s): error %+.3f%%" % (fmt_ohm(st.mean), fmt_ohm(st.stdev), err))
            if abs(err) > a.ref_tolerance and not op.yes("  Outside +/-%.2f%%. Continue anyway?" % a.ref_tolerance):
                raise Abort("ohmmeter check failed")
        # thermometer
        if self.therm.meter is not None:
            cjc = op.ask("Thermocouple reference-junction (DMM terminal / room) temperature, C", 23.0, float)
            self.therm.cjc = cjc
            cal["cjc_c"] = cjc
        self.therm.setup()
        op.say("  thermometer reads %.2f C" % self.therm.read())
        ref_t = op.ask("Reference temperature now (ice bath 0.0, or a reference thermometer), C",
                       None, float, optional=True)
        if ref_t is not None:
            op.pause("Probe in the reference, settled")
            off = self.therm.calibrate(ref_t, n=3)
            cal["thermometer_offset_c"] = off
            op.say("  thermometer offset %+.2f C" % off)
        # current zero (output is off)
        if self.shunt:
            self.shunt.configure("dcv", range="auto", nplc=10 if self.shunt._has("nplc") else None)
            z = self.shunt.null(a.readings)
            cal["shunt_zero_v"] = z
            op.say("  shunt zero %.3f uV (output off)" % (z * 1e6))
        else:
            z = self.supply.measure_current()
            cal["supply_current_zero_a"] = z
            op.say("  supply current readback with the output off: %.2f mA" % (z * 1e3))
        if self.cell:
            self.cell.configure("dcv", range="auto", nplc=10 if self.cell._has("nplc") else None)

    def configure_ohmmeter(self):
        a = self.args
        self.ohm.configure("ohm4", range=a.ohm_range, nplc=a.nplc if self.ohm._has("nplc") else None)
        if self.ohm._has("offset_comp"):
            self.ohm.configure(offset_comp=True)

    # 3 ------------------------------------------------------------------------------------------
    def resistance(self, label):
        """Resistance at each probe position: [Stats], and their overall mean."""
        op, a = self.op, self.args
        per = []
        for k in range(1, a.positions + 1):
            op.pause("%s: probe at position %d of %d" % (label, k, a.positions))
            st = self.ohm.measure(a.readings)
            per.append(st)
            op.say("  position %d: %s  (sd %s, n %d)" % (k, fmt_ohm(st.mean), fmt_ohm(st.stdev), st.n))
        overall = Stats([s.mean for s in per])
        op.say("  %s resistance %s, spread between positions %s" % (label, fmt_ohm(overall.mean), fmt_ohm(overall.stdev)))
        return overall, per

    def sample(self):
        op, r = self.op, self.result
        op.step("3. Sample")
        r["sample"] = op.ask("Sample ID", "S1")
        r["w0"] = op.ask("Initial weight (g)", None, float, positive)
        r["length"] = op.ask("Plated length (cm; PCB or Kapton window inside)", 4.0, float, positive)
        r["width"] = op.ask("Plated width (cm)", 4.0, float, positive)
        r["sides"] = op.ask("Sides plated (1 or 2)", 1, int, lambda v: None if v in (1, 2) else "1 or 2")
        r["area"] = r["length"] * r["width"] * r["sides"]
        r_init, per = self.resistance("Initial")
        r["r_init"], r["r_init_positions"] = r_init.mean, [s.mean for s in per]
        if r_init.mean <= 0:
            raise Abort("initial resistance %s is not positive: check the probe zero" % fmt_ohm(r_init.mean))
        cu = copper_estimate(r_init.mean, r["width"], self.args.f2)
        op.say("  copper, 2D sheet: Rs %s/sq, t %s" % (fmt_ohm(cu["rs_cu"]), thickness_line(cu["t_cu_2d"])))
        op.say("  copper, 1D strip: %.2f squares, t %s" % (PROBE_SPACING_CM / r["width"], thickness_line(cu["t_cu_1d"])))
        r["copper"] = cu

    # 4 ------------------------------------------------------------------------------------------
    def bath(self):
        op, r, a = self.op, self.result, self.args
        op.step("4. Bath and current")
        r["ph"] = op.ask("Bath pH", 4.0, float)
        lo, hi = self.cfg["bath"]["temp_min_c"], self.cfg["bath"]["temp_max_c"]
        op.say("  waiting for the bath to reach %.1f-%.1f C (up to %.0f min; Ctrl-C to stop)" % (lo, hi, a.temp_wait))
        t_end = self.clock() + a.temp_wait * 60
        while True:
            t = self.therm.read()
            op.say("  %s  bath %.2f C" % (now_code(), t))
            if lo <= t <= hi:
                break
            if self.clock() >= t_end:
                if not op.yes("  Bath at %.2f C, outside the window. Plate anyway?" % t):
                    raise Abort("bath temperature out of range")
                break
            self.sleep(a.temp_poll)
        r["temp_start"] = t
        i_ideal = r["area"] * TARGET_J
        imax = self.supply.max_current
        op.say("\nPlated area %.2f cm2; ideal current at %.0f mA/cm2: %.1f mA" % (r["area"], TARGET_J * 1e3, i_ideal * 1e3))
        if i_ideal <= imax and op.yes("Use %.1f mA?" % (i_ideal * 1e3), "y"):
            current = i_ideal
        else:
            current = op.ask("Current (mA, max %.0f)" % (imax * 1e3), round(min(i_ideal, imax) * 1e3, 1), float,
                             lambda v: None if 0 < v <= imax * 1e3 else "0 to %.0f mA" % (imax * 1e3)) / 1e3
        j = current / r["area"]
        if not J_WINDOW[0] <= j <= J_WINDOW[1]:
            op.say("  note: %.1f mA/cm2 is outside the usual %.0f-%.0f mA/cm2 window"
                   % (j * 1e3, J_WINDOW[0] * 1e3, J_WINDOW[1] * 1e3))
        vmax = self.supply.max_voltage
        r["compliance"] = op.ask("Compliance voltage (V, max %.2f)" % vmax, min(3.0, vmax), float,
                                 lambda v: None if 0 < v <= vmax else "0 to %.2f V" % vmax)
        r["current"] = current
        r["minutes"] = op.ask("Duration (min)", 30.0, float, positive)
        r["interval"] = op.ask("Sample interval (s)", 10.0, float, lambda v: None if v >= 1 else "1 s minimum")

    # 5 ------------------------------------------------------------------------------------------
    def sample_point(self):
        """(i, v_shunt, v_cell, v_supply, temp)."""
        if self.shunt:
            vs = self.shunt.read()
            i = vs / self.shunt_ohms
        else:
            vs, i = None, self.supply.measure_current()
        v_supply = self.supply.measure_voltage()
        v_cell = self.cell.read() if self.cell else v_supply
        return i, vs, v_cell, v_supply, self.therm.read()

    def plate(self):
        op, r, a = self.op, self.result, self.args
        op.step("5. Plating")
        lo, hi = self.cfg["bath"]["temp_min_c"], self.cfg["bath"]["temp_max_c"]
        self.supply.set(voltage=r["compliance"], current=r["current"])
        op.say("Supply set: %.3f V compliance, %.1f mA, output OFF." % (r["compliance"], r["current"] * 1e3))
        op.say("Connect the cell: anode to +, coupon (cathode) to -, coupon immersed, agitation on.")
        if not op.yes("Cell ready - turn the output ON and start?"):
            raise Abort("not started; output left off")
        started = datetime.datetime.now()
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in r["sample"])
        path = os.path.join(a.log_dir, "%s_%s.csv" % (safe_id, started.strftime("%Y%m%d-%H%M%S")))
        meta = {"sample": r["sample"], "started": now_code(), "supply": self.supply.id,
                "ohmmeter": self.ohm.id, "thermometer": self.therm.id,
                "shunt": "%s (%g ohm)" % (self.shunt.id, self.shunt_ohms) if self.shunt else "supply readback",
                "cell": self.cell.id if self.cell else "supply readback",
                "current_set_a": r["current"], "compliance_v": r["compliance"], "area_cm2": r["area"],
                "initial_weight_g": r["w0"], "r_init_ohm": r["r_init"], "ph": r["ph"],
                "calibration": json.dumps(r.get("calibration", {}))}
        self.log = log = DataLog(path, ["i_a", "v_shunt_v", "v_cell_v", "v_supply_v", "r_cell_ohm", "q_c",
                                        "temp_c", "alarm"], meta, clock=self.clock)
        r["log"] = path
        op.say("\nLogging to %s   (Ctrl-C stops early; the output is turned off either way)\n" % path)
        op.say("Times are local (%s); the CSV has full ISO 8601 time codes." % now_code()[19:])
        op.say("%-12s %8s %9s %8s %8s %8s %9s %7s" % ("time", "t (s)", "I (mA)", "V_cell", "V_psu", "R_cell", "Q (C)", "T (C)"))
        duration = r["minutes"] * 60.0
        q, last, low, rows, k, final = 0.0, None, 0, 0, 0, False
        temps = []
        stopped = "completed"
        with self.supply.guard():
            self.supply.on()
            log.restart_clock()
            log.note("output on")
            try:
                while True:
                    i, vs, v_cell, v_supply, temp = self.sample_point()
                    t = log.elapsed()
                    if last is not None:
                        q += 0.5 * (i + last[1]) * (t - last[0])
                    last = (t, i)
                    temps.append(temp)
                    rc = v_cell / i if i > R_CELL_MIN_I else None
                    alarms = []
                    low = low + 1 if i < 0.95 * r["current"] else 0
                    if low >= 3:
                        alarms.append("I-LOW")
                    if v_supply >= 0.98 * r["compliance"]:
                        alarms.append("AT-COMPLIANCE")
                    if not lo <= temp <= hi:
                        alarms.append("TEMP")
                    code, _ = log.record(i_a=i, v_shunt_v=vs, v_cell_v=v_cell, v_supply_v=v_supply, r_cell_ohm=rc,
                                         q_c=q, temp_c=temp, alarm=" ".join(alarms))
                    rows += 1
                    op.say("%-12s %8.1f %9.2f %8.4f %8.4f %8s %9.3f %7.2f  %s" % (
                        code[11:23], t, i * 1e3, v_cell, v_supply, "-" if rc is None else "%.3f" % rc, q, temp,
                        " ".join(alarms)))
                    if final or t >= duration:
                        break
                    k += 1
                    nxt = min(k * r["interval"], duration)
                    final = nxt >= duration
                    wait = nxt - log.elapsed()
                    if wait > 0:
                        self.sleep(wait)
            except KeyboardInterrupt:
                stopped = "stopped early (Ctrl-C)"
            finally:
                self.supply.off()
                log.note("output off: %s" % stopped)
        op.say("\nOutput OFF (%s, %d samples)." % (stopped, rows))
        r.update(q=q, seconds=last[0] if last else 0.0, stopped=stopped, samples=rows,
                 temp_mean=sum(temps) / len(temps) if temps else None)

    # 6 ------------------------------------------------------------------------------------------
    def after(self):
        op, r = self.op, self.result
        op.step("6. After plating")
        op.pause("Rinse the coupon, dry it")
        r["w1"] = op.ask("Final weight (g)", None, float, positive)
        if op.yes("Re-zero the probe on the shorting bar first?", "y"):
            op.pause("Put the Kelvin probe on the shorting bar")
            z = self.ohm.null(self.args.readings)
            op.say("  probe zero %s" % fmt_ohm(z))
        r_final, per = self.resistance("Final")
        r["r_final"], r["r_final_positions"] = r_final.mean, [s.mean for s in per]

    # 7 ------------------------------------------------------------------------------------------
    def report(self):
        r, a = self.result, self.args
        s = plating_summary(r["q"], r["area"], r["w0"], r["r_init"], r["width"], a.f2, a.efficiency)
        lines = ["Sample %s, %s" % (r["sample"], now_code()),
                 "Run: %.1f min (%s), %.1f mA set, charge %.3f C (%.2f mAh), mean current %.2f mA"
                 % (r["seconds"] / 60, r["stopped"], r["current"] * 1e3, r["q"], r["q"] / 3.6,
                    r["q"] / r["seconds"] * 1e3 if r["seconds"] else 0.0)]
        if r.get("temp_mean") is not None:
            lines.append("Bath: pH %.2f, mean %.2f C" % (r["ph"], r["temp_mean"]))
        measured = r["w1"] - r["w0"] if r.get("w1") is not None else None
        lines.append("Nickel, Faraday prediction: %.2f mg -> final weight %.4f g" % (s["mass"] * 1e3, s["final_weight"]))
        if measured is not None and s["mass"] > 0:
            eff = measured / s["mass"] * 100
            lines.append("Nickel, measured: %.2f mg (final %.4f g): current efficiency %.1f%%"
                         % (measured * 1e3, r["w1"], eff))
            r["efficiency_pct"] = eff
        lines.append("Nickel thickness from charge: %s" % thickness_line(s["t_ni"], oz=False))
        t_meas = measured / NI_DENSITY / r["area"] if measured and measured > 0 else None
        if t_meas:
            lines.append("Nickel thickness from weight: %s" % thickness_line(t_meas, oz=False))
        lines.append("Resistance: initial %s" % fmt_ohm(r["r_init"]))
        preds = [("charge", s)] if "r_2d" in s else []
        if t_meas:
            preds.append(("weight", predict_resistance(t_meas, r["r_init"], r["width"], a.f2)))
        for basis, p in preds:
            for name, key in (("2D sheet", "r_2d"), ("1D strip", "r_1d")):
                rp = p[key]
                lines.append("  predicted (%s, %s): %s  (drop %.2f%%)"
                             % (basis, name, fmt_ohm(rp), (r["r_init"] - rp) / r["r_init"] * 100))
        if r.get("r_final") is not None:
            lines.append("  measured final: %s  (drop %.2f%%)"
                         % (fmt_ohm(r["r_final"]), (r["r_init"] - r["r_final"]) / r["r_init"] * 100))
        lines.append("(Predictions use bulk Ni resistivity and the nickel on the probed face only.)")
        r["prediction"] = s
        self.op.step("7. Report")
        for line in lines:
            self.op.say(line)
        if r.get("log"):
            base = os.path.splitext(r["log"])[0]
            with open(base + ".summary.txt", "w") as fh:
                fh.write("\n".join(lines) + "\n")
            with open(base + ".json", "w") as fh:
                json.dump(r, fh, indent=2, default=str)
            self.op.say("\nLog %s\nSummary %s.summary.txt, data %s.json" % (r["log"], base, base))
        return lines

    # --------------------------------------------------------------------------------------------
    def run(self):
        try:
            self.connect()
            self.calibrate()
            self.sample()
            self.bath()
            self.plate()
            self.after()
            self.report()
            return self.result
        finally:
            if self.log is not None:
                self.log.close()
            if self.bench is not None:
                self.bench.close()          # turns off anything still on, releases the locks


# -- Command line ---------------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(description="Guided bright nickel plating experiment.")
    ap.add_argument("--bench", default=DEFAULT_BENCH, help="bench file (default %s)" % DEFAULT_BENCH)
    ap.add_argument("--setup", action="store_true", help="scan the bus and write the bench file")
    ap.add_argument("--host", help="GPIB-Remote server (default: the bench file, then the CLI's setting)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--board", type=int, default=0)
    ap.add_argument("--log-dir", default="plating-logs")
    ap.add_argument("--positions", type=int, default=3, help="probe positions per resistance measurement")
    ap.add_argument("--readings", type=int, default=5, help="readings averaged per position")
    ap.add_argument("--ohm-range", type=float, default=30.0, help="ohmmeter range (ohm), or 0 for auto")
    ap.add_argument("--nplc", type=float, default=100.0, help="ohmmeter integration (power-line cycles)")
    ap.add_argument("--autocal-wait", type=float, default=60.0, help="seconds to wait for the ohmmeter self-cal")
    ap.add_argument("--ref-tolerance", type=float, default=0.5, help="reference resistor tolerance (%%)")
    ap.add_argument("--temp-wait", type=float, default=30.0, help="minutes to wait for the bath temperature")
    ap.add_argument("--temp-poll", type=float, default=5.0, help="seconds between bath readings while waiting")
    ap.add_argument("--f2", type=float, default=1.0, help="finite-size (Smits) correction for the 2D model")
    ap.add_argument("--efficiency", type=float, default=1.0, help="current efficiency for the Faraday prediction")
    return ap


def main(argv=None, inp=input, out=sys.stdout, clock=time.monotonic, sleep=time.sleep):
    args = build_parser().parse_args(argv)
    if args.ohm_range == 0:
        args.ohm_range = "auto"
    op = Operator(inp, out)
    try:
        if args.setup:
            setup_bench_file(args, op)
            return 0
        cfg = load_bench_file(args.bench)
        PlatingRun(args, op, cfg, clock, sleep).run()
        return 0
    except Abort as exc:
        op.say("\nstopped: %s" % exc)
        return 1
    except (ServerError, ApiError) as exc:
        op.say("\nstopped, instrument error: %s" % exc)
        return 1
    except KeyboardInterrupt:
        op.say("\ncancelled")
        return 130
    except OSError as exc:
        op.say("\ncannot reach the server: %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
