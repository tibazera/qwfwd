#!/bin/sh
set -eu
command -v python3 >/dev/null 2>&1 || { echo 'Python 3 is required for installation.' >&2; exit 1; }
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 - "$script_dir" "$@" <<'PY'
import json, os, pathlib, platform, shutil, struct, subprocess, sys

source = pathlib.Path(sys.argv[1])
if platform.machine() not in ('x86_64', 'AMD64'):
    raise SystemExit('This package requires Linux x64 (x86_64).')
extension_id = (source.parent / 'browser-extension/extension-id.txt').read_text().strip()
if len(extension_id) != 32 or any(c not in 'abcdefghijklmnop' for c in extension_id):
    raise SystemExit('Invalid extension ID.')
install_dir = pathlib.Path.home() / '.local/lib/qw-mesh-ping'
install_dir.mkdir(parents=True, exist_ok=True)
binary = install_dir / 'QwMeshPing'
shutil.copy2(source / 'dist/QwMeshPing', binary)
binary.chmod(0o755)
body = json.dumps({'id': 'install', 'op': 'health'}).encode()
result = subprocess.run([str(binary)], input=struct.pack('<I', len(body)) + body,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
if result.returncode or len(result.stdout) < 4:
    raise SystemExit('Helper failed to start: ' + result.stderr.decode(errors='replace'))
length = struct.unpack('<I', result.stdout[:4])[0]
if not json.loads(result.stdout[4:4 + length]).get('ok'):
    raise SystemExit('Helper health check failed.')
manifest = {'name': 'com.qwfwd.ping', 'description': 'QW Mesh UDP measurements',
            'path': str(binary), 'type': 'stdio',
            'allowed_origins': [f'chrome-extension://{extension_id}/']}
config = pathlib.Path(os.environ.get('XDG_CONFIG_HOME', str(pathlib.Path.home() / '.config')))
profiles = [pathlib.Path(p).expanduser().resolve() for p in sys.argv[2:]] or [
    config / 'google-chrome', config / 'chromium']
for profile in profiles:
    directory = profile / 'NativeMessagingHosts'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'com.qwfwd.ping.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Registered:', directory)
print('SUCCESS: helper installed and tested. Load browser-extension in Chrome and reload QW Mesh.')
PY
