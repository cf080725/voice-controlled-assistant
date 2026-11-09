import json
import sqlite3
import threading
import urllib.error
import zipfile
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import canaan_config as cfg
import canaan_knowledge as knowledge


@pytest.fixture
def knowledge_base(tmp_path, monkeypatch):
    from canaan_memory import MemoryStore

    monkeypatch.setattr(MemoryStore, "_load_vec", lambda self: False)
    database = knowledge.KnowledgeBase(tmp_path / "knowledge.db")
    yield database
    database.store.db.close()


def test_interrupted_replacement_never_exposes_or_leaves_unmapped_chunks(knowledge_base, tmp_path, monkeypatch):
    source = tmp_path / "manual.txt"
    source.write_text("原来的接线说明", encoding="utf-8")
    knowledge_base.import_file(source)
    previous_catalog = knowledge_base.documents()
    source.write_text("新说明甲" * 130 + "\n" + "新说明乙" * 130, encoding="utf-8")
    original_add = knowledge_base.store.add_episode

    def interrupt_after_inserting(summary):
        original_add(summary)
        with closing(sqlite3.connect(knowledge_base.store.path)) as other:
            assert other.execute("SELECT summary FROM episodes").fetchall() == [("原来的接线说明",)]
            assert other.execute("SELECT summary FROM episodes_fts").fetchall() == [("原来的接线说明",)]
        raise KeyboardInterrupt

    monkeypatch.setattr(knowledge_base.store, "add_episode", interrupt_after_inserting)
    with pytest.raises(KeyboardInterrupt):
        knowledge_base.import_file(source)

    assert not knowledge_base.store.db.in_transaction
    assert knowledge_base.documents() == previous_catalog
    assert [row[0] for row in knowledge_base.store.db.execute("SELECT summary FROM episodes")] == ["原来的接线说明"]


def test_rejected_chunk_keeps_previous_document(knowledge_base, tmp_path):
    source = tmp_path / "manual.txt"
    source.write_text("原来的接线说明", encoding="utf-8")
    knowledge_base.import_file(source)
    previous_catalog = knowledge_base.documents()
    knowledge_base.store.incognito = True
    source.write_text("新接线说明", encoding="utf-8")

    with pytest.raises(RuntimeError, match="保存资料片段"):
        knowledge_base.import_file(source)

    assert knowledge_base.documents() == previous_catalog
    assert [row[0] for row in knowledge_base.store.db.execute("SELECT summary FROM episodes")] == ["原来的接线说明"]


def test_successful_replacement_updates_the_catalog_and_search_indexes(knowledge_base, tmp_path):
    source = tmp_path / "manual.txt"
    source.write_text("old signal wiring", encoding="utf-8")
    assert knowledge_base.import_file(source)["changed"]
    assert knowledge_base.search("old")[0]["text"] == "old signal wiring"

    source.write_text("replacement voltage guide", encoding="utf-8")
    assert knowledge_base.import_file(source)["changed"]
    assert not knowledge_base.import_file(source)["changed"]

    assert not knowledge_base.store.db.execute(
        "SELECT 1 FROM episodes_fts WHERE episodes_fts MATCH ?", ("old",)
    ).fetchall()
    assert knowledge_base.search("voltage")[0]["text"] == "replacement voltage guide"
    assert knowledge_base.documents()[0]["chunks"] == 1


def test_rejects_docx_with_excessive_declared_uncompressed_size(tmp_path, monkeypatch):
    source = tmp_path / "large.docx"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 2048)
    monkeypatch.setattr(knowledge, "MAX_DOCX_UNCOMPRESSED_BYTES", 1024, raising=False)

    with pytest.raises(ValueError, match="解压"):
        knowledge.read_document(source)


def test_extracts_docx_paragraphs_and_tables(tmp_path):
    from docx import Document

    source = tmp_path / "manual.docx"
    document = Document()
    document.add_paragraph("接线说明")
    row = document.add_table(rows=1, cols=2).rows[0]
    row.cells[0].text = "端口"
    row.cells[1].text = "电压"
    document.save(source)

    assert knowledge.read_document(source) == (source, "接线说明\n端口\t电压")


@pytest.fixture
def ollama_server(monkeypatch):
    paths = []
    answer = json.dumps({"message": {"content": "根据资料 [1]"}}, ensure_ascii=False).encode()

    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            paths.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/api/chat")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        do_GET = respond
        do_POST = respond

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setenv("no_proxy", "127.0.0.1")
        monkeypatch.setattr(urllib.request, "_opener", None)
        monkeypatch.setattr(cfg, "OLLAMA_API_URL", url + "/api/chat")
        try:
            yield url, paths
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.fixture
def answering_knowledge(knowledge_base, monkeypatch):
    monkeypatch.setattr(knowledge_base, "search", lambda *_, **__: [{"text": "私有的接线说明"}])
    return knowledge_base


def test_local_answer_does_not_follow_redirect(answering_knowledge, ollama_server, monkeypatch):
    url, paths = ollama_server
    monkeypatch.setattr(cfg, "OLLAMA_API_URL", url + "/redirect")

    with pytest.raises(urllib.error.HTTPError) as error:
        answering_knowledge.answer("怎么接线？")

    assert error.value.code == 302
    error.value.close()
    assert paths == ["/redirect"]


def test_local_answer_ignores_environment_proxy(answering_knowledge, ollama_server, monkeypatch):
    url, paths = ollama_server
    monkeypatch.setenv("http_proxy", url)
    monkeypatch.setenv("no_proxy", "invalid.example")
    assert answering_knowledge.answer("怎么接线？")["answer"] == "根据资料 [1]"
    assert paths == ["/api/chat"]


def test_local_answer_bounds_response_bytes(answering_knowledge, ollama_server, monkeypatch):
    monkeypatch.setattr(knowledge, "MAX_OLLAMA_RESPONSE_BYTES", 20, raising=False)

    with pytest.raises(RuntimeError, match="响应超过"):
        answering_knowledge.answer("怎么接线？")
