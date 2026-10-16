"""Run on Linux: python3 tests/test_linux_package.py (after packaging)."""
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile
import tempfile

archive = Path(__file__).resolve().parents[1] / 'distribution/qw-mesh-linux-x64-test.tar.gz'
with tempfile.TemporaryDirectory() as directory:
    work = Path(directory)
    with tarfile.open(archive) as package:
        package.extractall(work, filter='data')
    source = work / 'qw-mesh-linux-x64-test'
    env = dict(os.environ, HOME=str(work / 'user'), XDG_CONFIG_HOME=str(work / 'config'))
    subprocess.run(['sh', str(source / 'native-ping-host/InstallLinux.sh')], env=env, check=True)
    manifest_path = work / 'config/google-chrome/NativeMessagingHosts/com.qwfwd.ping.json'
    manifest = json.loads(manifest_path.read_text())
    assert manifest['allowed_origins'] == ['chrome-extension://fabelkeohcbikapfaoeflaabbjpobgcl/']
    assert Path(manifest['path']).is_absolute() and os.access(manifest['path'], os.X_OK)
    request = json.dumps({'id': 'private', 'op': 'ping', 'target': '127.0.0.1:26000'}).encode()
    result = subprocess.run([manifest['path']], input=struct.pack('<I', len(request)) + request,
                            stdout=subprocess.PIPE, env=env, check=True, timeout=15)
    assert json.loads(result.stdout[4:])['error'] == 'invalid_target_or_operation'
    subprocess.run(['sh', str(source / 'native-ping-host/UninstallLinux.sh')], env=env, check=True)
    assert not manifest_path.exists() and not Path(manifest['path']).exists()
print('PASS: extracted Linux package, installation, native protocol, private targets and uninstall')
