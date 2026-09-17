"""Local output contract and conservative, observable format normalization."""
import json
import re


class ProtocolError(RuntimeError):
    def __init__(self, message, raw_text=''):
        super().__init__(message)
        self.raw_text = raw_text


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('Non-finite JSON value')


def parse_json(text):
    """Accept one complete object, optionally wrapped in prose/fences; never repair values."""
    if not isinstance(text, str):
        raise ProtocolError('Response content must be text')
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.S).strip()
    if '<think>' in cleaned or cleaned.lstrip().startswith('['):
        raise ProtocolError('Unfinished reasoning or top-level array', text)
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    start = cleaned.find('{')
    if start < 0:
        raise ProtocolError('No JSON object in response', text)
    try:
        value, end = decoder.raw_decode(cleaned, start)
        if not isinstance(value, dict) or any(c in cleaned[end:] for c in '{}[]'):
            raise ValueError('Expected one JSON object only')
    except ValueError as exc:
        raise ProtocolError('Invalid or ambiguous JSON: ' + type(exc).__name__, text) from exc
    return value


def normalize_alternatives(value, notices):
    """Optional explanation metadata must not invalidate otherwise grounded scoring fields."""
    if value is None:
        notices.append('alternatives: null/missing normalized to []')
        return []
    if isinstance(value, str):
        notices.append('alternatives: string wrapped in list')
        return [value] if value.strip() else []
    if isinstance(value, dict):
        notices.append('alternatives: object preserved as JSON text')
        return [json.dumps(value, ensure_ascii=False, sort_keys=True)]
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                notices.append('alternatives: list object preserved as JSON text')
                result.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                notices.append('alternatives: unsupported optional item omitted; see raw response')
        return result
    notices.append('alternatives: unsupported optional value omitted; see raw response')
    return []


ANSWER_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['datetime', 'component', 'reason', 'confidence', 'evidence_ids', 'rationale', 'alternatives'],
    'properties': {
        **{k: {'type': 'string', 'minLength': 1} for k in ('datetime', 'component', 'reason', 'rationale')},
        'confidence': {'enum': ['low', 'medium', 'high']},
        'evidence_ids': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}},
        'alternatives': {'type': 'array', 'items': {'type': 'string'}},
    },
}
