"""Process-tree supervision for the public diagnosis CLI, including data/tool stalls."""
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .cli import ROOT, atomic_text, commit_result, load_query_rows, parser, spent_dollars
from .config import Config
from .process_tree import attach_tree, close_tree, stop_tree


def read_json(path):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError,PermissionError,json.JSONDecodeError):return None


class WorkerChannel:
    def __init__(self,out,token):
        self.root=Path(out)/'supervision';self.token=token;self.row_id=None;self.case_count=0

    @classmethod
    def from_environment(cls,out):
        token=os.environ.get('MINIRCA_WORKER_TOKEN')
        return cls(out,token) if token else None

    def publish(self,phase,**data):
        atomic_text(self.root/'state.json',json.dumps({'generation':self.token,'phase':phase,**data}))

    def start_case(self,rid,started):
        self.row_id=rid;self.started=started
        origin=float(os.environ.get('MINIRCA_WORKER_LAUNCHED',started)) if not self.case_count else started
        reserve=float(os.environ.get('MINIRCA_FINALIZE_RESERVE',1))
        self.hard_deadline=min(float(os.environ.get('MINIRCA_RUN_DEADLINE','inf'))-reserve,
                               origin+float(os.environ.get('MINIRCA_CASE_SECONDS','inf'))-reserve)
        self.deadline=self.hard_deadline-0.25
        self.case_count+=1
        state={'generation':self.token,'phase':'case_start','row_id':rid,'started':started,
               'hard_deadline':self.hard_deadline}
        # Durable per-row starts survive state.json being advanced to later rows.
        # A resumed worker must never grant another allowance to this row.
        atomic_text(self.root/'started'/f'{rid}.json',json.dumps(state))
        self.publish('case_start',row_id=rid,started=started,hard_deadline=self.hard_deadline)

    def checkpoint(self,result):
        self._snapshot('checkpoints',result)

    def _snapshot(self,kind,result):
        atomic_text(self.root/kind/f"{result['row_id']}.json",json.dumps({
            'generation':self.token,'at':time.monotonic(),'result':result},ensure_ascii=False))

    def result(self,result):
        self._snapshot('results',result)
        self.publish('result',row_id=result['row_id'],started=self.started,hard_deadline=self.hard_deadline)
        while True:
            ack=read_json(self.root/'ack.json')
            if ack and ack.get('generation')==self.token and ack.get('row_id')==result['row_id']:return
            time.sleep(0.01)


def valid_snapshot(snapshot,rid,token,started=None,cutoff=None):
    if not isinstance(snapshot,dict) or snapshot.get('generation')!=token:return False
    if not isinstance(snapshot.get('result'),dict) or snapshot['result'].get('row_id')!=rid:return False
    at=snapshot.get('at')
    if not isinstance(at,(int,float)) or not math.isfinite(at):return False
    return (started is None or at>=started) and (cutoff is None or at<=cutoff)


def reconcile_audit(out,rid,result,failure_kind):
    """Restore actual or reserved spend once, including an interrupted request."""
    events=[];models={};cost=0.0
    for path in sorted((out/'model_responses'/str(rid)).glob('*.json')):
        record=read_json(path)
        if not record:continue
        event=record.get('event',{})
        if event.get('status')=='pending':
            event.update(status='failed',failure_kind=failure_kind,
                         cost_dollars=event.get('reserved_dollars',0),usage_estimated=True)
            record['event']=event
            atomic_text(path,json.dumps(record,ensure_ascii=False,indent=2))
        events.append(event)
        cost+=event.get('cost_dollars',event.get('reserved_dollars',0))
        if 'prompt_tokens' in event:
            counts=models.setdefault(event['model'],{'prompt_tokens':0,'completion_tokens':0,'calls':0})
            for field in ('prompt_tokens','completion_tokens'):counts[field]+=event.get(field,0)
            counts['calls']+=1
    result['cost_dollars']=max(cost,result.get('cost_dollars',0))
    if events:result.update(models=models,model_events=events)
    return result


def recover_result(out,rid,token,elapsed,reason,started=None,cutoff=None):
    snapshot=read_json(out/'supervision/checkpoints'/f'{rid}.json')
    if valid_snapshot(snapshot,rid,token,started,cutoff):
        result=snapshot['result']
    else:
        result={'row_id':rid,'prediction':'','mode':'failed','models':{},'model_events':[],
                'ledger':[],'warnings':[],
                'evidence':'# Answer\nNo grounded answer was available before the hard deadline.\n\n# Confidence\nUnresolved; no component or timestamp invented.\n\n# Evidence\nNo completed evidence snapshot.\n\n# Ruled out\nNone.\n'}
    reconcile_audit(out,rid,result,'interrupted_resume' if reason.startswith('interrupted') else 'hard_case_timeout')
    result.update(wall_s=round(elapsed,3),hard_timeout=True,termination_reason=reason)
    result.setdefault('warnings',[]).append(reason+'; process tree stopped, pending request costs retained.')
    result['evidence']=result['evidence'].replace('# Confidence\n','# Confidence\n\n'+reason+'; stopped locally. Remote generation/billing may continue.\n',1)
    return result


def complete_artifacts(out,rid,prediction):
    """A CSV row alone is not a complete commit; repair interrupted later writes."""
    try:
        evidence=(out/'evidence'/f'{rid}.md').read_text(encoding='utf-8')
        trace=read_json(out/'traces'/f'{rid}.json')
        usage=[json.loads(line) for line in (out/'usage.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        usage=[record for record in usage if int(record['row_id'])==rid]
        return (all('# '+section in evidence for section in ('Answer','Confidence','Evidence','Ruled out'))
                and isinstance(trace,dict) and trace.get('row_id')==rid
                and trace.get('prediction')==prediction['prediction'] and len(usage)==1
                and abs(float(usage[0]['cost_dollars'])-float(prediction['cost_dollars']))<1e-12
                and abs(float(trace['cost_dollars'])-float(prediction['cost_dollars']))<1e-12)
    except (OSError,ValueError,KeyError,TypeError):
        return False


def recover_interrupted(out,rows,known_ids,case_seconds,run_deadline,reserve,errors,events):
    """Close old starts before spawning a new generation, even if time remains."""
    states={}
    state_path=out/'supervision/state.json'
    state=read_json(state_path)
    if state_path.exists() and state is None:
        raise ValueError('Interrupted worker state is unreadable; refusing to grant a new case allowance')
    if state and state.get('phase') in {'case_start','result'}:states[state.get('row_id')]=state
    for path in sorted((out/'supervision/started').glob('*.json')):
        state=read_json(path)
        if state is None:
            raise ValueError('Interrupted case journal is unreadable; refusing to grant a new case allowance')
        if state and state.get('phase') in {'case_start','result'}:states[state.get('row_id')]=state
    done={int(row['row_id']):row for row in rows}
    for rid,state in states.items():
        if rid not in known_ids:continue
        committed=done.get(rid)
        if committed and complete_artifacts(out,rid,committed):continue
        token=state.get('generation');old_started=state.get('started')
        if not isinstance(token,str) or not isinstance(old_started,(int,float)) or not math.isfinite(old_started):
            raise ValueError('Interrupted worker state has no trustworthy generation/start; refusing to reexecute')
        cutoff=state.get('hard_deadline',min(run_deadline-reserve,old_started+case_seconds-reserve))
        if not isinstance(cutoff,(int,float)) or not math.isfinite(cutoff):
            raise ValueError('Interrupted worker state has no finite deadline; refusing to reexecute')
        final=read_json(out/'supervision/results'/f'{rid}.json')
        final_matches=(valid_snapshot(final,rid,token,old_started,cutoff)
                       and (committed is None or final['result'].get('prediction')==committed['prediction']))
        if final_matches:
            result=reconcile_audit(out,rid,final['result'],'interrupted_resume')
            result['wall_s']=round(max(0,final['at']-old_started),3)
            result['recovered_on_resume']=True
            reason='interrupted_completed_result'
        else:
            reason='interrupted_case_resume'
            # Downtime is not execution latency. Preserve it separately while
            # marking the budget-based fallback estimate explicitly everywhere.
            interval=max(0,time.monotonic()-old_started)
            estimate=max(0,min(case_seconds,cutoff-old_started))
            result=recover_result(out,rid,token,estimate,reason,
                                  started=old_started,cutoff=cutoff)
            result.update(wall_s_estimated=True,
                          wall_s_note='Conservative estimate capped by the original case allowance; actual interrupted execution duration was not recovered. Resume interval includes downtime and is not measured case latency.',
                          resume_interval_s=round(interval,3),hard_timeout=False,interrupted=True)
            result['evidence']=result['evidence'].replace('# Confidence\n','# Confidence\n\n'+result['wall_s_note']+'\n',1)
        if committed:
            if result.get('prediction')!=committed['prediction']:
                raise ValueError('Committed prediction has no matching trusted snapshot; refusing to overwrite it during artifact repair')
            result['recovered_committed_artifacts']=True
        commit_result(out,rows,result,errors)
        done[rid]=next(row for row in rows if int(row['row_id'])==rid)
        events.append({'row_id':rid,'reason':reason,'wall_s':result['wall_s'],'limit_s':case_seconds,
                       'wall_s_estimated':result.get('wall_s_estimated',False),
                       **({'resume_interval_s':result['resume_interval_s']} if 'resume_interval_s' in result else {})})


def supervise(argv,command=None):
    """Keep a persistent worker for cache reuse; restart only after a killed case."""
    started=time.monotonic();now_wall=time.time()
    args=parser().parse_args(argv)
    if args.list_tools or args.tool or args.prepare:
        from .cli import main
        return main(argv)
    alias={'agents.routed':'routed','agents.heuristic':'offline'}.get(args.agent)
    if alias and args.mode and alias!=args.mode:raise ValueError('--agent and --mode conflict')
    config=Config(mode=args.mode or alias or 'routed',fast_model=args.fast_model,strong_model=args.strong_model,
                  case_seconds=args.case_seconds,run_seconds=args.run_seconds,query_seconds=args.query_seconds,
                  case_dollars=args.case_dollars,run_dollars=args.run_dollars,max_output_tokens=args.max_output_tokens,
                  experiences=args.experiences,thinking=args.thinking)
    if args.limit<0:raise ValueError('--limit must be nonnegative')
    out=args.out.resolve();dataset=args.dataset.resolve()
    if out==dataset or dataset in out.parents:raise ValueError('Output must be outside dataset')
    source=load_query_rows(args.queries or dataset/'query.csv')
    previous=read_json(out/'run.json') if args.resume else None
    query_hash=hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest()
    if previous and (previous['dataset']!=str(dataset) or previous['config']!=config.__dict__ or previous.get('query_hash')!=query_hash):
        raise ValueError('Resume dataset/config/queries mismatch')
    selected=source
    if args.row_ids:
        ids={int(x) for x in args.row_ids.split(',')}
        if ids-{int(r['row_id']) for r in source}:raise ValueError('Unknown requested row_id')
        selected=[r for r in source if int(r['row_id']) in ids]
    if args.limit:selected=selected[:args.limit]
    selected_ids={int(r['row_id']) for r in selected}
    if not args.resume and any((out/name).exists() for name in ('run.json','predictions.csv')):
        raise FileExistsError('Output exists; use a fresh --out or --resume')
    out.mkdir(parents=True,exist_ok=True)
    metadata_path=out/'supervision/run.json'
    metadata=read_json(metadata_path) if args.resume else None
    origin=(metadata or {}).get('started_at_unix',now_wall)
    # Older unsupervised runs still retain their original run deadline on resume.
    if not metadata and previous:origin=previous['started_at_unix']
    # Preserve a past deadline as well: a legacy final written after the original
    # run limit must not become timely merely because resume happened later.
    run_deadline=started+args.run_seconds-(now_wall-origin)
    atomic_text(metadata_path,json.dumps({'started_at_unix':origin,'run_seconds':args.run_seconds}))
    rows=[]
    if (out/'predictions.csv').exists():
        with (out/'predictions.csv').open(encoding='utf-8',newline='') as f:rows=list(csv.DictReader(f))
    prior_cost=spent_dollars(out);errors=[];deadline_events=[]
    # Cleanup/final artifact writes get their own portion of the configured budget.
    reserve=min(1.0,args.case_seconds*0.15,args.run_seconds*0.1)
    process=None;restart=args.resume;exit_code=0
    try:
        if args.resume:
            recover_interrupted(out,rows,{int(row['row_id']) for row in source},args.case_seconds,
                                run_deadline,reserve,errors,deadline_events)
        while selected_ids-{int(r['row_id']) for r in rows} and time.monotonic()<run_deadline-reserve:
            token=uuid.uuid4().hex
            env=os.environ.copy();env['MINIRCA_WORKER_TOKEN']=token
            child_args=list(argv)
            if restart and '--resume' not in child_args:child_args.append('--resume')
            cmd=list(command) if command else [sys.executable,'-B',str(ROOT/'run.py'),*child_args]
            opts={'creationflags':subprocess.CREATE_NO_WINDOW|0x4} if os.name=='nt' else {'start_new_session':True}
            launched=time.monotonic();active=None;active_started=None;committed=set()
            env.update(MINIRCA_WORKER_LAUNCHED=str(launched),MINIRCA_RUN_DEADLINE=str(run_deadline),
                       MINIRCA_CASE_SECONDS=str(args.case_seconds),MINIRCA_FINALIZE_RESERVE=str(reserve))
            startup_deadline=min(run_deadline-reserve,launched+args.case_seconds-reserve)
            with (out/'supervision/worker.log').open('a',encoding='utf-8') as log:
                process=subprocess.Popen(cmd,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,**opts)
                if os.name=='nt':process._mini_rca_suspended=True
                attach_tree(process)
                killed=False
                while True:
                    state=read_json(out/'supervision/state.json')
                    if state and state.get('generation')==token:
                        rid=state.get('row_id')
                        if state['phase']=='case_start' and rid not in committed:
                            active=rid;active_started=state['started'] if committed else launched
                        elif state['phase']=='result' and rid not in committed:
                            active=rid;active_started=state['started'] if committed else launched
                            final=read_json(out/'supervision/results'/f'{rid}.json')
                            cutoff=min(run_deadline-reserve,(active_started or launched)+args.case_seconds-reserve)
                            if valid_snapshot(final,rid,token,active_started,cutoff):
                                result=final['result']
                                result['wall_s']=round(time.monotonic()-(active_started or launched),3)
                                commit_result(out,rows,result,errors);committed.add(rid)
                                atomic_text(out/'supervision/ack.json',json.dumps({'generation':token,'row_id':rid}))
                                print(f"row {rid}: {result['mode']}, {result['wall_s']:.2f}s, ${result['cost_dollars']:.6f}",flush=True)
                                active=None;active_started=None
                                startup_deadline=min(run_deadline-reserve,time.monotonic()+args.case_seconds-reserve)
                    if selected_ids<={int(r['row_id']) for r in rows}:
                        try:process.wait(timeout=min(reserve,max(0,run_deadline-time.monotonic())))
                        except subprocess.TimeoutExpired:stop_tree(process)
                        break
                    cutoff=min(run_deadline-reserve,active_started+args.case_seconds-reserve) if active is not None else startup_deadline
                    expired=time.monotonic()>=cutoff
                    finished=process.poll() is not None
                    if expired or finished:
                        if expired or active is not None:stop_tree(process)
                        if active is not None:
                            reason='hard_run_deadline' if cutoff==run_deadline-reserve else ('hard_case_deadline' if expired else 'worker_exit')
                            result=recover_result(out,active,token,time.monotonic()-active_started,reason,
                                                  started=active_started,cutoff=cutoff)
                            commit_result(out,rows,result,errors)
                            result_wall=round(time.monotonic()-active_started,3)
                            deadline_events.append({'row_id':active,'reason':reason,'wall_s':result_wall,'limit_s':args.case_seconds})
                            print(f"row {active}: {reason}, {result_wall:.2f}s, ${result['cost_dollars']:.6f}",flush=True)
                            killed=True
                        elif expired:
                            deadline_events.append({'row_id':None,'reason':'startup_or_run_deadline','wall_s':round(time.monotonic()-launched,3)})
                        elif process.returncode:
                            exit_code=process.returncode
                        break
                    time.sleep(0.01)
            close_tree(process)
            if not killed:break
            restart=True
    finally:
        if process is not None and getattr(process,'_mini_rca_tree',None):close_tree(process)
        cost=spent_dollars(out)
        summary={'requested':len(selected),'completed':len({int(r['row_id']) for r in rows}&selected_ids),
                 'valid_predictions':sum(bool(r['prediction']) for r in rows if int(r['row_id']) in selected_ids),
                 'run_wall_s':round(time.monotonic()-started,3),'cumulative_wall_s':round(time.time()-origin,3),
                 'run_cost_dollars':cost,'invocation_cost_dollars':cost-prior_cost,
                 'io_errors':errors,'deadline_events':deadline_events,'hard_supervision':True,
                 'cost_note':'Competition rates; pending/unknown requests conservatively reserved. Local cancellation does not guarantee provider cancellation.'}
        atomic_text(out/'summary.json',json.dumps(summary,indent=2))
        print(json.dumps(summary),flush=True)
    if exit_code:raise SystemExit(exit_code)
    return summary


def main(argv=None):
    return supervise(sys.argv[1:] if argv is None else argv)
