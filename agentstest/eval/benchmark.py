"""Repeat fixed-case experiments with an explicit opt-in for paid model calls."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,required=True);p.add_argument('--queries',type=Path,required=True)
    p.add_argument('--labels',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--row-ids',default='0,1,4,5,8,9,25');p.add_argument('--repeats',type=int,default=3)
    p.add_argument('--modes',nargs='+',choices=['offline','single','routed'],default=['offline'])
    p.add_argument('--live',action='store_true');p.add_argument('--env-file',type=Path)
    args=p.parse_args()
    if args.repeats<2:p.error('Use at least two repeats to measure variability')
    if any(m!='offline' for m in args.modes) and not args.live:p.error('Model runs require --live; telemetry is sent to the configured endpoint')
    if args.out.exists():p.error('Use a new output directory')
    root=Path(__file__).resolve().parents[1];args.out.mkdir(parents=True)
    sources={str(f.relative_to(root)):hashlib.sha256(f.read_bytes()).hexdigest() for d in ['mini_rca','prompts','knowledge'] for f in (root/d).glob('*') if f.is_file()}
    manifest={'row_ids':args.row_ids,'modes':args.modes,'repeats':args.repeats,'source_hashes':sources,
              'live':args.live,'note':'Same default retrieval/model budgets; no prebuilt Parquet. OS cache is not cleared. Labels only enter the separate scorer.'}
    (args.out/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    runs=[]
    for mode in args.modes:
        for repeat in range(args.repeats):
            out=args.out/f'{mode}-{repeat+1}'
            command=[sys.executable,'-B',str(root/'run.py'),'--dataset',str(args.dataset.resolve()),'--queries',str(args.queries.resolve()),
                     '--out',str(out.resolve()),'--mode',mode,'--row-ids',args.row_ids]
            if args.env_file and mode!='offline':command+=['--env-file',str(args.env_file.resolve())]
            subprocess.run(command,check=True)
            subprocess.run([sys.executable,'-B',str(root/'evaluate.py'),'--predictions',str(out/'predictions.csv'),
                            '--labels',str(args.labels),'--row-ids',args.row_ids,'--out',str(out/'evaluation.json')],check=True)
            score=json.loads((out/'evaluation.json').read_text(encoding='utf-8'));summary=json.loads((out/'summary.json').read_text(encoding='utf-8'))
            with (out/'predictions.csv').open(encoding='utf-8',newline='') as f:predictions=list(csv.DictReader(f))
            runs.append({'mode':mode,'repeat':repeat+1,'strict':score['strict'],'partial':score['partial'],'coverage':score['coverage'],
                         'seconds_per_case':summary['run_wall_s']/score['denominator'],'dollars_per_case':summary['run_cost_dollars']/score['denominator'],
                         'model_adoption_rate':sum(r['mode'].endswith('-model') for r in predictions)/score['denominator']})
            aggregate={m:{key:{'mean':statistics.mean(r[key] for r in runs if r['mode']==m),
                                    'stdev':statistics.stdev([r[key] for r in runs if r['mode']==m]) if sum(r['mode']==m for r in runs)>1 else None}
                          for key in ['strict','partial','coverage','seconds_per_case','dollars_per_case','model_adoption_rate']}
                       for m in {r['mode'] for r in runs}}
            (args.out/'results.json').write_text(json.dumps({'runs':runs,'aggregate':aggregate,'limits':'Development cases only; no hidden-deployment or container-resource certification.'},indent=2),encoding='utf-8')
    print(json.dumps(aggregate,indent=2))


if __name__=='__main__':main()
