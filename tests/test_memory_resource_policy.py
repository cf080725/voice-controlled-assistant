"""记忆与陪聊共用模型驻留配置；不实际请求 Ollama 或卸载模型。"""
import json

import pytest

import canaan_config
import canaan_memory


@pytest.mark.parametrize("configured,expected", [("10m", "10m"), ("2m", "2m"), ("24h", "24h"), ("", "10m")])
def test_memory_requests_use_chat_residency(monkeypatch, configured, expected):
    monkeypatch.setattr(canaan_config, "CHAT_KEEP_ALIVE", configured)
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"message": {"content": '{"summary":"虚构摘要"}'}}).encode()

    def request(req, **kw):
        requests.append(json.loads(req.data))
        return Response()

    monkeypatch.setattr(canaan_memory.urllib.request, "urlopen", request)
    assert canaan_memory._llm_json("fake-model", "虚构输入", {}) == {"summary": "虚构摘要"}
    assert requests[0]["keep_alive"] == expected
