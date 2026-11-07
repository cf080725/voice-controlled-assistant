"""A capped/rejected reply must release its model request before the next one starts."""
import json

import pytest

import canaan_companion as cc


class Response:
    def __init__(self, pieces):
        self.pieces = pieces
        self.closed = False
        self.read = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def __iter__(self):
        for piece in self.pieces:
            self.read += 1
            yield json.dumps({"message": {"content": piece}}).encode()


@pytest.mark.parametrize("guard", [False, True])
def test_sentence_cap_closes_http_stream_without_reading_unused_tail(monkeypatch, guard):
    response = Response(["先休息一会儿。", "明天继续。", "这是多余的第三句。"])
    monkeypatch.setattr(cc.urllib.request, "urlopen", lambda *args, **kwargs: response)
    chat = cc.CompanionChat(guard=guard)
    stream = cc.ChatStream(chat._chunks([], None), guard=guard)
    stream.max_sentences = 1
    assert "".join(stream.iter_text()) == "先休息一会儿。"
    assert response.read < 3
    assert response.closed, "The capped reply left the model HTTP request open"


def test_rejected_draft_is_closed_before_regeneration(monkeypatch):
    bad = Response(["本小姐陪你休息，", "后续不该继续生成。"])
    good = Response(["先休息一会儿。"])
    requests = []

    def open_response(*args, **kwargs):
        requests.append(1)
        if len(requests) == 1:
            return bad
        assert bad.closed, "Regeneration started while the rejected model request was still open"
        return good

    monkeypatch.setattr(cc.urllib.request, "urlopen", open_response)
    chat = cc.CompanionChat()
    stream = cc.ChatStream(chat._chunks([]), regen=lambda: chat._chunks([]))
    assert "".join(stream.iter_text()) == "先休息一会儿。"
    assert stream.guard_action == "regenerated" and bad.closed and good.closed


def test_closing_consumer_releases_model_request(monkeypatch):
    response = Response(["先休息一会儿，", "明天再说。"])
    monkeypatch.setattr(cc.urllib.request, "urlopen", lambda *args, **kwargs: response)
    stream = cc.ChatStream(cc.CompanionChat()._chunks([]))
    consumer = stream.iter_text()
    assert next(consumer) == "先休息一会儿，"
    consumer.close()
    assert response.closed, "Interrupt left the request running"
    assert stream.full_text == "先休息一会儿，"
