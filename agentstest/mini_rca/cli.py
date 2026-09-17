"""Submission CLI. Parsing and output failures are isolated by row_id."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from .agents import Controller
from .case import Case
from .config import Config
from .store import TelemetryStore, SCHEMAS
from .tools import Tools, TOOL_SCHEMAS

ROOT=Path(__file__).resolve().parents[1]


def load_query_rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if 'scoring_points' in (reader.fieldnames or []):
            raise ValueError('Diagnosis requires query.csv WITHOUT scoring_points; evaluate labels separately')
        if not {'row_id','task_index','instruction'} <= set(reader.fieldnames or []):
            raise ValueError('Missing required query columns')
        rows=[{k:r[k] for k in ('row_id','task_index','instruction')} for r in reader]
    ids=[int(r['row_id']) for r in rows]
    if len(set(ids))!=len(ids) or any(r<0 for r in ids):
        raise ValueError('row_id must be unique nonnegative integers')
    return rows


def load_queries(path):
    """Strict helper for development; the CLI parses each row inside its failure boundary."""
    return [Case.parse(r) for r in load_query_rows(path)]


def atomic_text(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(text,encoding='utf-8')
    # Windows readers briefly deny delete-sharing while the supervisor polls.
    until=time.monotonic()+0.15
    while True:
        try:
            os.replace(tmp,path)
            break
        except PermissionError:
            if time.monotonic()>=until:raise
            time.sleep(0.002)


def save_predictions(path,rows):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['row_id','prediction','mode','wall_s','cost_dollars'])
        writer.writeheader();writer.writerows(sorted(rows,key=lambda x:int(x['row_id'])))
    until=time.monotonic()+0.15
    while True:
        try:
            os.replace(tmp,path)
            break
        except PermissionError:
            if time.monotonic()>=until:raise
            time.sleep(0.002)


def spent_dollars(out):
    """Include pending/crashed requests. Never overwrite a prior attempt's audit file."""
    audited=0.0
    for p in (out/'model_responses').glob('*/*.json'):
        e=json.loads(p.read_text(encoding='utf-8')).get('event',{})
        audited+=e.get('cost_dollars',e.get('reserved_dollars',0))
    usage=0.0
    if (out/'usage.jsonl').exists():
        for line in (out/'usage.jsonl').read_text(encoding='utf-8').splitlines():
            if line.strip(): usage+=json.loads(line).get('cost_dollars',0)
    return max(audited,usage)


def parser():
    ap=argparse.ArgumentParser(description='Track 1 structured-evidence RCA agent')
    ap.add_argument('--dataset',type=Path,default=ROOT.parent/'Track1/data/Market-cloudbed-1')
    ap.add_argument('--queries',type=Path)
    ap.add_argument('--out',type=Path,default=ROOT/'out/local')
    ap.add_argument('--agent',choices=['mini_rca.adapter','agents.routed','agents.heuristic'],default='mini_rca.adapter',help='Starter-compatible selector; agents.heuristic explicitly selects the offline baseline')
    ap.add_argument('--mode',choices=['offline','routed','single'],default=None)
    ap.add_argument('--fast-model',default=Config.fast_model)
    ap.add_argument('--strong-model',default=Config.strong_model)
    ap.add_argument('--limit',type=int,default=0)
    ap.add_argument('--row-ids')
    ap.add_argument('--resume',action='store_true')
    for name in ['case-seconds','run-seconds','query-seconds','case-dollars','run-dollars']:
        ap.add_argument('--'+name,type=float,default=getattr(Config,name.replace('-','_')))
    ap.add_argument('--max-output-tokens',type=int,default=Config.max_output_tokens)
    ap.add_argument('--experiences',action='store_true')
    ap.add_argument('--thinking',action='store_true')
    ap.add_argument('--env-file',type=Path,help='Explicit credentials file for authorized live runs only')
    ap.add_argument('--list-tools',action='store_true')
    ap.add_argument('--tool')
    ap.add_argument('--tool-args',default='{}')
    ap.add_argument('--tool-args-file',type=Path)
    ap.add_argument('--prepare',nargs='+',choices=list(SCHEMAS))
    ap.add_argument('--start',default='2022-03-20 00:00:00')
    ap.add_argument('--end',default='2022-03-22 00:00:00')
    return ap


def commit_result(out, rows, result, io_errors):
    """Only the supervising process writes final artifacts in managed runs."""
    rid=result['row_id']
    rows[:]=[r for r in rows if int(r['row_id'])!=rid]
    rows.append({k:result[k] for k in ['row_id','prediction','mode','wall_s','cost_dollars']})
    writes=[('prediction',lambda:save_predictions(out/'predictions.csv',rows)),
            ('evidence',lambda:atomic_text(out/'evidence'/f'{rid}.md',result['evidence'])),
            ('trace',lambda:atomic_text(out/'traces'/f'{rid}.json',json.dumps({k:v for k,v in result.items() if k!='evidence'},ensure_ascii=False,indent=2)))]
    for kind,write in writes:
        try:write()
        except OSError as exc:io_errors.append({'row_id':rid,'artifact':kind,'error_type':type(exc).__name__})
    try:
        path=out/'usage.jsonl'
        old=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()] if path.exists() else []
        old=[u for u in old if int(u['row_id'])!=rid]
        usage={k:result[k] for k in ['row_id','mode','wall_s','cost_dollars','models','model_events']}
        usage.update({k:result[k] for k in ['wall_s_estimated','wall_s_note','resume_interval_s',
                                          'recovered_on_resume','recovered_committed_artifacts'] if k in result})
        old.append(usage)
        atomic_text(path,''.join(json.dumps(u)+'\n' for u in old))
    except OSError as exc:io_errors.append({'row_id':rid,'artifact':'usage','error_type':type(exc).__name__})


def main(argv=None):
    ap=parser()
    args=ap.parse_args(argv)
    if args.limit<0: ap.error('--limit must be nonnegative')
    if args.list_tools:
        print(json.dumps(TOOL_SCHEMAS,indent=2));return
    alias_mode={'agents.routed':'routed','agents.heuristic':'offline'}.get(args.agent)
    if alias_mode and args.mode and alias_mode!=args.mode:
        ap.error('--agent and --mode conflict')
    mode=args.mode or alias_mode or 'routed'
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file,override=False)
    config=Config(mode=mode,fast_model=args.fast_model,strong_model=args.strong_model,
        case_seconds=args.case_seconds,run_seconds=args.run_seconds,query_seconds=args.query_seconds,
        case_dollars=args.case_dollars,run_dollars=args.run_dollars,max_output_tokens=args.max_output_tokens,
        experiences=args.experiences,thinking=args.thinking)
    from .supervision import WorkerChannel
    channel=WorkerChannel.from_environment(args.out)
    started=time.monotonic();started_at=time.time()
    store=TelemetryStore(args.dataset,args.out,config)
    try:
        if args.prepare:
            store.deadline=started+config.run_seconds
            print(json.dumps(store.prepare(args.prepare,args.start,args.end),indent=2));return
        if args.tool:
            data=args.tool_args_file.read_text(encoding='utf-8-sig') if args.tool_args_file else args.tool_args
            result=Tools(store,config).call(args.tool,json.loads(data))
            atomic_text(args.out/'tool-result.json',json.dumps(result,ensure_ascii=False,indent=2))
            print(json.dumps(result,ensure_ascii=False,indent=2));return
        source_rows=load_query_rows(args.queries or args.dataset/'query.csv')
        query_hash=hashlib.sha256(json.dumps(source_rows,sort_keys=True).encode()).hexdigest()
        selected=source_rows
        if args.row_ids:
            requested={int(x) for x in args.row_ids.split(',')}
            if requested-{int(r['row_id']) for r in selected}:raise ValueError('Unknown requested row_id')
            selected=[r for r in selected if int(r['row_id']) in requested]
        if args.limit:selected=selected[:args.limit]
        pred_path=args.out/'predictions.csv';manifest_path=args.out/'run.json'
        rows=[];done=set();prior_cost=0
        if manifest_path.exists() or pred_path.exists():
            if not args.resume:raise FileExistsError('Output exists; use a fresh --out or --resume')
            prior=json.loads(manifest_path.read_text(encoding='utf-8'))
            if prior['dataset']!=str(args.dataset.resolve()) or prior['config']!=config.__dict__ or prior.get('query_hash')!=query_hash:
                raise ValueError('Resume dataset/config/queries mismatch')
            started_at=prior['started_at_unix']
            prior_cost=spent_dollars(args.out)
            if pred_path.exists():
                with pred_path.open(encoding='utf-8',newline='') as f:rows=list(csv.DictReader(f))
                done={int(r['row_id']) for r in rows}
        controller=Controller(store,config)
        controller.llm.total_dollars=prior_cost
        controller.run_deadline=started+max(0,config.run_seconds-(time.time()-started_at))
        manifest={'dataset':str(args.dataset.resolve()),'config':config.__dict__,'agent':args.agent,
                  'query_hash':query_hash,'started_at_unix':started_at,'selected_row_ids':[int(r['row_id']) for r in selected],
                  'note':'Resume includes prior attempt costs and elapsed wall clock, including downtime. Offline is a weak heuristic, not model diagnosis.'}
        atomic_text(manifest_path,json.dumps(manifest,indent=2))
        print(f'agent={args.agent}, mode={mode}',flush=True)
        io_errors=[]
        for row in selected:
            rid=int(row['row_id'])
            if rid in done:continue
            if time.monotonic()>=controller.run_deadline:break
            case_started=time.monotonic()
            if channel:channel.start_case(rid,case_started)
            controller.external_deadline=channel.deadline if channel else float('inf')
            controller.llm.begin_case(rid);controller.tools.ledger=[]
            controller.checkpoint_hook=channel.checkpoint if channel else None
            try:
                result=controller.solve(Case.parse(row))
            except Exception as exc:
                result={'row_id':rid,'prediction':'','mode':'failed','wall_s':round(time.monotonic()-case_started,3),'cost_dollars':controller.llm.case_cost,
                        'evidence':f'# Answer\nNo grounded answer could be formed.\n\n# Confidence\nUnresolved input or absent telemetry; no invented component or timestamp.\n\n# Evidence\nFailure type: {type(exc).__name__}.\n\n# Ruled out\nNone.\n',
                        'models':controller.llm.usage,'model_events':controller.llm.events,'ledger':controller.tools.ledger,'warnings':[type(exc).__name__]}
            if channel:
                channel.result(result)
                rows.append({k:result[k] for k in ['row_id','prediction','mode','wall_s','cost_dollars']})
            else:
                commit_result(args.out,rows,result,io_errors)
            print(f"row {rid}: {result['mode']}, {result['wall_s']:.2f}s, ${result['cost_dollars']:.6f}",flush=True)
        selected_ids={int(r['row_id']) for r in selected}
        summary={'requested':len(selected),'completed':len({int(r['row_id']) for r in rows}&selected_ids),
                 'valid_predictions':sum(bool(r['prediction']) for r in rows if int(r['row_id']) in selected_ids),
                 'run_wall_s':round(time.monotonic()-started,3),'cumulative_wall_s':round(time.time()-started_at,3),
                 'run_cost_dollars':controller.llm.total_dollars,'invocation_cost_dollars':controller.llm.total_dollars-prior_cost,
                 'io_errors':io_errors,'cost_note':'Competition rates; pending/unknown attempts conservatively reserved. Resume totals include prior costs.'}
        if not channel:atomic_text(args.out/'summary.json',json.dumps(summary,indent=2))
        print(json.dumps(summary))
        if io_errors:print('Artifact write failures recorded in summary.json',file=sys.stderr)
    finally:
        store.close()
