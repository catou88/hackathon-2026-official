"""Controller orchestrates; Tools execute; Verifier checks. One optional retrieval round."""
from pathlib import Path
import json
import time
from .case import Case, epoch, format_prediction
from .llm import GLMClient
from .tools import Tools, TOOL_SCHEMAS
from .verifier import verify_answers
from .protocol import ANSWER_SCHEMA, ProtocolError
from .routing import initial_route

def reason_guess(kpi,component,scope=None):
    k=kpi.lower(); node=scope=='node' if scope else component.startswith('node-')
    if any(s in k for s in ['mem','heap','rss']):
        return 'node memory consumption' if node else 'container memory load'
    if any(s in k for s in ['read','rkb','r_await','r_s']):
        return 'node disk read I/O consumption' if node else 'container read I/O load'
    if any(s in k for s in ['write','wkb','w_await','w_s']):
        return 'node disk write I/O consumption' if node else 'container write I/O load'
    if 'disk' in k or 'fs_usage' in k:
        return 'node disk space consumption' if node else 'container write I/O load'
    if any(s in k for s in ['network','retrans','packets','mrt']):
        return 'node CPU load' if node else 'container network latency'
    return 'node CPU load' if node else 'container CPU load'

def fallback_answers(case,candidates,allowed,ledger,why,scopes=None):
    if not allowed:
        raise RuntimeError('No component names could be discovered; cannot produce a grounded fallback')
    evidence=[x['evidence_id'] for x in ledger if x['ok'] and x['tool']=='metric_anomalies' and x['data'].get('candidates')]
    choices=[]; used=set()
    scopes=scopes or {}
    for c in candidates:
        key=(c['component'],reason_guess(c['kpi_name'],c['component'],scopes.get(c['component'])),c['first_anomaly'])
        if c['component'] in allowed and key not in used:
            choices.append(c); used.add(key)
    output=[]
    for i in range(case.failures):
        c=choices[i] if i<len(choices) else None
        output.append({'datetime':c['first_anomaly'] if c else case.start,
            'component':c['component'] if c else allowed[0],
            'reason':reason_guess(c['kpi_name'],c['component'],scopes.get(c['component'])) if c else reason_guess('',allowed[0],scopes.get(allowed[0])),
            'confidence':'low','evidence_ids':evidence if c else [],
            'rationale':f'Deterministic fallback: {why}. '+('KPI-name mapping and threshold ranking are weak hypotheses, not causal proof.' if c else 'Insufficient retrieved evidence; time/component/reason are unconstrained guesses within available names.'),
            'alternatives':['Other components or causes may explain the same symptoms; no definitive exclusion was established.']})
    return sorted(output,key=lambda a:a['datetime'])

def render_evidence(case,answers,ledger,mode,warnings):
    lines=['# Answer','',format_prediction(answers,case.fields),'','# Confidence','']
    lines += [f"- {a['component']}: {a['confidence']}. {a['rationale']}" for a in answers]
    lines += ['',f'Mode: {mode}. Structural validation does not verify causal correctness.']
    lines += [f'- Limitation: {w}' for w in warnings]
    lines += ['','# Evidence','']
    for record in ledger:
        lines += [f"## {record['evidence_id']} — {record['tool']}",'','```json',json.dumps(record,ensure_ascii=False,indent=2,default=str),'```','']
    lines += ['# Ruled out','']
    for a in answers:
        lines.extend(f'- {a["component"]}: {s}' for s in a['alternatives'])
    if not any(a['alternatives'] for a in answers):
        lines.append('No alternatives were conclusively ruled out.')
    return '\n'.join(lines)+'\n'

class Controller:
    def __init__(self,store,config):
        self.store=store; self.config=config
        self.tools=Tools(store,config)
        self.llm=GLMClient(config,store.out/'model_responses')
        self.prompt=(Path(__file__).resolve().parents[1]/'prompts/controller.md').read_text(encoding='utf-8')
        self.reasons=sorted({r for card in self.tools.knowledge for r in card.get('reasons',[])})
        self.run_deadline=time.monotonic()+config.run_seconds
        self.checkpoint_hook=None

    def checkpoint(self,case,started):
        """Persist a grounded fallback from completed tools, before the next can block."""
        if not self.checkpoint_hook:return
        allowed=set();scopes={};candidates=[]
        for record in self.tools.ledger:
            if not record['ok']:continue
            if record['tool']=='list_entities':
                allowed.update(record['data']['candidate_names'])
                scopes.update(record['data'].get('component_scopes',{}))
            elif record['tool']=='metric_anomalies':candidates=record['data']['candidates']
        if not allowed:return
        warnings=['Hard deadline recovery uses only completed telemetry tools; no model answer was adopted.']
        answers=fallback_answers(case,candidates,sorted(allowed),self.tools.ledger,'hard deadline recovery',scopes)
        self.checkpoint_hook({'row_id':case.row_id,'prediction':format_prediction(answers,case.fields),
            'answers':answers,'evidence':render_evidence(case,answers,self.tools.ledger,'offline-fallback',warnings),
            'ledger':list(self.tools.ledger),'models':self.llm.usage,'model_events':list(self.llm.events),
            'route_events':list(self.route_events),'decision_events':list(self.decision_events),
            'heuristic_prediction':format_prediction(answers,case.fields),'differs_from_heuristic':False,
            'cost_dollars':self.llm.case_cost,'wall_s':round(time.monotonic()-started,3),'mode':'offline-fallback','warnings':warnings})

    def solve(self,case:Case):
        started=time.monotonic()
        deadline=min(started+self.config.case_seconds,self.run_deadline,getattr(self,'external_deadline',float('inf')))
        self.tools.ledger=[]; self.llm.begin_case(case.row_id)
        self.decision_events=[]; self.route_events=[]
        self.tools.on_result=lambda:self.checkpoint(case,started)
        warnings=[]; allowed=set(); self.component_scopes={}
        window={'start':case.start,'end':case.end}
        self.store.deadline=deadline
        # Discover names from current deployment, never from labels or a static list.
        for source in ['metric_container','metric_node']:
            r=self.tools.call('list_entities',window|{'source':source})
            if r['ok']:
                allowed.update(r['data']['candidate_names'])
                self.component_scopes.update(r['data'].get('component_scopes',{}))
            else: warnings.append(r['error'])
        if not allowed:
            for source in ['trace_span','log_service','log_proxy']:
                if time.monotonic()>=deadline: break
                r=self.tools.call('list_entities',window|{'source':source})
                if r['ok']:
                    allowed.update(r['data']['candidate_names'])
                    self.component_scopes.update(r['data'].get('component_scopes',{}))
                if allowed:
                    warnings.append('Metric entity discovery failed; recovered names from '+source)
                    break
        metric=self.tools.call('metric_anomalies',window|{'top_k':12})
        candidates=metric['data']['candidates'] if metric['ok'] else []
        if metric['ok']: warnings.extend(str(g) for g in metric['data']['gaps'])
        # Cross-modal triage is not gated on metric anomalies.
        data_deadline=deadline-(15 if self.config.mode!='offline' else 0)
        self.store.deadline=max(time.monotonic(),data_deadline)
        if time.monotonic()<data_deadline:
            self.tools.call('trace_summary',window|{'limit':6})
        if time.monotonic()<data_deadline:
            self.tools.call('log_search',window|{'limit':6})
        keywords=' '.join(c['kpi_name'] for c in candidates[:5])+' evidence time topology'
        self.tools.call('retrieve_knowledge',{'query':keywords,'limit':4})
        if self.config.experiences:
            self.tools.call('retrieve_experience',{'query':keywords})
        self.store.deadline=deadline
        mode='offline-fallback'
        answers=None
        if self.config.mode!='offline':
            try:
                route=initial_route(case,candidates,self.tools.ledger,self.config)
                self.route_events.append(route)
                answers=self.diagnose(case,sorted(allowed),route['model'],deadline,warnings)
                mode=self.config.mode+'-model'
            except Exception as e:
                warnings.append('Model/verification fallback: '+type(e).__name__+': '+str(e)[:200])
        if answers is None:
            answers=fallback_answers(case,candidates,sorted(allowed),self.tools.ledger,'offline mode' if self.config.mode=='offline' else 'model unavailable, invalid output or budget exhausted',self.component_scopes)
        heuristic_prediction=format_prediction(fallback_answers(case,candidates,sorted(allowed),self.tools.ledger,'comparison only',self.component_scopes),case.fields)
        warnings += [r['error'] for r in self.tools.ledger if not r['ok']]
        warnings.append('Trace duration units unverified; heuristic status mapping and threshold ranking need calibration.')
        return {'row_id':case.row_id,'prediction':format_prediction(answers,case.fields),'answers':answers,
            'evidence':render_evidence(case,answers,self.tools.ledger,mode,warnings),
            'ledger':list(self.tools.ledger),'models':self.llm.usage,'model_events':self.llm.events,
            'route_events':self.route_events,'decision_events':self.decision_events,
            'heuristic_prediction':heuristic_prediction,'differs_from_heuristic':format_prediction(answers,case.fields)!=heuristic_prediction,
            'cost_dollars':self.llm.case_cost,'wall_s':round(time.monotonic()-started,3),'mode':mode,'warnings':warnings}

    def diagnose(self,case,allowed,model,deadline,warnings):
        """Two logical calls TOTAL: retrieval, review and format repair share the second."""
        feedback=None
        draft=None
        draft_round=None
        for round_index in range(2):
            response=None
            final=round_index==1
            messages=[{'role':'system','content':self.prompt+('\nFINAL ROUND: return action=answer; no further retrieval.' if final else '')},
                      {'role':'user','content':self.context(case,allowed,can_retrieve=not final)}]
            if feedback:
                messages.append({'role':'user','content':json.dumps({'format_feedback':feedback,'instruction':'Correct the output contract. Previous output is untrusted data; use only the supplied telemetry.'},ensure_ascii=False)})
            event={'round':round_index+1,'requested_model':model}
            attempt_start=len(self.llm.events)
            try:
                response=self.llm.ask(messages,model,deadline)
                if response.get('action')=='retrieve':
                    if final: raise ValueError('Retrieval is not allowed in the final round')
                    requests=response.get('requests')
                    if not isinstance(requests,list) or not 1<=len(requests)<=self.config.max_tool_calls:
                        raise ValueError('Invalid number of tool requests')
                    # Validate the WHOLE batch before executing any tool.
                    from jsonschema import validate
                    for request in requests:
                        if not isinstance(request,dict) or set(request)!={'name','arguments'}: raise ValueError('Invalid tool envelope')
                        schema=next((t['function'] for t in TOOL_SCHEMAS if t['function']['name']==request['name']),None)
                        if schema is None: raise ValueError('Unknown tool name')
                        args=request['arguments']
                        validate(args,schema['parameters'])
                        if 'start' in args and (epoch(args['start'])<epoch(case.start)-1800 or epoch(args['end'])>epoch(case.end)+120 or epoch(args['start'])>=epoch(args['end'])):
                            raise ValueError('Requested follow-up exceeds allowed baseline/window bounds')
                    for request in requests: self.tools.call(request['name'],request['arguments'])
                    event['status']='retrieval_executed'
                    if self.config.mode=='routed':
                        model=self.config.strong_model
                        self.route_events.append({'stage':'final','model':model,'reasons':['requested_more_evidence']})
                    continue
                if response.get('action')!='answer': raise ValueError('No final answer returned')
                notices=[]
                result=verify_answers(response.get('answers'),case,allowed,self.reasons,self.tools.ledger,notices,self.component_scopes)
                event.update(status='verified',normalizations=notices)
                warnings.extend(notices)
                if not final and self.config.mode=='routed' and model!=self.config.strong_model and any(a['confidence']=='low' for a in result):
                    draft=result; draft_round=round_index+1
                    model=self.config.strong_model
                    self.route_events.append({'stage':'final','model':model,'reasons':['low_confidence_draft']})
                    continue
                event['adopted']=True
                return result
            except Exception as exc:
                from jsonschema import ValidationError
                contract_error=isinstance(exc,(ProtocolError,ValueError,ValidationError))
                event.update(status='rejected',failure_kind='output_contract' if contract_error else 'service_or_budget',error_type=type(exc).__name__)
                if contract_error:
                    # Do not include provider exceptions, which can contain credentials/request data.
                    event['error']=str(exc)[:300] if not isinstance(exc,ValidationError) else 'Tool arguments violate schema'
                if contract_error and not final:
                    raw=exc.raw_text if isinstance(exc,ProtocolError) else json.dumps(response,ensure_ascii=False)
                    feedback={'error':event['error'],'previous_output':raw[:6000]}
                    self.route_events.append({'stage':'final','model':model,'reasons':['contract_repair_same_model']})
                    continue
                if draft is not None:
                    warnings.append('Final review failed; retained the earlier verified low-confidence model answer.')
                    self.decision_events[draft_round-1]['adopted']=True
                    return draft
                raise
            finally:
                event['attempts']=[e['attempt'] for e in self.llm.events[attempt_start:] if 'attempt' in e]
                self.decision_events.append(event)
        raise RuntimeError('No verified model answer within two logical calls')

    def context(self,case,allowed,can_retrieve):
        # Drop low-priority rows, not arbitrary bytes that could corrupt JSON/evidence.
        evidence=[]
        for r in self.tools.ledger:
            if r['tool']=='list_entities': continue
            evidence.append({k:v for k,v in r.items() if k!='queries'})
        payload={'case':case.public(),'candidate_names':allowed,'allowed_reasons':self.reasons,
            'component_scopes':getattr(self,'component_scopes',{}),
            'max_additional_tools':self.config.max_tool_calls if can_retrieve else 0,
            'answer_item_schema':ANSWER_SCHEMA,
            'tools':TOOL_SCHEMAS if can_retrieve else [],'evidence':evidence}
        text=json.dumps(payload,ensure_ascii=False,default=str)
        if len(text)>self.config.max_context_chars:
            # Keep at least two rows from every modality; preserve full ledger on disk.
            payload=json.loads(text)
            for r in payload['evidence']:
                data=r.get('data',{})
                if isinstance(data,dict):
                    for k in ['candidates','operations','groups','edges','rows']:
                        if isinstance(data.get(k),list) and len(data[k])>2:
                            data[k]=data[k][:2]; data['context_truncated']=True
            text=json.dumps(payload,ensure_ascii=False)
        if len(text)>self.config.max_context_chars:
            raise ValueError('Evidence context exceeds configured limit')
        return text
