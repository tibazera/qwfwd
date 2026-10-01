#!/bin/sh
set -eu
python3 - "$@" <<'PY'
import os, pathlib, sys
config = pathlib.Path(os.environ.get('XDG_CONFIG_HOME', str(pathlib.Path.home() / '.config')))
profiles = [pathlib.Path(p).expanduser().resolve() for p in sys.argv[1:]] or [
    config / 'google-chrome', config / 'chromium']
for profile in profiles:
    (profile / 'NativeMessagingHosts/com.qwfwd.ping.json').unlink(missing_ok=True)
directory = pathlib.Path.home() / '.local/lib/qw-mesh-ping'
(directory / 'QwMeshPing').unlink(missing_ok=True)
print('Helper removed. Remove the extension through chrome://extensions.')
PY
