"""
英語・日本語のメッセージの対応表（elbx/messages.py）と、言語の決め方のテスト。

出力はゴールデンテスト（tests/casebook_elbx_ja.yaml / casebook_elbx_en.yaml）で日英とも全件を
確かめる。ここでは対応表そのもの（キーと {…} の穴がそろうこと、すべてのキーを使っていること）を
機械的に確かめる（gettext の `msgfmt --check` と同じ考え方）。
"""

from __future__ import annotations

import ast
import re
import string
from pathlib import Path

import pytest

import elbx
from elbx import ELBError, cli, get_language, set_language
from elbx import messages as messages_module
from elbx.messages import LANGUAGES, MESSAGES, detect_language

_JAPANESE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uff01-\uff5e]")
_PACKAGE = Path(elbx.__file__).resolve().parent


def _placeholders(text: str) -> set[str]:
    """`{…}` の穴の名前を返す。"""
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


def _string_constants_outside_messages() -> list[tuple[str, int, str]]:
    """messages.py 以外の elbx のソースにある文字列の定数（ファイル名・行・値）を返す。"""
    found: list[tuple[str, int, str]] = []
    for path in sorted(_PACKAGE.glob("*.py")):
        if path.name == "messages.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found += [
            (path.name, node.lineno, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
    return found


# =============================================================================
# 対応表
# =============================================================================


def test_languages_have_same_keys_and_placeholders() -> None:
    """en と ja で、キーと {…} の穴の名前がそろっている。"""
    assert set(MESSAGES) == set(LANGUAGES)
    assert set(MESSAGES["en"]) == set(MESSAGES["ja"])
    different = {
        key: (_placeholders(MESSAGES["en"][key]), _placeholders(MESSAGES["ja"][key]))
        for key in MESSAGES["en"]
        if _placeholders(MESSAGES["en"][key]) != _placeholders(MESSAGES["ja"][key])
    }
    assert different == {}


def test_english_messages_have_no_japanese() -> None:
    """英語の文に日本語（全角の記号を含む）が混ざっていない。"""
    mixed = {key: text for key, text in MESSAGES["en"].items() if _JAPANESE.search(text)}
    assert mixed == {}


@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_message_formats_with_its_placeholders(lang: str) -> None:
    """穴に値を入れて埋められる（番号の穴 `{}` や、閉じていない `{` がない）。"""
    for key, text in MESSAGES[lang].items():
        names = _placeholders(text)
        assert "" not in names, key
        assert text.format(**dict.fromkeys(names, "x")), key


def test_every_key_is_used_and_every_used_key_exists() -> None:
    """表のキーはすべてコードで使っていて、コードで使うキーはすべて表にある。"""
    keys = set(MESSAGES["en"])
    key_like = re.compile(r"^(cli|client|fmt|log|model|sim)\.[a-z_.]+$")
    constants = _string_constants_outside_messages()
    used = {value for _, _, value in constants if value in keys}
    missing = sorted(
        f"{f}:{n}: {v}" for f, n, v in constants if key_like.match(v) and v not in keys
    )
    assert missing == []
    assert sorted(keys - used) == []


def test_no_japanese_text_outside_messages() -> None:
    """利用者に見える日本語の文は、messages.py の外に書かない（docstring とコメントは除く）。"""
    found = []
    for path in sorted(_PACKAGE.glob("*.py")):
        if path.name == "messages.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        found += [
            f"{path.name}:{node.lineno}: {node.value[:40]}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _JAPANESE.search(node.value)
        ]
    assert found == []


# =============================================================================
# 言語の決め方
# =============================================================================


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"ELBX_LANG": "ja"}, "ja"),
        ({"ELBX_LANG": "en", "LANG": "ja_JP.UTF-8"}, "en"),
        ({"LANG": "ja_JP.UTF-8"}, "ja"),
        ({"LANG": "ja"}, "ja"),
        ({"LANG": "en_US.UTF-8"}, "en"),
        ({"LANG": "C.UTF-8"}, "en"),
        ({"LANG": "fr_FR.UTF-8"}, "en"),
        ({"LC_ALL": "C", "LANG": "ja_JP.UTF-8"}, "en"),
        ({"LC_MESSAGES": "ja_JP.UTF-8", "LANG": "en_US.UTF-8"}, "ja"),
        ({"LC_ALL": "", "LANG": "ja_JP.UTF-8"}, "ja"),
        ({"ELBX_LANG": "", "LANG": "ja_JP.UTF-8"}, "ja"),
    ],
    ids=[
        "elbx_lang",
        "elbx_lang_wins",
        "lang_ja",
        "lang_ja_short",
        "lang_en",
        "lang_c",
        "lang_other",
        "lc_all_wins",
        "lc_messages_before_lang",
        "empty_is_unset",
        "empty_elbx_lang_is_unset",
    ],
)
def test_detect_language_from_environment(environ: dict[str, str], expected: str) -> None:
    """ELBX_LANG → LC_ALL → LC_MESSAGES → LANG の順に見て、日本語なら ja、それ以外は en。"""
    assert detect_language(environ) == expected


@pytest.mark.parametrize(
    "os_language", ["ja", "en", None], ids=["windows_ja", "windows_en", "other"]
)
def test_detect_language_falls_back_to_the_os(
    monkeypatch: pytest.MonkeyPatch, os_language: str | None
) -> None:
    """ロケールの環境変数がなければ Windows の表示言語、それもなければ英語。"""
    monkeypatch.setattr(messages_module, "_windows_ui_language", lambda: os_language)
    assert detect_language({}) == (os_language or "en")


def test_invalid_elbx_lang_is_an_error() -> None:
    """ELBX_LANG の誤りは、黙って英語にせずエラーにする。"""
    with pytest.raises(ELBError, match="ELBX_LANG must be one of: en, ja"):
        detect_language({"ELBX_LANG": "fr"})


def test_set_language_overrides_the_environment() -> None:
    """set_language() で選んだ言語は環境より優先し、None で環境に戻る。"""
    set_language("en")
    assert get_language() == "en"
    set_language(None)
    assert get_language() == "ja"  # conftest.py が ELBX_LANG=ja にしている
    with pytest.raises(ELBError, match="unsupported language"):
        set_language("fr")


# =============================================================================
# CLI の --lang
# =============================================================================


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--lang", "en", "--help"], "as a tree"),
        (["--lang", "ja", "--help"], "ツリーで表示する"),
        (["ls", "--lang", "en", "--help"], "print JSON"),
    ],
    ids=["en", "ja", "after_subcommand"],
)
def test_help_is_in_the_chosen_language(
    argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """ヘルプも --lang の言語で出る（サブコマンドの後ろに書いても効く）。"""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv)
    assert exc_info.value.code == 0
    assert expected in capsys.readouterr().out


def test_invalid_lang_option_is_an_argparse_error(capsys: pytest.CaptureFixture[str]) -> None:
    """--lang の誤りは argparse のエラー（終了コード 2）。"""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--lang", "fr", "ls"])
    assert exc_info.value.code == 2
    assert "--lang" in capsys.readouterr().err


def test_invalid_elbx_lang_in_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI でも ELBX_LANG の誤りは、英語の1行のエラー（終了コード 1）。"""
    monkeypatch.setenv("ELBX_LANG", "fr")
    assert cli.main(["ls"]) == 1
    assert capsys.readouterr().err == "Error: ELBX_LANG must be one of: en, ja (got 'fr')\n"


def test_cli_lang_does_not_stay_after_main(
    elb_client: elbx.ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """--lang は main() の間だけ効き、同じプロセスのライブラリの言語には残らない。"""
    assert cli.main(["--lang", "en", "tree", "--search", "no-such-lb"], client=elb_client) == 0
    assert capsys.readouterr().out == "No load balancers found.\n"
    assert get_language() == "ja"
