"""Measurement invariants; these tests never start PostgreSQL or generate load."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('comparison', Path(__file__).with_name('run.py'))
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


class DriverTests(unittest.TestCase):
    def test_rotated_order_is_repeatable_and_complete(self):
        names = ['minimal', 'aot', 'jit', 'reactor']
        result = comparison.schedule(names, [30000, 80000], 3, 42)
        self.assertEqual(result, comparison.schedule(names, [30000, 80000], 3, 42))
        self.assertEqual(len(result), 24)
        for users in (30000, 80000):
            for round_number in (1, 2, 3):
                self.assertCountEqual([row['variant'] for row in result if row['users'] == users and row['round'] == round_number], names)
        self.assertNotEqual([r['variant'] for r in result[:4]], [r['variant'] for r in result[4:8]])

    def make_summary(self, **overrides):
        raw = {'users': 30000, 'reqs': 10000, 'fails': 100, 'pass': True,
               'p99_ms': 17, 'timeline': [{'t': 65, 'conns': 30000, 'p99_ms': 21}, {'t': 70, 'conns': 30000, 'p99_ms': 23}]}
        raw.update(overrides.pop('raw', {}))
        samples = [{'t': t, 'app': {'cpu_s': t * .5, 'rss_highwater_bytes': 1024},
                    'database': {'cpu_s': t * .5},
                    'generator': {'cpu_s': t * .2, 'rss_highwater_bytes': 512}} for t in (0, 60, 70)]
        if overrides.pop('limited', False):
            samples[-1]['database']['cpu_s'] = samples[-2]['database']['cpu_s'] + 9
        return comparison.trial_summary(raw, samples, ramp=60, app_cores=2,
                                        db_cores=1, generator_cores=1, app_alive=overrides.pop('alive', True))

    def test_measures_cpu_and_keeps_percentile_definitions_separate(self):
        row = self.make_summary()
        self.assertAlmostEqual(row['app_cpu_us_per_successful_request'], 35e6 / 9900)
        self.assertEqual(row['p99_ms_including_ramp'], 17)
        self.assertEqual(row['max_hold_window_p99_ms'], 23)
        self.assertEqual(row['app_hold_cpu_pct_of_allocation'], 25)
        self.assertTrue(row['valid_diagnostic_pass'])

    def test_mload_flag_does_not_hide_missing_connections(self):
        row = self.make_summary(raw={'timeline': [{'t': 65, 'conns': 100, 'p99_ms': 1}]})
        self.assertTrue(row['mload_thresholds_passed'])
        self.assertFalse(row['valid_diagnostic_pass'])

    def test_limited_database_invalidates_capacity_claim(self):
        row = self.make_summary(limited=True)
        self.assertEqual(row['database_hold_cpu_pct_of_allocation'], 90)
        self.assertTrue(row['generator_or_database_cpu_limited'])
        self.assertFalse(row['valid_diagnostic_pass'])

    def test_mload_failure_and_app_exit_remain_failures(self):
        self.assertFalse(self.make_summary(raw={'pass': False})['valid_diagnostic_pass'])
        self.assertFalse(self.make_summary(alive=False)['valid_diagnostic_pass'])

    def test_missing_hold_samples_cannot_pass(self):
        row = self.make_summary(raw={'timeline': []})
        self.assertFalse(row['valid_diagnostic_pass'])


if __name__ == '__main__':
    unittest.main()
