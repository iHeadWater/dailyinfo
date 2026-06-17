#!/usr/bin/env python3
"""Polish a NotebookLM-generated MD using DeepSeek and the weekly-report skill."""

import argparse
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from paths import PROJECT_ROOT


def load_skill_prompt() -> str:
    skill_file = PROJECT_ROOT / "skills" / "weekly-report-skill.skill"
    if not skill_file.exists():
        raise FileNotFoundError(f"Skill file not found: {skill_file}")
    with zipfile.ZipFile(skill_file) as zf:
        with zf.open("weekly-report-skill/SKILL.md") as f:
            return f.read().decode("utf-8")


def polish(md_content: str, skill_prompt: str) -> str:
    import requests
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not set in .env")

    resp = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": skill_prompt},
                {"role": "user", "content": md_content},
            ],
            "temperature": 0.7,
            "max_tokens": 4096,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def main():
    parser = argparse.ArgumentParser(description="Polish WeChat article MD with DeepSeek")
    parser.add_argument("input", help="Input MD file path")
    parser.add_argument("-o", "--output", help="Output MD file (default: overwrite input)")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path

    print(f"[1/3] Loading skill prompt...")
    skill_prompt = load_skill_prompt()

    print(f"[2/3] Polishing {input_path.name} with DeepSeek...")
    md_content = input_path.read_text(encoding="utf-8")
    polished = polish(md_content, skill_prompt)

    output_path.write_text(polished, encoding="utf-8")
    print(f"[3/3] Saved: {output_path}")


if __name__ == "__main__":
    main()
