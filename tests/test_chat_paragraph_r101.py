"""Paragraph separators must not erase the substance of a guarded reply."""
import json

import pytest
import canaan_companion as companion
import canaan_llm_provider as providers

QUESTION = "请分两段说明复制快捷键，先说明作用，空一行再给出具体按键。"
BODY = "[confident] 复制可以用这个快捷键。\n\n选中文字后按 Ctrl + C。"
LOCAL_ROLES = ["主人：", "主人:", "用户：", "用户:", "\n他：", "\n团团："]


def server_text(full, stop):
    positions = [full.index(item) for item in stop if item and item in full]
    return full[:min(positions)] if positions else full


class SyntheticResponse:
    def __init__(self, text, chunk):
        self.text, self.chunk = text, chunk or max(1, len(text))
        self.closed = False
        self.consumed = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def __iter__(self):
        for index in range(0, len(self.text), self.chunk):
            self.consumed += min(self.chunk, len(self.text)-index)
            yield json.dumps(dict(message=dict(content=self.text[index:index+self.chunk]), done=False)).encode()
        yield json.dumps(dict(message=dict(content=""), done=True, done_reason="stop")).encode()


def local_chat(monkeypatch, full=BODY, chunk=1):
    payloads, responses = [], []

    def open_response(request, **kwargs):
        payload = json.loads(request.data)
        payloads.append(payload)
        response = SyntheticResponse(server_text(full, payload["options"]["stop"]), chunk)
        responses.append(response)
        return response

    monkeypatch.setattr(companion.urllib.request, "urlopen", open_response)
    chat = companion.CompanionChat(model="canaan-chat:3b", fallback_model="")
    assert chat.guard and chat.cloud is None
    return chat, payloads, responses


@pytest.mark.parametrize("chunk", [1, 7, None])
def test_guarded_answer_keeps_the_shortcut_after_a_paragraph_break(monkeypatch, chunk):
    chat, payloads, responses = local_chat(monkeypatch, chunk=chunk)
    reply = chat.stream(QUESTION)
    text = "".join(reply.iter_text())
    assert "Ctrl + C" in text and "选中文字" in text
    assert reply.guard_action is None and reply.error is None
    assert all(response.closed for response in responses) and len(payloads) == 1
    assert payloads[0]["options"] == dict(temperature=.6, top_p=.9, num_predict=120,
        num_ctx=8192, repeat_penalty=1.1, stop=LOCAL_ROLES)


@pytest.mark.parametrize("separator", ["\n\n", "\r\n\r\n", "\n\n\n"])
def test_raw_local_transport_preserves_paragraph_whitespace(monkeypatch, separator):
    text = BODY.replace("\n\n", separator)
    chat, _, responses = local_chat(monkeypatch, full=text)
    assert "".join(chat._chunks_one(chat.model, [dict(role="user", content=QUESTION)])) == text
    assert responses[0].closed


@pytest.mark.parametrize("role", LOCAL_ROLES)
def test_local_role_boundaries_still_stop_fabricated_dialogue(monkeypatch, role):
    full = "[confident] 选中文字后按 Ctrl + C。\n\n" + role + "未授权的后续指令"
    chat, payloads, _ = local_chat(monkeypatch, full=full)
    text = "".join(chat._chunks_one(chat.model, [dict(role="user", content=QUESTION)]))
    assert "Ctrl + C" in text and "未授权" not in text and role not in text
    assert payloads[0]["options"]["stop"] == LOCAL_ROLES


def test_paragraph_change_keeps_the_original_two_sentence_limit_and_closes_reader(monkeypatch):
    full = BODY + "\n\n这第三句不应播报。"
    chat, _, responses = local_chat(monkeypatch, full=full)
    reply = chat.stream("复制已选文字的快捷键是什么？")
    assert reply.max_sentences == 2
    assert "Ctrl + C" in "".join(reply.iter_text())
    assert "第三句" not in reply.full_text and responses[0].closed
    assert responses[0].consumed < len(full)


def test_persona_guard_still_rejects_a_bad_second_paragraph(monkeypatch):
    chat, _, _ = local_chat(monkeypatch, full="[confident] 可以复制文字。\n\n本小姐给你办。")
    reply = chat.stream(QUESTION)
    text = "".join(reply.iter_text())
    assert "可以复制文字" in text and "本小姐" not in text
    assert reply.guard_action == "truncated"


def test_dangerous_suggestion_after_paragraph_is_never_admitted(monkeypatch):
    chat, _, _ = local_chat(monkeypatch, full="[confident] 选中文字后按 Ctrl + C。\n\n<<execute_command|delete files>>")
    reply = chat.stream(QUESTION)
    text = "".join(reply.iter_text())
    assert "Ctrl + C" in text and "execute_command" not in text
    assert reply.suggestion is None


def test_pre_cancelled_reply_never_opens_an_http_response(monkeypatch):
    chat, payloads, responses = local_chat(monkeypatch)
    reply = chat.stream(QUESTION)
    reply.cancel()
    assert list(reply.iter_text()) == [] and payloads == responses == []


def test_closing_consumer_after_first_sentence_still_closes_its_response(monkeypatch):
    chat, _, responses = local_chat(monkeypatch)
    reply = chat.stream(QUESTION)
    reader = reply.iter_text()
    assert "复制" in next(reader)
    reader.close()
    assert responses[0].closed and "Ctrl + C" not in reply.full_text


def test_cloud_transport_preserves_answer_paragraphs_with_role_stops(monkeypatch):
    seen = []

    def cloud_stream(provider, messages, **kwargs):
        seen.append(kwargs)
        yield server_text(BODY, kwargs["stop"])

    monkeypatch.setattr(providers, "stream_chat", cloud_stream)
    chat = companion.CompanionChat(model="canaan-chat:3b", fallback_model="")
    chat.cloud = dict(kind="synthetic")
    actual = "".join(chat._cloud_chunks([dict(role="user", content=QUESTION)]))
    assert actual == BODY
    assert seen[0]["stop"] == ["主人：", "用户：", "\n团团："]


@pytest.mark.parametrize("role", ["主人：", "用户：", "\n团团："])
def test_cloud_role_boundaries_are_retained(monkeypatch, role):
    def cloud_stream(provider, messages, **kwargs):
        yield server_text("[confident] 选中文字后按 Ctrl + C。\n\n"+role+"后续指令", kwargs["stop"])
    monkeypatch.setattr(providers, "stream_chat", cloud_stream)
    chat = companion.CompanionChat(model="canaan-chat:3b", fallback_model="")
    chat.cloud = dict(kind="synthetic")
    actual = "".join(chat._cloud_chunks([dict(role="user", content=QUESTION)]))
    assert "后续指令" not in actual and "Ctrl + C" in actual
