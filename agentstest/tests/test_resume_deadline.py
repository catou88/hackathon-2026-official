"""Post-evaluation regressions for interrupted-case recovery and CSV replacement.

These fixtures represent a worker interrupted after starting a case. The old
case allowance has expired, but the original run allowance has not. No test
calls a model or terminates an evaluation process. Most tests prohibit Popen;
one synthetic worker checks that only a previously unstarted row executes.
"""
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mini_rca.case import format_prediction
from mini_rca.cli import load_query_rows, save_predictions
from mini_rca.config import Config
from mini_rca.supervision import supervise


UNSTARTED_WORKER = r'''
import csv, json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from mini_rca.supervision import WorkerChannel
out = Path(sys.argv[2]); queries = Path(sys.argv[3])
done = set()
if (out/'predictions.csv').exists():
    with (out/'predictions.csv').open(newline='', encoding='utf-8') as f:
        done = {int(row['row_id']) for row in csv.DictReader(f)}
with queries.open(newline='', encoding='utf-8') as f:
    rows = list(csv.DictReader(f))
channel = WorkerChannel.from_environment(out)
for row in rows:
    rid = int(row['row_id'])
    if rid in done: continue
    with (out/'synthetic-started.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps({'row_id': rid})+'\n')
    channel.start_case(rid, time.monotonic())
    channel.result({'row_id': rid, 'prediction': 'new-case-'+str(rid),
                    'mode': 'offline-fallback', 'wall_s': 0, 'cost_dollars': 0,
                    'evidence': '# Answer\nFixture\n\n# Confidence\nLow\n\n# Evidence\nFixture\n\n# Ruled out\nNone\n',
                    'models': {}, 'model_events': [], 'ledger': [], 'warnings': []})
'''


class InterruptedResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dataset = self.root/'data'; self.dataset.mkdir()
        self.out = self.root/'out'; self.out.mkdir()
        self.queries = self.root/'query.csv'
        self.generation = 'worker-before-external-interruption'
        self.config = Config(mode='offline', case_seconds=3, run_seconds=120)
        self.old_started = time.monotonic()-8
        self.origin_wall = time.time()-8

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, relative, value):
        path = self.out/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def read_json(self, relative):
        return json.loads((self.out/relative).read_text(encoding='utf-8'))

    def setup_run(self, row_ids=(0,), phase='case_start'):
        with self.queries.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['row_id', 'task_index', 'instruction'])
            writer.writeheader()
            for rid in row_ids:
                writer.writerow({'row_id': rid, 'task_index': 'task_7',
                    'instruction': 'March 20, 2022, from 01:00 to 01:30, one failure occurred.'})
        source = load_query_rows(self.queries)
        self.write_json('run.json', {
            'dataset': str(self.dataset.resolve()), 'config': self.config.__dict__,
            'agent': 'mini_rca.adapter',
            'query_hash': hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
            'started_at_unix': self.origin_wall, 'selected_row_ids': list(row_ids)})
        self.write_json('supervision/run.json', {
            'started_at_unix': self.origin_wall, 'run_seconds': self.config.run_seconds})
        self.write_json('supervision/state.json', {
            'generation': self.generation, 'phase': phase,
            'row_id': 0, 'started': self.old_started})

    def args(self):
        return ['--dataset', str(self.dataset), '--queries', str(self.queries),
                '--out', str(self.out), '--mode', 'offline', '--resume',
                '--case-seconds', str(self.config.case_seconds),
                '--run-seconds', str(self.config.run_seconds)]

    def result(self, component='checkpoint-container', cost=0.0):
        answers = [{'datetime': '2022-03-20 01:05:00', 'component': component,
                    'reason': 'container CPU load', 'confidence': 'low',
                    'evidence_ids': [], 'rationale': 'Synthetic recovery fixture.',
                    'alternatives': []}]
        prediction = format_prediction(answers, ('datetime', 'component', 'reason'))
        return {'row_id': 0, 'prediction': prediction, 'answers': answers,
                'mode': 'offline-fallback', 'wall_s': 1.0, 'cost_dollars': cost,
                'evidence': '# Answer\n'+prediction+'\n\n# Confidence\nLow\n\n# Evidence\nSynthetic completed tool\n\n# Ruled out\nNone\n',
                'models': {}, 'model_events': [],
                'ledger': [{'complete': True, 'fixture_component': component}],
                'warnings': []}

    def snapshot(self, kind, result, generation=None, at=None):
        self.write_json(f'supervision/{kind}/0.json', {
            'generation': self.generation if generation is None else generation,
            'at': self.old_started+1 if at is None else at, 'result': result})

    def audit(self, pending=True, cost=0.031):
        event = {'model': self.config.fast_model, 'attempt': 1,
                 'status': 'pending' if pending else 'ok', 'reserved_dollars': cost}
        if not pending:
            event.update(cost_dollars=cost, usage_estimated=False,
                         prompt_tokens=100, completion_tokens=20)
        self.write_json('model_responses/0/01.json', {
            'row_id': 0, 'attempt': 1, 'model': self.config.fast_model, 'event': event})

    def no_spawn_resume(self):
        with patch('mini_rca.supervision.subprocess.Popen',
                   side_effect=AssertionError('An already-started case must not be executed again')) as spawn:
            summary = supervise(self.args())
        spawn.assert_not_called()
        return summary

    def predictions(self):
        with (self.out/'predictions.csv').open(newline='', encoding='utf-8') as f:
            return {int(row['row_id']): row for row in csv.DictReader(f)}

    def usage_rows(self):
        return [json.loads(line) for line in (self.out/'usage.jsonl').read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def test_interrupted_case_is_finalized_without_new_time_allowance(self):
        self.setup_run()
        checkpoint = self.result()
        self.snapshot('checkpoints', checkpoint)
        self.audit()
        summary = self.no_spawn_resume()
        self.assertEqual(summary['completed'], 1)
        self.assertEqual(summary['valid_predictions'], 1)
        self.assertEqual(self.predictions()[0]['prediction'], checkpoint['prediction'])
        self.assertEqual(self.read_json('traces/0.json')['ledger'], checkpoint['ledger'])
        self.assertAlmostEqual(summary['run_cost_dollars'], 0.031)
        self.assertAlmostEqual(float(self.predictions()[0]['cost_dollars']), 0.031)
        self.assertTrue((self.out/'evidence/0.md').exists())

    def test_only_previously_unstarted_rows_execute_after_recovery(self):
        self.setup_run(row_ids=(0, 1))
        checkpoint = self.result()
        self.snapshot('checkpoints', checkpoint)
        self.audit()
        worker = self.root/'unstarted_worker.py'
        worker.write_text(UNSTARTED_WORKER, encoding='utf-8')
        summary = supervise(self.args(), command=[sys.executable, '-B', str(worker),
                                                 str(ROOT), str(self.out), str(self.queries)])
        started = [json.loads(line)['row_id'] for line in
                   (self.out/'synthetic-started.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(started, [1])
        self.assertEqual(summary['completed'], 2)
        self.assertEqual(set(self.predictions()), {0, 1})
        self.assertEqual(self.predictions()[0]['prediction'], checkpoint['prediction'])
        self.assertEqual(self.predictions()[1]['prediction'], 'new-case-1')
        self.assertAlmostEqual(summary['run_cost_dollars'], 0.031)

    def test_timely_completed_result_is_recovered_without_ack_or_reexecution(self):
        self.setup_run(phase='result')
        self.snapshot('checkpoints', self.result())
        completed = self.result('already-completed-container', cost=0.004)
        self.snapshot('results', completed)
        self.audit(pending=False, cost=0.004)
        self.assertFalse((self.out/'supervision/ack.json').exists())
        summary = self.no_spawn_resume()
        self.assertEqual(self.predictions()[0]['prediction'], completed['prediction'])
        self.assertEqual(self.read_json('traces/0.json')['ledger'], completed['ledger'])
        self.assertAlmostEqual(summary['run_cost_dollars'], 0.004)

    def test_repeated_resume_keeps_one_usage_row_and_does_not_double_charge(self):
        self.setup_run()
        checkpoint = self.result()
        self.snapshot('checkpoints', checkpoint)
        self.audit()
        for _ in range(3):
            summary = self.no_spawn_resume()
            self.assertEqual(summary['completed'], 1)
            self.assertAlmostEqual(summary['run_cost_dollars'], 0.031)
            self.assertAlmostEqual(summary['invocation_cost_dollars'], 0.0)
            self.assertEqual(self.predictions()[0]['prediction'], checkpoint['prediction'])
            usage = self.usage_rows()
            self.assertEqual([row['row_id'] for row in usage], [0])
            self.assertAlmostEqual(usage[0]['cost_dollars'], 0.031)
        self.assertEqual(len(list((self.out/'model_responses/0').glob('*.json'))), 1)

    def test_checkpoint_from_another_generation_is_not_adopted(self):
        self.setup_run()
        self.snapshot('checkpoints', self.result('poisoned-container'), generation='unrelated-worker')
        self.audit()
        summary = self.no_spawn_resume()
        self.assertEqual(summary['completed'], 1)
        self.assertEqual(summary['valid_predictions'], 0)
        self.assertEqual(self.predictions()[0]['prediction'], '')
        self.assertAlmostEqual(summary['run_cost_dollars'], 0.031)
        self.assertNotIn('poisoned-container', (self.out/'evidence/0.md').read_text(encoding='utf-8'))

    def test_completed_result_from_another_generation_uses_valid_checkpoint(self):
        self.setup_run(phase='result')
        checkpoint = self.result()
        self.snapshot('checkpoints', checkpoint)
        self.snapshot('results', self.result('poisoned-complete-container'), generation='unrelated-worker')
        self.audit()
        self.no_spawn_resume()
        self.assertEqual(self.predictions()[0]['prediction'], checkpoint['prediction'])

    def test_late_completed_result_is_not_adopted_on_resume(self):
        self.setup_run(phase='result')
        checkpoint = self.result()
        self.snapshot('checkpoints', checkpoint)
        self.snapshot('results', self.result('late-complete-container'),
                      at=self.old_started+self.config.case_seconds+1)
        self.audit()
        self.no_spawn_resume()
        self.assertEqual(self.predictions()[0]['prediction'], checkpoint['prediction'])

    def test_prediction_only_commit_repairs_artifacts_from_timely_final(self):
        self.setup_run(phase='result')
        completed=self.result('completed-before-write-interruption',cost=0.004)
        self.snapshot('results',completed)
        self.audit(pending=False,cost=0.004)
        save_predictions(self.out/'predictions.csv',[
            {key:completed[key] for key in ('row_id','prediction','mode','wall_s','cost_dollars')}])
        for _ in range(2):
            summary=self.no_spawn_resume()
            self.assertEqual(self.predictions()[0]['prediction'],completed['prediction'])
            self.assertEqual(self.read_json('traces/0.json')['ledger'],completed['ledger'])
            self.assertTrue((self.out/'evidence/0.md').exists())
            self.assertEqual(len(self.usage_rows()),1)
            self.assertAlmostEqual(summary['run_cost_dollars'],0.004)
            self.assertAlmostEqual(summary['invocation_cost_dollars'],0)
        from mini_rca.supervision import commit_result
        with patch('mini_rca.supervision.commit_result',wraps=commit_result) as commit:
            self.no_spawn_resume()
        commit.assert_not_called()

    def test_partial_commit_missing_usage_is_repaired_from_matching_checkpoint(self):
        self.setup_run()
        checkpoint=self.result(cost=0.031)
        self.snapshot('checkpoints',checkpoint)
        self.audit()
        save_predictions(self.out/'predictions.csv',[
            {key:checkpoint[key] for key in ('row_id','prediction','mode','wall_s','cost_dollars')}])
        self.write_json('traces/0.json',{key:value for key,value in checkpoint.items() if key!='evidence'})
        (self.out/'evidence').mkdir()
        (self.out/'evidence/0.md').write_text(checkpoint['evidence'],encoding='utf-8')
        self.no_spawn_resume()
        self.assertEqual(self.predictions()[0]['prediction'],checkpoint['prediction'])
        self.assertEqual([row['row_id'] for row in self.usage_rows()],[0])
        self.assertTrue(self.read_json('traces/0.json')['recovered_committed_artifacts'])

    def test_partial_commit_never_changes_prediction_to_different_checkpoint(self):
        self.setup_run()
        original=self.result('original-committed-container')
        self.snapshot('checkpoints',self.result('different-checkpoint-container'))
        save_predictions(self.out/'predictions.csv',[
            {key:original[key] for key in ('row_id','prediction','mode','wall_s','cost_dollars')}])
        with patch('mini_rca.supervision.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError,'no matching trusted snapshot'):
                supervise(self.args())
        spawn.assert_not_called()
        self.assertEqual(self.predictions()[0]['prediction'],original['prediction'])

    def test_interrupted_wall_time_is_marked_estimate_and_downtime_separate(self):
        self.setup_run()
        self.snapshot('checkpoints',self.result())
        self.audit()
        summary=self.no_spawn_resume()
        trace=self.read_json('traces/0.json')
        self.assertTrue(trace['wall_s_estimated'])
        self.assertFalse(trace['hard_timeout'])
        self.assertLessEqual(trace['wall_s'],self.config.case_seconds)
        self.assertGreater(trace['resume_interval_s'],self.config.case_seconds)
        self.assertIn('not measured case latency',trace['wall_s_note'])
        self.assertTrue(summary['deadline_events'][0]['wall_s_estimated'])
        self.assertLessEqual(self.usage_rows()[0]['wall_s'],self.config.case_seconds)
        self.assertTrue(self.usage_rows()[0]['wall_s_estimated'])
        self.assertEqual(trace['model_events'][0]['failure_kind'],'interrupted_resume')

    def test_started_journal_recovers_multiple_rows_after_state_advances(self):
        self.setup_run(row_ids=(0,1))
        for rid in (0,1):
            generation='started-generation-'+str(rid)
            self.write_json(f'supervision/started/{rid}.json',{
                'generation':generation,'phase':'case_start','row_id':rid,
                'started':self.old_started,'hard_deadline':self.old_started+2.5})
            result=self.result('checkpoint-for-row-'+str(rid));result['row_id']=rid
            self.write_json(f'supervision/checkpoints/{rid}.json',{
                'generation':generation,'at':self.old_started+1,'result':result})
        self.write_json('supervision/state.json',{
            'generation':'started-generation-1','phase':'case_start','row_id':1,
            'started':self.old_started,'hard_deadline':self.old_started+2.5})
        summary=self.no_spawn_resume()
        self.assertEqual(summary['completed'],2)
        self.assertEqual(set(self.predictions()),{0,1})
        self.assertEqual({row['row_id'] for row in self.usage_rows()},{0,1})
        self.assertIn('checkpoint-for-row-0',self.predictions()[0]['prediction'])
        self.assertIn('checkpoint-for-row-1',self.predictions()[1]['prediction'])

    def test_unreadable_existing_state_fails_closed(self):
        self.setup_run()
        from mini_rca.supervision import read_json
        def unreadable(path):
            return None if path==self.out/'supervision/state.json' else read_json(path)
        with patch('mini_rca.supervision.read_json',side_effect=unreadable),patch('mini_rca.supervision.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError,'state is unreadable'):
                supervise(self.args())
        spawn.assert_not_called()
        self.assertFalse((self.out/'predictions.csv').exists())


class PredictionReplacementTests(unittest.TestCase):
    def test_one_transient_windows_replace_error_retries_and_preserves_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'predictions.csv'
            rows = [{'row_id': 0, 'prediction': 'complete fixture, with comma',
                     'mode': 'offline-fallback', 'wall_s': 0.1, 'cost_dollars': 0.0}]
            replace = os.replace
            calls = []

            def briefly_locked(source, target):
                calls.append((source, target))
                if len(calls) == 1:
                    raise PermissionError('Synthetic transient read lock')
                return replace(source, target)

            with patch('mini_rca.cli.os.replace', side_effect=briefly_locked):
                save_predictions(path, rows)
            self.assertGreaterEqual(len(calls), 2)
            with path.open(newline='', encoding='utf-8') as f:
                saved = list(csv.DictReader(f))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]['prediction'], rows[0]['prediction'])
            self.assertEqual(saved[0]['row_id'], '0')


if __name__ == '__main__':
    unittest.main()
