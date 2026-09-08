"""Integration check: run with the same Python environment as the study."""
import math
import unittest

import MG_Analysis_2.run_four_microgrids_24h_battery_optimized as study


class DieselLimitsTest(unittest.TestCase):
    def test_capacity_scaling_does_not_compound(self):
        original = dict(study.DIESEL_CAPACITY_SCALING_PCT)
        try:
            study.DIESEL_CAPACITY_SCALING_PCT.update(MG1=50.0, MG2=80.0)
            for _ in range(2):
                study.apply_diesel_capacity_scaling()
                self.assertEqual(study.base.MICROGRIDS['MG1'].diesel_kva['mg1_gen4'], 175.0)
                self.assertEqual(set(study.base.MICROGRIDS['MG2'].diesel_kva.values()), {600.0})
            study.DIESEL_CAPACITY_SCALING_PCT['MG1'] = 0
            with self.assertRaises(ValueError):
                study.apply_diesel_capacity_scaling()
        finally:
            study.DIESEL_CAPACITY_SCALING_PCT.update(original)
            study.apply_diesel_capacity_scaling()

    def test_shedding_protection_restoration_and_infeasibility(self):
        base = study.base
        dss = study.dss
        original = {name: dict(mg.diesel_kva) for name, mg in base.MICROGRIDS.items()}
        try:
            schedules, _ = study.optimize_all_batteries()
            base.LOAD_SCALING = study.LOAD_SCALING
            base.SOLAR_SCALING = study.SOLAR_SCALING
            base.OTHER_LOAD_SCALING = study.OTHER_LOAD_SCALING
            base.compile_and_add_assets()
            regions = base.build_microgrid_bus_sets()
            ratings = base.capture_loads(regions)
            base.set_loads(18, ratings)
            base.set_solar(18)
            study.configure_operating_state(18, schedules)
            self.assertTrue(base.solve())
            study.equally_share_mg2_diesels(18)
            before = {}
            for name in dss.Loads.AllNames():
                dss.Loads.Name(name)
                before[name] = (dss.Loads.kW(), dss.Loads.kvar())
            base.MICROGRIDS['MG1'].diesel_kva['mg1_gen4'] = 150
            base.MICROGRIDS['MG2'].diesel_kva.update(mg2_gen4=400, mg2_gen6=400)
            result = study.enforce_islanded_diesel_limits(18, ratings)
            self.assertTrue((result.iloc[:2].Shed_Setpoint_kW > 0).all())
            for mg_name, mg in base.MICROGRIDS.items():
                for diesel in mg.diesel_generators:
                    element = (f'Vsource.gfm_{mg_name.lower()}'
                               if diesel == mg.forming_diesel else f'Generator.{diesel}')
                    self.assertLessEqual(math.hypot(*base.element_output(element)),
                                         mg.diesel_kva[diesel] + study.DIESEL_KVA_TOLERANCE)
                for name in ratings[mg_name]:
                    dss.Circuit.SetActiveElement(f'Load.{name}')
                    bus = base.bus_base(dss.CktElement.BusNames()[0])
                    dss.Loads.Name(name)
                    if (mg_name not in study.CRITICAL_LOAD_BUSES
                            or bus in study.CRITICAL_LOAD_BUSES[mg_name]):
                        self.assertEqual(before[name], (dss.Loads.kW(), dss.Loads.kvar()))
            # No eligible load remains when every load bus is protected.
            protected = study.CRITICAL_LOAD_BUSES
            try:
                study.CRITICAL_LOAD_BUSES = {name: tuple(buses) for name, buses in regions.items()}
                base.MICROGRIDS['MG1'].diesel_kva['mg1_gen4'] = 1
                with self.assertRaisesRegex(RuntimeError, 'capacity infeasible'):
                    study.enforce_islanded_diesel_limits(18, ratings)
            finally:
                study.CRITICAL_LOAD_BUSES = protected
            base.set_loads(19, ratings)
            for mg_name, loads in ratings.items():
                scale = (study.OTHER_LOAD_SCALING[19] if mg_name == 'FEEDER'
                         else study.LOAD_SCALING[mg_name][19])
                for name, (p, q) in loads.items():
                    dss.Loads.Name(name)
                    self.assertAlmostEqual(dss.Loads.kW(), p * scale, places=6)
                    self.assertAlmostEqual(dss.Loads.kvar(), q * scale, places=6)
        finally:
            for name, values in original.items():
                base.MICROGRIDS[name].diesel_kva.update(values)


if __name__ == '__main__':
    unittest.main()
