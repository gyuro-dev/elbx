"""
日本語の出力のケース（tests/casebook_elbx_ja.yaml）と英語の出力のケース（tests/casebook_elbx_en.yaml）が
ずれていないことを確かめる。

2 つの YAML は、説明の文の言語だけが違い、ケースそのものは同じ（二重管理）。片方だけにケースを
足す・コマンドを変える、といったずれを、1 件ずつ目で比べずにここで機械的に見つける。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import pytest_casebook
from pytest_casebook import CasebookDoc

_TESTS = Path(__file__).resolve().parent
_JAPANESE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uff01-\uff5e]")


@pytest.fixture(scope="module")
def docs() -> dict[str, CasebookDoc]:
    """2 つの YAML を読む（言語 → CasebookDoc）。"""
    return {
        lang: pytest_casebook.load(_TESTS / f"casebook_elbx_{lang}.yaml") for lang in ("ja", "en")
    }


def _cases(doc: CasebookDoc) -> list[tuple[Any, ...]]:
    """言語によらないケースの中身（グループは何番目か、で比べる。名前は言語ごとに違うため）。"""
    groups: list[str] = []
    rows = []
    for case in doc.cases:
        group = case.groups[0].name if case.groups else ""
        if group not in groups:
            groups.append(group)
        env = {k: v for k, v in case.env.items() if k != "ELBX_LANG"}
        rows.append(
            (case.name, groups.index(group), case.command, case.state, case.marks, case.meta, env)
        )
    return rows


def test_both_files_have_the_same_cases(docs: dict[str, CasebookDoc]) -> None:
    """ケース名・グループ・コマンド・状態・marks・meta・env が、同じ順で同じ。"""
    assert _cases(docs["en"]) == _cases(docs["ja"])


def test_both_files_have_the_same_settings(docs: dict[str, CasebookDoc]) -> None:
    """ヘルス状態（states）とコマンドの前に付けるもの（command_prefix）が同じ。"""
    assert docs["en"].states == docs["ja"].states
    assert docs["en"].command_prefix == docs["ja"].command_prefix


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_each_file_runs_elbx_in_its_language(docs: dict[str, CasebookDoc], lang: str) -> None:
    """それぞれの YAML は、出力の言語を自分の言語に固定している。"""
    assert {case.env.get("ELBX_LANG") for case in docs[lang].cases} == {lang}


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_each_file_reports_in_its_language(docs: dict[str, CasebookDoc], lang: str) -> None:
    """それぞれの YAML の lang:（試験項目表の枠と失敗のメッセージの言語）も、自分の言語。"""
    assert docs[lang].lang == lang


def test_descriptions_are_in_the_language_of_the_file(docs: dict[str, CasebookDoc]) -> None:
    """英語の YAML の文には日本語がなく、日本語の YAML の説明には日本語がある。"""
    en, ja = docs["en"], docs["ja"]
    english_texts = [en.title, en.description, *en.levels]
    english_texts += [c.description for c in en.cases]
    english_texts += [g.name + " " + g.description for c in en.cases for g in c.groups]
    assert [text for text in english_texts if _JAPANESE.search(text)] == []
    assert [c.name for c in ja.cases if not _JAPANESE.search(c.description)] == []
