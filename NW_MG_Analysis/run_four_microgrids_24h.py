"""24-hour OpenDSS study for the four IEEE-123 microgrids.

The four PCC switches are opened at ``ISLANDING_HOURS``.  During an islanded
snapshot, a Vsource located at the selected diesel-generator bus represents
that diesel's grid-forming voltage/frequency control.  Its measured injection
is therefore the diesel power required after solar, battery dispatch, load,
and network losses.  The ordinary Generator element at the same bus is
disabled while its grid-forming Vsource is active.

Results are written as CSV files and, when openpyxl is installed, one Excel
workbook in ``powerflow_results_four_microgrids_24h``.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
import math
import sys

import opendssdirect as dss
import pandas as pd


# =============================================================================
# PART 1: FILE LOCATIONS AND SIMULATION PERIOD
# =============================================================================
# BASE_DIR makes every path relative to this script.  This allows the script to
# run from the repository root or directly from IEEE123_V3_Modified.
BASE_DIR = Path(__file__).resolve().parent
MASTER_DSS = BASE_DIR / "IEEE123Maste_V3_Mod.dss"

# The runtime copy has incomplete relay definitions commented out.  It remains
# beside the master so relative Redirect commands inside the DSS file continue
# to find the line-code, load, regulator, and transformer files.
RUNTIME_DSS = BASE_DIR / "IEEE123Maste_V3_Mod_islanding_runtime.dss"
OUTPUT_DIR = BASE_DIR / "powerflow_results_four_microgrids_24h"
HOURS = range(24)

# All four microgrids are islanded during these two snapshots.  Change this
# tuple to study two different hours.
ISLANDING_HOURS = (11, 18)

# This normally closed tie joins MG3 (bus 151) to MG4 (bus 300), bypassing
# the specified MG4 PCC.  It must open whenever the MGs island so that the
# four regions are electrically independent.
AUXILIARY_ISOLATION_LINES = ("sw7",)


# =============================================================================
# PART 2: 24-HOUR OPERATING PROFILES
# =============================================================================
# LOAD_SCALING contains one multiplier per hour and microgrid.  A value of 0.92,
# for example, sets every load assigned to that MG to 92% of its compiled DSS
# kW and kvar ratings.  The values come from MG_PF_v1.py.
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

# MG1 and MG2 use the same normalized solar curve from MG_PF_v1.py.  The curve
# is multiplied by each MG's total solar capacity defined later.
SOLAR_SCALING = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.10, 0.25, 0.45,
    0.65, 0.80, 0.92, 1.00, 0.95, 0.85, 0.70, 0.50,
    0.30, 0.10, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
]

# From run_powerflow_with_datacenters_24h.py. Positive means discharge.
BATTERY_SCALING = [
    -0.10, -0.10, -0.10, -0.08, -0.05, 0.00, 0.00, 0.00,
     0.05,  0.08,  0.10,  0.10,  0.10, 0.10, 0.08, 0.05,
     0.00,  0.00,  0.00,  0.05,  0.08, 0.10, 0.05, 0.00,
]

# Loads outside the four MG regions retain the general feeder profile used by
# run_powerflow_with_datacenters_24h.py.
OTHER_LOAD_SCALING = [
    0.3920, 0.3696, 0.3528, 0.3416, 0.3472, 0.3808,
    0.4368, 0.4928, 0.5320, 0.5488, 0.5600, 0.5768,
    0.5880, 0.5824, 0.5712, 0.5600, 0.5936, 0.6440,
    0.6720, 0.6496, 0.6048, 0.5376, 0.4704, 0.4256,
]


# =============================================================================
# PART 3: MICROGRID EQUIPMENT AND PCC DEFINITIONS
# =============================================================================
@dataclass(frozen=True)
class Microgrid:
    """Names, locations, and ratings needed to operate one microgrid.

    ``pcc_line`` is the OpenDSS switch/line that connects the MG to the feeder.
    ``root_bus`` is the first bus on the microgrid side of that PCC and is used
    to discover downstream buses.  DER names are stored without an OpenDSS
    class prefix except for ``battery_element``, which can be either Generator
    or Storage in the current model.
    """

    pcc_line: str
    root_bus: str
    solar_generators: tuple[str, ...]
    solar_capacity_kw: float
    battery_element: str
    battery_rated_kw: float
    diesel_generators: tuple[str, ...]
    forming_diesel: str
    forming_bus: str
    diesel_kva: dict[str, float]


MICROGRIDS = {
    # MG1: conventional MG downstream of the bus 18--21 switch.  The generator
    # model at bus 28 is operated as a battery, and the bus-301 unit is diesel.
    "MG1": Microgrid(
        "sw8", "21", ("mg1_gen1", "mg1_gen2"), 250.0,
        "Generator.mg1_gen3", 100.0, ("mg1_gen4",),
        "mg1_gen4", "301", {"mg1_gen4": 350.0},
    ),
    # MG2: conventional MG downstream of the bus 67--72 switch.  The bus-76
    # generator is the battery.  Bus 771 is the primary grid-forming diesel;
    # bus 931 provides support when the primary unit approaches its rating.
    "MG2": Microgrid(
        "sw9", "72", ("mg2_gen1", "mg2_gen5"), 750.0,
        "Generator.mg2_gen3", 50.0, ("mg2_gen4", "mg2_gen6"),
        "mg2_gen4", "771", {"mg2_gen4": 750.0, "mg2_gen6": 750.0},
    ),
    # MG3 and MG4 are data-center microgrids.  Their load, generator, and
    # Storage objects are added after compiling the base DSS circuit.
    "MG3": Microgrid(
        "sw3", "135", (), 0.0, "Storage.stor42", 1200.0,
        ("gen401",), "gen401", "401", {"gen401": 1200.0},
    ),
    "MG4": Microgrid(
        "sw5", "197", (), 0.0, "Storage.stor105", 1200.0,
        ("gen1011",), "gen1011", "1011", {"gen1011": 1200.0},
    ),
}


# =============================================================================
# PART 4: DSS COMPILATION AND MODEL PREPARATION
# =============================================================================
def bus_base(bus: str) -> str:
    """Return a lowercase bus name without phase/node suffixes.

    OpenDSS may return names such as ``21.1.2.3``.  Topology comparisons must
    use only the physical bus portion, which is ``21`` in that example.
    """

    return bus.split(".", 1)[0].lower()


def make_runtime_master() -> None:
    """Create a current relay-safe copy beside the master file.

    Several relay statements specify only ``monitoredObj`` and omit the
    required ``switchedObj`` property.  Those statements can prevent a clean
    OpenDSS compilation, so only those incomplete relay lines are commented.
    No feeder electrical data are otherwise changed.
    """

    content = MASTER_DSS.read_text(encoding="utf-8")
    fixed = []
    for line in content.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("new relay.") and "switchedobj" not in stripped:
            fixed.append("! Disabled by run_four_microgrids_24h.py: " + line)
        else:
            fixed.append(line)
    RUNTIME_DSS.write_text("\n".join(fixed) + "\n", encoding="utf-8")


def compile_and_add_assets() -> None:
    """Compile the feeder and create the data-center DER/load elements.

    The ordinary feeder source stays active throughout the study.  Four extra
    Vsource elements are created disabled; each is enabled only when its PCC is
    open and represents the voltage/frequency-forming action of that island's
    diesel generator.
    """

    if not MASTER_DSS.exists():
        raise FileNotFoundError(MASTER_DSS)
    make_runtime_master()
    dss.Basic.ClearAll()
    dss.Text.Command(f'compile "{RUNTIME_DSS.as_posix()}"')

    # Data-center elements use the names and buses in the existing 24-h script.
    dss.Text.Command(
        "New Load.add44 Bus1=44 Phases=3 Model=1 kW=1000 kvar=300 kV=4.16"
    )
    dss.Text.Command(
        "New Load.add108 Bus1=108 Phases=3 Model=1 kW=1000 kvar=300 kV=4.16"
    )
    dss.Text.Command(
        "New Generator.gen401 Bus1=401 Phases=3 kV=4.16 kW=10 kvar=0 "
        "kVA=1200 Enabled=No"
    )
    dss.Text.Command(
        "New Generator.gen1011 Bus1=1011 Phases=3 kV=4.16 kW=10 kvar=0 "
        "kVA=1200 Enabled=No"
    )
    dss.Text.Command(
        "New Storage.stor42 Bus1=42 Phases=3 kV=4.16 kWrated=1200 kVA=1200 "
        "kWhrated=100 %stored=100 %reserve=0 %IdlingkW=0 %Idlingkvar=0 "
        "State=IDLING Enabled=Yes"
    )
    dss.Text.Command(
        "New Storage.stor105 Bus1=105 Phases=3 kV=4.16 kWrated=1200 kVA=1200 "
        "kWhrated=100 %stored=100 %reserve=0 %IdlingkW=0 %Idlingkvar=0 "
        "State=IDLING Enabled=Yes"
    )

    for mg_name, mg in MICROGRIDS.items():
        # A stiff voltage source is the OpenDSS grid-forming representation.
        dss.Text.Command(
            f"New Vsource.gfm_{mg_name.lower()} Bus1={mg.forming_bus} "
            "Phases=3 BasekV=4.16 pu=1.0 angle=0 frequency=60 "
            "MVAsc3=100 MVAsc1=100 Enabled=No"
        )

    dss.Text.Command("Set mode=snap controlmode=off algorithm=newton maxiterations=100")


# =============================================================================
# PART 5: AUTOMATIC MICROGRID TOPOLOGY AND LOAD MAPPING
# =============================================================================
def enabled_element_buses(full_name: str) -> list[str]:
    """Return normalized terminal buses for an enabled OpenDSS element."""

    dss.Circuit.SetActiveElement(full_name)
    if not dss.CktElement.Enabled():
        return []
    return [bus_base(name) for name in dss.CktElement.BusNames()]


def build_microgrid_bus_sets() -> dict[str, set[str]]:
    """Find exclusive downstream regions, treating every MG PCC as a cut.

    Lines and transformers form an undirected connectivity graph.  PCC lines
    and the sw7 bypass tie are omitted from that graph.  Breadth-first search
    from each root bus then finds the buses electrically inside each island.
    This avoids maintaining fragile, handwritten bus/load lists.
    """

    graph: dict[str, set[str]] = defaultdict(set)
    pccs = {mg.pcc_line.lower() for mg in MICROGRIDS.values()}
    pccs.update(name.lower() for name in AUXILIARY_ISOLATION_LINES)

    for line_name in dss.Lines.AllNames():
        if line_name.lower() in pccs:
            continue
        buses = enabled_element_buses(f"Line.{line_name}")
        if len(buses) >= 2:
            graph[buses[0]].add(buses[1])
            graph[buses[1]].add(buses[0])

    for transformer_name in dss.Transformers.AllNames():
        buses = enabled_element_buses(f"Transformer.{transformer_name}")
        for left, right in zip(buses, buses[1:]):
            graph[left].add(right)
            graph[right].add(left)

    regions: dict[str, set[str]] = {}
    claimed: set[str] = set()
    for mg_name, mg in MICROGRIDS.items():
        seen = {mg.root_bus.lower()}
        queue = deque(seen)
        while queue:
            current = queue.popleft()
            for neighbor in graph[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        overlap = claimed.intersection(seen)
        if overlap:
            raise RuntimeError(f"Overlapping MG regions at buses: {sorted(overlap)}")
        regions[mg_name] = seen
        claimed.update(seen)
    return regions


def capture_loads(regions: dict[str, set[str]]) -> dict[str, dict[str, tuple[float, float]]]:
    """Capture nominal kW/kvar and assign every load to an MG or the feeder.

    Ratings are stored once, before hourly scaling, so each hour is calculated
    from the original value rather than compounding the preceding hour's scale.
    """

    ratings: dict[str, dict[str, tuple[float, float]]] = {
        name: {} for name in (*MICROGRIDS.keys(), "FEEDER")
    }
    for name in dss.Loads.AllNames():
        dss.Loads.Name(name)
        dss.Circuit.SetActiveElement(f"Load.{name}")
        bus = bus_base(dss.CktElement.BusNames()[0])
        group = next((mg for mg, buses in regions.items() if bus in buses), "FEEDER")
        ratings[group][name] = (float(dss.Loads.kW()), float(dss.Loads.kvar()))
    return ratings


def validate_model(regions: dict[str, set[str]], load_ratings) -> None:
    """Fail early when a profile, equipment name, or topology is inconsistent."""

    if len(set(ISLANDING_HOURS)) != 2 or any(h not in HOURS for h in ISLANDING_HOURS):
        raise ValueError("ISLANDING_HOURS must contain exactly two distinct hours from 0 to 23")
    for name, values in {**LOAD_SCALING, "solar": SOLAR_SCALING,
                         "battery": BATTERY_SCALING,
                         "feeder": OTHER_LOAD_SCALING}.items():
        if len(values) != 24:
            raise ValueError(f"{name} profile must have 24 values")
    all_generators = {name.lower() for name in dss.Generators.AllNames()}
    for mg_name, mg in MICROGRIDS.items():
        needed = set(mg.solar_generators) | set(mg.diesel_generators)
        if mg.battery_element.lower().startswith("generator."):
            needed.add(mg.battery_element.split(".", 1)[1].lower())
        missing = needed - all_generators
        if missing:
            raise RuntimeError(f"{mg_name} missing Generator elements: {sorted(missing)}")
        if not load_ratings[mg_name]:
            raise RuntimeError(f"No loads were mapped to {mg_name}; root={mg.root_bus}")
        if mg.forming_bus.lower() not in regions[mg_name]:
            raise RuntimeError(f"{mg_name} forming bus {mg.forming_bus} is outside its island")


# =============================================================================
# PART 6: APPLY HOURLY LOAD, SOLAR, BATTERY, AND SWITCH SETTINGS
# =============================================================================
def set_loads(hour: int, ratings) -> None:
    """Scale both real and reactive load from their captured nominal values."""

    for group, group_ratings in ratings.items():
        scale = OTHER_LOAD_SCALING[hour] if group == "FEEDER" else LOAD_SCALING[group][hour]
        for name, (kw, kvar) in group_ratings.items():
            dss.Text.Command(
                f"Edit Load.{name} kW={kw * scale:.8f} kvar={kvar * scale:.8f} Enabled=Yes"
            )


def set_solar(hour: int) -> None:
    """Apply the solar curve and split total MG solar equally between units.

    kVA is also set to the allocated installed capacity so that the MG_PF_v1
    capacity values, rather than old placeholder kW setpoints, define output.
    """

    for mg in MICROGRIDS.values():
        if not mg.solar_generators:
            continue
        each_kw = mg.solar_capacity_kw * SOLAR_SCALING[hour] / len(mg.solar_generators)
        each_kva = mg.solar_capacity_kw / len(mg.solar_generators)
        for name in mg.solar_generators:
            dss.Text.Command(
                f"Edit Generator.{name} kW={each_kw:.8f} kvar=0 "
                f"kVA={each_kva:.8f} model=1 Enabled=Yes"
            )


def set_battery(full_name: str, rated_kw: float, scale: float) -> None:
    """Apply signed battery dispatch to either Generator or Storage models.

    The conventional-MG batteries are Generator objects: positive kW injects
    power and negative kW absorbs power.  Data-center batteries are true
    Storage objects and therefore use OpenDSS CHARGING/DISCHARGING states.
    """

    class_name, name = full_name.split(".", 1)
    kw = rated_kw * scale
    if class_name.lower() == "generator":
        # A negative Generator kW represents charging load.
        dss.Text.Command(
            f"Edit Generator.{name} kW={kw:.8f} kvar=0 "
            f"kVA={rated_kw:.8f} model=1 Enabled=Yes"
        )
    else:
        if scale > 0:
            state = "DISCHARGING"
            setting = f"%discharge={100 * scale:.8f} %charge=0"
        elif scale < 0:
            state = "CHARGING"
            setting = f"%charge={-100 * scale:.8f} %discharge=0"
        else:
            state = "IDLING"
            setting = "%charge=0 %discharge=0"
        dss.Text.Command(
            f"Edit Storage.{name} kWrated={rated_kw:.8f} {setting} "
            f"State={state} Enabled=Yes"
        )


def configure_operating_state(hour: int) -> None:
    """Set PCC/tie positions and enable the correct grid-forming references.

    Grid-connected hours have closed PCCs, disabled local forming sources, and
    zero diesel setpoints.  Islanded hours open every PCC and sw7, disable each
    forming diesel's ordinary Generator representation, and enable the Vsource
    at the same generator bus.
    """

    islanded = hour in ISLANDING_HOURS
    for line_name in AUXILIARY_ISOLATION_LINES:
        dss.Text.Command(f"Edit Line.{line_name} Enabled={'No' if islanded else 'Yes'}")
    for mg_name, mg in MICROGRIDS.items():
        dss.Text.Command(f"Edit Line.{mg.pcc_line} Enabled={'No' if islanded else 'Yes'}")
        dss.Text.Command(
            f"Edit Vsource.gfm_{mg_name.lower()} Enabled={'Yes' if islanded else 'No'}"
        )
        for diesel in mg.diesel_generators:
            dss.Text.Command(
                f"Edit Generator.{diesel} kW=0 kvar=0 kVA={mg.diesel_kva[diesel]} "
                "model=1 Enabled=No"
            )
        set_battery(mg.battery_element, mg.battery_rated_kw, BATTERY_SCALING[hour])


# =============================================================================
# PART 7: POWER-FLOW SOLUTION AND ISLANDED DIESEL CONTROL
# =============================================================================
def solve() -> bool:
    """Initialize and solve one independent snapshot, returning convergence."""

    dss.Solution.InitSnap()
    dss.Solution.Solve()
    return bool(dss.Solution.Converged())


def element_output(full_name: str) -> tuple[float, float]:
    """Return terminal-one real/reactive output using the generation sign.

    OpenDSS reports positive power flowing *into* an element.  Negating the
    terminal-one values makes positive values consistently mean DER output.
    """

    dss.Circuit.SetActiveElement(full_name)
    powers = dss.CktElement.Powers()
    phases = max(1, dss.CktElement.NumPhases())
    terminal_one = powers[: 2 * phases]
    # OpenDSS reports power into an element; negate for DER/source output.
    return (-sum(terminal_one[0::2]), -sum(terminal_one[1::2]))


def support_overloaded_forming_diesels() -> None:
    """Dispatch a second diesel if the MG2 forming unit exceeds 90% kVA.

    The voltage-forming source always balances the island, so its measured
    output is checked after each solve.  If overloaded, a power-controlled
    support diesel is increased and the circuit is re-solved.  Iteration is
    needed because voltage-dependent load and losses change after redispatch.
    """

    for mg_name, mg in MICROGRIDS.items():
        support = [name for name in mg.diesel_generators if name != mg.forming_diesel]
        if not support:
            continue
        rating = mg.diesel_kva[mg.forming_diesel]
        support_dispatch = {name: 0.0 for name in support}
        for _iteration in range(8):
            p_kw, q_kvar = element_output(f"Vsource.gfm_{mg_name.lower()}")
            target_kva = 0.90 * rating
            if math.hypot(p_kw, q_kvar) <= target_kva + 0.5:
                break
            allowable_p = math.sqrt(max(0.0, target_kva**2 - q_kvar**2))
            required_support = max(0.0, p_kw - allowable_p)
            if required_support <= 0:
                break
            for name in support:
                headroom = 0.90 * mg.diesel_kva[name] - support_dispatch[name]
                increment = min(required_support, max(0.0, headroom))
                support_dispatch[name] += increment
                required_support -= increment
                dss.Text.Command(
                    f"Edit Generator.{name} kW={support_dispatch[name]:.8f} kvar=0 "
                    f"kVA={mg.diesel_kva[name]} model=1 Enabled=Yes"
                )
            if required_support > 1e-6:
                raise RuntimeError(
                    f"{mg_name} diesel capacity is insufficient by {required_support:.2f} kW"
                )
            if not solve():
                raise RuntimeError(f"{mg_name} did not converge after support-diesel dispatch")


def curtail_solar_reverse_power() -> None:
    """Curtail island PV when it would make a forming diesel absorb real power.

    A diesel is not used as an energy sink.  When its balancing Vsource reports
    negative kW, solar is reduced enough to leave approximately 1 kW of positive
    diesel output, and the circuit is solved again with that curtailed setpoint.
    """

    for mg_name, mg in MICROGRIDS.items():
        if not mg.solar_generators:
            continue
        p_forming, _q_forming = element_output(f"Vsource.gfm_{mg_name.lower()}")
        if p_forming >= 0.5:
            continue
        present = []
        for name in mg.solar_generators:
            p_kw, _q_kvar = element_output(f"Generator.{name}")
            present.append(max(0.0, p_kw))
        reduced_total = max(0.0, sum(present) + p_forming - 1.0)
        each_kw = reduced_total / len(mg.solar_generators)
        each_kva = mg.solar_capacity_kw / len(mg.solar_generators)
        for name in mg.solar_generators:
            dss.Text.Command(
                f"Edit Generator.{name} kW={each_kw:.8f} kvar=0 "
                f"kVA={each_kva:.8f} model=1 Enabled=Yes"
            )
    if not solve():
        raise RuntimeError("Power flow did not converge after island solar curtailment")


# =============================================================================
# PART 8: DETAILED RESULT EXTRACTION
# =============================================================================
def extract_bus_voltages(hour: int, regions) -> pd.DataFrame:
    """Return per-node voltage magnitude/angle with its MG assignment."""

    rows = []
    for bus in dss.Circuit.AllBusNames():
        dss.Circuit.SetActiveBus(bus)
        nodes = dss.Bus.Nodes()
        values = dss.Bus.puVmagAngle()
        group = next((mg for mg, buses in regions.items() if bus.lower() in buses), "FEEDER")
        for index, node in enumerate(nodes):
            rows.append({
                "Hour": hour, "Microgrid": group, "Bus": bus, "Node": node,
                "Voltage_pu": values[2 * index], "Angle_deg": values[2 * index + 1],
            })
    return pd.DataFrame(rows)


def extract_load_powers(hour: int, regions) -> pd.DataFrame:
    """Return solved real/reactive terminal power for every load."""

    rows = []
    for name in dss.Loads.AllNames():
        dss.Loads.Name(name)
        dss.Circuit.SetActiveElement(f"Load.{name}")
        bus = bus_base(dss.CktElement.BusNames()[0])
        group = next((mg for mg, buses in regions.items() if bus in buses), "FEEDER")
        powers = dss.CktElement.Powers()
        phases = max(1, dss.CktElement.NumPhases())
        terminal = powers[: 2 * phases]
        rows.append({"Hour": hour, "Microgrid": group, "Load": name, "Bus": bus,
                     "P_kW": sum(terminal[0::2]), "Q_kvar": sum(terminal[1::2])})
    return pd.DataFrame(rows)


def extract_der_dispatch(hour: int) -> pd.DataFrame:
    """Return solar, battery, and diesel outputs with DER type and rating.

    During islanding the forming diesel row is measured from its Vsource.
    During grid-connected operation it is measured from the ordinary Generator
    object, which is intentionally disabled and therefore reports zero output.
    """

    rows = []
    islanded = hour in ISLANDING_HOURS
    for mg_name, mg in MICROGRIDS.items():
        for name in mg.solar_generators:
            p, q = element_output(f"Generator.{name}")
            rows.append({"Hour": hour, "Microgrid": mg_name, "DER": name,
                         "Type": "Solar", "P_kW": p, "Q_kvar": q,
                         "Rating_kVA": mg.solar_capacity_kw / len(mg.solar_generators)})
        p, q = element_output(mg.battery_element)
        rows.append({"Hour": hour, "Microgrid": mg_name,
                     "DER": mg.battery_element.split(".", 1)[1], "Type": "Battery",
                     "P_kW": p, "Q_kvar": q, "Rating_kVA": mg.battery_rated_kw})
        for diesel in mg.diesel_generators:
            if islanded and diesel == mg.forming_diesel:
                full_name = f"Vsource.gfm_{mg_name.lower()}"
                der_type = "Diesel_grid_forming"
            else:
                full_name = f"Generator.{diesel}"
                der_type = "Diesel_support" if diesel != mg.forming_diesel else "Diesel"
            p, q = element_output(full_name)
            rows.append({"Hour": hour, "Microgrid": mg_name, "DER": diesel,
                         "Type": der_type, "P_kW": p, "Q_kvar": q,
                         "Rating_kVA": mg.diesel_kva[diesel]})
    return pd.DataFrame(rows)


def extract_line_losses(hour: int) -> pd.DataFrame:
    """Return enabled state and solved kW/kvar losses for every line."""

    rows = []
    for name in dss.Lines.AllNames():
        dss.Circuit.SetActiveElement(f"Line.{name}")
        losses = dss.CktElement.Losses()
        rows.append({"Hour": hour, "Line": name, "Enabled": dss.CktElement.Enabled(),
                     "P_loss_kW": losses[0] / 1000.0,
                     "Q_loss_kvar": losses[1] / 1000.0})
    return pd.DataFrame(rows)


def make_summary(hour: int, converged: bool, bus_df: pd.DataFrame,
                 load_df: pd.DataFrame, der_df: pd.DataFrame) -> list[dict]:
    """Aggregate detailed results into one row per hour and microgrid.

    The actual OpenDSS PCC enabled state determines ``Operating_Mode``.  Zero or
    near-zero dummy/open-phase nodes are retained in the detailed voltage CSV
    but excluded from the meaningful minimum/maximum voltage envelope.
    """

    rows = []
    for mg_name, mg in MICROGRIDS.items():
        dss.Circuit.SetActiveElement(f"Line.{mg.pcc_line}")
        pcc_closed = bool(dss.CktElement.Enabled())
        voltages = bus_df.loc[
            (bus_df["Microgrid"] == mg_name) & (bus_df["Voltage_pu"] > 0.5),
            "Voltage_pu",
        ]
        loads = load_df.loc[load_df["Microgrid"] == mg_name]
        ders = der_df.loc[der_df["Microgrid"] == mg_name]
        forming = ders.loc[ders["Type"] == "Diesel_grid_forming"]
        forming_kva = float(
            (forming["P_kW"] ** 2 + forming["Q_kvar"] ** 2).pow(0.5).sum()
        ) if not forming.empty else 0.0
        rating = mg.diesel_kva[mg.forming_diesel]
        rows.append({
            "Hour": hour, "Microgrid": mg_name,
            "Operating_Mode": "Grid_connected" if pcc_closed else "Islanded",
            "PCC_Switch": f"Line.{mg.pcc_line}",
            "PCC_Closed": pcc_closed, "Converged": converged,
            "Load_Scale": LOAD_SCALING[mg_name][hour],
            "Solar_Scale": SOLAR_SCALING[hour] if mg.solar_generators else 0.0,
            "Battery_Scale": BATTERY_SCALING[hour],
            "Load_P_kW": loads["P_kW"].sum(), "Load_Q_kvar": loads["Q_kvar"].sum(),
            "Solar_P_kW": ders.loc[ders["Type"] == "Solar", "P_kW"].sum(),
            "Battery_P_kW": ders.loc[ders["Type"] == "Battery", "P_kW"].sum(),
            "Diesel_P_kW": ders.loc[ders["Type"].str.startswith("Diesel"), "P_kW"].sum(),
            "Diesel_Q_kvar": ders.loc[ders["Type"].str.startswith("Diesel"), "Q_kvar"].sum(),
            "Forming_Diesel_Loading_pct": 100 * forming_kva / rating,
            "Min_Voltage_pu": voltages.min() if not voltages.empty else math.nan,
            "Max_Voltage_pu": voltages.max() if not voltages.empty else math.nan,
        })
    return rows


# =============================================================================
# PART 9: RESULT PLOTS
# =============================================================================
def mark_islanding_hours(ax) -> None:
    """Shade configured islanding snapshots on an hourly plot."""
    for index, hour in enumerate(ISLANDING_HOURS):
        ax.axvspan(
            hour - 0.5,
            hour + 0.5,
            color="#d62728",
            alpha=0.10,
            label="Islanded hour" if index == 0 else None,
        )


def save_plots(tables: dict[str, pd.DataFrame]) -> list[Path]:
    """Create power, voltage, and islanded-dispatch result plots.

    The Agg backend renders PNG files without opening GUI windows, which makes
    the script suitable for terminals, scheduled jobs, and remote machines.
    """

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    summary = tables["hourly_microgrid_summary"]
    saved: list[Path] = []

    fig, axes = plt.subplots(2, 2, figsize=(15, 10), sharex=True)
    # Plot the four resource traces separately for each microgrid.  This makes
    # the sudden diesel response at islanding easy to compare with load/solar.
    for ax, mg_name in zip(axes.flat, MICROGRIDS):
        data = summary.loc[summary["Microgrid"] == mg_name].sort_values("Hour")
        ax.plot(data["Hour"], data["Load_P_kW"], marker="o", markersize=3,
                linewidth=1.7, label="Load")
        ax.plot(data["Hour"], data["Solar_P_kW"], linewidth=1.5, label="Solar")
        ax.plot(data["Hour"], data["Battery_P_kW"], linewidth=1.5, label="Battery")
        ax.plot(data["Hour"], data["Diesel_P_kW"], linewidth=1.7, label="Diesel")
        mark_islanding_hours(ax)
        ax.axhline(0, color="black", linewidth=0.6)
        ax.set_title(mg_name)
        ax.set_ylabel("Real power (kW)")
        ax.set_xticks(range(0, 24, 2))
        ax.grid(True, alpha=0.25)
    axes[1, 0].set_xlabel("Hour")
    axes[1, 1].set_xlabel("Hour")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.suptitle("Four-Microgrid 24-Hour Power Profiles", y=0.995, fontsize=14)
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.965),
               ncol=5, frameon=False)
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    path = OUTPUT_DIR / "microgrid_power_profiles.png"
    fig.savefig(path, dpi=200)
    plt.close(fig)
    saved.append(path)

    fig, axes = plt.subplots(2, 2, figsize=(15, 9), sharex=True, sharey=True)
    # The filled band spans the lowest and highest valid bus voltage in each MG.
    for ax, mg_name in zip(axes.flat, MICROGRIDS):
        data = summary.loc[summary["Microgrid"] == mg_name].sort_values("Hour")
        ax.fill_between(
            data["Hour"].to_numpy(dtype=float),
            data["Min_Voltage_pu"].to_numpy(dtype=float),
            data["Max_Voltage_pu"].to_numpy(dtype=float),
            alpha=0.25,
            color="#1f77b4",
            label="Min-max envelope",
        )
        ax.plot(data["Hour"], data["Min_Voltage_pu"], linewidth=1.2, label="Minimum")
        ax.plot(data["Hour"], data["Max_Voltage_pu"], linewidth=1.2, label="Maximum")
        ax.axhline(0.90, color="#d62728", linestyle="--", linewidth=1, label="0.90/1.10 limits")
        ax.axhline(1.10, color="#d62728", linestyle="--", linewidth=1)
        mark_islanding_hours(ax)
        ax.set_title(mg_name)
        ax.set_ylabel("Voltage (p.u.)")
        ax.set_xticks(range(0, 24, 2))
        ax.grid(True, alpha=0.25)
    axes[1, 0].set_xlabel("Hour")
    axes[1, 1].set_xlabel("Hour")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.suptitle("Microgrid Bus-Voltage Envelopes", y=0.995, fontsize=14)
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.965),
               ncol=5, frameon=False)
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    path = OUTPUT_DIR / "microgrid_voltage_envelopes.png"
    fig.savefig(path, dpi=200)
    plt.close(fig)
    saved.append(path)

    islanded = summary.loc[summary["Operating_Mode"] == "Islanded"].copy()
    # A grouped bar chart gives a direct load-versus-resource comparison for
    # every MG at the two islanding snapshots.
    islanded["Case"] = islanded.apply(
        lambda row: f"H{int(row['Hour']):02d}\n{row['Microgrid']}", axis=1
    )
    x_values = list(range(len(islanded)))
    width = 0.20
    fig, ax = plt.subplots(figsize=(15, 7))
    series = [
        ("Load_P_kW", "Load", "#4c78a8"),
        ("Solar_P_kW", "Solar", "#f2cf5b"),
        ("Battery_P_kW", "Battery", "#59a14f"),
        ("Diesel_P_kW", "Diesel", "#e15759"),
    ]
    for offset, (column, label, color) in enumerate(series):
        positions = [x + (offset - 1.5) * width for x in x_values]
        ax.bar(positions, islanded[column], width=width, label=label, color=color)
    ax.axhline(0, color="black", linewidth=0.7)
    ax.set_xticks(x_values)
    ax.set_xticklabels(islanded["Case"])
    ax.set_ylabel("Real power (kW)")
    ax.set_xlabel("Islanded snapshot and microgrid")
    ax.set_title("Islanded-Hour Load and DER Dispatch")
    ax.grid(True, axis="y", alpha=0.25)
    ax.legend(ncol=4, frameon=False)
    fig.tight_layout()
    path = OUTPUT_DIR / "islanded_load_and_der_dispatch.png"
    fig.savefig(path, dpi=200)
    plt.close(fig)
    saved.append(path)

    return saved


# =============================================================================
# PART 10: CSV/EXCEL EXPORT
# =============================================================================
def save_results(frames: dict[str, list[pd.DataFrame]], summary_rows: list[dict]) -> None:
    """Combine hourly frames and save detailed tables, workbook, and plots.

    CSV is always produced.  Excel is optional so a missing openpyxl package
    does not discard the simulation results or plots.
    """

    OUTPUT_DIR.mkdir(exist_ok=True)
    tables = {name: pd.concat(parts, ignore_index=True) for name, parts in frames.items()}
    tables["hourly_microgrid_summary"] = pd.DataFrame(summary_rows)
    tables["switch_status"] = tables["hourly_microgrid_summary"][[
        "Hour", "Microgrid", "Operating_Mode", "PCC_Switch", "PCC_Closed", "Converged"
    ]]
    auxiliary_rows = []
    for hour in HOURS:
        for line_name in AUXILIARY_ISOLATION_LINES:
            auxiliary_rows.append({
                "Hour": hour,
                "Microgrid": "MG3-MG4 auxiliary tie",
                "Operating_Mode": "Islanded" if hour in ISLANDING_HOURS else "Grid_connected",
                "PCC_Switch": f"Line.{line_name}",
                "PCC_Closed": hour not in ISLANDING_HOURS,
                "Converged": bool(
                    tables["hourly_microgrid_summary"].loc[
                        tables["hourly_microgrid_summary"]["Hour"] == hour,
                        "Converged",
                    ].all()
                ),
            })
    tables["switch_status"] = pd.concat(
        [tables["switch_status"], pd.DataFrame(auxiliary_rows)], ignore_index=True
    ).sort_values(["Hour", "Microgrid"])
    for name, table in tables.items():
        table.to_csv(OUTPUT_DIR / f"{name}.csv", index=False)
    for plot_path in save_plots(tables):
        print(f"Saved plot: {plot_path.name}")
    try:
        with pd.ExcelWriter(OUTPUT_DIR / "four_microgrids_24h_results.xlsx") as writer:
            for name, table in tables.items():
                table.to_excel(writer, sheet_name=name[:31], index=False)
    except ImportError:
        print("openpyxl is not installed; CSV outputs were saved, Excel was skipped.")


# =============================================================================
# PART 11: MAIN 24-HOUR WORKFLOW
# =============================================================================
def main() -> None:
    """Build the circuit, run 24 snapshots, and save/print all results."""

    # Compile once.  Snapshot setpoints and switch states are then edited in
    # place for each hour, avoiding unnecessary model recompilation.
    compile_and_add_assets()
    regions = build_microgrid_bus_sets()
    load_ratings = capture_loads(regions)
    validate_model(regions, load_ratings)
    for mg_name in MICROGRIDS:
        total_kw = sum(value[0] for value in load_ratings[mg_name].values())
        print(f"{mg_name}: {len(regions[mg_name])} buses, "
              f"{len(load_ratings[mg_name])} loads, {total_kw:.1f} nominal kW")

    frames: dict[str, list[pd.DataFrame]] = {
        "bus_voltages": [], "load_powers": [], "der_dispatch": [], "line_losses": []
    }
    summary_rows: list[dict] = []
    failures = []
    for hour in HOURS:
        # 1. Apply this hour's demand and DER schedules.
        set_loads(hour, load_ratings)
        set_solar(hour)
        configure_operating_state(hour)

        # 2. Solve once, then perform island-only balancing refinements.  Solar
        # curtailment is evaluated before bringing on a supporting diesel.
        converged = solve()
        if converged and hour in ISLANDING_HOURS:
            curtail_solar_reverse_power()
            support_overloaded_forming_diesels()
            converged = solve()
        if not converged:
            failures.append(hour)

        # 3. Extract the solved state even if a case failed, so diagnostics are
        # available in the output instead of silently losing that hour.
        bus_df = extract_bus_voltages(hour, regions)
        load_df = extract_load_powers(hour, regions)
        der_df = extract_der_dispatch(hour)
        frames["bus_voltages"].append(bus_df)
        frames["load_powers"].append(load_df)
        frames["der_dispatch"].append(der_df)
        frames["line_losses"].append(extract_line_losses(hour))
        summary_rows.extend(make_summary(hour, converged, bus_df, load_df, der_df))
        mode = "ISLANDED" if hour in ISLANDING_HOURS else "grid-connected"
        print(f"Hour {hour:02d}: {mode}, converged={converged}")

    # Consolidate and persist all hourly records only after the time loop.
    save_results(frames, summary_rows)
    summary = pd.DataFrame(summary_rows)
    island_summary = summary.loc[summary["Operating_Mode"] == "Islanded", [
        "Hour", "Microgrid", "Converged", "Load_P_kW", "Solar_P_kW",
        "Battery_P_kW", "Diesel_P_kW", "Forming_Diesel_Loading_pct",
        "Min_Voltage_pu", "Max_Voltage_pu",
    ]]
    print("\nIslanded-hour summary:")
    print(island_summary.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print(f"\nResults saved to: {OUTPUT_DIR}")
    if failures:
        raise RuntimeError(f"Power flow did not converge at hours: {failures}")


if __name__ == "__main__":
    # Keep a traceback for engineering diagnostics while also printing a short
    # error line that is visible in redirected console logs.
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
