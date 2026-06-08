"""
24-hour scaled power-flow analysis for IEEE123Maste_V3_Mod.dss.

This standalone script runs 24 snapshot power-flow cases. Base loads, added
loads, added generators, and storage are scaled independently for each hour.
"""

from pathlib import Path
import sys
import math

import pandas as pd
import opendssdirect as dss

def get_venv_site_packages():
    """Add virtualenv site-packages if running outside the venv."""
    if sys.prefix != sys.base_prefix:
        return

    venv_site = (
        Path(__file__).resolve().parent.parent
        / ".venv"
        / "Lib"
        / "site-packages"
    )
    if venv_site.exists():
        sys.path.insert(0, str(venv_site))


get_venv_site_packages()



BASE_DIR = Path(__file__).resolve().parent
DSS_FILE = BASE_DIR / "IEEE123Maste_V3_Mod.dss"
OUTPUT_DIR = BASE_DIR / "powerflow_results_24h_scaled"
OUTPUT_DIR.mkdir(exist_ok=True)


def compile_circuit():
    """Compile the OpenDSS circuit with relay error handling."""
    print(f"Compiling circuit from: {DSS_FILE}")

    if not DSS_FILE.exists():
        raise FileNotFoundError(f"DSS file not found: {DSS_FILE}")

    dss.Basic.ClearAll()

    with open(DSS_FILE, "r") as f:
        content = f.read()

    fixed_lines = []
    for line in content.split("\n"):
        if line.strip().lower().startswith("new relay.") and "switchedobj" not in line.lower():
            fixed_lines.append("! " + line)
            print(f"  Commented out: {line.strip()}")
        else:
            fixed_lines.append(line)

    temp_dss = BASE_DIR / "IEEE123Maste_V3_Mod_fixed.dss"
    with open(temp_dss, "w") as f:
        f.write("\n".join(fixed_lines))

    dss.Text.Command(f'compile "{temp_dss.resolve().as_posix()}"')
    print("[OK] Circuit compiled successfully (relays without SwitchedObj disabled)")


def add_loads(load_specs: list[tuple[int, float, float]]):
    """Add loads to the compiled circuit."""
    print("\nAdding specified loads to the circuit...")
    for spec in load_specs:
        try:
            bus, p_kw, q_kvar = spec
            name = f"add{bus}"
            dss.Text.Command(
                f"New Load.{name} Bus1={bus} Phases=3 Model=1 "
                f"kW={p_kw} kvar={q_kvar} kV=4.16"
            )
            print(f"  [OK] Added Load.{name} at Bus {bus} ({p_kw} kW, {q_kvar} kVAR)")
        except Exception as exc:
            print(f"  Warning: failed to add load for spec {spec}: {exc}")


def add_generators(gen_specs: list[tuple[int, float, float]]):
    """Add generators to the compiled circuit."""
    print("\nAdding generators to the circuit...")
    for spec in gen_specs:
        try:
            bus, p_kw, q_kvar = spec
            name = f"gen{bus}"
            dss.Text.Command(
                f"New Generator.{name} Bus1={bus} Phases=3 kV=4.16 "
                f"kW={p_kw} kvar={q_kvar} Enabled=No"
            )
            print(f"  [OK] Added Generator.{name} at Bus {bus} ({p_kw} kW, {q_kvar} kVAR)")
        except Exception as exc:
            print(f"  Warning: failed to add generator for spec {spec}: {exc}")


def add_storage(storage_specs: list[tuple[int, float, float, float]]):
    """Add energy storage devices to the compiled circuit."""
    print("\nAdding storage devices to the circuit...")
    for spec in storage_specs:
        try:
            bus, p_kw, energy_kwh, state = spec
            name = f"stor{bus}"
            dss.Text.Command(
                f"New Storage.{name} Bus1={bus} Phases=3 kV=4.16 "
                f"kW={p_kw} kWh={energy_kwh} State={state}"
            )
            print(
                f"  [OK] Added Storage.{name} at Bus {bus} "
                f"({p_kw} kW, {energy_kwh} kWh, state={state})"
            )
        except Exception as exc:
            print(f"  Warning: failed to add storage for spec {spec}: {exc}")


def set_source_voltage(source_name: str = "source", pu: float = 1.0):
    """Set the source voltage in per-unit for the specified Vsource."""
    print(f"\nSetting source voltage for Vsource.{source_name} to {pu} pu...")
    try:
        dss.Text.Command(f"Edit Vsource.{source_name} pu={pu}")
        print(f"  [OK] Source voltage for Vsource.{source_name} set to {pu} pu")
    except Exception as exc:
        print(f"  Warning: failed to set source voltage for Vsource.{source_name}: {exc}")


def configure_solution():
    """Configure solution parameters."""
    print("\nConfiguring solution parameters...")
    dss.Text.Command("set mode=snap")
    dss.Text.Command("set controlmode=off")
    dss.Text.Command("set algorithm=newton")
    print("[OK] Solution configured")


def run_powerflow():
    """Execute the power flow solution."""
    print("\nRunning power flow analysis...")
    dss.Solution.Solve()

    if dss.Solution.Converged():
        print("[OK] Power flow converged successfully")
        return True

    print("[ERROR] Power flow did NOT converge")
    return False


def extract_bus_results():
    """Extract and return bus voltage results."""
    results = []

    for bus_name in dss.Circuit.AllBusNames():
        dss.Circuit.SetActiveBus(bus_name)

        kv_base = dss.Bus.kVBase()
        v_magnitudes = dss.Bus.Voltages()

        for phase in range(1, 4):
            idx = (phase - 1) * 2
            if idx < len(v_magnitudes):
                v_real = v_magnitudes[idx]
                v_imag = v_magnitudes[idx + 1] if idx + 1 < len(v_magnitudes) else 0
                v_mag = (v_real**2 + v_imag**2) ** 0.5
                v_angle = math.degrees(math.atan2(v_imag, v_real))
                v_pu = v_mag / (kv_base * 1000) if kv_base > 0 else 0

                results.append(
                    {
                        "Bus": bus_name,
                        "Phase": phase,
                        "Node": f"{bus_name}.{phase}",
                        "kVBase": kv_base,
                        "Voltage_Real_V": v_real,
                        "Voltage_Imag_V": v_imag,
                        "Voltage_V": v_mag,
                        "Voltage_Angle_Deg": v_angle,
                        "Voltage_pu": v_pu,
                    }
                )

    return pd.DataFrame(results)


def extract_line_losses():
    """Extract and return line loss results."""
    results = []

    for line_name in dss.Lines.AllNames():
        dss.Lines.Name(line_name)
        dss.Circuit.SetActiveElement(f"Line.{line_name}")

        losses = dss.CktElement.Losses()
        p_loss = losses[0] / 1000 if losses else 0
        q_loss = losses[1] / 1000 if len(losses) > 1 else 0

        results.append(
            {
                "Line": line_name,
                "P_Loss_kW": p_loss,
                "Q_Loss_kVAR": q_loss,
                "S_Loss_kVA": (p_loss**2 + q_loss**2) ** 0.5,
            }
        )

    return pd.DataFrame(results)


def extract_load_results():
    """Extract and return load power results."""
    results = []

    for load_name in dss.Loads.AllNames():
        dss.Loads.Name(load_name)
        dss.Circuit.SetActiveElement(f"Load.{load_name}")

        bus = dss.CktElement.BusNames()[0].split(".")[0]
        power = dss.CktElement.Powers()
        p_total = sum(power[i] for i in range(0, len(power), 2))
        q_total = sum(power[i] for i in range(1, len(power), 2))

        results.append(
            {
                "Load": load_name,
                "Bus": bus,
                "P_kW": p_total,
                "Q_kVAR": q_total,
                "S_kVA": (p_total**2 + q_total**2) ** 0.5,
            }
        )

    return pd.DataFrame(results)


def print_summary():
    """Print a summary of circuit statistics."""
    print("\n" + "=" * 60)
    print("CIRCUIT SUMMARY")
    print("=" * 60)

    print(f"Number of buses: {len(dss.Circuit.AllBusNames())}")
    print(f"Number of lines: {len(dss.Lines.AllNames())}")
    print(f"Number of loads: {len(dss.Loads.AllNames())}")
    print(f"Number of transformers: {len(dss.Transformers.AllNames())}")


# Edit these 24-point profiles as needed. Hour 0 is midnight to 1 AM.
BASE_LOAD_SCALE_24H = [
    0.70, 0.66, 0.63, 0.61, 0.62, 0.68,
    0.78, 0.88, 0.95, 0.98, 1.00, 1.03,
    1.05, 1.04, 1.02, 1.00, 1.06, 1.15,
    1.20, 1.16, 1.08, 0.96, 0.84, 0.76,
]

ADDED_LOAD_SCALE_24H = [
    0.89, 0.88, 0.88, 0.88, 0.88, 0.89,
    0.90, 0.91, 0.92, 0.93, 0.94, 0.94,
    0.95, 0.95, 0.95, 0.95, 0.95, 0.94,
    0.94, 0.93, 0.92, 0.91, 0.90, 0.89,
]

# GENERATOR_SCALE_24H = [
#     0.50, 0.50, 0.50, 0.50, 0.55, 0.65,
#     0.75, 0.85, 0.90, 0.95, 1.00, 1.00,
#     1.00, 1.00, 0.95, 0.90, 0.90, 1.00,
#     1.00, 0.95, 0.85, 0.75, 0.65, 0.55,
# ]

GENERATOR_SCALE_24H = [
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
    0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
]

# Positive storage kW discharges into the circuit; negative values charge.
STORAGE_SCALE_24H = [
   -0.10, -0.10, -0.10, -0.08, -0.05,  0.00,
    0.00,  0.00,  0.05,  0.08,  0.10,  0.10,
    0.10,  0.10,  0.08,  0.05,  0.00,  0.00,
    0.00,  0.05,  0.08,  0.10,  0.05,  0.00,
]


ADDED_LOAD_SPECS = [(44, 1000, 300), (108, 1000, 300)]
ADDED_GENERATOR_SPECS = [(401, 200, 0), (1011, 200, 0)]
ADDED_STORAGE_SPECS = [(42, 200, 400, 0), (105, 200, 400, 0)]
STORAGE_INITIAL_SOC_PERCENT = 50.0


def validate_24h_profile(name: str, profile: list[float]):
    """Confirm that each hourly profile has one scale for each hour."""
    if len(profile) != 24:
        raise ValueError(f"{name} must contain exactly 24 values, got {len(profile)}")


def load_names_from_specs(load_specs: list[tuple[int, float, float]]) -> list[str]:
    return [f"add{bus}" for bus, _p_kw, _q_kvar in load_specs]


def generator_names_from_specs(gen_specs: list[tuple[int, float, float]]) -> list[str]:
    return [f"gen{bus}" for bus, _p_kw, _q_kvar in gen_specs]


def capture_load_ratings(load_names: list[str]) -> dict[str, tuple[float, float]]:
    ratings = {}
    for name in load_names:
        dss.Loads.Name(name)
        ratings[name] = (dss.Loads.kW(), dss.Loads.kvar())
    return ratings


def capture_generator_ratings(gen_names: list[str]) -> dict[str, tuple[float, float]]:
    ratings = {}
    for name in gen_names:
        dss.Generators.Name(name)
        ratings[name] = (dss.Generators.kW(), dss.Generators.kvar())
    return ratings


def capture_storage_ratings(
    storage_specs: list[tuple[int, float, float, float]]
) -> dict[str, tuple[float, float]]:
    ratings = {}
    for bus, p_kw, energy_kwh, _state in storage_specs:
        name = f"stor{bus}"
        ratings[name] = (p_kw, energy_kwh)
    return ratings


def configure_storage_ratings(storage_ratings: dict[str, tuple[float, float]]):
    for name, (rated_kw, rated_kwh) in storage_ratings.items():
        dss.Text.Command(
            f"Edit Storage.{name} kWrated={rated_kw:.6f} kWhrated={rated_kwh:.6f} "
            f"%stored={STORAGE_INITIAL_SOC_PERCENT:.6f} State=IDLING Enabled=Yes"
        )


def edit_loads(ratings: dict[str, tuple[float, float]], scale: float):
    for name, (base_kw, base_kvar) in ratings.items():
        dss.Text.Command(
            f"Edit Load.{name} kW={base_kw * scale:.6f} kvar={base_kvar * scale:.6f}"
        )


def edit_generators(ratings: dict[str, tuple[float, float]], scale: float):
    for name, (base_kw, base_kvar) in ratings.items():
        dss.Text.Command(
            f"Edit Generator.{name} kW={base_kw * scale:.6f} kvar={base_kvar * scale:.6f} Enabled=Yes"
        )


def edit_storage(ratings: dict[str, tuple[float, float]], scale: float):
    if scale > 0:
        state = "DISCHARGING"
        dispatch_setting = f"%discharge={abs(scale) * 100:.6f} %charge=0"
    elif scale < 0:
        state = "CHARGING"
        dispatch_setting = f"%charge={abs(scale) * 100:.6f} %discharge=0"
    else:
        state = "IDLING"
        dispatch_setting = "%charge=0 %discharge=0"

    for name, (rated_kw, _rated_kwh) in ratings.items():
        dss.Text.Command(
            f"Edit Storage.{name} kWrated={rated_kw:.6f} {dispatch_setting} State={state} Enabled=Yes"
        )


def apply_hourly_scaling(
    hour: int,
    base_load_ratings: dict[str, tuple[float, float]],
    added_load_ratings: dict[str, tuple[float, float]],
    generator_ratings: dict[str, tuple[float, float]],
    storage_ratings: dict[str, tuple[float, float]],
):
    edit_loads(base_load_ratings, BASE_LOAD_SCALE_24H[hour])
    edit_loads(added_load_ratings, ADDED_LOAD_SCALE_24H[hour])
    edit_generators(generator_ratings, GENERATOR_SCALE_24H[hour])
    edit_storage(storage_ratings, STORAGE_SCALE_24H[hour])


def extract_generator_results(hour: int) -> pd.DataFrame:
    rows = []
    for gen_name in dss.Generators.AllNames():
        dss.Generators.Name(gen_name)
        dss.Circuit.SetActiveElement(f"Generator.{gen_name}")
        power = dss.CktElement.Powers()
        p_total = sum(power[i] for i in range(0, len(power), 2))
        q_total = sum(power[i] for i in range(1, len(power), 2))
        rows.append(
            {
                "Hour": hour,
                "Generator": gen_name,
                "Bus": dss.CktElement.BusNames()[0].split(".")[0],
                "P_kW": p_total,
                "Q_kVAR": q_total,
            }
        )
    return pd.DataFrame(rows)


def extract_storage_results(hour: int) -> pd.DataFrame:
    rows = []
    for storage_name in dss.Storages.AllNames():
        dss.Storages.Name(storage_name)
        dss.Circuit.SetActiveElement(f"Storage.{storage_name}")
        power = dss.CktElement.Powers()
        p_total = sum(power[i] for i in range(0, len(power), 2))
        q_total = sum(power[i] for i in range(1, len(power), 2))
        rows.append(
            {
                "Hour": hour,
                "Storage": storage_name,
                "Bus": dss.CktElement.BusNames()[0].split(".")[0],
                "P_kW": p_total,
                "Q_kVAR": q_total,
                "State": dss.Storages.State(),
                "puSOC": dss.Storages.puSOC(),
            }
        )
    return pd.DataFrame(rows)


def add_hour_column(df: pd.DataFrame, hour: int) -> pd.DataFrame:
    if df.empty:
        return df
    df.insert(0, "Hour", hour)
    return df


def save_24h_excel_results(
    excel_file: Path,
    bus_df: pd.DataFrame,
    line_df: pd.DataFrame,
    load_df: pd.DataFrame,
    generator_df: pd.DataFrame,
    storage_df: pd.DataFrame,
    summary_df: pd.DataFrame,
):
    with pd.ExcelWriter(excel_file, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Hourly_Summary", index=False)
        bus_df.to_excel(writer, sheet_name="Bus_Voltages", index=False)
        line_df.to_excel(writer, sheet_name="Line_Losses", index=False)
        load_df.to_excel(writer, sheet_name="Load_Powers", index=False)
        generator_df.to_excel(writer, sheet_name="Generator_Powers", index=False)
        storage_df.to_excel(writer, sheet_name="Storage_Powers", index=False)


def save_voltage_profile_plot(bus_df: pd.DataFrame, plot_file: Path):
    """Save one figure with 24 hourly node-voltage subplots."""
    if bus_df.empty:
        print("  Warning: no bus voltage data available for plotting")
        return

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    first_hour = int(bus_df["Hour"].min())
    node_order = bus_df.loc[bus_df["Hour"] == first_hour, "Node"].tolist()
    x_values = range(len(node_order))

    fig, axes = plt.subplots(6, 4, figsize=(24, 18), sharex=True, sharey=True)
    axes = axes.flatten()

    y_min = max(0.0, bus_df["Voltage_pu"].min() - 0.02)
    y_max = bus_df["Voltage_pu"].max() + 0.02

    for hour in range(24):
        ax = axes[hour]
        hourly = bus_df.loc[bus_df["Hour"] == hour].set_index("Node")
        voltages = hourly.reindex(node_order)["Voltage_pu"]

        ax.plot(x_values, voltages, color="#1f77b4", linewidth=0.9)
        ax.axhline(1.1, color="#c44e52", linestyle="--", linewidth=0.8)
        ax.axhline(0.9, color="#c44e52", linestyle="--", linewidth=0.8)
        ax.set_title(f"Hour {hour:02d}", fontsize=10)
        ax.grid(True, alpha=0.25, linewidth=0.5)
        ax.set_ylim(y_min, y_max)

    tick_count = min(12, len(node_order))
    if tick_count > 1:
        tick_positions = [
            round(i * (len(node_order) - 1) / (tick_count - 1))
            for i in range(tick_count)
        ]
        tick_labels = [node_order[i] for i in tick_positions]
        for ax in axes:
            ax.set_xticks(tick_positions)
            ax.set_xticklabels(tick_labels, rotation=90, fontsize=6)

    fig.suptitle("24-Hour Voltage Profiles at Every Node", fontsize=18)
    fig.supxlabel("Node")
    fig.supylabel("Voltage (p.u.)")
    fig.tight_layout(rect=(0.02, 0.03, 1, 0.96))
    fig.savefig(plot_file, dpi=200)
    plt.close(fig)


def main():
    try:
        validate_24h_profile("BASE_LOAD_SCALE_24H", BASE_LOAD_SCALE_24H)
        validate_24h_profile("ADDED_LOAD_SCALE_24H", ADDED_LOAD_SCALE_24H)
        validate_24h_profile("GENERATOR_SCALE_24H", GENERATOR_SCALE_24H)
        validate_24h_profile("STORAGE_SCALE_24H", STORAGE_SCALE_24H)

        print("=" * 60)
        print("IEEE 123-Bus 24-Hour Scaled Power Flow Analysis")
        print("=" * 60)

        compile_circuit()
        base_load_names = list(dss.Loads.AllNames())

        add_loads(ADDED_LOAD_SPECS)
        add_generators(ADDED_GENERATOR_SPECS)
        add_storage(ADDED_STORAGE_SPECS)
        set_source_voltage("source", 1.0)
        configure_solution()

        added_load_names = load_names_from_specs(ADDED_LOAD_SPECS)
        added_generator_names = generator_names_from_specs(ADDED_GENERATOR_SPECS)
        base_load_ratings = capture_load_ratings(base_load_names)
        added_load_ratings = capture_load_ratings(added_load_names)
        generator_ratings = capture_generator_ratings(added_generator_names)
        storage_ratings = capture_storage_ratings(ADDED_STORAGE_SPECS)
        configure_storage_ratings(storage_ratings)

        bus_frames = []
        line_frames = []
        load_frames = []
        generator_frames = []
        storage_frames = []
        summary_rows = []

        for hour in range(24):
            print(f"\nRunning hour {hour:02d}...")
            apply_hourly_scaling(
                hour,
                base_load_ratings,
                added_load_ratings,
                generator_ratings,
                storage_ratings,
            )

            if not run_powerflow():
                summary_rows.append(
                    {
                        "Hour": hour,
                        "Converged": False,
                        "Base_Load_Scale": BASE_LOAD_SCALE_24H[hour],
                        "Added_Load_Scale": ADDED_LOAD_SCALE_24H[hour],
                        "Generator_Scale": GENERATOR_SCALE_24H[hour],
                        "Storage_Scale": STORAGE_SCALE_24H[hour],
                    }
                )
                continue

            bus_df = add_hour_column(extract_bus_results(), hour)
            line_df = add_hour_column(extract_line_losses(), hour)
            load_df = add_hour_column(extract_load_results(), hour)
            gen_df = extract_generator_results(hour)
            stor_df = extract_storage_results(hour)

            bus_frames.append(bus_df)
            line_frames.append(line_df)
            load_frames.append(load_df)
            generator_frames.append(gen_df)
            storage_frames.append(stor_df)

            summary_rows.append(
                {
                    "Hour": hour,
                    "Converged": True,
                    "Base_Load_Scale": BASE_LOAD_SCALE_24H[hour],
                    "Added_Load_Scale": ADDED_LOAD_SCALE_24H[hour],
                    "Generator_Scale": GENERATOR_SCALE_24H[hour],
                    "Storage_Scale": STORAGE_SCALE_24H[hour],
                    "Total_Load_P_kW": load_df["P_kW"].sum() if not load_df.empty else 0,
                    "Total_Load_Q_kVAR": load_df["Q_kVAR"].sum() if not load_df.empty else 0,
                    "Total_Generator_P_kW": gen_df["P_kW"].sum() if not gen_df.empty else 0,
                    "Total_Storage_P_kW": stor_df["P_kW"].sum() if not stor_df.empty else 0,
                    "Min_Voltage_pu": bus_df["Voltage_pu"].min() if not bus_df.empty else 0,
                    "Max_Voltage_pu": bus_df["Voltage_pu"].max() if not bus_df.empty else 0,
                }
            )

        bus_all = pd.concat(bus_frames, ignore_index=True) if bus_frames else pd.DataFrame()
        line_all = pd.concat(line_frames, ignore_index=True) if line_frames else pd.DataFrame()
        load_all = pd.concat(load_frames, ignore_index=True) if load_frames else pd.DataFrame()
        generator_all = pd.concat(generator_frames, ignore_index=True) if generator_frames else pd.DataFrame()
        storage_all = pd.concat(storage_frames, ignore_index=True) if storage_frames else pd.DataFrame()
        summary_df = pd.DataFrame(summary_rows)

        bus_file = OUTPUT_DIR / "bus_voltages_24h_scaled.csv"
        line_file = OUTPUT_DIR / "line_losses_24h_scaled.csv"
        load_file = OUTPUT_DIR / "load_powers_24h_scaled.csv"
        generator_file = OUTPUT_DIR / "generator_powers_24h_scaled.csv"
        storage_file = OUTPUT_DIR / "storage_powers_24h_scaled.csv"
        summary_file = OUTPUT_DIR / "hourly_summary_24h_scaled.csv"
        excel_file = OUTPUT_DIR / "powerflow_results_24h_scaled.xlsx"
        voltage_plot_file = OUTPUT_DIR / "voltage_profiles_24h_scaled.png"

        bus_all.to_csv(bus_file, index=False)
        line_all.to_csv(line_file, index=False)
        load_all.to_csv(load_file, index=False)
        generator_all.to_csv(generator_file, index=False)
        storage_all.to_csv(storage_file, index=False)
        summary_df.to_csv(summary_file, index=False)
        save_24h_excel_results(
            excel_file,
            bus_all,
            line_all,
            load_all,
            generator_all,
            storage_all,
            summary_df,
        )
        save_voltage_profile_plot(bus_all, voltage_plot_file)

        print(f"\n[OK] 24-hour results saved to {OUTPUT_DIR}")
        print(f"  - {bus_file.name}")
        print(f"  - {line_file.name}")
        print(f"  - {load_file.name}")
        print(f"  - {generator_file.name}")
        print(f"  - {storage_file.name}")
        print(f"  - {summary_file.name}")
        print(f"  - {excel_file.name}")
        print(f"  - {voltage_plot_file.name}")

        print_summary()
        if not summary_df.empty:
            print("\nHourly Summary:")
            print(summary_df)

        print("\n" + "=" * 60)
        print("24-hour analysis complete!")
        print("=" * 60)

    except Exception as exc:
        print(f"\n[ERROR] Error: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
