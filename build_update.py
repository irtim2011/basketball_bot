"""Build the self-contained updater from the reviewed source files."""
import base64
import hashlib
import io
from pathlib import Path
import zipfile
from version import VERSION

root = Path(__file__).resolve().parent
buffer = io.BytesIO()
files = sorted([*root.glob('*.py'), root/'requirements.txt', root/'README.md',
                root/'manage.sh', root/'install.sh', root/'deploy_existing.sh'])
with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in files:
        info = zipfile.ZipInfo(path.name, (2026, 9, 5, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, path.read_bytes())
header = '''#!/usr/bin/env bash
set -Eeuo pipefail
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
python3 - "$0" "$stage" <<'EXTRACT'
import base64, io, pathlib, sys, zipfile
raw = pathlib.Path(sys.argv[1]).read_bytes()
payload = raw.split(b'\\n__TRAINING_BOT_PAYLOAD__\\n', 1)[1]
with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload))) as archive:
    for entry in archive.infolist():
        if '/' in entry.filename or '\\\\' in entry.filename or entry.filename in {'.', '..'}:
            raise SystemExit('Unexpected archive path')
    archive.extractall(sys.argv[2])
EXTRACT
bash "$stage/deploy_existing.sh"
exit 0
__TRAINING_BOT_PAYLOAD__
'''
target = root/f'training-bot-update-{VERSION}.sh'
target.write_bytes(header.encode() + base64.encodebytes(buffer.getvalue()))
sums = root/'SHA256SUMS.txt'
previous = [line for line in sums.read_text().splitlines() if not line.endswith('  '+target.name) and (root/line.split('  ',1)[-1]).is_file()] if sums.exists() else []
sums.write_text('\n'.join(previous + [hashlib.sha256(target.read_bytes()).hexdigest()+'  '+target.name])+'\n')
print(target)
