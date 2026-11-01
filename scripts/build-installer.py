"""Wrap a verified release ZIP into one Linux shell installer."""
import base64
import hashlib
from pathlib import Path
import sys


HEADER = r'''#!/bin/bash
set -euo pipefail
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  echo "Usage: sudo bash $0 [--display-pre2025] [--setup]"
  echo "Requires Raspberry Pi Zero 2 W, 32-bit Bookworm, user streamer and setup Wi-Fi."
  echo "Automatic discovery starts on the next boot by default. --setup pauses automatic joining."
  exit 0
fi
if [[ $EUID -ne 0 ]]; then echo "Run with sudo bash $0" >&2; exit 1; fi
command -v python3 >/dev/null || { echo 'Python 3 is required.' >&2; exit 1; }
workdir=$(mktemp -d /tmp/streamer-install.XXXXXXXX)
cleanup() {
  python3 - "$workdir" <<'CLEANUP_PY'
from pathlib import Path
import shutil, sys
path = Path(sys.argv[1])
if path.parent == Path('/tmp') and path.name.startswith('streamer-install.') and not path.is_symlink():
    shutil.rmtree(path)
CLEANUP_PY
}
trap cleanup EXIT
python3 - "$0" "$workdir" <<'EXTRACT_PY'
import base64, hashlib, io, sys, zipfile
from pathlib import Path
source, target = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
payload = base64.b64decode(source.read_bytes().split(b'\n__STREAMER_PAYLOAD__\n', 1)[1])
if hashlib.sha256(payload).hexdigest() != 'EXPECTED_SHA256':
    raise SystemExit('Installer checksum failed; no installation was attempted.')
with zipfile.ZipFile(io.BytesIO(payload)) as archive:
    for entry in archive.infolist():
        (target / entry.filename).resolve().relative_to(target)
    archive.extractall(target)
EXTRACT_PY
bash "$workdir/scripts/install.sh" "$@"
exit 0
__STREAMER_PAYLOAD__
'''


def build(archive):
    payload = archive.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    header = HEADER.replace('EXPECTED_SHA256', digest)
    output = archive.with_name('install-' + archive.stem + '.run')
    output.write_bytes(header.encode() + base64.encodebytes(payload))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(checksum + '  ' + output.name + '\n')
    print(output)
    return output


if __name__ == '__main__':
    build(Path(sys.argv[1]))
