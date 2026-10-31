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
               'p99_ms': 17, 'timeline': [{'t': 80, 'conns': 30000, 'p99_ms': 21}, {'t': 85, 'conns': 30000, 'p99_ms': 23}]}
        raw.update(overrides.pop('raw', {}))
        samples = [{'t': t, 'app': {'cpu_s': t * .5, 'rss_highwater_bytes': 1024},
                    'database': {'cpu_s': t * .5},
                    'generator': {'cpu_s': t * .2, 'rss_highwater_bytes': 512}} for t in (0, 75, 85)]
        samples = overrides.pop('samples', samples)
        if overrides.pop('limited', False):
            samples[-1]['database']['cpu_s'] = samples[-2]['database']['cpu_s'] + 9
        return comparison.trial_summary(raw, samples, ramp=60, app_cores=2,
                                        db_cores=1, generator_cores=1, app_alive=overrides.pop('alive', True))

    def test_measures_cpu_and_keeps_percentile_definitions_separate(self):
        row = self.make_summary()
        self.assertAlmostEqual(row['app_cpu_us_per_successful_request'], 42.5e6 / 9900)
        self.assertEqual(row['p99_ms_including_ramp'], 17)
        self.assertEqual(row['max_hold_window_p99_ms'], 23)
        self.assertEqual(row['app_hold_cpu_pct_of_allocation'], 25)
        self.assertTrue(row['valid_diagnostic_pass'])

    def test_mload_flag_does_not_hide_missing_connections(self):
        row = self.make_summary(raw={'timeline': [{'t': 85, 'conns': 100, 'p99_ms': 1}]})
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

    def test_initial_think_delay_does_not_invalidate_later_full_connections(self):
        # Reproduce the smoke run's 14,449 connections at 65s and full 15,000
        # from 75s onward. Tick75's latency window still overlaps settling.
        timeline = [{'t': 65, 'conns': 14449, 'p99_ms': 800},
                    {'t': 70, 'conns': 14890, 'p99_ms': 700},
                    {'t': 75, 'conns': 15000, 'p99_ms': 900}]
        timeline += [{'t': t, 'conns': 15000, 'p99_ms': 30 if t == 80 else 20}
                     for t in range(80, 121, 5)]
        samples = [{'t': t, 'app': {'cpu_s': t * .5, 'rss_highwater_bytes': 1024},
                    'database': {'cpu_s': min(t, 75) + max(0, t - 75) * .5},
                    'generator': {'cpu_s': min(t, 75) + max(0, t - 75) * .2,
                                  'rss_highwater_bytes': 512}} for t in (0, 60, 65, 70, 75, 80, 100, 120)]
        row = self.make_summary(raw={'users': 15000, 'timeline': timeline}, samples=samples)
        self.assertEqual(row['min_hold_connections'], 15000)
        self.assertTrue(row['valid_diagnostic_pass'])
        self.assertEqual(row['database_hold_cpu_pct_of_allocation'], 50)
        self.assertEqual(row['generator_hold_cpu_pct_of_allocation'], 20)
        self.assertEqual(row['max_hold_window_p99_ms'], 30)
        self.assertEqual(row['hold_cpu_window_starts_at_s'], 75)
        self.assertEqual(row['connection_window_starts_at_s'], 75)
        self.assertEqual(row['latency_window_ends_at_or_after_s'], 80)

    def test_connection_shortfall_at_settling_boundary_still_fails(self):
        timeline = [{'t': 75, 'conns': 14449, 'p99_ms': 1},
                    {'t': 80, 'conns': 15000, 'p99_ms': 1},
                    {'t': 85, 'conns': 15000, 'p99_ms': 1}]
        row = self.make_summary(raw={'users': 15000, 'timeline': timeline})
        self.assertEqual(row['min_hold_connections'], 14449)
        self.assertFalse(row['valid_diagnostic_pass'])

    def test_parallel_replica_orders_match_serial_round_orders(self):
        names = ['minimal-api', 'kestrel-aot', 'kestrel-jit', 'reactor-epoll', 'reactor-uring']
        serial = comparison.schedule(names, [30000, 80000], 3, 42)
        for replica in range(3):
            parallel = comparison.schedule(names, [30000, 80000], 1, 42, replica)
            self.assertEqual([r['variant'] for r in parallel],
                             [r['variant'] for r in serial if r['round'] == replica + 1])
            self.assertTrue(all(r['replicate_index'] == replica for r in parallel))

    def paired_trial(self, variant, **overrides):
        row = {'host_block_id': 'host-a', 'replicate_index': 0, 'users': 30000,
               'round': 1, 'variant': variant, 'id': variant + '-trial',
               'valid_diagnostic_pass': True, 'app_cpu_us_per_successful_request': 100,
               'p99_ms_including_ramp': 20, 'app_peak_rss_bytes': 2000}
        row.update(overrides)
        return row

    def test_ratios_use_candidate_over_paired_baseline(self):
        baseline = self.paired_trial('kestrel-jit')
        candidate = self.paired_trial('reactor-epoll', app_cpu_us_per_successful_request=75,
                                      p99_ms_including_ramp=10, app_peak_rss_bytes=1500)
        pair, = comparison.paired_ratios([candidate, baseline])
        self.assertTrue(pair['eligible_for_comparison'])
        self.assertEqual(pair['candidate_over_baseline_ratios'],
                         {'cpu_us_per_successful_request': .75, 'p99_ms': .5, 'peak_rss_bytes': .75})

    def test_invalid_pair_keeps_observations_but_has_no_comparison_ratios(self):
        baseline = self.paired_trial('kestrel-jit', valid_diagnostic_pass=False,
                                     generator_or_database_cpu_limited=True)
        candidate = self.paired_trial('reactor-epoll')
        pair, = comparison.paired_ratios([candidate, baseline])
        self.assertFalse(pair['eligible_for_comparison'])
        self.assertIsNone(pair['candidate_over_baseline_ratios'])
        self.assertEqual(pair['observations']['baseline']['cpu_us_per_successful_request'], 100)
        self.assertTrue(pair['baseline_status']['generator_or_database_cpu_limited'])
        self.assertTrue(pair['exclusion_reasons'])

    def test_never_pairs_across_hosts_even_with_same_replica_index(self):
        baseline = self.paired_trial('kestrel-jit', host_block_id='host-a')
        candidate = self.paired_trial('reactor-epoll', host_block_id='host-b')
        self.assertEqual(comparison.paired_ratios([candidate, baseline]), [])

    def test_never_pairs_different_user_counts_or_rounds(self):
        baseline = self.paired_trial('kestrel-jit')
        for override in ({'users': 80000}, {'round': 2}, {'confirmation': True}):
            candidate = self.paired_trial('reactor-epoll', **override)
            self.assertEqual(comparison.paired_ratios([candidate, baseline]), [])

    def test_unmeasurable_ratio_is_not_reported_as_zero(self):
        for baseline_value, candidate_value in ((0, 20), (20, 0), (20, None)):
            baseline = self.paired_trial('kestrel-jit', p99_ms_including_ramp=baseline_value)
            candidate = self.paired_trial('reactor-epoll', p99_ms_including_ramp=candidate_value)
            pair, = comparison.paired_ratios([candidate, baseline])
            self.assertIsNone(pair['candidate_over_baseline_ratios']['p99_ms'])

    def test_duplicate_trials_are_not_silently_overwritten(self):
        baseline = self.paired_trial('kestrel-jit')
        with self.assertRaises(ValueError):
            comparison.paired_ratios([baseline, dict(baseline)])


if __name__ == '__main__':
    unittest.main()
