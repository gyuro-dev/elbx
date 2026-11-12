"""
A local playground to try the output of elbx without an AWS account.

It builds the test configuration (tests/data/infra.yaml) on moto (an in-memory mock AWS)
and runs elbx commands against it. Nothing is sent to real AWS.

Usage:

1. See the representative cases (the cases with meta.demo: true in the case file):
   uv run python playground.py

2. Run a case by name (prints the same as its golden file):
   uv run python playground.py --case route_nlb_to_alb
   uv run python playground.py --list                     # list the cases

3. Run any command (--state picks a health state):
   uv run python playground.py tree prod-web-alb
   uv run python playground.py --full                     # without a subcommand: tree
   uv run python playground.py --state incident targets --unhealthy
   uv run python playground.py route "https://api.elbx-test.internal/x?mode=fast" -X POST \\
       -H "X-Env: ip-direct" --source-ip 203.0.113.10

The language (--lang en / --lang ja, then ELBX_LANG, then the OS language, as elbx itself)
chooses the language of the help and the output, and the case file:
tests/casebook_elbx_en.yaml or tests/casebook_elbx_ja.yaml (the same cases; the golden files
are in tests/golden/casebook_elbx_<lang>/).

To change the configuration, edit tests/data/infra.yaml; to change the health states, edit
the states of both case files.

AWS アカウントなしで elbx の出力を手元で試せるプレイグラウンド。

テストと同じ構成（tests/data/infra.yaml）を moto（インメモリの擬似 AWS）上に作り、
elbx のコマンドを実行する。実際の AWS には一切通信しない。

使い方:

1. 代表的なケースをまとめて見る（ケースファイルの meta.demo: true のケース）:
   uv run python playground.py

2. 確認ケースを名前で実行する（期待出力と同じ内容が出る）:
   uv run python playground.py --case route_nlb_to_alb
   uv run python playground.py --list                     # ケースの一覧

3. 任意のコマンドを実行する（--state で障害状態を指定できる）:
   uv run python playground.py tree prod-web-alb
   uv run python playground.py --full                     # サブコマンドを省略すると tree
   uv run python playground.py --state incident targets --unhealthy
   uv run python playground.py route "https://api.elbx-test.internal/x?mode=fast" -X POST \\
       -H "X-Env: ip-direct" --source-ip 203.0.113.10

言語（elbx と同じく --lang en / --lang ja、次に ELBX_LANG、次に OS の言語）で、ヘルプと出力の
言語、読むケースファイルが決まる: tests/casebook_elbx_en.yaml か tests/casebook_elbx_ja.yaml
（同じケース。期待出力は tests/golden/casebook_elbx_<言語>/）。

構成を変えたいときは tests/data/infra.yaml、障害状態を変えたいときは
両方のケースファイルの states を編集する。
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

import pytest_casebook
from pytest_casebook import Case, CasebookDoc

from elbx import cli
from elbx.errors import ELBError
from elbx.messages import LANGUAGES, MESSAGES, get_language, using_language
from tests import conftest
from tests.harness import TestEnv, apply_health_overrides, moto_environment

TESTS = Path(__file__).resolve().parent / "tests"
# 言語ごとのケースファイル（同じケースで、説明と出力の言語だけが違う）
CASEBOOKS = {lang: TESTS / f"casebook_elbx_{lang}.yaml" for lang in LANGUAGES}

# プレイグラウンドが出す文（elbx 本体の messages.py には入れない。開発用のスクリプトのため）
TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "description": "run elbx on a mock AWS on moto (nothing is sent to real AWS)",
        "state": "health state (the states of {file})",
        "case": "run a case of {file} by name",
        "list": "list the cases",
        "lang": (
            "language of the output and of the cases: en or ja "
            "(default: ELBX_LANG, then the OS language)"
        ),
        "no_case": "error: no case '{name}' in {file} (see --list)",
        "banner": "nothing is sent to real AWS (moto, an in-memory mock)",
    },
    "ja": {
        "description": "moto 上の擬似 AWS で elbx を実行する（実際の AWS には通信しない）",
        "state": "ヘルス状態（{file} の states）",
        "case": "{file} のケースを名前で実行する",
        "list": "ケースの一覧を出す",
        "lang": "出力とケースの言語: en か ja（既定: ELBX_LANG、なければ OS の言語）",
        "no_case": "エラー: ケース '{name}' は {file} にありません（--list で一覧）",
        "banner": "実際の AWS には一切通信していません（moto のインメモリの擬似環境）",
    },
}


def _print_banner(title: str) -> None:
    """
    Print a section banner.

    セクションの見出しのバナーを出す。

    :param title: the title in the banner / バナーに出すタイトル
    """
    bar = "=" * 88
    print(f"\n\033[1;36m{bar}\n  {title}\n{bar}\033[0m\n")


def _run_cases(doc: CasebookDoc, env: TestEnv, cases: list[Case]) -> None:
    """
    Run the cases one by one and print them in the golden-file format.

    `run_case()` of pytest-casebook runs them and replaces the IDs, with the same hooks of
    tests/conftest.py as the tests, so the output is the same as the golden file
    (tests/golden/casebook_elbx_<lang>/<name>.txt). Whether the description is written
    follows the casebook_* options of pyproject.toml read by `load(ini=True)`.

    ケースを順に実行し、期待出力と同じ形で出す。

    実行と ID の置き換えは pytest-casebook の `run_case()` が、テストと同じ tests/conftest.py の
    hook で行う（そのため、期待出力 tests/golden/casebook_elbx_<言語>/<name>.txt と同じ内容に
    なる）。description を書くかどうかは、`load(ini=True)` で読んだ pyproject.toml の
    casebook_* に従う。

    :param doc: the case file read with `load(ini=True)` / `load(ini=True)` で読んだケースファイル
    :param env: the test environment on moto / moto 上のテスト環境
    :param cases: the cases to run / 実行するケース
    """
    for case in cases:
        _print_banner(f"{case.name}: {case.description}")
        print(pytest_casebook.run_case(doc, case, env=env, hooks=conftest), end="")


def main(argv: list[str] | None = None) -> int:
    """
    Entry point of the playground. Arguments that are not the playground's own are passed
    to elbx.

    プレイグラウンドのエントリポイント。プレイグラウンド自身のもの以外の引数は elbx に渡す。

    :param argv: command line arguments (default `sys.argv[1:]`)
        / コマンドライン引数（省略時は `sys.argv[1:]`）
    :return: exit code: 0 on success, 1 on error / 終了コード（0 = 正常、1 = エラー）
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8")

    # ヘルプの文と読むケースファイルを決めるため、elbx と同じく --lang だけを先に読む
    lang = cli._lang_option(sys.argv[1:] if argv is None else argv)
    with using_language(lang if lang in LANGUAGES else None):
        try:
            lang = get_language()
        except ELBError as exc:
            print(MESSAGES["en"]["cli.error"].format(message=exc), file=sys.stderr)
            return 1
        return _run(argv, lang)


def _run(argv: list[str] | None, lang: str) -> int:
    """
    The body of `main()`, after the language is chosen.

    `main()` の本体（言語を決めたあと）。

    :param argv: command line arguments / コマンドライン引数
    :param lang: the language of the output and of the cases / 出力とケースの言語
    :return: exit code / 終了コード
    """
    text = TEXTS[lang]
    casebook = CASEBOOKS[lang]
    file = casebook.name
    doc = pytest_casebook.load(casebook, ini=True)
    parser = argparse.ArgumentParser(prog="playground.py", description=text["description"])
    parser.add_argument(
        "--state",
        default="default",
        choices=sorted(doc.states or {}),
        help=text["state"].format(file=file),
    )
    parser.add_argument("--case", help=text["case"].format(file=file))
    parser.add_argument("--list", action="store_true", help=text["list"])
    parser.add_argument("--lang", choices=LANGUAGES, help=text["lang"])
    args, elbx_args = parser.parse_known_args(argv)

    cases_by_name = {c.name: c for c in doc.cases}
    if args.list:
        for case in doc.cases:
            print(f"{case.name:<34} {case.description}")
        return 0
    if args.case and args.case not in cases_by_name:
        print(text["no_case"].format(name=args.case, file=file), file=sys.stderr)
        return 1

    with moto_environment() as env:
        if args.case:
            _run_cases(doc, env, [cases_by_name[args.case]])
            return 0
        if not elbx_args:
            print(f"\033[1;32m[elbx playground]\033[0m {text['banner']}")
            _run_cases(doc, env, [c for c in doc.cases if c.meta.get("demo")])
            return 0

        client = env.client()
        apply_health_overrides(client, env, (doc.states or {})[args.state])
        return cli.main(["--lang", lang, *elbx_args], client=client)


if __name__ == "__main__":
    sys.exit(main())
