"""Export a new standalone submission directory/ZIP from a strict file allowlist."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT=Path(__file__).resolve().parent
FILES=['Dockerfile','.dockerignore','.gitignore','Makefile','README.md','REPORT.md','DEMO.md',
       'THIRD_PARTY_NOTICES.md','requirements.txt','run.py','evaluate.py','package_submission.py']
FOLDERS=['mini_rca','prompts','knowledge','examples','eval','tests','docs']


def export(destination):
    destination=Path(destination).resolve()
    if destination==ROOT or destination in ROOT.parents:
        raise ValueError('Destination must be a new directory, never a source parent')
    if destination.exists() or destination.with_suffix('.zip').exists():raise FileExistsError('Use a new destination')
    selected=[ROOT/('Dockerfile.alternative' if name == 'Dockerfile' and (ROOT/'Dockerfile.alternative').is_file() else name) for name in FILES]
    for folder in FOLDERS:
        selected.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py','.md','.json','.csv','.txt'})
    for path in selected:
        if not path.is_file() or path.is_symlink():raise ValueError('Missing or symbolic-link export input: '+path.name)
        if path.name.startswith('.env') or path.suffix=='.pyc':raise ValueError('Forbidden export input')
    destination.mkdir(parents=True)
    hashes={}
    for path in selected:
        relative=Path('Dockerfile') if path.name == 'Dockerfile.alternative' else path.relative_to(ROOT)
        target=destination/relative
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
        hashes[relative.as_posix()]=hashlib.sha256(target.read_bytes()).hexdigest()
    (destination/'EXPORT_MANIFEST.json').write_text(json.dumps({'files':hashes,'note':'Source artifact only; no claim of Docker build, live validation, publication or submission.'},indent=2),encoding='utf-8')
    archive=destination.with_suffix('.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for path in sorted(destination.rglob('*')):
            if path.is_file():z.write(path,path.relative_to(destination))
    return {'directory':str(destination),'archive':str(archive),'files':len(hashes)+1}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    print(json.dumps(export(p.parse_args().out),indent=2))
