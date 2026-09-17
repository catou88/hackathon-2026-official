"""Offline evaluation only. This is the sole entry point permitted to read dev labels."""
import argparse
import csv
import json
from pathlib import Path
from eval.official_score import evaluate

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--predictions',required=True,type=Path)
    p.add_argument('--labels',required=True,type=Path)
    p.add_argument('--out',required=True,type=Path)
    p.add_argument('--row-ids',help='Explicit subset; omitted means the complete label file')
    args=p.parse_args()
    with args.labels.open(encoding='utf-8-sig',newline='') as f: labels=list(csv.DictReader(f))
    if args.row_ids:
        ids=set(map(int,args.row_ids.split(','))); labels=[r for r in labels if int(r['row_id']) in ids]
        if {int(r['row_id']) for r in labels}!=ids: raise ValueError('Unknown evaluation IDs')
    with args.predictions.open(encoding='utf-8-sig',newline='') as f: predictions=list(csv.DictReader(f))
    if len({r['row_id'] for r in predictions})!=len(predictions): raise ValueError('Duplicate predictions')
    lookup={r['row_id']:r for r in predictions}
    details=[]
    for row in labels:
        pred=lookup.get(row['row_id'],{})
        _,_,score=evaluate(pred.get('prediction',''),row['scoring_points'])
        details.append({'row_id':int(row['row_id']),'task':row['task_index'],'present':bool(pred),'strict':score==1,'partial':score,'mode':pred.get('mode','missing')})
    n=len(details)
    result={'denominator':n,'coverage':sum(r['present'] for r in details)/n if n else 0,
        'strict':sum(r['strict'] for r in details)/n if n else 0,'partial':sum(r['partial'] for r in details)/n if n else 0,
        'details':details,'note':'Missing predictions score zero; development-only results, not hidden-deployment accuracy.'}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='details'}))

if __name__=='__main__': main()
