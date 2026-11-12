"""
性質のテスト（Hypothesis）。固定の例ではなく、生成した値で「どんな値でも落ちない」
「同じ値に戻る」のような不変条件を確かめる。
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from elbx._ids import canonical_ip
from elbx.cli import _parse_header
from elbx.formatter import _AnsiColor, _escape_controls
from elbx.simulator import SimulatedRequest, wildcard_match

# CI の遅い runner で時間切れの誤検知にならないように、1 件ごとの時間の上限は付けない
settings.register_profile("elbx", deadline=None)
settings.load_profile("elbx")

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# 制御文字と ESC [ … m が混ざりやすい文字列
_noisy_text = st.text(
    alphabet=st.one_of(
        st.characters(),
        st.sampled_from(["\x1b", "[", "m", ";", "0", "1", "\r", "\n", "\x07", "\x9b"]),
    )
)


@given(_noisy_text)
def test_escape_controls_leaves_no_control_characters(text: str) -> None:
    """置き換えたあとに制御文字は（改行も）残らない。2 回かけても変わらない。"""
    once = _escape_controls(text)
    assert _CONTROL.search(once) is None
    assert _escape_controls(once) == once


@given(_noisy_text)
def test_render_keeps_only_the_colors_of_elbx(text: str) -> None:
    """色を付けても、文字列の中の制御文字は残らず、ESC は elbx が付けた色の分だけになる。"""
    c = _AnsiColor(enabled=True)
    rendered = c.render([c.wrap(text, c.RED)])
    assert rendered.startswith(c.RED)
    assert rendered.endswith(c.RESET)
    body = rendered[len(c.RED) : -len(c.RESET)]
    assert _CONTROL.search(body) is None


def _wildcard_as_regex(pattern: str, value: str) -> bool:
    """ワイルドカードを正規表現に直して照合する（速くはないが、規則が明らかな比較相手）。"""
    regex = "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern)
    return re.fullmatch(regex, value, re.DOTALL) is not None


@given(st.text(alphabet="ab*?.", max_size=8), st.text(alphabet="ab*?.\n", max_size=8))
def test_wildcard_match_agrees_with_the_regular_expression(pattern: str, value: str) -> None:
    """`*` は 0 文字以上、`?` は 1 文字、ほかはその文字。正規表現で書いた規則と同じ結果になる。"""
    assert wildcard_match(pattern, value, ignore_case=False) is _wildcard_as_regex(pattern, value)


@given(st.text(alphabet="abAB*?", max_size=8), st.text(alphabet="abAB", max_size=8))
def test_wildcard_match_ignore_case_is_the_same_as_lower_case(pattern: str, value: str) -> None:
    """大文字小文字を区別しない照合は、両方を小文字にして照合するのと同じ。"""
    expected = wildcard_match(pattern.lower(), value.lower(), ignore_case=False)
    assert wildcard_match(pattern, value, ignore_case=True) is expected


@given(st.text())
def test_simulated_request_from_any_text_raises_only_valueerror(url: str) -> None:
    """どんな URL の文字列でも、解釈できなければ ValueError（elbx が ELBError に変える）。"""
    try:
        req = SimulatedRequest.from_url(url)
    except ValueError:
        return
    assert req.scheme in ("http", "https")
    assert req.host
    assert req.host == req.host.lower()
    assert 0 <= req.port <= 65535
    assert req.path.startswith("/") or req.path


@given(st.text(alphabet="ab*?\\", max_size=8), st.text(alphabet="ab*?\\", max_size=8))
def test_escaped_pattern_matches_only_the_value_it_was_made_from(value: str, other: str) -> None:
    """`*` と `?` の前に `\\` を付けたパターンは、元の値とだけ一致する。"""
    pattern = value.replace("*", "\\*").replace("?", "\\?")
    assert wildcard_match(pattern, value, ignore_case=False, escape=True)
    assert wildcard_match(pattern, other, ignore_case=False, escape=True) is (other == value)


@given(st.text(alphabet="ab*?.", max_size=8), st.text(alphabet="ab*?.\n", max_size=8))
def test_escape_changes_nothing_without_a_backslash(pattern: str, value: str) -> None:
    """`\\` のないパターンは、エスケープを読んでも読まなくても同じ結果になる。"""
    expected = wildcard_match(pattern, value, ignore_case=False)
    assert wildcard_match(pattern, value, ignore_case=False, escape=True) is expected


@given(st.text(), st.booleans())
def test_wildcard_pattern_matches_itself(value: str, ignore_case: bool) -> None:
    """値そのものをパターンにすると一致する（* と ? も、自分自身の文字に一致する）。"""
    assert wildcard_match(value, value, ignore_case=ignore_case)
    assert wildcard_match("*", value, ignore_case=ignore_case)


@given(st.text(alphabet=st.characters(exclude_characters="*?")), st.text())
def test_pattern_without_wildcards_is_plain_equality(pattern: str, value: str) -> None:
    """* と ? のないパターンは、大文字小文字を区別するときはただの等しさと同じ。"""
    assert wildcard_match(pattern, value, ignore_case=False) == (pattern == value)


@given(
    st.text(min_size=1).filter(lambda s: ":" not in s and s.strip() != ""),
    st.text(),
)
def test_parse_header_round_trip(name: str, value: str) -> None:
    """`名前: 値` を分けると、前後の空白を除いた名前と値に戻る（値の中の : はそのまま）。"""
    assert _parse_header(f"{name}:{value}") == (name.strip(), value.strip())


@given(st.text())
def test_parse_header_raises_only_argparse_errors(value: str) -> None:
    """どんな文字列でも、受け付けないときは argparse のエラーだけを出す。"""
    with contextlib.suppress(argparse.ArgumentTypeError):
        _parse_header(value)


@given(st.one_of(st.ip_addresses(v=4), st.ip_addresses(v=6)))
def test_canonical_ip_normalizes_any_notation(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> None:
    """IP アドレスは、大文字・省略なしの表記でも同じ正規化表記になる。"""
    assert canonical_ip(str(ip)) == str(ip)
    assert canonical_ip(ip.exploded.upper()) == str(ip)


@given(st.text())
def test_canonical_ip_never_raises(value: str) -> None:
    """IP アドレスでない文字列は、例外ではなく None になる。"""
    result = canonical_ip(value)
    assert result is None or result == canonical_ip(result)
