"""Actual imported HTTP Handler, repeated Message headers, no listener."""
from email.message import Message
import io
import json
from types import SimpleNamespace
import pytest

@pytest.fixture
def actual_header_request(monkeypatch):
    import canaan_api_server as server
    captured, calls = {}, []
    class FakeServer:
        def __init__(self,address,handler): captured['handler']=handler;self.server_port=18181
        def serve_forever(self): raise AssertionError('No listener')
    class FakeThread:
        def __init__(self,*args,**kwargs): pass
        def start(self): pass
    monkeypatch.setattr(server,'ThreadingHTTPServer',FakeServer)
    monkeypatch.setattr(server.threading,'Thread',FakeThread)
    monkeypatch.setattr(server,'authenticate_credential',lambda clients,token:SimpleNamespace(name='synthetic') if token=='synthetic' else None)
    runtime=server.IntegrationRuntime.__new__(server.IntegrationRuntime)
    runtime.clients=object();runtime.voices=SimpleNamespace(start=lambda:None)
    runtime.route=lambda *args,**kwargs:calls.append((args,kwargs)) or {'ok':True}
    runtime.start(port=0,capture=False)
    def request(path,extra=(),payload=b''):
        handler=captured['handler'].__new__(captured['handler'])
        handler.command,handler.path='GET',path
        repeated=Message()
        for key,value in [('Host','127.0.0.1:18181'),('Authorization','Bearer synthetic'),*extra]: repeated[key]=value
        handler.headers,handler.rfile,handler.wfile=repeated,io.BytesIO(payload),io.BytesIO()
        status=[];handler.send_response=status.append;handler.send_header=lambda *args:None;handler.end_headers=lambda:None
        handler.connection=SimpleNamespace(settimeout=lambda _:None)
        count=len(calls);handler.handle_request()
        return {'status':status[0],'body':json.loads(handler.wfile.getvalue()),'routed':len(calls)-count,'consumed':handler.rfile.tell()}
    return request

cases = [
    ('no_length',[],b'',200),('single_zero',[('Content-Length','0')],b'',200),
    ('duplicate_zero',[('Content-Length','0'),('Content-Length','0')],b'',400),
    ('conflicting_zero_first',[('Content-Length','0'),('Content-Length','1')],b'x',400),
    ('conflicting_one_first',[('Content-Length','1'),('Content-Length','0')],b'x',400),
    ('triple_zero',[('Content-Length','0')]*3,b'',400),
    ('empty_te',[('Transfer-Encoding','')],b'',400),
    ('chunked_te',[('Transfer-Encoding','chunked')],b'1\r\nx\r\n0\r\n\r\n',400),
    ('empty_then_chunked_te',[('Transfer-Encoding',''),('Transfer-Encoding','chunked')],b'x',400),
    ('chunked_then_empty_te',[('Transfer-Encoding','chunked'),('Transfer-Encoding','')],b'x',400),
    ('zero_plus_empty_te',[('Content-Length','0'),('Transfer-Encoding','')],b'',400),
    ('zero_empty_then_chunked_te',[('Content-Length','0'),('Transfer-Encoding',''),('Transfer-Encoding','chunked')],b'x',400),
    ('mixed_case_duplicate',[('content-length','0'),('CONTENT-LENGTH','1')],b'x',400),
    ('comma_length',[('Content-Length','0, 0')],b'',400),
]

@pytest.mark.parametrize('name,headers,payload,expected',cases)
def test_repeated_http_headers_reject_before_route_or_read(actual_header_request,name,headers,payload,expected):
    got=actual_header_request('/api/device-text/capabilities',headers,payload)
    assert got['status']==expected
    assert got['routed']==(1 if expected==200 else 0)
    assert got['consumed']==0

@pytest.mark.parametrize('name,path,headers,expected',[('query', '/api/device-text/capabilities?confirmed=', [], 400), ('nondevice_scope', '/api/status', [('Content-Length', '0'), ('Content-Length', '1'), ('Transfer-Encoding', '')], 200)])
def test_http_query_and_nondevice_scope_controls(actual_header_request,name,path,headers,expected):
    got=actual_header_request(path,headers,b'')
    assert got['status']==expected and got['routed']==(1 if expected==200 else 0)
