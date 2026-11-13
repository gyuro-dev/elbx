"""scripts/release_notes.py（CHANGELOG.md から版のリリースノートを作る）のテスト。"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "release_notes", Path(__file__).resolve().parent.parent / "scripts" / "release_notes.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
release_notes_module = importlib.util.module_from_spec(_SPEC)
sys.modules["release_notes"] = release_notes_module
_SPEC.loader.exec_module(release_notes_module)

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- not yet released

## [0.2.0] - 2026-11-01

### Added

- feature A
- feature B

## [0.1.0] - 2026-10-20

### Added

- first release

[Unreleased]: https://github.com/gyuro-dev/elbx/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/gyuro-dev/elbx/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gyuro-dev/elbx/releases/tag/v0.1.0
"""


def test_notes_contain_only_the_versions_section_and_links() -> None:
    """版の節だけを取り出し、PyPI のリンクと比較のリンクを足す（次の節は含めない）。"""
    notes = release_notes_module.release_notes(CHANGELOG, "0.2.0")
    assert notes is not None
    assert "feature A" in notes
    assert "feature B" in notes
    assert "first release" not in notes
    assert "not yet released" not in notes
    assert "**PyPI**: https://pypi.org/project/elbx/0.2.0/" in notes
    assert "**Full changelog**: https://github.com/gyuro-dev/elbx/compare/v0.1.0...v0.2.0" in notes


def test_first_version_has_no_compare_link() -> None:
    """最初の版は比較のリンクがないので、PyPI のリンクだけになる。"""
    notes = release_notes_module.release_notes(CHANGELOG, "0.1.0")
    assert notes is not None
    assert "first release" in notes
    assert "Full changelog" not in notes


def test_missing_version_returns_none() -> None:
    """節のない版は None（リリースを作らずに止めるため）。"""
    assert release_notes_module.release_notes(CHANGELOG, "9.9.9") is None


@pytest.mark.parametrize("argv", [[], ["a", "b"]])
def test_main_rejects_wrong_argument_count(argv: list[str]) -> None:
    assert release_notes_module.main(argv) == 2


def test_main_writes_utf8_whatever_the_console_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ノートは、端末の文字コードによらず UTF-8 で出す。Windows の端末（cp932）では、`–` のような
    文字を書けずに落ちていた（0.1.1 の CHANGELOG の「U+202A–U+202E」で起きた）。
    """
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "## [1.2.3] - 2026-11-14\n\n- U+202A–U+202E、省略の … と日本語\n", encoding="utf-8"
    )
    monkeypatch.setattr(release_notes_module, "CHANGELOG", changelog)
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii"))
    assert release_notes_module.main(["v1.2.3"]) == 0
    notes = raw.getvalue().decode("utf-8")
    assert notes.startswith("- U+202A–U+202E、省略の … と日本語\n")
    assert "**PyPI**: https://pypi.org/project/elbx/1.2.3/" in notes


def test_real_changelog_has_unreleased_section() -> None:
    """リポジトリの CHANGELOG.md は Keep a Changelog の形で、Unreleased の節がある。"""
    text = release_notes_module.CHANGELOG.read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
