import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock
from canaan_feishu_service import FeishuService
from canaan_message_center import MessageCenter


def test_managed_listener_backoff_and_external_listener_ownership(tmp_path,monkeypatch):
    import canaan_tray
    class Child:
        def __init__(self,*a,**kw):
            self.proc=None
            self.backoff=SimpleNamespace(ready=lambda:True,failed=Mock())
            self.starts=0
            self.stops=0
        def alive(self): return self.proc is not None and self.proc.alive
        def start(self):
            self.starts+=1
            self.proc=SimpleNamespace(alive=True,pid=123)
        def stop(self):
            self.stops+=1
            self.proc=None
    monkeypatch.setattr(canaan_tray,'Child',Child)
    monkeypatch.setattr(canaan_tray,'Job',lambda:SimpleNamespace(handle=0))
    monkeypatch.setattr(canaan_tray,'kernel32',SimpleNamespace(CloseHandle=lambda _:None))
    (tmp_path/'feishu_listener.json').write_text(json.dumps({'enabled':True,'python':sys.executable}))
    service=FeishuService(tmp_path,MessageCenter(tmp_path/'messages.db'))
    monkeypatch.setattr(service,'_external',lambda:True)
    service.tick()
    assert service.snapshot()['state']=='external' and service.child is None
    service.stop()
    assert service.child is None
    service=FeishuService(tmp_path,MessageCenter(tmp_path/'messages.db'))
    monkeypatch.setattr(service,'_external',lambda:False)
    service.tick()
    child=service.child
    assert child.starts==1 and service.snapshot()['owned']
    service.tick()
    assert child.starts==1
    child.proc.alive=False
    child.backoff.ready=lambda:False
    service.tick()
    assert child.backoff.failed.call_count==1 and child.starts==1
    child.backoff.ready=lambda:True
    service.tick()
    assert child.starts==2
    service.stop()
    service.tick()
    assert child.stops==1 and service.snapshot()['state']=='stopped'
