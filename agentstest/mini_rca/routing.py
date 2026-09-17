"""Observable cost policy, not a calibrated probability of diagnostic difficulty."""


def initial_route(case, candidates, ledger, config):
    reasons = []
    if case.failures > 1:
        reasons.append('multiple_failures')
    if not candidates:
        reasons.append('no_metric_candidates')
    telemetry = [r for r in ledger if r['tool'] in {'metric_anomalies', 'trace_summary', 'log_search'}]
    if any(not r['ok'] or (isinstance(r.get('data'), dict) and r['data'].get('gaps')) for r in telemetry):
        reasons.append('incomplete_telemetry')
    missing = {'metric_anomalies', 'trace_summary', 'log_search'} - {r['tool'] for r in telemetry}
    if missing:
        reasons.append('missing_modality')
    strong = bool(reasons) and config.mode == 'routed'
    return {'stage': 'initial', 'model': config.strong_model if strong else config.fast_model,
            'tier': 'strong' if strong else 'fast',
            'reasons': reasons if config.mode == 'routed' else ['fixed_single_model'],
            'policy': 'Single-failure complete evidence starts cheap; retrieval/low confidence may escalate once.',
            'candidate_components': len({c['component'] for c in candidates})}
