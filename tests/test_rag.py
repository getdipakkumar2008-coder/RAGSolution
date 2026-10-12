import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import azure.functions as func

import function_app
from rag import DIMENSIONS, MAX_CHUNKS, InvalidInput, RagService, SqlStore, chunk_text, vector_json


def embedding_response(texts):
    return SimpleNamespace(data=[
        SimpleNamespace(index=index, embedding=[1.0] * DIMENSIONS)
        for index in reversed(range(len(texts)))
    ])


class RagTests(unittest.TestCase):
    def setUp(self):
        self.extractor = Mock()
        self.openai = Mock()
        self.store = Mock()
        self.openai.embeddings.create.side_effect = (
            lambda **kwargs: embedding_response(kwargs["input"])
        )
        self.service = RagService(
            self.extractor, self.openai, self.store, "embedding", "chat",
        )

    def test_chunk_overlap_and_final_tail(self):
        self.assertEqual(chunk_text("abcdefghij", 5, 2), ["abcde", "defgh", "ghij"])
        self.assertEqual(chunk_text("  "), [])
        self.assertEqual(chunk_text("abc", 3, 1), ["abc"])
        with self.assertRaises(ValueError):
            chunk_text("abc", 3, 3)
        with self.assertRaises(InvalidInput):
            chunk_text("a" * (MAX_CHUNKS + 1), 1, 0)

    def test_vector_validation(self):
        self.assertEqual(len(json.loads(vector_json([1.0] * DIMENSIONS))), DIMENSIONS)
        for vector in ([1.0], [float("nan")] * DIMENSIONS,
                       [float("inf")] * DIMENSIONS, [0.0] * DIMENSIONS):
            with self.subTest(vector=vector[:1]), self.assertRaises(ValueError):
                vector_json(vector)

    def test_pdf_ocr_embedding_and_binary_storage(self):
        self.extractor.begin_analyze_document.return_value.result.return_value = (
            SimpleNamespace(content="Extracted facts")
        )
        content = b"%PDF-example"
        result = self.service.ingest(content, "facts.pdf", "application/pdf")
        self.extractor.begin_analyze_document.assert_called_once_with(
            "prebuilt-read", body=content, content_type="application/pdf",
        )
        self.assertEqual(result["document_id"], hashlib.sha256(content).hexdigest())
        self.assertEqual(result["chunks"], 1)
        saved = self.store.save.call_args.args
        self.assertEqual(saved[3], content)
        self.assertEqual(saved[4], ["Extracted facts"])
        self.assertEqual(len(saved[5][0]), DIMENSIONS)

    def test_plain_text_bypasses_ocr(self):
        self.service.ingest("Résumé".encode(), "facts.txt", "text/plain")
        self.extractor.begin_analyze_document.assert_not_called()
        self.assertEqual(self.store.save.call_args.args[4], ["Résumé"])

    def test_ann_mode_rejects_ingestion_before_ocr(self):
        self.store.search_mode = "ann"
        with self.assertRaises(InvalidInput):
            self.service.ingest(b"image", "x.png", "image/png")
        self.extractor.begin_analyze_document.assert_not_called()
        self.openai.embeddings.create.assert_not_called()

    def test_invalid_uploads_do_not_embed_or_save(self):
        for content, filename, mime in (
            (b"", "x.pdf", "application/pdf"),
            (b"x", "", "text/plain"),
            (b"x", "x.zip", "application/zip"),
            (b"\xff", "x.txt", "text/plain"),
            (b"   ", "x.txt", "text/plain"),
            (b"x" * (20 * 1024 * 1024 + 1), "x.txt", "text/plain"),
        ):
            with self.subTest(mime=mime), self.assertRaises(InvalidInput):
                self.service.ingest(content, filename, mime)
        self.openai.embeddings.create.assert_not_called()
        self.store.save.assert_not_called()

    def test_empty_ocr_does_not_save(self):
        self.extractor.begin_analyze_document.return_value.result.return_value = (
            SimpleNamespace(content=None)
        )
        with self.assertRaises(InvalidInput):
            self.service.ingest(b"image", "empty.png", "image/png")
        self.store.save.assert_not_called()

    def test_embedding_batches_and_response_count(self):
        self.assertEqual(len(self.service.embed(["text"] * 17)), 17)
        self.assertEqual(self.openai.embeddings.create.call_count, 2)
        self.openai.embeddings.create.side_effect = None
        self.openai.embeddings.create.return_value = SimpleNamespace(data=[])
        with self.assertRaises(ValueError):
            self.service.embed(["text"])

    def test_query_grounding_and_citations(self):
        source = {
            "document_id": "abc", "filename": "facts.pdf", "chunk": 0,
            "text": "Revenue increased.", "distance": 0.1,
        }
        self.store.search.return_value = [source]
        self.openai.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Revenue increased [1]."))],
        )
        result = self.service.ask("What changed?", 3)
        self.assertEqual(result["sources"][0]["citation"], 1)
        self.assertEqual(result["sources"][0]["filename"], "facts.pdf")
        messages = self.openai.chat.completions.create.call_args.kwargs["messages"]
        self.assertIn("untrusted", messages[0]["content"])
        self.assertEqual(json.loads(messages[1]["content"])["sources"][0]["text"], source["text"])
        self.assertEqual(self.store.search.call_args.args[1], 3)
        self.assertEqual(self.store.search.call_args.args[2], "embedding")

    def test_empty_retrieval_does_not_call_chat(self):
        self.store.search.return_value = []
        result = self.service.ask("Question")
        self.assertEqual(result["sources"], [])
        self.openai.chat.completions.create.assert_not_called()

    def test_invalid_questions_and_top_k(self):
        for question, top_k in (("", 5), (None, 5), ("x" * 2001, 5),
                                ("x", 0), ("x", 21), ("x", True), ("x", "5")):
            with self.subTest(top_k=top_k), self.assertRaises(InvalidInput):
                self.service.ask(question, top_k)
        self.openai.embeddings.create.assert_not_called()


class SqlTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.execute.return_value = self.cursor
        self.cursor.fetchone.return_value = None
        self.connect = patch("rag.pyodbc.connect", return_value=self.connection)
        self.connect.start()
        self.addCleanup(self.connect.stop)
        self.store = SqlStore("connection", None)

    def test_atomic_parameterized_save(self):
        self.store.save("id", "quote';.txt", "text/plain", b"data", ["data"],
                        [[1.0] * DIMENSIONS], "embedding")
        calls = self.cursor.execute.call_args_list
        self.assertIn("UPDLOCK, HOLDLOCK", calls[0].args[0])
        self.assertNotIn("quote';.txt", calls[1].args[0])
        self.assertIn("CAST(? AS VECTOR(1536))", calls[2].args[0])
        self.connection.commit.assert_called_once()
        self.connection.close.assert_called_once()

    def test_duplicate_does_not_rewrite_chunks(self):
        self.cursor.fetchone.return_value = ("embedding",)
        self.store.save("id", "x", "text/plain", b"data", ["data"],
                        [[1.0] * DIMENSIONS], "embedding")
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.connection.commit.assert_called_once()

    def test_duplicate_with_different_model_rolls_back(self):
        self.cursor.fetchone.return_value = ("old-model",)
        with self.assertRaises(ValueError):
            self.store.save("id", "x", "text/plain", b"data", ["data"],
                            [[1.0] * DIMENSIONS], "new-model")
        self.connection.rollback.assert_called_once()
        self.assertEqual(self.cursor.execute.call_count, 1)

    def test_failure_rolls_back_and_closes(self):
        self.cursor.execute.side_effect = RuntimeError("failure")
        with self.assertRaises(RuntimeError):
            self.store.save("id", "x", "text/plain", b"data", ["data"],
                            [[1.0] * DIMENSIONS], "embedding")
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()
        self.connection.close.assert_called_once()

    def test_save_rejects_mismatched_embeddings(self):
        with self.assertRaises(ValueError):
            self.store.save("id", "x", "text/plain", b"data", ["data"], [], "embedding")
        self.connection.cursor.assert_not_called()

    def test_exact_and_ann_search_map_metadata(self):
        self.cursor.fetchall.return_value = [("id", "x.pdf", 1, "text", 0.25)]
        for mode, expression in (("exact", "VECTOR_DISTANCE"), ("ann", "VECTOR_SEARCH")):
            self.store.search_mode = mode
            result = self.store.search([1.0] * DIMENSIONS, 5, "embedding")
            self.assertIn(expression, self.cursor.execute.call_args.args[0])
            self.assertEqual(self.cursor.execute.call_args.args[2], 5)
            self.assertEqual(self.cursor.execute.call_args.args[3], "embedding")
            self.assertEqual(result[0]["distance"], 0.25)
            self.assertEqual(result[0]["filename"], "x.pdf")


class HttpTests(unittest.TestCase):
    def request(self, body, content_type="application/json", params=None):
        return func.HttpRequest(
            method="POST", url="https://localhost/api/query",
            headers={"content-type": content_type}, params=params or {}, body=body,
        )

    @patch("function_app.get_service")
    def test_ingest_http_contract(self, service):
        service.return_value.ingest.return_value = {"document_id": "id", "chunks": 1}
        result = function_app.ingest_document(self.request(
            b"facts", "text/plain; charset=utf-8", {"filename": "facts.txt"},
        ))
        self.assertEqual(result.status_code, 201)
        service.return_value.ingest.assert_called_once_with(
            b"facts", "facts.txt", "text/plain",
        )

    @patch("function_app.get_service")
    def test_query_http_contract(self, service):
        service.return_value.ask.return_value = {"answer": "Facts [1]", "sources": []}
        result = function_app.query_documents(self.request(b'{"question":"Facts?"}'))
        self.assertEqual(result.status_code, 200)
        service.return_value.ask.assert_called_once_with("Facts?", 5)

    def test_malformed_json_returns_400(self):
        for body in (b"{", b"[]", b"null"):
            self.assertEqual(function_app.query_documents(self.request(body)).status_code, 400)

    def test_oversized_query_returns_400(self):
        self.assertEqual(function_app.query_documents(
            self.request(b" " * (16 * 1024 + 1)),
        ).status_code, 400)

    @patch("function_app.get_service")
    def test_errors_are_sanitized(self, service):
        service.side_effect = RuntimeError("secret-connection-details")
        result = function_app.query_documents(self.request(b'{"question":"Facts?"}'))
        self.assertEqual(result.status_code, 503)
        self.assertNotIn(b"secret-connection-details", result.get_body())

    @patch("function_app.get_service")
    def test_validation_returns_400(self, service):
        service.return_value.ingest.side_effect = InvalidInput("Unsupported content type.")
        self.assertEqual(function_app.ingest_document(self.request(b"x")).status_code, 400)


if __name__ == "__main__":
    unittest.main()
