import sys
from unittest.mock import patch
sys.path.insert(0, 'collector')
import collector as c
import protocol
A=('8.8.8.8',31000); B=('1.1.1.1',31001); G=('9.9.9.9',26000)
def probe(addr, graph):
    if addr in (A,B):
        graph.confirmed_proxies.add(addr)
        graph.edges.setdefault(addr, [])
    return {B} if addr == A else set()
with patch.object(c,'discover_servers',return_value=[A,G,('127.0.0.1',30000)]), patch.object(c,'fetch_qw_data_servers',return_value=[]), patch.object(c,'PINNED_PROXIES',[]), patch.object(c,'probe_one',side_effect=probe):
    c.collect_once()
assert set(c.graph.edges)=={A,B}
assert c.graph.discovery == dict(candidates=3,probed=3,confirmed=2,not_confirmed=1,remaining=0,complete=True)
with patch.object(protocol,'udp_request',return_value=protocol.OOB+b'n'):
    assert c.probe_pingstatus(None,A)==[]
with patch.object(protocol,'udp_request',return_value=protocol.OOB+b'nerror'):
    assert c.probe_pingstatus(None,A) is None
assert not c.public_endpoint(('192.168.0.1',30000))
assert not c.public_endpoint(('8.8.8.8',70000))
print('Discovery: nonstandard ports, recursive peers, empty replies, private addresses and statistics PASS')
