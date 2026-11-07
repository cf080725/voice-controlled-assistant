import json
import pytest
from message_tools import feishu_connector as connector


def test_cli_keychain_reference_is_bound_to_app(tmp_path, monkeypatch):
    monkeypatch.delenv('FEISHU_APP_ID', raising=False)
    monkeypatch.delenv('FEISHU_APP_SECRET', raising=False)
    path = tmp_path / 'config.json'
    data = {'apps': [{'appId': 'cli_test', 'brand': 'feishu', 'appSecret': {'source': 'keychain', 'id': 'appsecret:cli_test'}}]}
    path.write_text(json.dumps(data), encoding='utf-8')
    calls = []
    monkeypatch.setattr(connector, '_lark_windows_secret', lambda account: calls.append(account) or 'test-secret')
    assert connector.load_credentials(path) == ('cli_test', 'test-secret')
    assert calls == ['appsecret:cli_test']
    data['apps'][0]['appSecret']['id'] = 'appsecret:other'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='当前应用'):
        connector.load_credentials(path)
    assert len(calls) == 1


def test_environment_does_not_open_cli_keychain(monkeypatch):
    monkeypatch.setenv('FEISHU_APP_ID', 'cli_env')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'local-secret')
    assert connector.load_credentials('/does/not/exist') == ('cli_env', 'local-secret')
