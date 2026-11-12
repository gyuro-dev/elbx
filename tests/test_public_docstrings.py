"""
公開 API の docstring が「英語の段落のあとに日本語の段落」になっていることを確かめる。

公開するものは英語と日本語の両方で書く決まり。抜け（英語だけ・日本語だけ・順番が逆）を
1 件ずつ目で探さずに、ここで機械的に見つける。
"""

from __future__ import annotations

import importlib
import inspect
import re
from types import ModuleType

import pytest

import elbx

# 日本語（ひらがな・カタカナ・漢字）を含むか
_JAPANESE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")

# モジュールの先頭の docstring を確かめるモジュール（利用者が読む・import するもの）
PUBLIC_MODULES = [
    "elbx",
    "elbx.cli",
    "elbx.client",
    "elbx.errors",
    "elbx.formatter",
    "elbx.messages",
    "elbx.models",
    "elbx.simulator",
]


def _public_objects() -> list[tuple[str, object]]:
    """`elbx.__all__` の関数・クラスと、`ELBClient` の公開メソッドを返す。"""
    objects: list[tuple[str, object]] = []
    for name in elbx.__all__:
        if name == "__version__":
            continue
        obj = getattr(elbx, name)
        # Literal の型（TargetKind など）は docstring を持てないので、関数とクラスだけを見る
        if not (inspect.isclass(obj) or inspect.isfunction(obj)):
            continue
        objects.append((name, obj))
        if name == "ELBClient":
            objects += [
                (f"ELBClient.{m}", fn)
                for m, fn in inspect.getmembers(obj, inspect.isfunction)
                if not m.startswith("_") or m == "__init__"
            ]
    return objects


def _check(doc: str | None) -> str | None:
    """
    docstring の形を確かめ、違っていれば理由を返す。

    :param doc: 確かめる docstring
    :return: 問題がなければ None、あれば理由
    """
    if not doc:
        return "docstring がない"
    # `:param` などより前の、説明の部分だけを段落に分ける（引数の説明は英語側だけでもよい）
    body = re.split(r"^\s*:(?:param|return|raises)\b", inspect.cleandoc(doc), flags=re.M)[0]
    paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    # ✓ / ✗ や → のような記号は英語の段落にも出てくるので、ASCII ではなく「日本語がない」で見る
    if _JAPANESE.search(paragraphs[0]):
        return "最初の段落が英語でない（日本語が入っている）"
    if not any(_JAPANESE.search(p) for p in paragraphs[1:]):
        return "英語のあとに日本語の段落がない"
    return None


@pytest.mark.parametrize(
    ("name", "obj"), _public_objects(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_public_api_docstring_is_english_then_japanese(name: str, obj: object) -> None:
    """`__all__` の関数・クラスと ELBClient の公開メソッドは、英語のあとに日本語の docstring。"""
    problem = _check(inspect.getdoc(obj) if not isinstance(obj, ModuleType) else obj.__doc__)
    assert problem is None, f"{name}: {problem}"


@pytest.mark.parametrize("module_name", PUBLIC_MODULES)
def test_public_module_docstring_is_english_then_japanese(module_name: str) -> None:
    """公開するモジュールの先頭の docstring も、英語のあとに日本語。"""
    module = importlib.import_module(module_name)
    problem = _check(module.__doc__)
    assert problem is None, f"{module_name}: {problem}"
