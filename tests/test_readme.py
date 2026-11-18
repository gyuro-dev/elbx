"""
README の例が、いまの実装と合っているかを確かめる（README の例は手で貼っているため、
実装を変えたときに直し忘れやすい）。

- 出力の例: README の `text` のブロックの各行が、同じコマンドを moto の構成
  （tests/data/infra.yaml）で実行した出力に、同じ順で出ること。README には行や列を手で
  間引いた例があるので、丸ごとは比べない。空白の幅と、実行のたびに変わる ID・日時は、
  そろえてから比べる。`...` / `…` で間引いた行は比べない。障害時の例もあるので、
  ケースファイルの states のどれかで合えばよい
- Python の例: README の `python` のブロックが `mypy --strict` を通ること（README に
  「そのまま通る」と書いてある）。前のブロックで作った `client` などを使うので、それらの型を
  宣言してから、ブロックごとに確かめる
"""

from __future__ import annotations

import html
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_casebook
from mypy import api as mypy_api
from pytest_casebook import run_in_process

from elbx import cli

from .harness import TestEnv, apply_health_overrides

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
READMES = {"en": ROOT / "README.md", "ja": ROOT / "README.ja.md"}

# 実行のたびに変わるもの（moto の ID・日時・アカウント）を、どちらの側でも同じ記号にする
_VOLATILE = [
    (re.compile(r"\b(?:vpc|subnet|sg|eni|i|igw|rtb|acl)-[0-9a-f]{8,17}\b"), "<id>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{16,32}\b"), "<hex>"),
    (re.compile(r"\b\d{12}\b"), "<account>"),
    (
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?)?"
        ),
        "<time>",
    ),
    (re.compile(r"\b[A-Z0-9]{14}\b"), "<zone>"),  # Route 53 のゾーン ID
    (re.compile(r"\b\d+ ms\b"), "<ms>"),  # -vv の API の所要時間
    (re.compile(r"\s+"), " "),
]

# 間引いた行の印
_ELLIPSIS = ("...", "…")

# 表の見出しの下の線（README は列を間引いた例があり、線の長さが合わないので比べない）
_TABLE_RULE = re.compile(r"^[-\s]+$")


@dataclass
class Example:
    """README の出力の例 1 つ。"""

    lang: str
    line: int
    command: str
    expected: list[str]

    @property
    def id(self) -> str:
        return f"{self.lang}:{self.line}:{self.command}"


def _normalize(line: str) -> str:
    """比べるために、空白の幅と、実行のたびに変わる ID・日時をそろえる。"""
    for pattern, repl in _VOLATILE:
        line = pattern.sub(repl, line)
    return line.strip()


def _examples(lang: str) -> list[Example]:
    """
    README の出力の例を取り出す。コマンドは、ブロックの最初の行の `$ elbx …` か、ブロックの前の
    `<summary>Example: <code>elbx …</code>` から取る。コマンドのないブロックは対象にしない。
    """
    lines = READMES[lang].read_text(encoding="utf-8").splitlines()
    examples: list[Example] = []
    pending: str | None = None
    i = 0
    while i < len(lines):
        summary = re.search(r"<summary>[^<]*<code>(elbx [^<]+)</code>", lines[i])
        if summary:
            pending = html.unescape(summary.group(1))
        fence = re.match(r"^\s*```(\w*)\s*$", lines[i])
        if fence:
            start = i + 1
            end = next((j for j in range(start, len(lines)) if lines[j].strip() == "```"), None)
            if end is None:
                raise AssertionError(f"{READMES[lang].name}:{i + 1}: ``` のブロックが閉じていない")
            # `<summary>` のコマンドは直後のブロックだけに使う（text 以外のブロックでも捨てる）
            command, pending = pending, None
            if fence.group(1) == "text":
                block = lines[start:end]
                if block and block[0].startswith("$ elbx "):
                    command, block = block[0][2:], block[1:]
                if command:
                    examples.append(Example(lang, start + 1, command, block))
            i = end
        i += 1
    return examples


_ALL_EXAMPLES = [example for lang in READMES for example in _examples(lang)]


def test_readmes_have_examples() -> None:
    """例を取り出せている（README の書き方が変わって、何も比べていないことがないように）。"""
    for lang in READMES:
        assert len([e for e in _ALL_EXAMPLES if e.lang == lang]) >= 10


def _run(env: TestEnv, lang: str, argv: list[str], state: dict[str, object]) -> str:
    """moto の構成でコマンドを実行し、標準出力と標準エラー出力（-v のログ）を返す。"""
    client = env.client()
    apply_health_overrides(client, env, state)  # type: ignore[arg-type]
    result = run_in_process(
        lambda a: cli.main(a, client=client), ["--lang", lang, *argv, "--no-color"]
    )
    return f"{result.stdout}\n{result.stderr}"


def _missing_in_order(expected: list[str], actual: list[str]) -> list[str]:
    """
    `expected` の行のうち、`actual` に同じ順で現れないものを返す（間引いた行は飛ばしてよい）。

    :param expected: README の行（そろえたもの）
    :param actual: 実際の出力の行（そろえたもの）
    """
    missing: list[str] = []
    position = 0
    for line in expected:
        try:
            position = actual.index(line, position) + 1
        except ValueError:
            missing.append(line)
    return missing


@pytest.mark.parametrize("example", _ALL_EXAMPLES, ids=lambda e: e.id)
def test_output_example_matches_the_implementation(moto_env: TestEnv, example: Example) -> None:
    """README の出力の例の各行が、同じコマンドの実際の出力に、同じ順で出る。"""
    argv = shlex.split(example.command)[1:]
    expected = [
        _normalize(line)
        for line in example.expected
        if line.strip()
        and not any(mark in line for mark in _ELLIPSIS)
        and not _TABLE_RULE.match(line)
    ]
    doc = pytest_casebook.load(TESTS / f"casebook_elbx_{example.lang}.yaml", ini=True)
    states = doc.states or {"default": {}}
    best: list[str] | None = None
    for name in sorted(states, key=lambda n: n != "default"):
        output = _run(moto_env, example.lang, argv, states[name])
        missing = _missing_in_order(expected, [_normalize(line) for line in output.splitlines()])
        if not missing:
            return
        if best is None or len(missing) < len(best):
            best = missing
    pytest.fail(
        f"{READMES[example.lang].name}:{example.line} の例（{example.command}）に、実際の出力に"
        "ない行があります:\n" + "\n".join(best or [])
    )


# 前の Python の例で作ったものの型（ブロックごとに確かめるので、先に宣言する）
_PRELUDE = """\
from elbx import ELBClient, LoadBalancerView, RouteSimulationResult

client: ELBClient
lb: LoadBalancerView
result: RouteSimulationResult
"""


def _python_blocks(lang: str) -> list[tuple[int, str]]:
    """README の `python` のブロック（行番号, 中身）を返す。"""
    text = READMES[lang].read_text(encoding="utf-8")
    return [
        (text.count("\n", 0, m.start()) + 2, m.group(1))
        for m in re.finditer(r"^```python\n(.*?)^```", text, flags=re.M | re.S)
    ]


def test_python_examples_pass_mypy_strict(tmp_path: Path) -> None:
    """README の Python の例が `mypy --strict` を通る。"""
    files: dict[Path, str] = {}
    for lang in READMES:
        blocks = _python_blocks(lang)
        assert blocks
        for line, code in blocks:
            path = tmp_path / f"readme_{lang}_{line}.py"
            path.write_text(_PRELUDE + "\n" + code, encoding="utf-8")
            files[path] = f"{READMES[lang].name}:{line}"
    # elbx の依存ではないライブラリ（pandas のレシピ）の型の情報は、入っていなくてもよい
    stdout, stderr, status = mypy_api.run(
        [
            "--strict",
            "--disable-error-code=import-untyped",
            "--disable-error-code=import-not-found",
            "--cache-dir",
            str(tmp_path / ".mypy"),
            *map(str, files),
        ]
    )
    for path, where in files.items():
        stdout = stdout.replace(str(path), where)
    assert status == 0, stdout + stderr
