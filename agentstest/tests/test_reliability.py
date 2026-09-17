import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mini_rca.config import Config
from mini_rca.llm import GLMClient
from mini_rca.protocol import ProtocolError, parse_json
from mini_rca.routing import initial_route
from mini_rca.verifier import verify_answers
from mini_rca.case import Case

MODEL = 'zai-org/GLM-4.7-Flash'


def response(content='{"action":"answer"}', finish='stop'):
    return SimpleNamespace(id='fake-request', usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
                           choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))])


def client(replies, audit_dir=None):
    llm = GLMClient(Config(mode='single',service_attempts=1), audit_dir)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=replies))))
    return llm


def ask(llm):
    return llm.ask([{'role': 'user', 'content': 'synthetic telemetry'}], MODEL, time.monotonic()+10)


class ReliabilityTests(unittest.TestCase):
    def test_empty_content_uses_standalone_provider_json_and_audits_channel(self):
        for field in ('reasoning', 'reasoning_content'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                reply=response('');setattr(reply.choices[0].message,field,'{"action":"answer"}')
                llm=client([reply],Path(tmp));llm.begin_case(7)
                self.assertEqual(ask(llm)['action'],'answer')
                self.assertEqual(llm.events[-1]['response_channel'],field)
                record=json.loads((Path(tmp)/'7/01.json').read_text())
                self.assertEqual(record['response']['choices'][0][field],'{"action":"answer"}')

    def test_content_wins_over_reasoning_and_malformed_content_is_not_replaced(self):
        for text in ('{"action":"answer"}', 'invalid'):
            reply=response(text);reply.choices[0].message.reasoning='{"action":"retrieve"}'
            llm=client([reply])
            if text=='invalid':
                with self.assertRaises(ProtocolError):ask(llm)
            else:self.assertEqual(ask(llm)['action'],'answer')
            self.assertEqual(llm.events[-1]['response_channel'],'content')

    def test_reasoning_prose_conflicts_and_truncation_are_not_answers(self):
        for text,other,finish in [('I am considering {"action":"answer"}',None,'stop'),
                                  ('{"action":"answer"}','{"action":"retrieve"}','stop'),
                                  ('{"action":"answer"}',None,'length'),
                                  ('{"action":"answer"}{"action":"retrieve"}',None,'stop')]:
            reply=response('',finish);reply.choices[0].message.reasoning=text
            reply.choices[0].message.reasoning_content=other
            llm=client([reply])
            with self.subTest(text=text,finish=finish),self.assertRaises(ProtocolError):ask(llm)
            self.assertEqual(llm.events[-1]['failure_kind'],'output_format')

    def test_service_retry_grows_wait_then_falls_back(self):
        error=SimpleNamespace(error={'code':'completion_error'},usage=None,choices=[])
        llm=client([error,error,error,response()]);llm.config.mode='routed';llm.config.service_attempts=3
        with patch('mini_rca.llm.time.sleep') as sleep:
            ask(llm)
        self.assertEqual([x.args[0] for x in sleep.call_args_list],[0.5,1.0])
        models=[c.kwargs['model'] for c in llm.client.chat.completions.create.call_args_list]
        self.assertEqual(models,[MODEL,MODEL,MODEL,'zai-org/GLM-5.3-Flash'])
        self.assertIn(MODEL,llm.open_until)
        self.assertTrue(all(e['cost_dollars']==0 for e in llm.events if e.get('failure_kind')=='service'))

    def test_explicit_error_wins_over_choices(self):
        bad=response();bad.error={'code':'completion_error'}
        llm=client([bad])
        with self.assertRaises(RuntimeError):ask(llm)
        self.assertEqual(llm.events[-1]['failure_kind'],'service')

    def test_retry_stops_when_wait_would_exceed_budget(self):
        llm=client([TimeoutError()]);llm.config.service_attempts=3
        with patch('mini_rca.llm.time.sleep') as sleep, self.assertRaises(RuntimeError):
            llm.ask([{'role':'user','content':'test'}],MODEL,time.monotonic()+2.1)
        sleep.assert_not_called()
        self.assertEqual(llm.client.chat.completions.create.call_count,1)

    def test_audit_attempts_survive_new_client_and_restore_cost(self):
        from mini_rca.cli import spent_dollars
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);first=client([response()],out/'model_responses');first.begin_case(10);ask(first)
            second=client([response()],out/'model_responses');second.begin_case(10);ask(second)
            self.assertTrue((out/'model_responses/10/01.json').exists())
            self.assertTrue((out/'model_responses/10/02.json').exists())
            self.assertAlmostEqual(spent_dollars(out),second.case_cost)
            self.assertGreater(second.case_cost,first.case_cost)

    def test_resumed_case_cannot_reset_its_cost_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);folder=out/'model_responses/10';folder.mkdir(parents=True)
            (folder/'01.json').write_text(json.dumps({'attempt':1,'event':{'status':'pending','reserved_dollars':0.19999}}))
            llm=client([response()],out/'model_responses');llm.begin_case(10)
            with self.assertRaisesRegex(RuntimeError,'cost budget'):ask(llm)
            llm.client.chat.completions.create.assert_not_called()
            self.assertAlmostEqual(llm.case_cost,0.19999)

    def test_competition_caps_cannot_be_disabled(self):
        for kwargs in ({'case_dollars':4},{'run_dollars':26},{'case_seconds':601},{'run_seconds':1201},{'run_seconds':float('nan')}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):Config(**kwargs)

    def test_wrapped_json_and_escaped_braces(self):
        self.assertEqual(parse_json('Answer: ```json\n{"text":"a } and \\\"b\\\""}\n```')['text'], 'a } and "b"')
        self.assertEqual(parse_json('<think>discard {}</think>{"a":1}'), {'a': 1})

    def test_ambiguous_or_incomplete_json_not_salvaged(self):
        for text in ['{"a":1}{"b":2}', '{"a":1,"a":2}', '{"n":NaN}', '{"action":', '[{"action":"answer"}]', '<think>{"action":"answer"}']:
            with self.subTest(text=text), self.assertRaises(ProtocolError): parse_json(text)

    def test_format_failures_do_not_skip_later_cases(self):
        llm=client([response('invalid'), response('{broken'), response()])
        for row in range(2):
            llm.begin_case(row)
            with self.assertRaises(ProtocolError): ask(llm)
            self.assertEqual(llm.events[-1]['failure_kind'], 'output_format')
            self.assertEqual(llm.down[MODEL], 0)
        llm.begin_case(2)
        self.assertEqual(ask(llm)['action'], 'answer')
        self.assertEqual(llm.client.chat.completions.create.call_count, 3)

    def test_service_failures_cool_down_and_probe(self):
        llm=client([TimeoutError(), TimeoutError(), response()])
        for row in range(2):
            llm.begin_case(row)
            with self.assertRaises(RuntimeError): ask(llm)
        with self.assertRaises(RuntimeError): ask(llm)
        self.assertEqual(llm.client.chat.completions.create.call_count, 2)
        self.assertEqual(llm.events[-1]['failure_kind'], 'circuit_open')
        llm.open_until[MODEL]=time.monotonic()-1
        ask(llm)
        self.assertEqual(llm.down[MODEL], 0)
        self.assertNotIn(MODEL, llm.open_until)

    def test_success_breaks_consecutive_service_failure_streak(self):
        llm=client([TimeoutError(), response(), TimeoutError()])
        with self.assertRaises(RuntimeError): ask(llm)
        ask(llm)
        with self.assertRaises(RuntimeError): ask(llm)
        self.assertEqual(llm.down[MODEL], 1)
        self.assertNotIn(MODEL, llm.open_until)

    def test_raw_response_saved_before_parse_and_key_redacted(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'FEATHERLESS_API_KEY': 'synthetic-secret'}):
            llm=client([response('invalid synthetic-secret')], Path(tmp))
            llm.begin_case(123)
            with self.assertRaises(ProtocolError): ask(llm)
            text=(Path(tmp)/'123/01.json').read_text(encoding='utf-8')
            self.assertNotIn('synthetic-secret', text)
            record=json.loads(text)
            self.assertEqual(record['response']['choices'][0]['content'], 'invalid [REDACTED_API_KEY]')
            self.assertEqual(record['event']['failure_kind'], 'output_format')
            self.assertEqual(record['event']['completion_tokens'], 20)
            self.assertTrue(record['messages'])
            self.assertGreater(llm.total_dollars, 0)

    def test_truncated_reply_is_logged_and_charged_without_circuit(self):
        llm=client([response('{"action":"answer"}', 'length')])
        with self.assertRaises(ProtocolError): ask(llm)
        self.assertEqual(llm.records[0]['response']['choices'][0]['finish_reason'], 'length')
        self.assertEqual(llm.down[MODEL], 0)
        self.assertGreater(llm.case_cost, 0)

    def test_bad_request_does_not_trip_service_breaker_or_retry(self):
        llm=client([ValueError('synthetic local bug')])
        with self.assertRaises(ValueError): ask(llm)
        self.assertFalse(llm.down)
        self.assertEqual(llm.events[-1]['failure_kind'], 'request_or_local')

    def test_routed_format_error_does_not_call_service_fallback(self):
        llm=client([response('not JSON'), response()])
        llm.config.mode='routed'
        with self.assertRaises(ProtocolError): ask(llm)
        self.assertEqual(llm.client.chat.completions.create.call_count,1)
        self.assertEqual(llm.down[MODEL],0)

    def test_repair_request_still_obeys_remaining_cost_budget(self):
        llm=client([response('not JSON'), response()])
        with self.assertRaises(ProtocolError): ask(llm)
        llm.config.case_dollars=llm.case_cost+llm.events[0]['reserved_dollars']-1e-9
        with self.assertRaisesRegex(RuntimeError,'cost budget'): ask(llm)
        self.assertEqual(llm.client.chat.completions.create.call_count,1)

    def test_optional_metadata_normalizes_without_changing_scoring_fields(self):
        case=Case.parse({'row_id':0,'task_index':'task_7','instruction':'March 20, 2022, from 01:00 to 01:30, one failure occurred.'})
        a={'datetime':'2022-03-20 01:05:00','component':'node-1','reason':'node CPU load','confidence':'low','evidence_ids':['ev_1'],'rationale':'Synthetic observation'}
        ledger=[{'evidence_id':'ev_1','ok':True,'tool':'metric_anomalies'}]
        for metadata in (None, 'memory is another hypothesis', {'component':'node-2','reason':'unresolved'}, [{'hypothesis':'network'}], 7):
            notices=[]
            result=verify_answers([a|{'alternatives':metadata}],case,['node-1'],['node CPU load'],ledger,notices)[0]
            self.assertTrue(notices)
            self.assertTrue(all(isinstance(s,str) for s in result['alternatives']))
            self.assertEqual({k:result[k] for k in a}, a)
        with self.assertRaises(ValueError):
            verify_answers([a|{'component':'invented','alternatives':'valid metadata'}],case,['node-1'],['node CPU load'],ledger)
        with self.assertRaises(ValueError):
            verify_answers([a|{'confidence':[]}],case,['node-1'],['node CPU load'],ledger)

    def test_many_candidates_alone_do_not_force_strong_model(self):
        config=Config(mode='routed')
        case=SimpleNamespace(failures=1)
        ledger=[{'tool':t,'ok':True,'data':{}} for t in ('metric_anomalies','trace_summary','log_search')]
        candidates=[{'component':f'pod-{i}'} for i in range(12)]
        self.assertEqual(initial_route(case,candidates,ledger,config)['model'], config.fast_model)
        case.failures=2
        self.assertEqual(initial_route(case,candidates,ledger,config)['model'], config.strong_model)
        case.failures=1
        ledger[1]['ok']=False
        self.assertIn('incomplete_telemetry', initial_route(case,candidates,ledger,config)['reasons'])


if __name__=='__main__': unittest.main()
