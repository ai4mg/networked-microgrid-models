"""Run four microgrids with day-ahead price-optimized battery schedules.

All study inputs are declared in this file.  The script uses only the common
OpenDSS model/result helpers in ``run_four_microgrids_24h.py``; it does not
import inputs or workflow from ``run_four_microgrids_24h_individual_status``.
MG1 and MG2 have 24-hour day-ahead price curves and battery operating
assumptions. A dependency-free dynamic-programming optimization computes their
charge/discharge schedules before OpenDSS is run. MG3 and MG4 are data-center
batteries held at 100% SOC as full-charge reserves; regular energy arbitrage is
not applied to them. Positive power means discharge and negative power means
charge.

The optimizer minimizes energy-arbitrage cost plus a small battery-throughput
cost while enforcing power, energy, SOC, efficiency, and end-of-day SOC
constraints. SOC is discretized in one-percentage-point increments.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import sys

import matplotlib.pyplot as plt
import opendssdirect as dss
import pandas as pd

import run_four_microgrids_24h as base


# =============================================================================
# PART 1: INDIVIDUAL MICROGRID STATUS INPUTS
# =============================================================================
# These inputs affect only the power-flow topology and diesel representation.
# They are deliberately NOT passed to the battery optimizer.  Battery dispatch
# is based solely on price, battery power/energy limits, SOC, efficiency, and
# throughput cost, regardless of grid-connected or islanded operation.
ISLANDED_HOURS = (11, 18)
ISLANDED = "ISLANDED"
GRID_CONNECTED = "GRID_CONNECTED"

# Bus numbers without phase suffixes. All other loads in MG1/MG2 may be shed.
# Data-center loads are protected: no automatic shedding in MG3 or MG4.
CRITICAL_LOAD_BUSES = {
    "MG1": ("28", "29", "30"),
    "MG2": ("82", "83", "84", "85", "86", "87", "92", "94", "95", "96"),
}
NONCRITICAL_SHEDDING_STEP = 0.01  # One percent of the hourly requested load.
DIESEL_KVA_TOLERANCE = 1e-3

# Diesel capacity percentages for scenario studies (100 = installed capacity).
# MG2's percentage applies to each of its two diesels. Reduced capacity can
# trigger non-critical load shedding. Values must be positive (e.g. 50 = 50%).
DIESEL_CAPACITY_SCALING_PCT = {
    "MG1": 100.0,
    "MG2": 100.0,
    "MG3": 100.0,
    "MG4": 100.0,
}
# Preserve installed ratings so repeated runs never compound the scaling.
NOMINAL_DIESEL_KVA = {
    name: dict(mg.diesel_kva) for name, mg in base.MICROGRIDS.items()
}


def apply_diesel_capacity_scaling() -> None:
    """Apply validated scenario ratings to dispatch, shedding, and reporting."""
    if set(DIESEL_CAPACITY_SCALING_PCT) != set(base.MICROGRIDS):
        raise ValueError("Diesel capacity scaling must define exactly MG1, MG2, MG3, MG4")
    for name, percent in DIESEL_CAPACITY_SCALING_PCT.items():
        if not math.isfinite(percent) or percent <= 0:
            raise ValueError(f"{name} diesel capacity percentage must be finite and positive")
    for name, mg in base.MICROGRIDS.items():
        mg.diesel_kva.update({
            diesel: rating * DIESEL_CAPACITY_SCALING_PCT[name] / 100.0
            for diesel, rating in NOMINAL_DIESEL_KVA[name].items()
        })

MICROGRID_STATUSES = {
    11: {
        "MG1": ISLANDED,
        "MG2": ISLANDED,
        "MG3": ISLANDED,
        "MG4": ISLANDED,
    },
    18: {
        "MG1": ISLANDED,
        "MG2": ISLANDED,
        "MG3": ISLANDED,
        "MG4": ISLANDED,
    },
}

# The supplied master DSS ends by opening sw2 for a feeder reconfiguration.
# That leaves MG2 and MG4 de-energized when sw7 is open, even though their PCC
# switches are reported closed.  Declare the required upstream feeder state
# here so all four MGs are energized in grid-connected snapshots.
UPSTREAM_FEEDER_SWITCH_STATES = {"sw2": True}

# This auxiliary MG3--MG4 tie bypasses MG4's own PCC and remains open so MG4 is
# connected only through its designated PCC, Line.sw5.
AUXILIARY_TIE_STATES = {"sw7": False}


# =============================================================================
# PART 2: 24-HOUR OPERATING PROFILES AND OPTIMAL BATTERY SCALING FACTORS
# =============================================================================
# All 24-hour inputs are explicit here; none are imported from another case.
LOAD_SCALING = {
    "MG1": [
        0.55, 0.50, 0.48, 0.47, 0.50, 0.60, 0.72, 0.85,
        0.95, 1.00, 0.98, 0.92, 0.88, 0.85, 0.82, 0.84,
        0.90, 0.98, 1.05, 1.08, 1.02, 0.90, 0.75, 0.62,
    ],
    "MG2": [
        0.55, 0.50, 0.48, 0.47, 0.50, 0.60, 0.72, 0.85,
        0.95, 1.00, 0.98, 0.92, 0.88, 0.85, 0.82, 0.84,
        0.90, 0.98, 1.05, 1.08, 1.02, 0.90, 0.75, 0.62,
    ],
    "MG3": [
        0.70, 0.68, 0.67, 0.67, 0.68, 0.72, 0.78, 0.85,
        0.90, 0.94, 0.96, 0.98, 1.00, 0.98, 0.96, 0.94,
        0.92, 0.95, 1.00, 0.98, 0.94, 0.88, 0.80, 0.74,
    ],
    "MG4": [
        0.65, 0.63, 0.62, 0.62, 0.64, 0.68, 0.75, 0.82,
        0.88, 0.92, 0.95, 0.97, 0.98, 0.96, 0.93, 0.91,
        0.90, 0.93, 0.97, 0.99, 0.95, 0.87, 0.78, 0.70,
    ],
}

SOLAR_SCALING = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.10, 0.25, 0.45,
    0.65, 0.80, 0.92, 1.00, 0.95, 0.85, 0.70, 0.50,
    0.30, 0.10, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
]

OTHER_LOAD_SCALING = [
    0.3920, 0.3696, 0.3528, 0.3416, 0.3472, 0.3808,
    0.4368, 0.4928, 0.5320, 0.5488, 0.5600, 0.5768,
    0.5880, 0.5824, 0.5712, 0.5600, 0.5936, 0.6440,
    0.6720, 0.6496, 0.6048, 0.5376, 0.4704, 0.4256,
]

# Sample locational day-ahead energy prices in $/MWh. Only MG1 and MG2
# participate in regular energy arbitrage. MG3 and MG4 are data centers whose
# batteries remain fully charged reserves and are not passed to the optimizer.
DAY_AHEAD_PRICE_USD_PER_MWH = {
    "MG1": [
        31, 28, 26, 24, 25, 30, 39, 52, 64, 58, 46, 38,
        34, 32, 36, 48, 67, 92, 116, 104, 78, 59, 44, 35,
    ],
    "MG2": [
        29, 27, 25, 23, 24, 29, 41, 55, 69, 61, 49, 40,
        35, 33, 38, 51, 72, 98, 122, 109, 82, 62, 45, 34,
    ],
}


@dataclass(frozen=True)
class BatteryScheduleInput:
    """Day-ahead battery assumptions used by the schedule optimizer."""

    energy_capacity_kwh: float
    initial_soc_pct: int = 50
    minimum_soc_pct: int = 20
    maximum_soc_pct: int = 90
    final_soc_pct: int = 50
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    throughput_cost_usd_per_mwh: float = 2.0


# Power ratings must agree with the equipment definitions in the common model.
BATTERY_RATED_KW = {
    "MG1": 100.0,
    "MG2": 50.0,
    "MG3": 1200.0,
    "MG4": 1200.0,
}

ENERGY_OPTIMIZED_MICROGRIDS = ("MG1", "MG2")
DATA_CENTER_MICROGRIDS = ("MG3", "MG4")

# Energy capacity and SOC/efficiency assumptions are explicit here.
BATTERY_SCHEDULE_INPUTS = {
    "MG1": BatteryScheduleInput(energy_capacity_kwh=400.0),
    "MG2": BatteryScheduleInput(energy_capacity_kwh=200.0),
    "MG3": BatteryScheduleInput(
        energy_capacity_kwh=100.0,
        initial_soc_pct=100,
        maximum_soc_pct=100,
        final_soc_pct=100,
    ),
    "MG4": BatteryScheduleInput(
        energy_capacity_kwh=100.0,
        initial_soc_pct=100,
        maximum_soc_pct=100,
        final_soc_pct=100,
    ),
}

# Zero normal dispatch keeps each data-center BESS at its initial 100% SOC.
# A future emergency/islanding controller may replace these profiles, but the
# regular price optimizer below never changes them.
DATA_CENTER_BATTERY_SCALING = {
    mg_name: [0.0] * 24 for mg_name in DATA_CENTER_MICROGRIDS
}

# One SOC percentage point is the optimization state resolution. Increase this
# only if a coarser and faster study is preferred.
SOC_STEP_PCT = 1


def validate_optimization_inputs() -> None:
    """Fail early for incomplete prices or physically invalid assumptions."""

    expected = set(base.MICROGRIDS)
    optimized = set(ENERGY_OPTIMIZED_MICROGRIDS)
    data_centers = set(DATA_CENTER_MICROGRIDS)
    if optimized | data_centers != expected or optimized & data_centers:
        raise ValueError("Optimized and data-center MG groups must partition all MGs")
    if set(DAY_AHEAD_PRICE_USD_PER_MWH) != optimized:
        raise ValueError(f"Price curves must contain exactly {sorted(optimized)}")
    if set(BATTERY_SCHEDULE_INPUTS) != expected:
        raise ValueError(f"Battery inputs must contain exactly {sorted(expected)}")
    if set(BATTERY_RATED_KW) != expected:
        raise ValueError(f"Battery ratings must contain exactly {sorted(expected)}")

    for mg_name in expected:
        data = BATTERY_SCHEDULE_INPUTS[mg_name]
        if mg_name in optimized:
            prices = DAY_AHEAD_PRICE_USD_PER_MWH[mg_name]
            if len(prices) != 24 or any(
                not math.isfinite(float(value)) for value in prices
            ):
                raise ValueError(f"{mg_name} must have 24 finite day-ahead prices")
        if data.energy_capacity_kwh <= 0:
            raise ValueError(f"{mg_name} energy capacity must be positive")
        if BATTERY_RATED_KW[mg_name] <= 0:
            raise ValueError(f"{mg_name} battery kW rating must be positive")
        if not math.isclose(
            BATTERY_RATED_KW[mg_name], base.MICROGRIDS[mg_name].battery_rated_kw
        ):
            raise ValueError(
                f"{mg_name} BATTERY_RATED_KW must match its OpenDSS equipment rating"
            )
        if not (
            0 <= data.minimum_soc_pct <= data.initial_soc_pct <= data.maximum_soc_pct <= 100
            and data.minimum_soc_pct <= data.final_soc_pct <= data.maximum_soc_pct
        ):
            raise ValueError(f"{mg_name} SOC limits/initial/final values are inconsistent")
        if any(
            value % SOC_STEP_PCT
            for value in (
                data.minimum_soc_pct, data.maximum_soc_pct,
                data.initial_soc_pct, data.final_soc_pct,
            )
        ):
            raise ValueError(f"{mg_name} SOC percentages must align with SOC_STEP_PCT")
        if not (0 < data.charge_efficiency <= 1 and 0 < data.discharge_efficiency <= 1):
            raise ValueError(f"{mg_name} efficiencies must be in (0, 1]")


def optimize_one_battery(mg_name: str) -> tuple[list[float], list[dict]]:
    """Return minimum-cost scaling and SOC details for one battery.

    A transition to a higher SOC charges from the grid; a transition to a lower
    SOC discharges to the grid/load. The constant native-load energy cost can
    be omitted because it does not affect the optimal battery schedule.
    """

    data = BATTERY_SCHEDULE_INPUTS[mg_name]
    rated_kw = BATTERY_RATED_KW[mg_name]
    prices = DAY_AHEAD_PRICE_USD_PER_MWH[mg_name]
    states = list(range(data.minimum_soc_pct, data.maximum_soc_pct + 1, SOC_STEP_PCT))
    infinity = float("inf")

    # Dynamic-programming keys are ending SOC percentages. Values are minimum
    # cumulative cost; parents retain the preceding SOC and signed battery kW.
    costs = {state: infinity for state in states}
    costs[data.initial_soc_pct] = 0.0
    parents: list[dict[int, tuple[int, float, float]]] = []

    for hour, price_usd_per_mwh in enumerate(prices):
        next_costs = {state: infinity for state in states}
        hour_parent: dict[int, tuple[int, float, float]] = {}
        price_usd_per_kwh = price_usd_per_mwh / 1000.0
        throughput_usd_per_kwh = data.throughput_cost_usd_per_mwh / 1000.0

        for start_soc, accumulated_cost in costs.items():
            if not math.isfinite(accumulated_cost):
                continue
            for end_soc in states:
                stored_delta_kwh = (
                    (end_soc - start_soc) * data.energy_capacity_kwh / 100.0
                )
                if stored_delta_kwh >= 0:
                    charge_kw = stored_delta_kwh / data.charge_efficiency
                    discharge_kw = 0.0
                else:
                    charge_kw = 0.0
                    discharge_kw = -stored_delta_kwh * data.discharge_efficiency
                if charge_kw > rated_kw + 1e-9 or discharge_kw > rated_kw + 1e-9:
                    continue

                signed_battery_kw = discharge_kw - charge_kw
                energy_cost = price_usd_per_kwh * (charge_kw - discharge_kw)
                wear_cost = throughput_usd_per_kwh * (charge_kw + discharge_kw)
                candidate = accumulated_cost + energy_cost + wear_cost
                if candidate < next_costs[end_soc] - 1e-12:
                    next_costs[end_soc] = candidate
                    hour_parent[end_soc] = (
                        start_soc, signed_battery_kw, energy_cost + wear_cost
                    )

        if not hour_parent:
            raise RuntimeError(f"No feasible {mg_name} battery transitions at hour {hour}")
        costs = next_costs
        parents.append(hour_parent)

    if not math.isfinite(costs[data.final_soc_pct]):
        raise RuntimeError(f"No feasible {mg_name} schedule reaches final SOC")

    schedule_kw = [0.0] * 24
    hourly_cost = [0.0] * 24
    ending_soc = [0] * 24
    state = data.final_soc_pct
    for hour in range(23, -1, -1):
        ending_soc[hour] = state
        previous_state, signed_kw, incremental_cost = parents[hour][state]
        schedule_kw[hour] = signed_kw
        hourly_cost[hour] = incremental_cost
        state = previous_state
    if state != data.initial_soc_pct:
        raise RuntimeError(f"{mg_name} schedule reconstruction did not reach initial SOC")

    scaling = [
        0.0 if abs(value) < 1e-10 else max(-1.0, min(1.0, value / rated_kw))
        for value in schedule_kw
    ]
    details = []
    start_soc = data.initial_soc_pct
    for hour in range(24):
        details.append({
            "Hour": hour,
            "Microgrid": mg_name,
            "Price_USD_per_MWh": prices[hour],
            "Start_SOC_pct": start_soc,
            "End_SOC_pct": ending_soc[hour],
            "Battery_kW": schedule_kw[hour],
            "Battery_Scale": scaling[hour],
            "Incremental_Objective_USD": hourly_cost[hour],
        })
        start_soc = ending_soc[hour]
    return scaling, details


def optimize_all_batteries() -> tuple[dict[str, list[float]], pd.DataFrame]:
    """Optimize MG1/MG2 and hold data-center batteries at full-charge reserve."""

    validate_optimization_inputs()
    schedules = {}
    rows = []
    for mg_name in base.MICROGRIDS:
        if mg_name in ENERGY_OPTIMIZED_MICROGRIDS:
            schedules[mg_name], details = optimize_one_battery(mg_name)
            for detail in details:
                detail["Dispatch_Control"] = "Energy_optimized"
            rows.extend(details)
        else:
            schedules[mg_name] = list(DATA_CENTER_BATTERY_SCALING[mg_name])
            data = BATTERY_SCHEDULE_INPUTS[mg_name]
            rows.extend({
                "Hour": hour,
                "Microgrid": mg_name,
                "Price_USD_per_MWh": math.nan,
                "Start_SOC_pct": data.initial_soc_pct,
                "End_SOC_pct": data.initial_soc_pct,
                "Battery_kW": 0.0,
                "Battery_Scale": 0.0,
                "Incremental_Objective_USD": 0.0,
                "Dispatch_Control": "Data_center_full_charge_reserve",
            } for hour in range(24))
    return schedules, pd.DataFrame(rows)


# =============================================================================
# PART 3: RUN OPTIMIZATION AND THE 24-HOUR OPENDSS STUDY
# =============================================================================
OUTPUT_DIR = Path(__file__).resolve().parent / (
    "powerflow_results_four_microgrids_24h_price_optimized_battery"
)
WORKBOOK_FILENAME = "four_microgrids_24h_price_optimized_battery.xlsx"


def validate_study_inputs(schedules: dict[str, list[float]]) -> None:
    """Validate explicit topology, status, profile, and dispatch inputs."""

    expected = set(base.MICROGRIDS)
    if set(MICROGRID_STATUSES) != set(ISLANDED_HOURS):
        raise ValueError("MICROGRID_STATUSES must define every ISLANDED_HOURS entry")
    for hour, statuses in MICROGRID_STATUSES.items():
        if hour not in base.HOURS or set(statuses) != expected:
            raise ValueError(f"Invalid or incomplete status input at hour {hour}")
        if not set(statuses.values()) <= {ISLANDED, GRID_CONNECTED}:
            raise ValueError(f"Invalid operating status at hour {hour}")
    for name, values in {
        **LOAD_SCALING,
        "solar": SOLAR_SCALING,
        "other_loads": OTHER_LOAD_SCALING,
        **{f"{name}_battery": values for name, values in schedules.items()},
    }.items():
        if len(values) != 24 or any(not math.isfinite(float(value)) for value in values):
            raise ValueError(f"{name} must contain 24 finite values")
    if set(LOAD_SCALING) != expected or set(schedules) != expected:
        raise ValueError(f"Load and battery profiles must contain exactly {sorted(expected)}")


def is_islanded(hour: int, mg_name: str) -> bool:
    """Return the requested power-flow state; not used by optimization."""

    return MICROGRID_STATUSES.get(hour, {}).get(mg_name, GRID_CONNECTED) == ISLANDED


def configure_operating_state(hour: int, schedules: dict[str, list[float]]) -> None:
    """Apply topology/diesel state, then apply the independent energy schedule."""

    for line_name, closed in UPSTREAM_FEEDER_SWITCH_STATES.items():
        dss.Text.Command(f"Edit Line.{line_name} Enabled={'Yes' if closed else 'No'}")
    for line_name, closed in AUXILIARY_TIE_STATES.items():
        dss.Text.Command(f"Edit Line.{line_name} Enabled={'Yes' if closed else 'No'}")

    for mg_name, mg in base.MICROGRIDS.items():
        islanded = is_islanded(hour, mg_name)
        dss.Text.Command(f"Edit Line.{mg.pcc_line} Enabled={'No' if islanded else 'Yes'}")
        dss.Text.Command(
            f"Edit Vsource.gfm_{mg_name.lower()} Enabled={'Yes' if islanded else 'No'}"
        )
        for diesel in mg.diesel_generators:
            dss.Text.Command(
                f"Edit Generator.{diesel} kW=0 kvar=0 "
                f"kVA={mg.diesel_kva[diesel]} model=1 Enabled=No"
            )

        # This command is intentionally outside all status decisions.  The
        # price/SOC optimizer alone determines the signed battery setpoint.
        base.set_battery(
            mg.battery_element, BATTERY_RATED_KW[mg_name], schedules[mg_name][hour]
        )


def curtail_islanded_solar_reverse_power(hour: int) -> None:
    """Curtail PV if an island's forming source would otherwise absorb power."""

    changed = False
    for mg_name, mg in base.MICROGRIDS.items():
        if not is_islanded(hour, mg_name) or not mg.solar_generators:
            continue
        p_forming, _ = base.element_output(f"Vsource.gfm_{mg_name.lower()}")
        if p_forming >= 0.5:
            continue
        present_kw = sum(
            max(0.0, base.element_output(f"Generator.{name}")[0])
            for name in mg.solar_generators
        )
        each_kw = max(0.0, present_kw + p_forming - 1.0) / len(mg.solar_generators)
        each_kva = mg.solar_capacity_kw / len(mg.solar_generators)
        for name in mg.solar_generators:
            dss.Text.Command(
                f"Edit Generator.{name} kW={each_kw:.8f} kvar=0 "
                f"kVA={each_kva:.8f} model=1 Enabled=Yes"
            )
        changed = True
    if changed and not base.solve():
        raise RuntimeError("Power flow failed after islanded solar curtailment")


def equally_share_mg2_diesels(hour: int) -> None:
    """Make MG2's reference and support diesels share island demand equally."""

    if not is_islanded(hour, "MG2"):
        return
    mg = base.MICROGRIDS["MG2"]
    support_name = next(name for name in mg.diesel_generators if name != mg.forming_diesel)
    support_element = f"Generator.{support_name}"
    reference_element = "Vsource.gfm_mg2"
    dss.Text.Command(
        f"Edit {support_element} kW=0 kvar=0 kVA={mg.diesel_kva[support_name]} "
        "model=1 Enabled=Yes"
    )
    if not base.solve():
        raise RuntimeError("MG2 failed when its support diesel was enabled")
    for _ in range(20):
        reference_p, reference_q = base.element_output(reference_element)
        support_p, support_q = base.element_output(support_element)
        if abs(reference_p - support_p) <= 0.25 and abs(reference_q - support_q) <= 0.25:
            return
        target_p = 0.5 * (reference_p + support_p)
        target_q = 0.5 * (reference_q + support_q)
        # Temporary overloads are allowed while solving; the load-shedding
        # controller checks both units before accepting the hourly snapshot.
        dss.Text.Command(
            f"Edit {support_element} kW={target_p:.8f} kvar={target_q:.8f} "
            f"kVA={mg.diesel_kva[support_name]} model=1 Enabled=Yes"
        )
        if not base.solve():
            raise RuntimeError("MG2 failed during equal diesel sharing")
    raise RuntimeError("MG2 equal diesel sharing did not settle")


def enforce_islanded_diesel_limits(hour: int, load_ratings) -> pd.DataFrame:
    """Shed non-critical P/Q proportionally until every diesel is within kVA.

    Vsource has no diesel capacity limiter. This outer power-flow loop enforces
    capacity on measured terminal injection, including reactive demand/losses.
    Critical loads and battery commands are never changed. Each hour starts
    from base.set_loads, so shedding does not carry over into the next hour.
    """
    rows = []
    for mg_name, mg in base.MICROGRIDS.items():
        eligible = {}
        critical = {str(bus).lower() for bus in CRITICAL_LOAD_BUSES.get(mg_name, ())}
        for name, (kw, kvar) in load_ratings[mg_name].items():
            dss.Circuit.SetActiveElement(f"Load.{name}")
            bus = base.bus_base(dss.CktElement.BusNames()[0])
            if mg_name in CRITICAL_LOAD_BUSES and bus not in critical:
                eligible[name] = (kw * LOAD_SCALING[mg_name][hour],
                                  kvar * LOAD_SCALING[mg_name][hour])

        retained = 1.0
        if is_islanded(hour, mg_name):
            for step in range(math.ceil(1.0 / NONCRITICAL_SHEDDING_STEP) + 1):
                overloaded = []
                for diesel in mg.diesel_generators:
                    element = (f"Vsource.gfm_{mg_name.lower()}"
                               if diesel == mg.forming_diesel else f"Generator.{diesel}")
                    p, q = base.element_output(element)
                    kva = math.hypot(p, q)
                    if kva > mg.diesel_kva[diesel] + DIESEL_KVA_TOLERANCE:
                        overloaded.append(f"{diesel}: {kva:.3f} > {mg.diesel_kva[diesel]:.3f} kVA")
                if not overloaded:
                    break
                if not eligible or retained <= 0.0:
                    raise RuntimeError(
                        f"Hour {hour} {mg_name}: diesel capacity infeasible with protected "
                        f"loads/battery dispatch; no remaining non-critical load to shed: "
                        + "; ".join(overloaded)
                    )
                retained = max(0.0, 1.0 - (step + 1) * NONCRITICAL_SHEDDING_STEP)
                for name, (kw, kvar) in eligible.items():
                    dss.Text.Command(
                        f"Edit Load.{name} kW={kw * retained:.8f} kvar={kvar * retained:.8f}"
                    )
                if not base.solve():
                    raise RuntimeError(f"Hour {hour} {mg_name}: load-shedding power flow failed")
                curtail_islanded_solar_reverse_power(hour)
                equally_share_mg2_diesels(hour)
        rows.append({
            "Hour": hour, "Microgrid": mg_name,
            "Noncritical_Load_Retained_pct": 100.0 * retained,
            "Shed_Setpoint_kW": (1.0 - retained) * sum(p for p, _ in eligible.values()),
            "Shed_Setpoint_kvar": (1.0 - retained) * sum(q for _, q in eligible.values()),
        })
    return pd.DataFrame(rows)


def extract_der_dispatch(hour: int) -> pd.DataFrame:
    """Extract solar, battery, and status-aware diesel output."""

    rows = []
    for mg_name, mg in base.MICROGRIDS.items():
        for name in mg.solar_generators:
            p_kw, q_kvar = base.element_output(f"Generator.{name}")
            rows.append({
                "Hour": hour, "Microgrid": mg_name, "DER": name, "Type": "Solar",
                "P_kW": p_kw, "Q_kvar": q_kvar,
                "Rating_kVA": mg.solar_capacity_kw / len(mg.solar_generators),
            })
        p_kw, q_kvar = base.element_output(mg.battery_element)
        rows.append({
            "Hour": hour, "Microgrid": mg_name,
            "DER": mg.battery_element.split(".", 1)[1], "Type": "Battery",
            "P_kW": p_kw, "Q_kvar": q_kvar, "Rating_kVA": BATTERY_RATED_KW[mg_name],
        })
        for diesel in mg.diesel_generators:
            if is_islanded(hour, mg_name) and diesel == mg.forming_diesel:
                element, der_type = f"Vsource.gfm_{mg_name.lower()}", "Diesel_grid_forming"
            else:
                element = f"Generator.{diesel}"
                der_type = "Diesel_support" if diesel != mg.forming_diesel else "Diesel"
            p_kw, q_kvar = base.element_output(element)
            rows.append({
                "Hour": hour, "Microgrid": mg_name, "DER": diesel, "Type": der_type,
                "P_kW": p_kw, "Q_kvar": q_kvar, "Rating_kVA": mg.diesel_kva[diesel],
            })
    return pd.DataFrame(rows)


def save_results(
    frames: dict[str, list[pd.DataFrame]], summary_rows: list[dict], audit: pd.DataFrame
) -> None:
    """Save results and verify actual battery power follows the optimization."""

    OUTPUT_DIR.mkdir(exist_ok=True)
    tables = {name: pd.concat(parts, ignore_index=True) for name, parts in frames.items()}
    tables["hourly_microgrid_summary"] = pd.DataFrame(summary_rows)
    tables["switch_status"] = tables["hourly_microgrid_summary"][[
        "Hour", "Microgrid", "Operating_Mode", "PCC_Switch", "PCC_Closed", "Converged"
    ]].copy()
    measured = tables["der_dispatch"].loc[
        tables["der_dispatch"]["Type"] == "Battery",
        ["Hour", "Microgrid", "P_kW"],
    ].rename(columns={"P_kW": "Measured_Battery_kW"})
    verification = audit.merge(measured, on=["Hour", "Microgrid"], validate="one_to_one")
    verification = verification.rename(columns={"Battery_kW": "Commanded_Battery_kW"})
    verification["Dispatch_Error_kW"] = (
        verification["Measured_Battery_kW"] - verification["Commanded_Battery_kW"]
    )
    tables["battery_dispatch_verification"] = verification

    for name, table in tables.items():
        table.to_csv(OUTPUT_DIR / f"{name}.csv", index=False)
    audit.to_csv(OUTPUT_DIR / "optimized_battery_schedule.csv", index=False)

    # The combined power plot uses a common load/DER scale within each MG.
    # This dedicated plot gives every battery its own rating-based y-axis and
    # shows both the scheduled command and the measured OpenDSS output.
    fig, axes = plt.subplots(2, 2, figsize=(14, 8), sharex=True)
    for ax, mg_name in zip(axes.flat, base.MICROGRIDS):
        data = verification.loc[verification["Microgrid"] == mg_name]
        ax.step(
            data["Hour"], data["Commanded_Battery_kW"], where="mid",
            linewidth=2.0, label="Optimized command",
        )
        ax.plot(
            data["Hour"], data["Measured_Battery_kW"], "o", markersize=3.5,
            label="OpenDSS measured",
        )
        rating = BATTERY_RATED_KW[mg_name]
        ax.set_ylim(-1.1 * rating, 1.1 * rating)
        ax.axhline(0, color="black", linewidth=0.8)
        ax.set_title(f"{mg_name} ({rating:g} kW rated)")
        ax.set_ylabel("Battery power (kW)")
        ax.grid(alpha=0.25)
    for ax in axes[-1, :]:
        ax.set_xlabel("Hour")
    axes[0, 0].legend(loc="best")
    fig.suptitle("Price-Optimized Battery Dispatch (positive = discharge)")
    fig.tight_layout()
    battery_plot = OUTPUT_DIR / "optimized_battery_dispatch.png"
    fig.savefig(battery_plot, dpi=180)
    plt.close(fig)
    print(f"Saved plot: {battery_plot.name}")

    for plot_path in base.save_plots(tables):
        print(f"Saved plot: {plot_path.name}")
    try:
        with pd.ExcelWriter(OUTPUT_DIR / WORKBOOK_FILENAME) as writer:
            for name, table in tables.items():
                table.to_excel(writer, sheet_name=name[:31], index=False)
            audit.to_excel(writer, sheet_name="optimized_battery_schedule", index=False)
    except ImportError:
        print("openpyxl is unavailable; CSV outputs were saved, Excel was skipped.")

    tolerance = verification["Microgrid"].map(
        {name: max(0.25, 0.03 * rating) for name, rating in BATTERY_RATED_KW.items()}
    )
    failed = verification[verification["Dispatch_Error_kW"].abs() > tolerance]
    if not failed.empty:
        examples = failed[[
            "Hour", "Microgrid", "Commanded_Battery_kW", "Measured_Battery_kW"
        ]].head().to_dict("records")
        raise RuntimeError(f"Battery dispatch did not follow optimization: {examples}")


def run_power_flow(schedules: dict[str, list[float]], audit: pd.DataFrame) -> None:
    """Run 24 snapshots using this file's explicit inputs."""

    validate_study_inputs(schedules)
    apply_diesel_capacity_scaling()
    base.LOAD_SCALING = LOAD_SCALING
    base.SOLAR_SCALING = SOLAR_SCALING
    base.OTHER_LOAD_SCALING = OTHER_LOAD_SCALING
    base.ISLANDING_HOURS = ISLANDED_HOURS
    base.OUTPUT_DIR = OUTPUT_DIR
    base.compile_and_add_assets()
    regions = base.build_microgrid_bus_sets()
    load_ratings = base.capture_loads(regions)
    if not 0 < NONCRITICAL_SHEDDING_STEP <= 1:
        raise ValueError("NONCRITICAL_SHEDDING_STEP must be in (0, 1]")
    for mg_name, buses in CRITICAL_LOAD_BUSES.items():
        if mg_name not in regions:
            raise ValueError(f"Unknown critical-load microgrid: {mg_name}")
        missing = {str(bus).lower() for bus in buses} - regions[mg_name]
        if missing:
            raise ValueError(f"{mg_name} critical buses outside its region: {sorted(missing)}")

    # The common validator requires a single battery curve only for its base
    # case. All actual dispatch below uses the four independent schedules.
    base.validate_model(regions, load_ratings)
    frames: dict[str, list[pd.DataFrame]] = {
        "bus_voltages": [], "load_powers": [], "der_dispatch": [], "line_losses": [],
        "load_shedding": [],
    }
    summary_rows: list[dict] = []
    failures = []
    for hour in base.HOURS:
        base.set_loads(hour, load_ratings)
        base.set_solar(hour)
        configure_operating_state(hour, schedules)
        converged = base.solve()
        if converged and any(is_islanded(hour, name) for name in base.MICROGRIDS):
            curtail_islanded_solar_reverse_power(hour)
            equally_share_mg2_diesels(hour)
            converged = base.solve()
        if converged:
            frames["load_shedding"].append(enforce_islanded_diesel_limits(hour, load_ratings))
        if not converged:
            failures.append(hour)
        bus_df = base.extract_bus_voltages(hour, regions)
        load_df = base.extract_load_powers(hour, regions)
        der_df = extract_der_dispatch(hour)
        frames["bus_voltages"].append(bus_df)
        frames["load_powers"].append(load_df)
        frames["der_dispatch"].append(der_df)
        frames["line_losses"].append(base.extract_line_losses(hour))
        rows = base.make_summary(hour, converged, bus_df, load_df, der_df)
        for row in rows:
            row["Battery_Scale"] = schedules[row["Microgrid"]][hour]
        summary_rows.extend(rows)
        modes = ", ".join(
            f"{name}={'islanded' if is_islanded(hour, name) else 'grid'}"
            for name in base.MICROGRIDS
        )
        print(f"Hour {hour:02d}: {modes}, converged={converged}")
    save_results(frames, summary_rows, audit)
    if failures:
        raise RuntimeError(f"Power flow did not converge at hours: {failures}")


def main() -> None:
    """Optimize day-ahead battery operation, then run the power-flow study."""

    schedules, audit = optimize_all_batteries()
    print("Optimized day-ahead battery scaling factors:")
    for mg_name, values in schedules.items():
        formatted = ", ".join(f"{value:+.3f}" for value in values)
        print(f"{mg_name}: [{formatted}]")

    run_power_flow(schedules, audit)
    print(f"Optimized schedule saved to: {OUTPUT_DIR / 'optimized_battery_schedule.csv'}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
