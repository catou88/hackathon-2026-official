"""Fixed 70-case offline/routed comparison with cumulative cost accounting.

Labels are passed only to the separate offline scorer. --live and --env-file
are both required before any routed batch can run. Completed or interrupted
batches are never replayed on resume; interrupted rows remain in the denominator.
"""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from mini_rca.case import Case


def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def read_csv(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def source_hashes():
    files = [ROOT/'run.py', ROOT/'evaluate.py', Path(__file__).resolve(), ROOT/'eval/official_score.py']
    for folder in ('mini_rca', 'prompts', 'knowledge'):
        files.extend(p for p in (ROOT/folder).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(set(files))}


def load_cases(path):
    rows = read_csv(path)
    if len(rows) != 70:
        raise ValueError('This experiment requires exactly 70 development query rows')
    if any('scoring_points' in row for row in rows):
        raise ValueError('Diagnosis queries must not contain scoring_points')
    cases = sorted((Case.parse(row) for row in rows), key=lambda case: case.row_id)
    if len({case.row_id for case in cases}) != len(cases):
        raise ValueError('Duplicate row_id in queries')
    return cases


def dollars(value):
    result = float(value or 0)
    if not math.isfinite(result) or result < 0:
        raise ValueError('Invalid persisted cost; refuse to reset the budget')
    return result


def audit_accounting(folder):
    """Prefer durable attempt costs/reservations; never add duplicate usage totals."""
    audit_total = known = reserved = 0.0
    per_case = defaultdict(float)
    events = []
    for path in sorted((folder/'model_responses').glob('*/*.json')):
        record = read_json(path)
        event = record.get('event', {})
        cost = dollars(event.get('cost_dollars', event.get('reserved_dollars', 0)))
        audit_total += cost
        rid = int(record.get('row_id', path.parent.name))
        per_case[rid] += cost
        if 'cost_dollars' not in event or event.get('usage_estimated'):
            reserved += cost
        else:
            known += cost
        events.append(dict(event, row_id=rid))
    usage_total = 0.0
    usage_rows = []
    if (folder/'usage.jsonl').exists():
        for line in (folder/'usage.jsonl').read_text(encoding='utf-8').splitlines():
            if line.strip():
                usage = json.loads(line)
                usage_total += dollars(usage.get('cost_dollars'))
                usage_rows.append(usage)
                rid = int(usage['row_id'])
                per_case[rid] = max(per_case[rid], dollars(usage.get('cost_dollars')))
    summary = read_json(folder/'summary.json', {})
    total = max(audit_total, usage_total, dollars(summary.get('run_cost_dollars')))
    return {'total_dollars': total, 'known_usage_dollars': known,
            'reserved_or_estimated_dollars': reserved,
            'unattributed_dollars': max(0.0, total-known-reserved),
            'per_case': dict(per_case), 'events': events, 'usage': usage_rows}


def percentile(values, proportion):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered)-1)*proportion
    lo = math.floor(position)
    hi = math.ceil(position)
    return ordered[lo] + (ordered[hi]-ordered[lo])*(position-lo)


def group_metrics(details):
    n = len(details)
    measured = [row['wall_s'] for row in details if row['wall_s'] is not None]
    return {'denominator': n, 'predictions_present': sum(row['present'] for row in details),
            'nonempty_predictions': sum(row['nonempty'] for row in details),
            'coverage': sum(row['present'] for row in details)/n if n else 0,
            'nonempty_coverage': sum(row['nonempty'] for row in details)/n if n else 0,
            'strict': sum(row['strict'] for row in details)/n if n else 0,
            'partial': sum(row['partial'] for row in details)/n if n else 0,
            'model_adopted': sum(row['model_adopted'] for row in details),
            'model_adoption_rate': sum(row['model_adopted'] for row in details)/n if n else 0,
            'model_attempted_cases': sum(row.get('model_attempts', 0) > 0 for row in details),
            'wall_s': {'measured_cases': len(measured), 'sum': sum(measured),
                       'mean': statistics.mean(measured) if measured else None,
                       'p50': percentile(measured, .5), 'p95': percentile(measured, .95),
                       'max': max(measured) if measured else None},
            'cost_dollars': sum(row['cost_dollars'] for row in details)}


def terminate_tree(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


def run_batch(command, folder, timeout):
    folder.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
    # Credentials are loaded only by the explicitly live CLI invocation.
    if '--env-file' not in command:
        environment.pop('FEATHERLESS_API_KEY', None)
    started = time.monotonic()
    with (folder/'runner.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=os.name != 'nt',
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            returncode = process.wait(timeout=timeout)
            status = 'completed' if returncode == 0 else 'failed'
        except subprocess.TimeoutExpired:
            terminate_tree(process)
            status, returncode = 'outer_timeout', None
        except BaseException:
            terminate_tree(process)
            raise
    return {'status': status, 'returncode': returncode,
            'process_wall_s': round(time.monotonic()-started, 3)}


def aggregate_mode(args, cases, state, mode):
    aggregate = args.out/mode/'aggregate'
    aggregate.mkdir(parents=True, exist_ok=True)
    predictions = {}
    costs = defaultdict(float)
    all_events, usage_rows = [], []
    warnings, tool_errors, protocol_rejections, skipped_events = Counter(), Counter(), Counter(), Counter()
    known = reserved = total = unattributed = 0.0
    budget_rows = set()
    for batch in state['batches']:
        if batch['mode'] != mode:
            continue
        folder = args.out/batch['folder']
        if batch.get('budget_exhausted_at_start'):
            budget_rows.update(batch['row_ids'])
        accounting = audit_accounting(folder)
        total += accounting['total_dollars']
        known += accounting['known_usage_dollars']
        reserved += accounting['reserved_or_estimated_dollars']
        unattributed += accounting['unattributed_dollars']
        all_events.extend(accounting['events'])
        usage_rows.extend(accounting['usage'])
        for rid, cost in accounting['per_case'].items():
            costs[rid] += cost
        for row in read_csv(folder/'predictions.csv'):
            rid = int(row['row_id'])
            if rid in predictions:
                raise ValueError(f'Duplicate prediction for {mode} row {rid}')
            if rid not in batch['row_ids']:
                raise ValueError('Batch emitted an unexpected row_id')
            predictions[rid] = row
            costs[rid] = max(costs[rid], dollars(row.get('cost_dollars')))
        for kind, suffix in [('evidence', '.md'), ('traces', '.json')]:
            for rid in batch['row_ids']:
                path = folder/kind/f'{rid}{suffix}'
                if path.exists():
                    destination = aggregate/kind/path.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
                    if kind == 'traces':
                        trace = read_json(path)
                        warnings.update(str(item) for item in trace.get('warnings', []))
                        for item in trace.get('ledger', []):
                            if item.get('ok') is False:
                                tool_errors[item.get('tool', 'unknown')] += 1
                        for event in trace.get('model_events', []):
                            if event.get('status') == 'skipped':
                                skipped_events[event.get('failure_kind', 'unknown')] += 1
                        for event in trace.get('decision_events', []):
                            if event.get('failure_kind') == 'output_contract':
                                protocol_rejections[event.get('error_type', 'output_contract')] += 1
    fields = ['row_id', 'prediction', 'mode', 'wall_s', 'cost_dollars']
    actual = [predictions[rid] for rid in sorted(predictions)]
    write_csv(aggregate/'scoring_predictions.csv', actual, fields)
    rows = [predictions.get(case.row_id, {'row_id': case.row_id, 'prediction': '',
            'mode': 'missing', 'wall_s': '', 'cost_dollars': costs[case.row_id]}) for case in cases]
    write_csv(aggregate/'predictions.csv', rows, fields)
    (aggregate/'usage.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in usage_rows), encoding='utf-8')
    subprocess.run([sys.executable, '-B', str(ROOT/'evaluate.py'), '--predictions',
                    str(aggregate/'scoring_predictions.csv'), '--labels', str(args.labels.resolve()),
                    '--row-ids', ','.join(str(case.row_id) for case in cases),
                    '--out', str(aggregate/'evaluation.json')], check=True, capture_output=True, text=True)
    scored = {row['row_id']: row for row in read_json(aggregate/'evaluation.json')['details']}
    details = []
    attempts_per_case = Counter(event['row_id'] for event in all_events)
    for case in cases:
        row = predictions.get(case.row_id, {})
        details.append({'row_id': case.row_id, 'task': case.task, 'failures': case.failures,
            'present': bool(row), 'nonempty': bool(row.get('prediction', '').strip()),
            'strict': scored[case.row_id]['strict'], 'partial': scored[case.row_id]['partial'],
            'mode': row.get('mode', 'missing'), 'model_adopted': row.get('mode', '').endswith('-model'),
            'model_attempts': attempts_per_case[case.row_id],
            'wall_s': float(row['wall_s']) if row.get('wall_s') not in (None, '') else None,
            'cost_dollars': costs[case.row_id], 'budget_exhausted_at_batch_start': case.row_id in budget_rows})
    output = group_metrics(details)
    output.update({'per_task': {task: group_metrics([row for row in details if row['task'] == task])
                               for task in sorted({case.task for case in cases})},
        'per_failure_count': {str(n): group_metrics([row for row in details if row['failures'] == n])
                              for n in sorted({case.failures for case in cases})},
        'single_multi_failure': {key: group_metrics([row for row in details if (row['failures'] > 1) == multi])
                                for key, multi in [('single', False), ('multi', True)]},
        'cost_dollars': total, 'known_usage_dollars': known, 'reserved_or_estimated_dollars': reserved,
        'unattributed_dollars': unattributed, 'model_attempts': len(all_events),
        'dollars_per_case': total/len(cases) if cases else 0,
        'model_counts': dict(Counter(event.get('model', 'unknown') for event in all_events)),
        'model_event_statuses': dict(Counter(event.get('status', 'unknown') for event in all_events)),
        'failure_kinds': dict(Counter(event.get('failure_kind', 'unknown') for event in all_events
                                     if event.get('status') in ('failed', 'pending'))),
        'error_types': dict(Counter(event.get('error_type', 'unknown') for event in all_events
                                   if event.get('status') == 'failed')),
        'tool_errors': dict(tool_errors), 'protocol_rejections': dict(protocol_rejections),
        'skipped_model_events': dict(skipped_events),
        'warnings': dict(warnings), 'prediction_modes': dict(Counter(row['mode'] for row in details)),
        'batch_budget_disabled_rows': len(budget_rows), 'details': details,
        'process_wall_s': sum(batch.get('process_wall_s', 0) for batch in state['batches'] if batch['mode'] == mode)})
    write_csv(aggregate/'case_details.csv', details, list(details[0]) if details else [])
    return output


def write_report(args, cases, state):
    modes = {mode: aggregate_mode(args, cases, state, mode) for mode in ('offline', 'routed')}
    result = {'denominator_per_mode': len(cases), 'modes': modes,
        'total_live_dollars': modes['routed']['cost_dollars'], 'live_budget_dollars': args.total_dollars,
        'budget_exhausted': modes['routed']['cost_dollars'] >= args.total_dollars-1e-9 or
                            any(batch.get('budget_exhausted_at_start') for batch in state['batches']),
        'batches': state['batches'], 'live_authorized': args.live,
        'limits': ['One run of development cases; no repeated-run uncertainty or hidden-deployment claim.',
                   'Offline and routed run sequentially; operating-system file caches are not cleared.',
                   'Missing, failed and model-skipped cases remain in the 70-case accuracy denominator.',
                   'Routed scores include fallback predictions; adoption is reported separately.',
                   'Costs use competition rates and conservative unknown-request reservations, not billing records.',
                   'Four batches reset circuit state; this development experiment is not the official 20-case resource test.']}
    if hasattr(args, 'case_seconds'):
        result['case_deadline_check'] = {mode: {'configured_seconds': args.case_seconds,
            'rows_over_configured_seconds': [row['row_id'] for row in metric['details']
                if row['wall_s'] is not None and row['wall_s'] > args.case_seconds],
            'note': 'Reported per-case wall clock; rounded to milliseconds by the runner.'}
            for mode, metric in modes.items()}
    left = {row['row_id']: row for row in modes['offline']['details']}
    changed, improved, worse = [], [], []
    offline_predictions = {int(row['row_id']): row['prediction'] for row in read_csv(args.out/'offline/aggregate/predictions.csv')}
    for row in modes['routed']['details']:
        baseline = left[row['row_id']]
        if row['partial'] > baseline['partial']:
            improved.append(row['row_id'])
        if row['partial'] < baseline['partial']:
            worse.append(row['row_id'])
    for row in read_csv(args.out/'routed/aggregate/predictions.csv'):
        if row['prediction'] != offline_predictions[int(row['row_id'])]:
            changed.append(int(row['row_id']))
    result['comparison'] = {'partial_delta': modes['routed']['partial']-modes['offline']['partial'],
                            'strict_delta': modes['routed']['strict']-modes['offline']['strict'],
                            'prediction_changed_rows': changed, 'improved_rows': improved, 'worse_rows': worse}
    atomic_json(args.out/'results.json', result)
    lines = ['# 70-case development evaluation', '',
             '| Mode | Coverage | Strict | Partial | Model adopted | Mean case s | P95 s | Max s | USD |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    def number(value):
        return f'{value:.3f}' if value is not None else 'N/A'
    for mode, metric in modes.items():
        wall = metric['wall_s']
        lines.append(f"| {mode} | {metric['predictions_present']}/70 | {metric['strict']:.6f} | "
                     f"{metric['partial']:.6f} | {metric['model_adopted']}/70 | {number(wall['mean'])} | "
                     f"{number(wall['p95'])} | {number(wall['max'])} | {metric['cost_dollars']:.6f} |")
    lines += ['', f"Live budget: ${args.total_dollars:.2f}; conservatively accounted: ${result['total_live_dollars']:.6f}.",
              f"Partial delta: {result['comparison']['partial_delta']:+.6f}; improved rows: {improved}; worse rows: {worse}.",
              '', '## Per task', '', '| Task | Offline partial | Routed partial | Offline strict | Routed strict |',
              '|---|---:|---:|---:|---:|']
    for task in modes['offline']['per_task']:
        offline, routed = modes['offline']['per_task'][task], modes['routed']['per_task'][task]
        lines.append(f"| {task} (n={offline['denominator']}) | {offline['partial']:.6f} | {routed['partial']:.6f} | {offline['strict']:.6f} | {routed['strict']:.6f} |")
    lines += ['', '## Limits and interpretation', ''] + ['- '+limit for limit in result['limits']]
    lines += ['', 'The aggregate predictions contain all 70 row IDs. Missing rows have an empty prediction and score zero; '
              'scoring_predictions.csv contains only rows actually emitted by the agent.',
              '', 'Detailed cost reservations, failure kinds, adoption, single/multiple-failure metrics and every case are in results.json.']
    (args.out/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'queries', 'labels', 'out'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--report-only', action='store_true')
    parser.add_argument('--total-dollars', type=float, default=1.0)
    parser.add_argument('--case-seconds', type=float, default=55.0)
    parser.add_argument('--batch-seconds', type=float, default=1200.0)
    args = parser.parse_args(argv)
    args.out = args.out.resolve()
    if not args.report_only and (not args.live or not args.env_file or not args.env_file.is_file()):
        parser.error('Full routed evaluation requires --live and an existing --env-file')
    if not 0 < args.total_dollars <= 25 or not 0 < args.case_seconds <= 600 or not 0 < args.batch_seconds <= 1200:
        parser.error('Invalid cost/time budget')
    cases = load_cases(args.queries)
    manifest = {'schema': 1, 'dataset': str(args.dataset.resolve()),
        'queries_sha256': hashlib.sha256(args.queries.read_bytes()).hexdigest(),
        'labels_sha256': hashlib.sha256(args.labels.read_bytes()).hexdigest(),
        'source_hashes': source_hashes(), 'case_seconds': args.case_seconds,
        'batch_seconds': args.batch_seconds, 'total_live_dollars': args.total_dollars,
        'row_ids': [case.row_id for case in cases], 'batch_sizes': [20, 20, 20, 10],
        'modes': ['offline', 'routed'], 'experiences': False, 'thinking': False,
        'max_output_tokens': 1200, 'case_dollars': .2}
    manifest_path = args.out/'manifest.json'
    if args.out.exists():
        if not (args.resume or args.report_only):
            parser.error('Use a new --out or --resume')
        if read_json(manifest_path) != manifest:
            parser.error('Resume/report inputs, configuration or source hashes changed')
        state = read_json(args.out/'state.json')
    else:
        if args.report_only:
            parser.error('--report-only requires an existing experiment')
        args.out.mkdir(parents=True)
        atomic_json(manifest_path, manifest)
        state = {'batches': []}
        atomic_json(args.out/'state.json', state)
    if not args.report_only:
        if any(batch['status'] == 'running' for batch in state['batches']):
            parser.error('A previous runner ended without recording shutdown. Do not replay uncertain requests; '
                         'stop its process tree and inspect state.json before resuming. --report-only remains available.')
        try:
            for mode in ('offline', 'routed'):
                for index, offset in enumerate(range(0, len(cases), 20), 1):
                    folder_name = f'{mode}/batch-{index:02d}'
                    if any(batch['folder'] == folder_name for batch in state['batches']):
                        continue
                    if source_hashes() != manifest['source_hashes']:
                        raise RuntimeError('Agent/scorer sources changed during the fixed experiment')
                    prior = sum(audit_accounting(args.out/batch['folder'])['total_dollars']
                                for batch in state['batches'] if batch['mode'] == 'routed')
                    remaining = max(0.0, args.total_dollars-prior)
                    disabled = mode == 'routed' and remaining <= 1e-9
                    allowance = max(1e-12, remaining) if mode == 'routed' else args.total_dollars
                    row_ids = [case.row_id for case in cases[offset:offset+20]]
                    batch = {'mode': mode, 'folder': folder_name, 'row_ids': row_ids, 'status': 'running',
                             'run_dollars': allowance, 'prior_live_dollars': prior,
                             'budget_exhausted_at_start': disabled, 'started_at_unix': time.time()}
                    state['batches'].append(batch)
                    atomic_json(args.out/'state.json', state)
                    command = [sys.executable, '-B', str(ROOT/'run.py'), '--dataset', str(args.dataset.resolve()),
                               '--queries', str(args.queries.resolve()), '--out', str(args.out/folder_name),
                               '--mode', mode, '--row-ids', ','.join(map(str, row_ids)),
                               '--case-seconds', str(args.case_seconds), '--run-seconds', str(args.batch_seconds),
                               '--case-dollars', '.2', '--run-dollars', str(allowance)]
                    if mode == 'routed':
                        command += ['--env-file', str(args.env_file.resolve())]
                    print(f'Starting {folder_name}: {len(row_ids)} cases; live remaining ${remaining:.6f}', flush=True)
                    try:
                        batch.update(run_batch(command, args.out/folder_name, args.batch_seconds+20))
                    except BaseException:
                        batch.update(status='interrupted', ended_at_unix=time.time())
                        atomic_json(args.out/'state.json', state)
                        raise
                    batch['ended_at_unix'] = time.time()
                    batch['accounted_dollars'] = audit_accounting(args.out/folder_name)['total_dollars']
                    atomic_json(args.out/'state.json', state)
                    print(f"Finished {folder_name}: {batch['status']}; ${batch['accounted_dollars']:.6f}", flush=True)
        finally:
            # A partial report retains all 70 cases even when this controller is interrupted.
            write_report(args, cases, state)
    else:
        write_report(args, cases, state)
    print(json.dumps({'report': str(args.out/'REPORT.md'),
                      'results': str(args.out/'results.json')}, indent=2))


if __name__ == '__main__':
    main()
