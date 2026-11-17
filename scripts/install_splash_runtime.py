#!/usr/bin/env python3
"""Install the pinned Splash runtime and already-downloaded DFlash2 drafts.

No cloud service or API credential is involved. Existing target models are
reused in place. The archive is verified before extraction; draft weights are
verified before installation. On APFS, copies share storage until modified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

VERSION = '1.6.1'
ARCHIVE_SHA256 = '0ed408a781f951a13ff58bed95c9e8c1d1dc67ab9d649fcaf365972b6720ce6c'
DRAFT_SHA256 = {
    '27b': '67fc76d68dc5a9415511a4f394ef744d67510cd20e93b37cc2cc7d28e4bab65c',
    '35b': 'd1f16c0ff85afbe6166fabfae998af3ec16fa11da87da4391b5e0e0a7c796a4d',
}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(8*1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def clone(source, destination):
    try:
        subprocess.run(['/bin/cp','-cR',str(source),str(destination)],check=True,capture_output=True)
    except (OSError,subprocess.CalledProcessError):
        if destination.exists():shutil.rmtree(destination)
        shutil.copytree(source,destination)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True,help='Verified Splash 1.6.1 arm64 macOS 26 release archive')
    p.add_argument('--draft-27b',type=Path,required=True)
    p.add_argument('--draft-35b',type=Path,required=True)
    p.add_argument('--destination',type=Path,default=Path.home()/'.local/share/claire/splash')
    a = p.parse_args()
    if digest(a.archive) != ARCHIVE_SHA256:raise SystemExit('Splash archive SHA-256 mismatch; nothing installed')
    for name,source in [('27b',a.draft_27b),('35b',a.draft_35b)]:
        if digest(source/'model.safetensors') != DRAFT_SHA256[name]:
            raise SystemExit(name+' draft SHA-256 mismatch; nothing installed')
    home = a.destination.expanduser().resolve()
    home.mkdir(parents=True,exist_ok=True)
    runtime = home/'runtime'/VERSION
    runtime.parent.mkdir(exist_ok=True)
    if not runtime.exists():
        with tempfile.TemporaryDirectory(dir=runtime.parent,prefix='.install-') as tmp:
            tmp = Path(tmp)
            # Only this pinned, verified publisher archive is extracted.
            with tarfile.open(a.archive) as tar:
                if hasattr(tarfile,'data_filter'):tar.extractall(tmp,filter='data')
                else:tar.extractall(tmp)
            candidates = [p.parent for p in tmp.rglob('release.json') if (p.parent/'engine/splash').exists()]
            if len(candidates) != 1:raise SystemExit('Unexpected Splash archive layout')
            source = candidates[0]
            if json.loads((source/'release.json').read_text())['version'] != VERSION:
                raise SystemExit('Unexpected Splash version')
            paths = source/'install/paths.py'
            # DATA is the sole local adaptation; never depend on /private/tmp.
            lines = paths.read_text().splitlines()
            paths.write_text('\n'.join('DATA = Path('+repr(str(home/'data'))+')' if line.startswith('DATA = ') else line for line in lines)+'\n')
            os.replace(source,runtime)
    elif json.loads((runtime/'release.json').read_text())['version'] != VERSION:
        raise SystemExit('An unexpected runtime already exists; left untouched')
    draft_home = home/'drafts';draft_home.mkdir(exist_ok=True)
    for name,source in [('27b',a.draft_27b),('35b',a.draft_35b)]:
        destination = draft_home/name
        if not destination.exists():
            with tempfile.TemporaryDirectory(dir=draft_home,prefix='.install-') as tmp:
                staged = Path(tmp)/name
                clone(source,staged)
                os.replace(staged,destination)
        if digest(destination/'model.safetensors') != DRAFT_SHA256[name]:
            raise SystemExit('Installed '+name+' draft verification failed')
    manifest = {'version':VERSION,'archive_sha256':ARCHIVE_SHA256,'draft_sha256':DRAFT_SHA256}
    (home/'installation.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({'installed_runtime':str(runtime),'drafts':str(draft_home),'version':VERSION}),flush=True)


if __name__ == '__main__':main()
