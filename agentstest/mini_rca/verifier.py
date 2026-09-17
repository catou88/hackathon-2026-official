from .case import epoch
from .protocol import normalize_alternatives

def verify_answers(answers,case,candidate_names,reasons,ledger,notices=None,component_scopes=None):
    notices = notices if notices is not None else []
    if not isinstance(answers,list) or len(answers)!=case.failures:
        raise ValueError('Wrong number of failures')
    valid_evidence={x['evidence_id'] for x in ledger if x['ok'] and x['tool'] in {'metric_anomalies','metric_series','trace_summary','trace_edges','log_search'}}
    normalized=[]
    for a in answers:
        if not isinstance(a,dict): raise ValueError('Answer must be an object')
        for k in ['datetime','component','reason','rationale']:
            if not isinstance(a.get(k),str) or not a[k].strip(): raise ValueError(f'Missing {k}')
        if '\n' in a['component']+a['reason']+a['datetime']: raise ValueError('Newline in scoring value')
        from datetime import datetime
        datetime.strptime(a['datetime'],'%Y-%m-%d %H:%M:%S')
        if not epoch(case.start)<=epoch(a['datetime'])<epoch(case.end): raise ValueError('Time outside case window')
        if a['component'] not in candidate_names: raise ValueError('Component not discovered in telemetry')
        if a['reason'] not in reasons: raise ValueError('Unknown reason label')
        if component_scopes is not None and component_scopes.get(a['component'])!=a['reason'].split(' ',1)[0]:
            raise ValueError('Reason scope does not match the observed node/container scope')
        if not isinstance(a.get('confidence'),str) or a['confidence'] not in {'low','medium','high'}: raise ValueError('Invalid confidence')
        ids=a.get('evidence_ids',[])
        if not isinstance(ids,list) or not ids or not all(isinstance(i,str) and i in valid_evidence for i in ids):
            raise ValueError('Missing or invalid telemetry evidence references')
        alternatives=normalize_alternatives(a.get('alternatives'),notices)
        normalized.append({k:a[k] for k in ['datetime','component','reason','confidence','evidence_ids','rationale']}|{'alternatives':alternatives})
    return sorted(normalized,key=lambda a:a['datetime'])
