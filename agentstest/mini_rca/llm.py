"""Budgeted GLM transport, durable response audit, and bounded service retries."""
import json
import os
from pathlib import Path
import time
from .config import PRICES
from .protocol import ProtocolError, parse_json
from .transport import HardTimeoutError, TransportError, request_completion


class ServiceResponseError(RuntimeError):
    pass


def response_text(message):
    """Prefer content; accept only a standalone JSON object in provider side channels."""
    content = getattr(message, 'content', None)
    if isinstance(content, str) and content.strip():
        return content, 'content'
    if content is not None and not isinstance(content, str):
        raise ProtocolError('Response content must be text')
    candidates = [(field, getattr(message, field, None)) for field in ('reasoning_content', 'reasoning')]
    candidates = [(field, value.strip()) for field, value in candidates if isinstance(value, str) and value.strip()]
    if not candidates:
        raise ProtocolError('No response text in content or provider side channels')
    if len({value for _, value in candidates}) != 1:
        raise ProtocolError('Conflicting provider side-channel outputs')
    field, value = candidates[0]
    if not (value.startswith('{') and value.endswith('}')):
        raise ProtocolError('Provider side channel is not a standalone JSON object')
    # parse_json and the Controller still enforce JSON, actions and evidence contracts.
    return value, field


class GLMClient:
    def __init__(self, config, audit_dir=None):
        self.config = config
        self.client = None
        self.down = {}
        self.open_until = {}
        self.total_dollars = 0.0
        self.audit_dir = Path(audit_dir) if audit_dir else None
        self.begin_case()

    def begin_case(self, row_id=None):
        self.row_id = row_id
        self.usage = {}
        self.events = []
        self.records = []
        self.case_cost = 0.0
        folder = self.audit_dir / str(row_id) if self.audit_dir else None
        self.attempt_number = max((int(p.stem) for p in folder.glob('*.json') if p.stem.isdigit()), default=0) if folder else 0
        # An externally interrupted case must retain its own cap on resume too.
        if folder and row_id is not None:
            for path in sorted(folder.glob('*.json')):
                record=json.loads(path.read_text(encoding='utf-8'))
                event=record.get('event',{})
                self.records.append(record);self.events.append(event)
                self.case_cost+=event.get('cost_dollars',event.get('reserved_dollars',0))
                if 'prompt_tokens' in event:
                    counts=self.usage.setdefault(event['model'],{'prompt_tokens':0,'completion_tokens':0,'calls':0})
                    counts['prompt_tokens']+=event.get('prompt_tokens',0)
                    counts['completion_tokens']+=event.get('completion_tokens',0)
                    counts['calls']+=1

    def _redact(self, value):
        if isinstance(value, str):
            key = os.environ.get('FEATHERLESS_API_KEY')
            return value.replace(key, '[REDACTED_API_KEY]') if key else value
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value

    def _save(self, record):
        if not self.audit_dir:
            return
        folder = self.audit_dir / str(self.row_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{record['attempt']:02d}.json"
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self._redact(record), ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, path)

    @staticmethod
    def _service_fault(exc):
        from openai import APIConnectionError, APIStatusError
        if isinstance(exc, TransportError):
            return exc.service_fault
        if isinstance(exc, (ServiceResponseError, APIConnectionError, TimeoutError)):
            return True
        return isinstance(exc, APIStatusError) and (exc.status_code in (408, 429) or exc.status_code >= 500)

    def ask(self, messages, model, deadline):
        if self.client is None:
            key = os.environ.get('FEATHERLESS_API_KEY')
            if not key:
                raise RuntimeError('FEATHERLESS_API_KEY is not configured')
        fallback = 'zai-org/GLM-5.3-Flash' if 'Flash' in model else 'zai-org/GLM-4.6'
        models = [model] if self.config.mode == 'single' else list(dict.fromkeys([model, fallback]))
        for chosen in models:
            if time.monotonic() < self.open_until.get(chosen, 0):
                self.events.append({'model': chosen, 'status': 'skipped', 'failure_kind': 'circuit_open', 'seconds': 0})
                continue
            for retry in range(self.config.service_attempts):
                if retry:
                    delay = self.config.retry_backoff_seconds * 2 ** (retry-1)
                    if deadline-time.monotonic() < delay+2:
                        self.events.append({'model':chosen,'status':'skipped','failure_kind':'retry_deadline','seconds':0})
                        break
                    self.events.append({'model':chosen,'status':'backoff','retry':retry,'seconds':delay})
                    time.sleep(delay)
                try:
                    return self._attempt(messages, chosen, deadline, retry)
                except Exception as exc:
                    if not self._service_fault(exc):
                        raise
                    self.down[chosen] = self.down.get(chosen, 0) + 1
            if self.down.get(chosen,0) >= 2:
                self.open_until[chosen] = time.monotonic() + self.config.circuit_cooldown_seconds
        raise RuntimeError('No available service returned a response within budget')

    def _attempt(self, messages, chosen, deadline, retry):
        remaining = deadline-time.monotonic()
        if remaining < 2:
            raise RuntimeError('Model deadline reached')
        prompt_cap = len(json.dumps(messages,ensure_ascii=False).encode('utf-8'))+2048
        rates = PRICES[chosen]
        reserve = (prompt_cap*rates[0]+self.config.max_output_tokens*rates[1])/1e6
        if self.case_cost+reserve > self.config.case_dollars or self.total_dollars+reserve > self.config.run_dollars:
            raise RuntimeError('Model cost budget exhausted')
        self.case_cost += reserve
        self.total_dollars += reserve
        self.attempt_number += 1
        started = time.monotonic()
        event = {'model':chosen,'attempt':self.attempt_number,'retry':retry,'reserved_dollars':reserve,'status':'pending'}
        record = {'attempt':self.attempt_number,'row_id':self.row_id,'model':chosen,
                  'messages':messages,'max_output_tokens':self.config.max_output_tokens,
                  'thinking':self.config.thinking,'response':None,'event':dict(event)}
        if self.audit_dir:
            event['response_file'] = f'model_responses/{self.row_id}/{self.attempt_number:02d}.json'
        self.records.append(record)
        self._save(record)  # Pending requests retain their conservative reservation after a crash.
        try:
            kwargs = dict(
                model=chosen,messages=messages,temperature=0,max_tokens=self.config.max_output_tokens,
                timeout=min(remaining,35),extra_body={'chat_template_kwargs':{'enable_thinking':self.config.thinking}})
            if self.client is not None:
                # Explicit injection remains available for deterministic offline tests.
                response = self.client.chat.completions.create(**kwargs)
            else:
                def started_worker(pid):
                    event['worker_pid'] = pid
                    event['transport'] = 'subprocess'
                    record['event'] = dict(event)
                    self._save(record)
                response = request_completion(kwargs, min(deadline-0.5, started+35), on_start=started_worker)
            provider_error = getattr(response,'error',None)
            # Inspect error BEFORE indexing choices. An error wins even if both are present.
            choices = [] if provider_error else (getattr(response,'choices',None) or [])
            record['response'] = {'id':getattr(response,'id',None),
                'provider_error':provider_error,
                'choices':[{'finish_reason':c.finish_reason,'content':c.message.content,
                            'reasoning_content':getattr(c.message,'reasoning_content',None),
                            'reasoning':getattr(c.message,'reasoning',None),
                            'refusal':getattr(c.message,'refusal',None)} for c in choices]}
            if callable(getattr(response, 'model_dump', None)):
                record['raw_provider_response'] = response.model_dump(mode='json')
            self._save(record)
            usage = getattr(response,'usage',None)
            known = usage is not None and usage.prompt_tokens is not None and usage.completion_tokens is not None
            if known:
                inputs,outputs = usage.prompt_tokens,usage.completion_tokens
            elif provider_error:
                inputs,outputs = 0,0  # Explicit capacity rejection without generation usage.
            else:
                inputs,outputs = prompt_cap,self.config.max_output_tokens
            cost = (inputs*rates[0]+outputs*rates[1])/1e6
            self.case_cost += cost-reserve
            self.total_dollars += cost-reserve
            counts = self.usage.setdefault(chosen,{'prompt_tokens':0,'completion_tokens':0,'calls':0})
            counts['prompt_tokens'] += inputs; counts['completion_tokens'] += outputs; counts['calls'] += 1
            event.update(usage_estimated=not known and not bool(provider_error),cost_dollars=cost,
                         prompt_tokens=inputs,completion_tokens=outputs)
            if provider_error or not choices:
                raise ServiceResponseError('Provider error body' if provider_error else 'Response without choices')
            self.down[chosen] = 0
            self.open_until.pop(chosen,None)
            choice = choices[0]
            event['finish_reason'] = choice.finish_reason
            if choice.finish_reason == 'length':
                raise ProtocolError('Output truncated; provide a shorter complete JSON answer',choice.message.content or '')
            output_text, channel = response_text(choice.message)
            event['response_channel'] = channel
            result = parse_json(output_text)
            event['status'] = 'ok'
            return result
        except ProtocolError as exc:
            event.update(status='failed',failure_kind='output_format',error_type=type(exc).__name__,error=str(exc))
            raise
        except Exception as exc:
            from openai import APIStatusError
            if (isinstance(exc,APIStatusError) or isinstance(exc,TransportError) and exc.status_code is not None) and 'cost_dollars' not in event:
                # Explicit HTTP rejection; unknown connection outcomes retain their reservation.
                self.case_cost -= reserve; self.total_dollars -= reserve
                event.update(cost_dollars=0,usage_estimated=False)
            event.update(status='failed',failure_kind='service' if self._service_fault(exc) else 'request_or_local',error_type=type(exc).__name__)
            if isinstance(exc, HardTimeoutError):
                event.update(hard_timeout=True,usage_estimated=True,cost_dollars=reserve)
            if isinstance(exc, TransportError):
                event['provider_error_type'] = exc.error_type
            raise
        finally:
            event['seconds'] = round(time.monotonic()-started,3)
            self.events.append(event)
            record['event'] = dict(event)
            self._save(record)
