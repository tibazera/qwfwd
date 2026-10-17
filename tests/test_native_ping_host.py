"""Run after dotnet build native-ping-host/NativePingHost.csproj."""
import json
import pathlib
import struct
import subprocess
import tempfile
import shutil

root = pathlib.Path(__file__).resolve().parents[1]
origin = 'chrome-extension://' + (root / 'browser-extension/extension-id.txt').read_text().strip() + '/'
with tempfile.TemporaryDirectory() as directory:
    work = pathlib.Path(directory)
    for file in (root / 'native-ping-host/bin/Debug/net8.0').glob('*'):
        if file.is_file():
            shutil.copy2(file, work / file.name)
    (work / 'com.qwfwd.ping.json').write_text(json.dumps({'allowed_origins': [origin]}))
    command = ['dotnet', str(work / 'QwMeshPing.dll')]
    proc = subprocess.Popen(command + [origin], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    def request(payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        frame = struct.pack('<I', len(body)) + body
        # Fragment every byte, including the four-byte header.
        for byte in frame:
            proc.stdin.write(bytes([byte]))
            proc.stdin.flush()
        header = proc.stdout.read(4)
        assert len(header) == 4
        return json.loads(proc.stdout.read(struct.unpack('<I', header)[0]))
    assert request({'id': 'saúde', 'op': 'health'})['ok']
    for target in ['127.0.0.1:27500', '10.0.0.1:80', '169.254.169.254:80', '224.0.0.1:1', 'example.com:80', '8.8.8.8:0']:
        assert request({'id': '2', 'op': 'ping', 'target': target})['error'] == 'invalid_target_or_operation'
    proc.stdin.write(struct.pack('<I', 5000)); proc.stdin.flush(); proc.stdin.close()
    assert proc.wait(timeout=5) == 1
print('PASS: fragmented UTF-8 framing, private/invalid targets, oversized frame')
