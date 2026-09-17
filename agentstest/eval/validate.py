"""Local output-contract validation; offline unless --live is explicitly selected."""
import argparse
import csv
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mini_rca.cli import load_queries
from mini_rca.case import FIELDS


def validate_output(out,cases):
    with (out/'predictions.csv').open(encoding='utf-8',newline='') as f: rows=list(csv.DictReader(f))
    if len(rows)!=len(cases) or {int(r['row_id']) for r in rows}!={c.row_id for c in cases}:
        raise ValueError('Missing, duplicate or unexpected prediction rows')
    by_id={c.row_id:c for c in cases}
    keys={'datetime':'root cause occurrence datetime','component':'root cause component','reason':'root cause reason'}
    for row in rows:
        case=by_id[int(row['row_id'])];answer=json.loads(row['prediction'])
        if list(answer)!=[str(i) for i in range(1,case.failures+1)]:raise ValueError('Wrong failure count/order')
        expected=[keys[k] for k in keys if k in case.fields]
        for a in answer.values():
            if list(a)!=expected or any(not isinstance(v,str) or not v or '\n' in v or '\r' in v for v in a.values()):
                raise ValueError('Invalid answer fields/order')
        evidence=(out/'evidence'/f'{case.row_id}.md').read_text(encoding='utf-8')
        if not all('# '+title in evidence for title in ('Answer','Confidence','Evidence','Ruled out')):
            raise ValueError('Evidence sections missing')
    return {'cases':len(cases),'shape_valid':True,'scope':'Output contract, not diagnosis accuracy or resource certification'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,required=True);p.add_argument('--queries',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--cases',type=int,default=2)
    p.add_argument('--live',action='store_true',help='Authorizes configured model calls; otherwise explicit offline validation')
    args=p.parse_args();root=Path(__file__).resolve().parents[1]
    mode='routed' if args.live else 'offline'
    subprocess.run([sys.executable,'-B',str(root/'run.py'),'--dataset',str(args.dataset.resolve()),'--queries',str(args.queries.resolve()),
                    '--out',str(args.out.resolve()),'--agent','mini_rca.adapter','--mode',mode,'--limit',str(args.cases)],check=True)
    result=validate_output(args.out,load_queries(args.queries)[:args.cases])|{'mode':mode}
    (args.out/'validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':main()
