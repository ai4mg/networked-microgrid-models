# Four-Microgrid 24-Hour Battery Study: Quick Guide

`run_four_microgrids_24h_price_optimized_battery.py` optimizes MG1/MG2 battery
dispatch using day-ahead prices, then applies the schedules to 24 independent
OpenDSS hourly snapshots for four microgrids. MG3/MG4 batteries remain idle as
full-charge reserves.

For implementation details and proposed extensions, see the
[full documentation](RUN_FOUR_MICROGRIDS_24H_PRICE_OPTIMIZED_BATTERY.md).

## Run

From the repository root in PowerShell:

```powershell
.\.venv\Scripts\python.exe .\MG_Analysis_2\run_four_microgrids_24h_price_optimized_battery.py
```

Required packages: `opendssdirect.py`, `pandas`, and `matplotlib`.
`openpyxl` is optional for Excel export. No external optimization solver is needed.

Keep the local helper `run_four_microgrids_24h.py` and feeder files together in
`MG_Analysis_2`: `IEEE123Maste_V3_Mod.dss`, `IEEELineCodes.DSS`,
`IEEE123Regulators.DSS`, `IEEE123Transformers.DSS`,
`IEEE123SecondaryLoads.DSS`, and `BusCoords.dat`.
The helper generates a runtime master with incomplete relay statements commented out.

## Microgrids and battery conventions

| Microgrid | Battery | Energy | Initial SOC | Policy |
|---|---:|---:|---:|---|
| MG1 | 100 kW | 400 kWh | 50% | Price optimization |
| MG2 | 50 kW | 200 kWh | 50% | Price optimization |
| MG3 | 1,200 kW | 100 kWh | 100% | Zero dispatch; reserve |
| MG4 | 1,200 kW | 100 kWh | 100% | Zero dispatch; reserve |

Positive battery kW means discharge; negative means charge.
`Battery_Scale = Battery_kW / BATTERY_RATED_KW`, within `[-1, 1]`.
Hours are 0–23, with one-hour intervals. MG1/MG2 use OpenDSS Generator objects;
MG3/MG4 use Storage objects.

## Configure inputs

Edit constants near the top of the main script. The program has no command-line
arguments and does not load scenario inputs from CSV, JSON, YAML, or Excel.

| Setting | Purpose / requirement |
|---|---|
| `DAY_AHEAD_PRICE_USD_PER_MWH` | Exactly 24 finite prices each for MG1 and MG2 |
| `BATTERY_SCHEDULE_INPUTS` | Energy, SOC bounds, final SOC, efficiencies, and wear cost |
| `BATTERY_RATED_KW` | Power ratings; must match the helper's equipment definitions |
| `SOC_STEP_PCT` | Default 1%; SOC settings must align with this step |
| `LOAD_SCALING`, `OTHER_LOAD_SCALING`, `SOLAR_SCALING` | 24 finite multipliers per profile |
| `ISLANDED_HOURS`, `MICROGRID_STATUSES` | Default event hours `(11, 18)`; specify all four MG statuses at each event |
| `DIESEL_CAPACITY_SCALING_PCT` | Positive capacity percentages for all four MGs; default 100 each |
| `CRITICAL_LOAD_BUSES` | Protected buses in MG1/MG2, without phase suffixes |
| `NONCRITICAL_SHEDDING_STEP` | Default `0.01`: reduce eligible hourly load requests by 1 percentage point per iteration |
| `OUTPUT_DIR`, `WORKBOOK_FILENAME` | Result location and workbook name |

Accepted statuses are `ISLANDED` and `GRID_CONNECTED`; non-event hours are
grid-connected. Keep upstream `sw2` closed and auxiliary tie `sw7` open for
the documented topology.

MG1/MG2 default battery settings are 20–90% SOC, 50% initial/final SOC,
95% charge/discharge efficiency, and a 2 USD/MWh throughput penalty.
MG3/MG4 receive 24 zero commands through `DATA_CENTER_BATTERY_SCALING`.

Nominal diesel capacities are MG1: 350 kVA; MG2: two 750 kVA units;
MG3/MG4: 1,200 kVA each. A percentage of `50.0` halves the corresponding
rating; zero is invalid. MG2 scaling applies separately to both units.

## How the study works

1. **Optimize batteries.** Dynamic programming finds the least-cost MG1/MG2
   SOC path, enforcing power limits, efficiencies, SOC bounds, and exact final SOC.
   The hourly objective is purchase cost minus discharge revenue plus a
   charge/discharge throughput penalty, with prices converted to USD/kWh.
2. **Run power flows.** Compile the feeder, discover microgrid regions, then
   apply hourly loads, solar, topology, and battery commands. Islanded MGs use
   local forming sources; excess islanded solar is curtailed, and MG2's two
   diesels share real/reactive demand.
3. **Enforce diesel capacity.** Check solved diesel apparent power
   `sqrt(P_kW² + Q_kvar²)` against scaled ratings. Shed eligible non-critical
   loads proportionally and re-solve until feasible. Critical loads and battery
   commands stay fixed. MG3/MG4 loads are protected by default. Persistent
   overload aborts the run; shedding resets at the next hour.
4. **Save and verify.** Export results and compare commanded versus measured
   battery power. Validation, convergence, capacity, or dispatch failures
   produce a nonzero exit and an `ERROR:` message.

The optimizer uses only prices and battery assumptions. Load, solar, islanding,
diesel limits, and network results do not change its schedules.

## Outputs

Results are saved under:

```text
MG_Analysis_2/powerflow_results_four_microgrids_24h_price_optimized_battery/
```

| Output | Contents |
|---|---|
| `optimized_battery_schedule.csv` | Hourly SOC, battery kW/scale, prices, objective, and dispatch policy |
| `battery_dispatch_verification.csv` | Commanded/measured battery kW and error |
| `hourly_microgrid_summary.csv` | MG status, load, DER power, diesel loading, voltage, and convergence |
| `der_dispatch.csv` | Individual DER P/Q and ratings |
| `load_shedding.csv` | Retained non-critical load percentage and shed kW/kvar setpoints |
| `bus_voltages.csv`, `load_powers.csv`, `line_losses.csv`, `switch_status.csv` | Detailed electrical results |
| `four_microgrids_24h_price_optimized_battery.xlsx` | Same tables as workbook sheets, when `openpyxl` is available |
| PNG plots | Battery dispatch, MG power profiles, voltage envelopes, and islanded dispatch |

Schedule and verification tables contain 96 rows: four MGs × 24 hours.
Data-center price fields are blank because MG3/MG4 are not optimized.
Shedding values describe requested setpoint reductions, not measured unserved energy.
Files are reused on subsequent runs; preserve results before comparing scenarios.
An aborted run may leave older output files in place.

## Checks and limitations

Run the integration checks from the repository root:

```powershell
.\.venv\Scripts\python.exe .\MG_Analysis_2\test_price_optimized_diesel_limits.py
```

These checks cover capacity scaling, invalid capacities, overload shedding,
critical-load protection, infeasibility, and next-hour load restoration.

- SOC chronology is enforced by the optimizer. OpenDSS evaluates independent
  snapshots and does not integrate MG1/MG2 storage energy.
- Diesel checks enforce aggregate terminal kVA; they do not model engine
  dynamics, frequency response, or physical source current saturation.
- Voltage and line/transformer limits are not enforced by the optimization or
  diesel-capacity controller; inspect electrical results separately.
- MG3/MG4 emergency battery dispatch is not implemented by the zero reserve schedule.
- For infeasible dispatch, review SOC/rating inputs, diesel capacity, protected
  buses, and load profiles. For missing battery output, inspect the verification
  CSV, bus voltages, switch states, and battery enabled state.
- If Excel is missing, install `openpyxl` or use the CSV files.
