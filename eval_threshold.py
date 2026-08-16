"""Phase 6: calibrate SIM_THRESHOLD against real questions (PLAN.md 11).

Reads eval/questions.tsv (columns: label<TAB>question, label is in|out) and
prints the top-1 similarity for each. Put the threshold between the lowest
in-scope score and the highest out-of-scope score. When the two ranges overlap,
choose the lower end: wrongly answering "not found" is less harmful than
answering confidently from unrelated material.
"""

from __future__ import annotations

import sys
from pathlib import Path

import config
import retriever

QUESTIONS_PATH = config.BASE_DIR / "eval" / "questions.tsv"


def load_questions(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        label, question = parts[0].strip().lower(), parts[1].strip()
        if label in ("in", "out"):
            rows.append((label, question))
    return rows


def main() -> int:
    if not QUESTIONS_PATH.exists():
        print(
            f"{QUESTIONS_PATH} がありません。"
            "eval/questions.tsv.example をコピーして実 FAQ を記入してください",
            file=sys.stderr,
        )
        return 1

    questions = load_questions(QUESTIONS_PATH)
    if not questions:
        print("質問が読み込めませんでした（形式: label<TAB>question）", file=sys.stderr)
        return 1

    in_scores: list[float] = []
    out_scores: list[float] = []

    print(f"現在のしきい値: {config.SIM_THRESHOLD}\n")
    print(f"{'label':6} {'top1':>7}  question")
    print("-" * 70)

    for label, question in questions:
        hits = retriever.search_all(question, k=1)
        score = hits[0]["similarity"] if hits else 0.0
        (in_scores if label == "in" else out_scores).append(score)
        print(f"{label:6} {score:7.3f}  {question}")

    print("-" * 70)
    if in_scores:
        print(f"in-scope : min={min(in_scores):.3f} max={max(in_scores):.3f} ({len(in_scores)} 問)")
    if out_scores:
        print(f"out-scope: min={min(out_scores):.3f} max={max(out_scores):.3f} ({len(out_scores)} 問)")

    if in_scores and out_scores:
        low, high = max(out_scores), min(in_scores)
        if low < high:
            print(f"\n推奨レンジ: {low:.3f} 〜 {high:.3f} → 中間の {(low + high) / 2:.2f} 付近")
        else:
            print(
                f"\n分離できていません（out の最大 {low:.3f} >= in の最小 {high:.3f}）。"
                f"\n低め（{high:.2f} 付近）に置き、TOP_K とプロンプトで補ってください。"
                "\nチャンク設計の見直しも検討してください。"
            )
    print("\n決めた値を config.py の SIM_THRESHOLD に反映してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
