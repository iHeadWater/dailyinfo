"""FastAPI service for local Qwen3-Embedding inference on MPS/CUDA/CPU."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
import time
from typing import Literal


DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"


def detailed_instruction(instruction: str, query: str) -> str:
    """Use the query format recommended by the Qwen3 model card."""

    return f"Instruct: {instruction.strip()}\nQuery: {query.strip()}"


def resolve_device(torch_module, requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    if torch_module.backends.mps.is_available():
        return "mps"
    if torch_module.cuda.is_available():
        return "cuda"
    return "cpu"


@dataclass
class QwenEmbeddingEngine:
    model_name: str = DEFAULT_MODEL
    device_name: str = "auto"

    def __post_init__(self) -> None:
        self._model = None
        self._tokenizer = None
        self._torch = None
        self.device = "unloaded"

    def load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            import torch.nn.functional as functional
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "embedding dependencies are missing; run `uv sync --extra embedding`"
            ) from exc

        self.device = resolve_device(torch, self.device_name)
        dtype = torch.float16 if self.device in {"mps", "cuda"} else torch.float32
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_name, padding_side="left"
        )
        self._model = AutoModel.from_pretrained(
            self.model_name,
            torch_dtype=dtype,
            attn_implementation="eager",
        ).to(self.device)
        self._model.eval()
        self._torch = torch
        self._functional = functional

    def _last_token_pool(self, hidden_states, attention_mask):
        torch = self._torch
        left_padding = attention_mask[:, -1].sum() == attention_mask.shape[0]
        if left_padding:
            return hidden_states[:, -1]
        lengths = attention_mask.sum(dim=1) - 1
        rows = torch.arange(hidden_states.shape[0], device=hidden_states.device)
        return hidden_states[rows, lengths]

    def encode(
        self,
        texts: list[str],
        *,
        dimension: int = 1024,
        batch_size: int = 8,
        max_length: int = 2048,
    ) -> list[list[float]]:
        self.load()
        if not 32 <= dimension <= 1024:
            raise ValueError("dimension must be between 32 and 1024")
        vectors: list[list[float]] = []
        torch = self._torch
        with torch.inference_mode():
            for start in range(0, len(texts), batch_size):
                batch = texts[start : start + batch_size]
                tokens = self._tokenizer(
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=max_length,
                    return_tensors="pt",
                ).to(self.device)
                output = self._model(**tokens)
                pooled = self._last_token_pool(
                    output.last_hidden_state, tokens["attention_mask"]
                )
                # Qwen3-Embedding is Matryoshka-trained: truncate the leading
                # dimensions and normalize again for 1024/512/256 comparisons.
                pooled = pooled[:, :dimension]
                pooled = self._functional.normalize(pooled, p=2, dim=1)
                vectors.extend(pooled.float().cpu().tolist())
        return vectors


def create_app(engine: QwenEmbeddingEngine | None = None):
    try:
        from fastapi import Body, FastAPI, HTTPException
        from pydantic import BaseModel, Field, ValidationError
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI dependencies are missing; run `uv sync --extra embedding`"
        ) from exc

    active_engine = engine or QwenEmbeddingEngine(
        model_name=os.environ.get("QWEN_EMBEDDING_MODEL", DEFAULT_MODEL),
        device_name=os.environ.get("QWEN_EMBEDDING_DEVICE", "auto"),
    )
    app = FastAPI(title="DailyInfo Qwen3 Embedding Service", version="1")

    class EmbeddingRequest(BaseModel):
        texts: list[str] = Field(min_length=1, max_length=1000)
        input_type: Literal["query", "document"] = "document"
        instruction: str = ""
        dimension: int = Field(default=1024, ge=32, le=1024)
        batch_size: int = Field(default=8, ge=1, le=128)
        max_length: int = Field(default=2048, ge=32, le=32768)

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "model": active_engine.model_name,
            "device": active_engine.device,
            "loaded": active_engine._model is not None,
        }

    @app.post("/v1/embeddings")
    def embeddings(payload: dict = Body(...)):
        try:
            request = EmbeddingRequest.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(422, exc.errors()) from exc
        texts = request.texts
        if request.input_type == "query":
            if not request.instruction.strip():
                raise HTTPException(400, "query input requires an instruction")
            texts = [detailed_instruction(request.instruction, text) for text in texts]
        started = time.perf_counter()
        try:
            vectors = active_engine.encode(
                texts,
                dimension=request.dimension,
                batch_size=request.batch_size,
                max_length=request.max_length,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(500, str(exc)) from exc
        return {
            "model": active_engine.model_name,
            "device": active_engine.device,
            "dimension": request.dimension,
            "embeddings": vectors,
            "elapsed_seconds": time.perf_counter() - started,
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--device", default="auto", choices=("auto", "mps", "cuda", "cpu")
    )
    parser.add_argument("--preload", action="store_true")
    args = parser.parse_args()
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("run `uv sync --extra embedding` first") from exc
    engine = QwenEmbeddingEngine(args.model, args.device)
    if args.preload:
        engine.load()
    uvicorn.run(create_app(engine), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
