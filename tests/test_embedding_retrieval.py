import pytest


def test_paper_text_modes_are_explicit():
    from embedding_retrieval import paper_text

    paper = {
        "title": "HydroCast",
        "abstract": "Forecasts streamflow.",
        "keywords": ["hydrology", "forecasting"],
    }
    assert paper_text(paper, "title_only") == "Title: HydroCast"
    assert "Abstract: Forecasts streamflow." in paper_text(
        paper, "title_abstract"
    )
    assert "Keywords: hydrology, forecasting" in paper_text(
        paper, "title_abstract_keywords"
    )


def test_cosine_similarity_supports_mrl_dimensions():
    from embedding_retrieval import cosine_similarity

    left = [1.0, 0.0, 100.0]
    right = [1.0, 0.0, -100.0]
    assert cosine_similarity(left, right, dimension=2) == pytest.approx(1.0)
    assert cosine_similarity(left, right) < 0


def test_embedding_config_validates_dimension_and_threshold():
    from embedding_retrieval import EmbeddingRetrievalConfig

    source = {
        "retrieval": {
            "dimension": 512,
            "threshold": 0.42,
            "text_mode": "title_abstract",
        }
    }
    config = EmbeddingRetrievalConfig.from_source(source)
    assert config.dimension == 512
    assert config.threshold == 0.42
    assert config.text_mode == "title_abstract"

    with pytest.raises(ValueError, match="dimension"):
        EmbeddingRetrievalConfig.from_source(
            {"retrieval": {"dimension": 16}}
        )


def test_qwen_client_formats_query_instruction_and_documents():
    from embedding_retrieval import EmbeddingRetrievalConfig, QwenEmbeddingClient

    calls = []

    class Response:
        def __init__(self, count):
            self.count = count

        def raise_for_status(self):
            return None

        def json(self):
            return {"embeddings": [[1.0, 0.0] for _ in range(self.count)]}

    class Session:
        def post(self, url, json, timeout):
            calls.append((url, json, timeout))
            return Response(len(json["texts"]))

    config = EmbeddingRetrievalConfig(dimension=32, batch_size=2)
    client = QwenEmbeddingClient(config, session=Session())
    client.embed_query("hydrology", "retrieve hydrology papers")
    client.embed_documents(["a", "b"])

    assert calls[0][1]["input_type"] == "query"
    assert calls[0][1]["instruction"] == "retrieve hydrology papers"
    assert calls[1][1]["input_type"] == "document"
    assert calls[1][1]["instruction"] == ""


def test_llama_cpp_client_uses_openai_embeddings_and_formats_query():
    from embedding_retrieval import EmbeddingRetrievalConfig, LlamaCppEmbeddingClient

    calls = []

    class Response:
        def __init__(self, count):
            self.count = count

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"index": index, "embedding": [1.0, 0.0, 0.0, 0.0]}
                    for index in range(self.count)
                ]
            }

    class Session:
        def post(self, url, json, timeout):
            calls.append((url, json, timeout))
            return Response(len(json["input"]))

    config = EmbeddingRetrievalConfig(
        backend="llama_cpp",
        model="Qwen3-Embedding-0.6B-GGUF-Q8_0",
        dimension=4,
        batch_size=2,
    )
    client = LlamaCppEmbeddingClient(config, session=Session())
    client.embed_query("hydrology", "retrieve hydrology papers")
    client.embed_documents(["a", "b", "c"])

    assert calls[0][0].endswith("/v1/embeddings")
    assert calls[0][1] == {
        "model": "Qwen3-Embedding-0.6B-GGUF-Q8_0",
        "input": ["Instruct: retrieve hydrology papers\nQuery: hydrology"],
    }
    assert [len(call[1]["input"]) for call in calls[1:]] == [2, 1]


def test_detailed_instruction_matches_qwen_format():
    from qwen_embedding_service import detailed_instruction

    assert detailed_instruction("retrieve papers", "hydrology") == (
        "Instruct: retrieve papers\nQuery: hydrology"
    )


def test_fastapi_embedding_endpoint_accepts_json_body():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from qwen_embedding_service import create_app

    class FakeEngine:
        model_name = "fake-qwen"
        device = "mps"
        _model = object()

        def encode(self, texts, *, dimension, batch_size, max_length):
            assert texts == ["Instruct: retrieve papers\nQuery: hydrology"]
            assert (dimension, batch_size, max_length) == (256, 1, 128)
            return [[1.0] * dimension]

    client = TestClient(create_app(FakeEngine()))
    response = client.post(
        "/v1/embeddings",
        json={
            "texts": ["hydrology"],
            "input_type": "query",
            "instruction": "retrieve papers",
            "dimension": 256,
            "batch_size": 1,
            "max_length": 128,
        },
    )

    assert response.status_code == 200
    assert len(response.json()["embeddings"][0]) == 256
