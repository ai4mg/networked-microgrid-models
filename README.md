# Networked Microgrid Models

24-hour scaled power-flow analysis of a modified IEEE 123-bus distribution
feeder using [OpenDSS](https://www.epri.com/pages/sa/opendss) via
[`opendssdirect.py`](https://dss-extensions.org/OpenDSSDirect.py/).

The simulation runs 24 snapshot power-flow cases (one per hour) with
independent hourly scaling profiles for base loads, added loads, generators,
and battery storage, and writes per-hour CSV/Excel reports plus a 24-panel
voltage-profile plot.

## Repository layout

```
networked-microgrid-models/
├── data/                # OpenDSS circuit files (IEEE 123-bus, modified)
│   ├── IEEE123Maste_V3_Mod.dss
│   ├── IEEE123Loads.DSS
│   ├── IEEE123Regulators.DSS
│   ├── IEEE123SecondaryLoads.DSS
│   ├── IEEE123Transformers.DSS
│   ├── IEEELineCodes.DSS
│   └── BusCoords.dat
├── doc/                 # Feeder diagram (PNG + draw.io source)
├── src/
│   └── run_powerflow_DCMG.py
├── out/                 # Generated results (gitignored)
├── requirements.txt
└── README.md
```

## Requirements

- Python 3.10+
- Packages listed in [`requirements.txt`](requirements.txt):
  `pandas`, `opendssdirect.py`, `openpyxl`, `matplotlib`

## Setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## Running the 24-hour power-flow study

From the repository root:

```bash
python src/run_powerflow_DCMG.py
```

On the first run the script reads
[data/IEEE123Maste_V3_Mod.dss](data/IEEE123Maste_V3_Mod.dss), comments out any
`Relay` definitions that lack a `SwitchedObj` target, and writes the cleaned
file back to `data/IEEE123Maste_V3_Mod_fixed.dss` before compiling.

Results are written to `out/powerflow_results_24h_scaled/`:

| File | Contents |
| --- | --- |
| `hourly_summary_24h_scaled.csv` | Per-hour convergence, scaling factors, totals, min/max voltage |
| `bus_voltages_24h_scaled.csv` | Per-hour, per-node voltage magnitudes and angles |
| `line_losses_24h_scaled.csv` | Per-hour real and reactive line losses |
| `load_powers_24h_scaled.csv` | Per-hour load power consumption |
| `generator_powers_24h_scaled.csv` | Per-hour generator dispatch |
| `storage_powers_24h_scaled.csv` | Per-hour storage P, Q, state, and SOC |
| `powerflow_results_24h_scaled.xlsx` | All of the above as one Excel workbook |
| `voltage_profiles_24h_scaled.png` | 6×4 grid of hourly voltage profiles |

## Configuring the scenario

The scenario is defined by the constants near the top of
[src/run_powerflow_DCMG.py](src/run_powerflow_DCMG.py):

- `ADDED_LOAD_SPECS` — extra three-phase loads as
  `(bus, kW, kvar)`.
- `ADDED_GENERATOR_SPECS` — extra generators as
  `(bus, kW, kvar)`.
- `ADDED_STORAGE_SPECS` — battery storage as
  `(bus, kW, kWh, state)`; `STORAGE_INITIAL_SOC_PERCENT` sets the
  starting state of charge.
- `BASE_LOAD_SCALE_24H` — 24-value multiplier applied to every base load.
- `ADDED_LOAD_SCALE_24H_BY_LOAD` — 24-value profile per added load
  (keyed by the auto-generated `add<bus>` name).
- `GENERATOR_SCALE_24H` — 24-value generator dispatch multiplier.
- `STORAGE_SCALE_24H` — 24-value storage dispatch multiplier; positive
  values discharge into the circuit, negative values charge.

All profiles must contain exactly 24 entries (hour 0 = midnight–1 AM);
the script validates this at startup.

## Documentation

A diagram of the modified feeder is in [doc/](doc/):

- [123_AI4MG.png](doc/123_AI4MG.png) — rendered image
- [Modified_123_AI4MG.drawio](doc/Modified_123_AI4MG.drawio) — editable source
