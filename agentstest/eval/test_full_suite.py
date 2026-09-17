"""Synthetic, network-free regression checks for the 70-case experiment runner."""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('full_suite', Path(__file__).with_name('full_suite.py'))
suite = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(suite)


class FullSuiteTests(unittest.TestCase):
    def fixtures(self, root):
        queries, labels = root/'query.csv', root/'labels.csv'
        rows = [{'row_id': index, 'task_index': 'task_2',
                 'instruction': 'On March 20, 2022, between 09:00 and 09:30, one failure occurred. What was the root cause reason?'}
                for index in range(70)]
        suite.write_csv(queries, rows, ['row_id', 'task_index', 'instruction'])
        suite.write_csv(labels, [dict(row, scoring_points='The only predicted root cause reason is container CPU load')
                                for row in rows], ['row_id', 'task_index', 'instruction', 'scoring_points'])
        return queries, labels

    def test_pending_cost_is_reserved_without_double_counting(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            suite.atomic_json(folder/'model_responses/0/01.json', {'row_id': 0, 'event': {
                'reserved_dollars': .02, 'status': 'pending'}})
            suite.atomic_json(folder/'model_responses/1/01.json', {'row_id': 1, 'event': {
                'reserved_dollars': .02, 'cost_dollars': .004, 'status': 'ok', 'usage_estimated': False}})
            (folder/'usage.jsonl').write_text(json.dumps({'row_id': 1, 'cost_dollars': .004})+'\n')
            result = suite.audit_accounting(folder)
            self.assertAlmostEqual(result['total_dollars'], .024)
            self.assertAlmostEqual(result['reserved_or_estimated_dollars'], .02)
            self.assertAlmostEqual(result['known_usage_dollars'], .004)

    def test_corrupt_audit_never_resets_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'model_responses/0/01.json'
            path.parent.mkdir(parents=True)
            path.write_text('{broken')
            with self.assertRaises(json.JSONDecodeError):
                suite.audit_accounting(Path(directory))

    def test_aggregate_missing_rows_remain_in_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queries, labels = self.fixtures(root)
            cases = suite.load_cases(queries)
            out = root/'out'
            suite.write_csv(out/'offline/batch-01/predictions.csv', [{
                'row_id': 0, 'prediction': '{"1":{"root cause reason":"container CPU load"}}',
                'mode': 'offline', 'wall_s': 1.5, 'cost_dollars': 0}],
                ['row_id', 'prediction', 'mode', 'wall_s', 'cost_dollars'])
            args = argparse.Namespace(out=out, labels=labels, total_dollars=1.0, live=False)
            state = {'batches': [{'mode': 'offline', 'folder': 'offline/batch-01', 'row_ids': list(range(20)),
                                 'status': 'interrupted'}]}
            result = suite.write_report(args, cases, state)
            self.assertEqual(result['modes']['offline']['denominator'], 70)
            self.assertAlmostEqual(result['modes']['offline']['strict'], 1/70)
            self.assertAlmostEqual(result['modes']['offline']['coverage'], 1/70)
            self.assertEqual(len(suite.read_csv(out/'offline/aggregate/predictions.csv')), 70)
            self.assertEqual(result['modes']['routed']['model_attempts'], 0)

    def test_batches_share_budget_and_resume_never_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queries, labels = self.fixtures(root)
            env_file = root/'.env'
            env_file.write_text('# synthetic: no credential')
            out = root/'out'
            commands = []
            def fake_batch(command, folder, timeout):
                commands.append(command)
                self.assertNotIn('--labels', command)
                mode = command[command.index('--mode')+1]
                allowance = float(command[command.index('--run-dollars')+1])
                ids = list(map(int, command[command.index('--row-ids')+1].split(',')))
                self.assertEqual(timeout, 1220)
                cost = min(.6, allowance) if mode == 'routed' and allowance > 1e-9 else 0
                suite.atomic_json(folder/'summary.json', {'run_cost_dollars': cost})
                if cost:
                    suite.atomic_json(folder/f'model_responses/{ids[0]}/01.json', {
                        'row_id': ids[0], 'event': {'cost_dollars': cost, 'status': 'ok',
                                                   'model': 'synthetic', 'usage_estimated': False}})
                suite.write_csv(folder/'predictions.csv', [{'row_id': rid, 'prediction': '',
                    'mode': 'offline-fallback', 'wall_s': .01, 'cost_dollars': cost if rid == ids[0] else 0}
                    for rid in ids], ['row_id', 'prediction', 'mode', 'wall_s', 'cost_dollars'])
                return {'status': 'completed', 'returncode': 0, 'process_wall_s': .1}
            arguments = ['--dataset', str(root), '--queries', str(queries), '--labels', str(labels),
                         '--out', str(out), '--live', '--env-file', str(env_file)]
            with patch.object(suite, 'run_batch', fake_batch):
                suite.main(arguments)
            self.assertEqual(len(commands), 8)
            routed = commands[4:]
            allowances = [float(cmd[cmd.index('--run-dollars')+1]) for cmd in routed]
            self.assertEqual(allowances, [1.0, .4, 1e-12, 1e-12])
            self.assertEqual([len(cmd[cmd.index('--row-ids')+1].split(',')) for cmd in routed], [20, 20, 20, 10])
            result = suite.read_json(out/'results.json')
            self.assertEqual(result['modes']['routed']['predictions_present'], 70)
            self.assertEqual(result['modes']['routed']['batch_budget_disabled_rows'], 30)
            self.assertEqual(result['total_live_dollars'], 1.0)
            with patch.object(suite, 'run_batch', side_effect=AssertionError('Must not replay completed batches')):
                suite.main(arguments+['--resume'])

    def test_live_requires_opt_in_and_env_file(self):
        with self.assertRaises(SystemExit):
            suite.main(['--dataset', '.', '--queries', 'query.csv', '--labels', 'labels.csv', '--out', 'unused'])


if __name__ == '__main__':
    unittest.main()
