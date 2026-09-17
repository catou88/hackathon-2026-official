"""Optional starter module: solve(instruction, dataset_dir, ctx) -> Solution."""
import atexit
import re
from dataclasses import dataclass, field
from pathlib import Path
from .agents import Controller
from .case import Case, FIELDS
from .config import Config
from .store import TelemetryStore


@dataclass
class Solution:
    prediction: str
    evidence: str = ''
    usage: dict = field(default_factory=dict)


def infer_task(instruction):
    text=instruction.lower().strip().rstrip('.').rsplit('.',1)[-1]
    fields=tuple(k for k,pattern in [('datetime',r'\b(?:datetime|times?)\b'),('component',r'\bcomponents?\b'),('reason',r'\breasons?\b')] if re.search(pattern,text))
    matches=[task for task,items in FIELDS.items() if set(items)==set(fields)]
    if len(matches)!=1:raise ValueError('Cannot infer requested fields; provide ctx[task_index]')
    return matches[0]


def solve(instruction,dataset_dir,ctx):
    dataset=Path(dataset_dir).resolve();out=Path(ctx['out_dir']).resolve()
    if '_mini_rca_controller' not in ctx:
        from .cli import load_query_rows
        config=ctx.get('config') or Config()
        store=TelemetryStore(dataset,out,config)
        atexit.register(store.close)
        ctx['_mini_rca_controller']=Controller(store,config)
        ctx['_mini_rca_paths']=(dataset,out)
        ctx['_mini_rca_queries']=load_query_rows(dataset/'query.csv') if (dataset/'query.csv').exists() else []
    if ctx['_mini_rca_paths']!=(dataset,out):raise ValueError('A shared ctx must retain dataset and output paths')
    row_id=ctx.get('row_id')
    if row_id is None:
        matches=[r for r in ctx['_mini_rca_queries'] if r['instruction']==instruction]
        if len(matches)!=1:raise ValueError('Provide ctx[row_id] for a case not uniquely found in query.csv')
        row_id=int(matches[0]['row_id'])
    case=Case.parse({'row_id':row_id,'task_index':ctx.get('task_index') or infer_task(instruction),'instruction':instruction})
    result=ctx['_mini_rca_controller'].solve(case)
    return Solution(result['prediction'],result['evidence'],result['models'])
