import csv
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mini_rca.case import Case,epoch,local_time,format_prediction
from mini_rca.config import Config
from mini_rca.store import TelemetryStore,SCHEMAS
from mini_rca.tools import Tools
from mini_rca.agents import Controller
from mini_rca.verifier import verify_answers
from mini_rca.cli import load_queries
from mini_rca.llm import parse_json,GLMClient

def write_table(root,name,rows,day='2022_03_20'):
    p=root/'telemetry'/day/name.split('_')[0]/(name+'.csv')
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f);w.writerow(SCHEMAS[name]);w.writerows(rows)

class SystemTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        base=Path(self.temp.name)
        self.data=base/'data';self.out=base/'out'
        t=epoch('2022-03-20 00:00:00');self.t=t
        write_table(self.data,'metric_node',[[t+i*60,'node-1','system.cpu.pct_usage',100 if i in [65,66,67] else 10] for i in range(120)])
        write_table(self.data,'metric_container',[[t+i*60,'node-1.cartservice-0','container_memory_working_set_MB',20] for i in range(120)])
        write_table(self.data,'metric_service',[["cartservice-grpc",t+i*60,100,100,3,20] for i in range(120)])
        write_table(self.data,'log_service',[
            ['l1',t+3900,'cartservice-0','application','INFO received request, id=123\nsecond line'],
            ['l2',t+3960,'cartservice-0','application','ERROR timeout request id=456'],
        ])
        # Same span ID in two traces; join MUST include trace_id to avoid false edges.
        write_table(self.data,'trace_span',[
            [(t+3900)*1000,'frontend-0','p','traceA',5000,'rpc','0','GET',''],
            [(t+3900)*1000+20,'cartservice-0','c','traceA',3000,'rpc','0','Cart/Get','p'],
            [(t+4000)*1000,'other-0','p','traceB',3000,'db','Ok','HGET',''],
            [(t+3900)*1000+20,'cartservice-0','c','traceA',3000,'rpc','0','Cart/Get','p'],
        ])
        self.config=Config(mode='offline',case_seconds=20,query_seconds=5)
        self.store=TelemetryStore(self.data,self.out,self.config)
        self.tools=Tools(self.store,self.config)
        self.window={'start':'2022-03-20 01:00:00','end':'2022-03-20 01:30:00'}
        self.case=Case.parse({'row_id':0,'task_index':'task_7','instruction':'March 20, 2022, from 01:00 to 01:30, one failure occurred.'})

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def test_timezone_and_midnight(self):
        self.assertEqual(epoch('2022-03-20 00:00:00'),1647705600)
        c=Case.parse({'row_id':1,'task_index':'task_6','instruction':'March 21, 2022, from 23:30 to March 22, 2022, at 00:00, two failures.'})
        self.assertEqual(c.end,'2022-03-22 00:00:00');self.assertEqual(c.failures,2)

    def test_all_real_case_windows(self):
        path=Path(__file__).resolve().parents[2]/'Track1/data/Market-cloudbed-1/query.csv'
        if not path.exists(): self.skipTest('Local bundle not present')
        cases=load_queries(path)
        self.assertEqual(len(cases),70)
        self.assertEqual(sum(c.failures==2 for c in cases),26)
        self.assertEqual(len({(c.start,c.end) for c in cases}),57)

    def test_anomaly_global_baseline_and_first_sample(self):
        r=self.tools.call('metric_anomalies',self.window|{'sources':['metric_node']})
        self.assertTrue(r['ok'],r)
        c=r['data']['candidates'][0]
        self.assertEqual(c['baseline_n'],120)
        self.assertEqual(c['baseline'],10)
        self.assertEqual(c['first_anomaly'],'2022-03-20 01:05:00')
        self.assertEqual(c['adjacent_anomalies'],2)

    def test_service_unpivot(self):
        r=self.tools.call('metric_series',self.window|{'source':'metric_service','cmdb_id':'cartservice-grpc','kpi_name':'mrt'})
        self.assertTrue(r['ok'],r);self.assertEqual(len(r['data']['rows']),30)

    def test_trace_units_and_join_key(self):
        r=self.tools.call('trace_edges',self.window)
        self.assertTrue(r['ok'],r)
        rows=r['data']['edges'];self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['matched_spans'],1)
        self.assertEqual(rows[0]['start_gap_p50_ms'],20)
        self.assertEqual(rows[0]['parent_component'],'frontend-0')

    def test_trace_mixed_status(self):
        r=self.tools.call('trace_summary',self.window)
        self.assertTrue(r['ok'],r)
        self.assertEqual(sum(x['nonstandard_status_count'] for x in r['data']['operations']),0)

    def test_csv_multiline_and_parameterized_search(self):
        r=self.tools.call('log_search',self.window|{'keywords':['INFO']})
        self.assertTrue(r['ok'],r)
        self.assertIn('\n',r['data']['groups'][0]['sample']['value'])
        r=self.tools.call('log_search',self.window|{'keywords':["' OR TRUE --"]})
        self.assertTrue(r['ok']);self.assertEqual(r['data']['groups'],[])

    def test_forbidden_tools_args_and_output(self):
        with self.assertRaises(ValueError): self.tools.call('read_file',{'path':'dev/query_dev.csv'})
        with self.assertRaises(Exception): self.tools.call('metric_series',self.window|{'source':'../../record'})
        with self.assertRaises(ValueError): TelemetryStore(self.data,self.data/'out',self.config)

    def test_cached_parquet_equivalence_and_invalidation(self):
        args=self.window|{'source':'metric_node','cmdb_id':'node-1','kpi_name':'system.cpu.pct_usage'}
        before=self.tools.call('metric_series',args)['data']['rows']
        paths=self.store.prepare(['metric_node'],**self.window)
        after=self.tools.call('metric_series',args)['data']['rows']
        self.assertEqual(before,after);self.assertTrue(Path(paths[0]).exists())
        p=self.store.files('metric_node',**self.window)[0]
        with p.open('a',encoding='utf-8') as f:f.write(f'{self.t+5000},node-1,system.cpu.pct_usage,15\n')
        self.assertNotIn('read_parquet',self.store.relation('metric_node',**self.window))

    def test_verification_count_evidence_and_key_order(self):
        r=self.tools.call('metric_anomalies',self.window|{'sources':['metric_node']})
        a={'datetime':'2022-03-20 01:05:00','component':'node-1','reason':'node CPU load','confidence':'low','evidence_ids':[r['evidence_id']],'rationale':'Observed excursion','alternatives':[]}
        verified=verify_answers([a],self.case,['node-1'],['node CPU load'],self.tools.ledger)
        self.assertEqual(list(json.loads(format_prediction(verified,self.case.fields))['1']),['root cause occurrence datetime','root cause component','root cause reason'])
        with self.assertRaises(ValueError):verify_answers([a,a],self.case,['node-1'],['node CPU load'],self.tools.ledger)
        with self.assertRaises(ValueError):verify_answers([a|{'evidence_ids':['fake']}],self.case,['node-1'],['node CPU load'],self.tools.ledger)

    def test_offline_does_not_call_llm(self):
        c=Controller(self.store,self.config)
        with patch.object(c.llm,'ask',side_effect=AssertionError('network')):
            result=c.solve(self.case)
        self.assertEqual(result['cost_dollars'],0)
        self.assertEqual(result['mode'],'offline-fallback')
        self.assertEqual(len(json.loads(result['prediction'])),1)

    def test_controller_one_followup_with_fake_model(self):
        self.config.mode='routed'
        c=Controller(self.store,self.config)
        def fake(messages,model,deadline):
            body=json.loads(messages[-1]['content'])
            if body['max_additional_tools']:
                return {'action':'retrieve','requests':[{'name':'trace_edges','arguments':self.window}]}
            ev=next(r for r in body['evidence'] if r['tool']=='metric_anomalies')
            return {'action':'answer','answers':[{'datetime':'2022-03-20 01:05:00','component':'node-1','reason':'node CPU load','confidence':'low','evidence_ids':[ev['evidence_id']],'rationale':'Synthetic test evidence','alternatives':[]}]}
        with patch.object(c.llm,'ask',side_effect=fake) as mock:
            result=c.solve(self.case)
        self.assertEqual(mock.call_count,2)
        self.assertEqual(result['mode'],'routed-model')

    def test_model_bad_json_falls_back(self):
        self.config.mode='routed';c=Controller(self.store,self.config)
        with patch.object(c.llm,'ask',return_value={'action':'answer','answers':[]}):
            result=c.solve(self.case)
        self.assertEqual(result['mode'],'offline-fallback')
        self.assertIn('Wrong number',str(result['warnings']))

    def test_label_file_rejected(self):
        p=self.out/'query.csv'
        p.write_text('row_id,task_index,instruction,scoring_points\n',encoding='utf-8')
        with self.assertRaises(ValueError):load_queries(p)

    def test_deadline_interrupt_and_recovery(self):
        with self.assertRaises(Exception):self.store.execute('SELECT sum(i*j) FROM range(1000000) a(i), range(1000000) b(j)',seconds=0.01)
        self.assertEqual(self.store.execute('SELECT 1 AS ok')[0]['ok'],1)

    def test_strict_json_and_optional_experience(self):
        self.assertEqual(parse_json('```json\n{"action":"answer"}\n```')['action'],'answer')
        with self.assertRaises(Exception):parse_json('{"action":')
        self.assertFalse(self.tools.call('retrieve_experience',{'query':'network'})['data']['enabled'])

    def fake_client(self,response=None,error=None):
        client=GLMClient(Config(mode='single'))
        client.client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response,side_effect=error))))
        return client

    def test_transport_usage_and_thinking_option_without_network(self):
        r=SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100,completion_tokens=20),choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='{"action":"answer"}'))])
        client=self.fake_client(r)
        result=client.ask([{'role':'user','content':'synthetic test'}],'zai-org/GLM-4.7-Flash',time.monotonic()+5)
        self.assertEqual(result['action'],'answer')
        self.assertAlmostEqual(client.case_cost,(100*.065+20*.4)/1e6)
        self.assertEqual(client.events[0]['finish_reason'],'stop')
        self.assertFalse(client.client.chat.completions.create.call_args.kwargs['extra_body']['chat_template_kwargs']['enable_thinking'])

    def test_transport_rejects_truncated_output_and_counts_usage(self):
        r=SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100,completion_tokens=1200),choices=[SimpleNamespace(finish_reason='length',message=SimpleNamespace(content='{"action":'))])
        client=self.fake_client(r)
        with self.assertRaises(RuntimeError):client.ask([{'role':'user','content':'test'}],'zai-org/GLM-4.7-Flash',time.monotonic()+5)
        self.assertEqual(client.usage['zai-org/GLM-4.7-Flash']['completion_tokens'],1200)
        self.assertEqual(client.events[0]['status'],'failed')

    def test_transport_budget_prevents_request(self):
        client=self.fake_client()
        client.config.case_dollars=0.00000001
        with self.assertRaises(RuntimeError):client.ask([{'role':'user','content':'test'}],'zai-org/GLM-4.7-Flash',time.monotonic()+5)
        client.client.chat.completions.create.assert_not_called()

    def test_repeated_component_allowed_for_two_faults(self):
        r=self.tools.call('metric_anomalies',self.window|{'sources':['metric_node']})
        case=Case.parse({'row_id':1,'task_index':'task_7','instruction':'March 20, 2022, from 01:00 to 01:30, two failures occurred.'})
        a={'datetime':'2022-03-20 01:05:00','component':'node-1','reason':'node CPU load','confidence':'low','evidence_ids':[r['evidence_id']],'rationale':'Synthetic test','alternatives':[]}
        result=verify_answers([a,a|{'datetime':'2022-03-20 01:15:00'}],case,['node-1'],['node CPU load'],self.tools.ledger)
        self.assertEqual(len(result),2)

    def test_controller_repairs_format_in_shared_final_round(self):
        from mini_rca.protocol import ProtocolError
        self.config.mode='single';c=Controller(self.store,self.config)
        def fake(messages,model,deadline):
            body=json.loads(messages[1]['content'])
            if body['max_additional_tools']:
                raise ProtocolError('Invalid JSON','synthetic malformed output')
            ev=next(r for r in body['evidence'] if r['tool']=='metric_anomalies')
            return {'action':'answer','answers':[{'datetime':'2022-03-20 01:05:00','component':'node-1','reason':'node CPU load','confidence':'low','evidence_ids':[ev['evidence_id']],'rationale':'Synthetic observation','alternatives':'Uncertain memory hypothesis'}]}
        with patch.object(c.llm,'ask',side_effect=fake) as mock:
            result=c.solve(self.case)
        self.assertEqual(mock.call_count,2)
        self.assertEqual(result['mode'],'single-model')
        self.assertEqual(result['decision_events'][0]['failure_kind'],'output_contract')
        self.assertTrue(result['decision_events'][1]['normalizations'])
        self.assertEqual(mock.call_args_list[0].args[1],mock.call_args_list[1].args[1])

    def test_controller_never_adds_third_round_for_bad_repair(self):
        from mini_rca.protocol import ProtocolError
        self.config.mode='single';c=Controller(self.store,self.config)
        with patch.object(c.llm,'ask',side_effect=ProtocolError('bad JSON','{')) as mock:
            result=c.solve(self.case)
        self.assertEqual(mock.call_count,2)
        self.assertEqual(result['mode'],'offline-fallback')

    def test_strong_review_failure_keeps_verified_draft(self):
        self.config.mode='routed';c=Controller(self.store,self.config)
        def fake(messages,model,deadline):
            body=json.loads(messages[1]['content'])
            if not body['max_additional_tools']: raise TimeoutError('synthetic timeout')
            ev=next(r for r in body['evidence'] if r['tool']=='metric_anomalies')
            return {'action':'answer','answers':[{'datetime':'2022-03-20 01:05:00','component':'node-1','reason':'node CPU load','confidence':'low','evidence_ids':[ev['evidence_id']],'rationale':'Synthetic observation','alternatives':[]}]}
        with patch.object(c.llm,'ask',side_effect=fake) as mock:
            result=c.solve(self.case)
        self.assertEqual(mock.call_count,2)
        self.assertEqual(result['mode'],'routed-model')
        self.assertEqual(result['route_events'][-1]['reasons'],['low_confidence_draft'])
        self.assertTrue(result['decision_events'][0]['adopted'])

    def test_query_timeout_has_phase_and_does_not_poison_cache(self):
        with self.assertRaises(TimeoutError):
            self.store.execute('SELECT sum(i*j) FROM range(1000000) a(i), range(1000000) b(j)',seconds=0.01,phase='synthetic_trace_scan')
        event=self.store.query_events[-1]
        self.assertEqual(event['status'],'timeout')
        self.assertEqual(event['phase'],'synthetic_trace_scan')
        r=self.tools.call('trace_summary',self.window)
        self.assertTrue(r['ok'])
        self.assertEqual(r['queries'][0]['phase'],'materialize_trace_span')

    def test_service_alias_is_not_an_answer_candidate(self):
        r=self.tools.call('list_entities',self.window|{'source':'metric_container'})
        self.assertIn('cartservice-0',r['data']['candidate_names'])
        self.assertNotIn('cartservice',r['data']['candidate_names'])
        self.assertEqual(r['data']['component_scopes']['cartservice-0'],'container')
        self.assertEqual(r['data']['component_scopes']['node-1'],'node')

    def test_reason_scope_must_match_entity_type(self):
        r=self.tools.call('metric_anomalies',self.window|{'sources':['metric_node']})
        a={'datetime':'2022-03-20 01:05:00','component':'node-1','reason':'container CPU load','confidence':'low','evidence_ids':[r['evidence_id']],'rationale':'Test','alternatives':[]}
        with self.assertRaisesRegex(ValueError,'scope'):
            verify_answers([a],self.case,['node-1'],['container CPU load'],self.tools.ledger,component_scopes={'node-1':'node'})

    def test_metric_discovery_failure_recovers_from_trace(self):
        c=Controller(self.store,self.config);real=c.tools.call
        def call(name,args=None):
            if name=='list_entities' and args['source'].startswith('metric_'):
                return {'ok':False,'error':'synthetic metric outage'}
            return real(name,args)
        with patch.object(c.tools,'call',side_effect=call):result=c.solve(self.case)
        self.assertTrue(result['prediction'])
        self.assertTrue(any('recovered names from trace_span' in s for s in result['warnings']))

    def test_adapter_field_inference_matches_real_queries(self):
        from mini_rca.adapter import infer_task
        path=Path(__file__).resolve().parents[2]/'Track1/data/Market-cloudbed-1/query.csv'
        if not path.exists():self.skipTest('Local data unavailable')
        for c in load_queries(path):self.assertEqual(infer_task(c.instruction),c.task)

    def test_default_cli_runs_model_path_with_synthetic_transport(self):
        from mini_rca.cli import main
        from mini_rca.llm import GLMClient
        query=self.out/'query.csv'
        with query.open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=['row_id','task_index','instruction']);w.writeheader()
            w.writerow({'row_id':17,'task_index':'task_7','instruction':self.case.instruction})
        def fake(messages,model,deadline):
            body=json.loads(messages[1]['content']);ev=next(r for r in body['evidence'] if r['tool']=='metric_anomalies')
            return {'action':'answer','answers':[{'datetime':'2022-03-20 01:05:00','component':'node-1','reason':'node CPU load','confidence':'medium','evidence_ids':[ev['evidence_id']],'rationale':'Synthetic local test','alternatives':[]}]}
        target=self.out/'default-cli'
        with patch.object(GLMClient,'ask',side_effect=fake) as mock:
            main(['--dataset',str(self.data),'--queries',str(query),'--out',str(target)])
        self.assertTrue(mock.called)
        manifest=json.loads((target/'run.json').read_text())
        self.assertEqual(manifest['config']['mode'],'routed')
        with (target/'predictions.csv').open(encoding='utf-8') as f:rows=list(csv.DictReader(f))
        self.assertEqual(rows[0]['mode'],'routed-model')
        self.assertEqual(rows[0]['row_id'],'17')

    def test_bad_query_row_and_evidence_write_do_not_stop_next_case(self):
        from mini_rca.cli import main,atomic_text
        query=self.out/'mixed.csv'
        with query.open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=['row_id','task_index','instruction']);w.writeheader()
            for rid in [3,5,7]:w.writerow({'row_id':rid,'task_index':'task_7','instruction':'malformed' if rid==5 else self.case.instruction})
        target=self.out/'isolated'
        def fail_one(path,text):
            if path.parent.name=='evidence' and path.stem=='3':raise OSError('synthetic write failure')
            return atomic_text(path,text)
        with patch('mini_rca.cli.atomic_text',side_effect=fail_one):
            main(['--dataset',str(self.data),'--queries',str(query),'--out',str(target),'--mode','offline'])
        with (target/'predictions.csv').open() as f:rows=list(csv.DictReader(f))
        self.assertEqual([r['row_id'] for r in rows],['3','5','7'])
        self.assertTrue(rows[-1]['prediction']);self.assertFalse(rows[1]['prediction'])
        self.assertEqual(json.loads((target/'summary.json').read_text())['io_errors'][0]['row_id'],3)

    def test_resume_keeps_original_deadline_and_pending_cost(self):
        from mini_rca.cli import main
        query=self.out/'resume.csv'
        with query.open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=['row_id','task_index','instruction']);w.writeheader()
            for rid in [11,13]:w.writerow({'row_id':rid,'task_index':'task_7','instruction':self.case.instruction})
        target=self.out/'resume-run';args=['--dataset',str(self.data),'--queries',str(query),'--out',str(target),'--mode','offline']
        main(args+['--limit','1'])
        manifest=json.loads((target/'run.json').read_text());manifest['started_at_unix']-=1300
        (target/'run.json').write_text(json.dumps(manifest),encoding='utf-8')
        folder=target/'model_responses/13';folder.mkdir(parents=True)
        (folder/'01.json').write_text(json.dumps({'event':{'reserved_dollars':0.01,'status':'pending'}}),encoding='utf-8')
        main(args+['--resume'])
        summary=json.loads((target/'summary.json').read_text())
        self.assertEqual(summary['completed'],1)
        self.assertEqual(summary['run_cost_dollars'],0.01)
        self.assertGreater(summary['cumulative_wall_s'],1300)

    def test_starter_adapter_returns_solution_and_reuses_controller(self):
        from mini_rca.adapter import solve
        ctx={'out_dir':self.out/'adapter','config':self.config,'row_id':19,'task_index':'task_7'}
        try:
            a=solve(self.case.instruction,self.data,ctx);controller=ctx['_mini_rca_controller']
            ctx['row_id']=23;b=solve(self.case.instruction,self.data,ctx)
            self.assertIs(controller,ctx['_mini_rca_controller'])
            self.assertTrue(a.prediction);self.assertTrue(b.evidence)
            self.assertEqual(b.usage,{})
        finally:
            if '_mini_rca_controller' in ctx:ctx['_mini_rca_controller'].store.close()

if __name__=='__main__': unittest.main()
