"""Benchmark Qwen3 embedding retrieval against a read-only DailyInfo baseline."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import statistics
import time

from embedding_retrieval import (
    DEFAULT_QUERY_INSTRUCTION,
    DEFAULT_QUERY_TEXT,
    DOMAIN_SPECIFIC_QUERY_INSTRUCTION,
    EmbeddingRetrievalConfig,
    QwenEmbeddingClient,
    cosine_similarity,
    paper_text,
)


INSTRUCTION_PRESETS = {
    "domain_specific": DOMAIN_SPECIFIC_QUERY_INSTRUCTION,
    "concise": DEFAULT_QUERY_INSTRUCTION,
    "chinese": "检索与水文、水资源、降雨径流、洪水和地球系统预测直接相关的机器学习会议论文。",
}


def _load_baseline(db_path: Path, source: str, limit: int | None = None):
    """Load the fullest stored sync run without changing the baseline DB."""

    uri = f"file:{db_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            """SELECT * FROM sync_runs WHERE source=?
               ORDER BY scanned_count DESC, updated_ms DESC LIMIT 1""",
            (source,),
        ).fetchone()
        if not run:
            raise ValueError(f"no baseline sync run found for {source}")
        query = (
            "SELECT s.forum_id,s.paper_json,p.relevance_json "
            "FROM sync_items s LEFT JOIN papers p "
            "ON p.source=s.source AND p.forum_id=s.forum_id "
            "WHERE s.run_id=? ORDER BY s.forum_id"
        )
        rows = conn.execute(query, [run["run_id"]]).fetchall()
    items = []
    for row in rows:
        paper = json.loads(row["paper_json"])
        relevance = json.loads(row["relevance_json"]) if row["relevance_json"] else {}
        items.append((paper, bool(relevance.get("relevant"))))
    if limit and len(items) > limit:
        positives = [item for item in items if item[1]]
        negatives = [item for item in items if not item[1]]
        items = positives + negatives[: max(0, limit - len(positives))]
    papers = [paper for paper, _relevant in items]
    baseline_relevant = {paper["forum_id"] for paper, relevant in items if relevant}
    return papers, baseline_relevant, dict(run)


def _metrics(selected: set[str], baseline: set[str]) -> dict:
    overlap = selected & baseline
    return {
        "selected": len(selected),
        "baseline_relevant": len(baseline),
        "overlap": len(overlap),
        "new_vs_baseline": len(selected - baseline),
        "missed_vs_baseline": len(baseline - selected),
        "precision_vs_baseline": len(overlap) / len(selected) if selected else 0.0,
        "recall_vs_baseline": len(overlap) / len(baseline) if baseline else None,
    }


def _write_markdown(result: dict, path: Path) -> None:
    lines = [
        "# Qwen3 Embedding Retrieval Comparison",
        "",
        f"- Source: `{result['source']}`",
        f"- Papers: {result['paper_count']}",
        f"- Baseline relevant: {result['baseline_relevant_count']}",
        f"- Model: `{result['config']['model']}`",
        "",
        "## Retrieval grid",
        "",
        "| instruction | text | dim | threshold | selected | overlap | new | missed | precision | recall |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["grid"]:
        metrics = row["metrics"]
        recall = metrics["recall_vs_baseline"]
        recall_text = f"{recall:.3f}" if recall is not None else "—"
        lines.append(
            f"| {row['instruction']} | {row['text_mode']} | {row['dimension']} | "
            f"{row['threshold']:.3f} | {metrics['selected']} | {metrics['overlap']} | "
            f"{metrics['new_vs_baseline']} | {metrics['missed_vs_baseline']} | "
            f"{metrics['precision_vs_baseline']:.3f} | "
            f"{recall_text} |"
        )
    lines.extend(
        [
            "",
            "## Default comparison",
            "",
            f"Variant: `{result['default_comparison']['variant']}` at threshold "
            f"`{result['default_comparison']['threshold']}`.",
            "",
            f"- New versus baseline: {result['default_comparison']['metrics']['new_vs_baseline']}",
            f"- Missed versus baseline: {result['default_comparison']['metrics']['missed_vs_baseline']}",
            f"- Recall versus baseline: {result['default_comparison']['metrics']['recall_vs_baseline']}",
            "",
            "### New candidates",
            "",
        ]
    )
    for item in result["default_comparison"]["new_vs_baseline"]:
        lines.append(f"- `{item['score']:.4f}` {item['title']} (`{item['forum_id']}`)")
    lines.extend(["", "### Missed baseline papers", ""])
    for item in result["default_comparison"]["missed_vs_baseline"]:
        lines.append(f"- `{item['score']:.4f}` {item['title']} (`{item['forum_id']}`)")
    lines.extend(
        [
            "",
            "## Batch-size timing",
            "",
            "| batch | items | seconds | items/s |",
            "|---:|---:|---:|---:|",
        ]
    )
    for row in result["batch_timing"]:
        lines.append(
            f"| {row['batch_size']} | {row['items']} | {row['seconds']:.3f} | "
            f"{row['items_per_second']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "The lexical/DeepSeek branch is used as a comparison baseline, not ground truth. "
            "New candidates and missed candidates require manual review before fixing a production threshold.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_benchmark(args) -> dict:
    papers, baseline, run = _load_baseline(args.state_db, args.source, args.limit)
    base_config = EmbeddingRetrievalConfig(
        endpoint=args.endpoint,
        model=args.model,
        query_text=args.query_text,
        query_instruction=DEFAULT_QUERY_INSTRUCTION,
        text_mode="title_abstract_keywords",
        dimension=1024,
        threshold=0.5,
        batch_size=args.batch_sizes[0],
        max_length=args.max_length,
        timeout_seconds=args.timeout,
    )
    client = QwenEmbeddingClient(base_config)
    paper_ids = [paper["forum_id"] for paper in papers]
    documents_by_mode: dict[str, list[list[float]]] = {}
    embedding_seconds: dict[str, float] = {}
    for mode in args.text_modes:
        texts = [paper_text(paper, mode) for paper in papers]
        print(f"embedding {len(texts)} papers with text_mode={mode}", flush=True)
        started = time.perf_counter()
        documents_by_mode[mode] = client.embed_documents(
            texts, dimension=1024, batch_size=args.batch_sizes[0]
        )
        embedding_seconds[mode] = time.perf_counter() - started

    query_embeddings = {
        name: client.embed_query(args.query_text, instruction, dimension=1024)
        for name, instruction in INSTRUCTION_PRESETS.items()
        if name in args.instructions
    }
    grid = []
    scored_variants: dict[str, list[float]] = {}
    for instruction_name, query_vector in query_embeddings.items():
        for mode, document_vectors in documents_by_mode.items():
            for dimension in args.dimensions:
                scores = [
                    cosine_similarity(query_vector, vector, dimension)
                    for vector in document_vectors
                ]
                variant = f"{instruction_name}:{mode}:{dimension}"
                scored_variants[variant] = scores
                for threshold in args.thresholds:
                    selected = {
                        forum_id
                        for forum_id, score in zip(paper_ids, scores, strict=True)
                        if score >= threshold
                    }
                    grid.append(
                        {
                            "instruction": instruction_name,
                            "text_mode": mode,
                            "dimension": dimension,
                            "threshold": threshold,
                            "score_mean": statistics.fmean(scores) if scores else None,
                            "metrics": _metrics(selected, baseline),
                        }
                    )

    timing_texts = [
        paper_text(paper, "title_abstract_keywords")
        for paper in papers[: args.batch_benchmark_items]
    ]
    batch_timing = []
    for batch_size in args.batch_sizes:
        started = time.perf_counter()
        client.embed_documents(timing_texts, dimension=1024, batch_size=batch_size)
        elapsed = time.perf_counter() - started
        batch_timing.append(
            {
                "batch_size": batch_size,
                "items": len(timing_texts),
                "seconds": elapsed,
                "items_per_second": len(timing_texts) / elapsed if elapsed else 0.0,
            }
        )

    default_variant = (
        f"{args.comparison_instruction}:{args.comparison_text_mode}:"
        f"{args.comparison_dimension}"
    )
    default_scores = scored_variants.get(
        default_variant, next(iter(scored_variants.values()))
    )
    ranking = sorted(
        (
            {
                "forum_id": paper["forum_id"],
                "title": paper.get("title", ""),
                "score": score,
                "baseline_relevant": paper["forum_id"] in baseline,
            }
            for paper, score in zip(papers, default_scores, strict=True)
        ),
        key=lambda item: item["score"],
        reverse=True,
    )
    default_selected = {
        item["forum_id"]
        for item in ranking
        if item["score"] >= args.comparison_threshold
    }
    default_comparison = {
        "variant": default_variant,
        "threshold": args.comparison_threshold,
        "metrics": _metrics(default_selected, baseline),
        "new_vs_baseline": [
            item for item in ranking if item["forum_id"] in default_selected - baseline
        ][: args.report_top],
        "missed_vs_baseline": [
            item for item in ranking if item["forum_id"] in baseline - default_selected
        ],
    }
    return {
        "source": args.source,
        "paper_count": len(papers),
        "baseline_relevant_count": len(baseline),
        "baseline_run": {
            key: run.get(key)
            for key in (
                "run_id",
                "status",
                "scanned_count",
                "candidate_count",
                "relevant_count",
            )
        },
        "config": asdict(base_config),
        "embedding_seconds": embedding_seconds,
        "grid": grid,
        "batch_timing": batch_timing,
        "default_comparison": default_comparison,
        "top_default_ranking": ranking[: args.report_top],
    }


def _csv_values(value: str, cast):
    return [cast(item.strip()) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--source", default="openreview_iclr_2026")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765")
    parser.add_argument("--model", default="Qwen/Qwen3-Embedding-0.6B")
    parser.add_argument("--query-text", default=DEFAULT_QUERY_TEXT)
    parser.add_argument(
        "--instructions",
        type=lambda x: _csv_values(x, str),
        default=list(INSTRUCTION_PRESETS),
    )
    parser.add_argument(
        "--text-modes",
        type=lambda x: _csv_values(x, str),
        default=["title_only", "title_abstract", "title_abstract_keywords"],
    )
    parser.add_argument(
        "--dimensions", type=lambda x: _csv_values(x, int), default=[1024, 512, 256]
    )
    parser.add_argument(
        "--thresholds",
        type=lambda x: _csv_values(x, float),
        default=[0.35, 0.40, 0.45, 0.50, 0.55, 0.60],
    )
    parser.add_argument(
        "--batch-sizes", type=lambda x: _csv_values(x, int), default=[2, 4, 8, 16]
    )
    parser.add_argument("--batch-benchmark-items", type=int, default=128)
    parser.add_argument("--max-length", type=int, default=2048)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--report-top", type=int, default=100)
    parser.add_argument("--comparison-threshold", type=float, default=0.45)
    parser.add_argument("--comparison-instruction", default="concise")
    parser.add_argument("--comparison-text-mode", default="title_abstract")
    parser.add_argument("--comparison-dimension", type=int, default=512)
    args = parser.parse_args()
    unknown = set(args.instructions) - set(INSTRUCTION_PRESETS)
    if unknown:
        parser.error(f"unknown instruction presets: {sorted(unknown)}")
    if args.comparison_instruction not in args.instructions:
        parser.error("comparison instruction must be included in --instructions")
    if args.comparison_text_mode not in args.text_modes:
        parser.error("comparison text mode must be included in --text-modes")
    if args.comparison_dimension not in args.dimensions:
        parser.error("comparison dimension must be included in --dimensions")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = run_benchmark(args)
    (args.output_dir / "comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_markdown(result, args.output_dir / "comparison.md")
    print(args.output_dir / "comparison.md")


if __name__ == "__main__":
    main()
