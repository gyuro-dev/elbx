"""Run the checks on every supported Python locally, each in its own virtual environment.

対応するすべての Python で、手元で検査を流す（版ごとに別の仮想環境を使う）。

Usage / 使い方::

    uv run --no-project python scripts/check.py            # Python 3.11 to 3.15
    uv run --no-project python scripts/check.py 3.13 3.15  # only these versions

- ruff and mypy run once (they do not depend on the Python version)
- for each version: `uv sync --frozen --python <version>` into `.venvs/py<version>` (a separate
  environment per version, so nothing mixes), then pytest with the same options as CI
- the results go to reports/check/ (summary.txt, and one directory per version with the test
  output, the JUnit XML and the HTML test specification sheet)
- the exit code is 1 when anything failed

- ruff と mypy は 1 回だけ流す（Python の版によらない）
- 版ごとに `uv sync --frozen --python <版>` で `.venvs/py<版>` に環境を作り（版ごとに別なので
  混ざらない）、CI と同じオプションで pytest を流す
- 結果は reports/check/ に残す（summary.txt と、版ごとのディレクトリにテストの出力・JUnit XML・
  試験項目表の HTML）
- 1 つでも失敗すれば終了コード 1
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports" / "check"
VENVS = ROOT / ".venvs"
# requires-python = ">=3.11" of pyproject.toml, up to the newest release
DEFAULT_PYTHONS = ("3.11", "3.12", "3.13", "3.14", "3.15")
# prints the versions actually used in a virtual environment / 仮想環境で実際に使う版を出す
VERSIONS_PROBE = (
    "import sys, boto3, moto; print(f'python {sys.version.split()[0]}"
    " / boto3 {boto3.__version__} / moto {moto.__version__}')"
)


def run(args: list[str], log: Path, env: dict[str, str]) -> int:
    """Run a command, appending its output to `log`; return the exit code."""
    with log.open("a", encoding="utf-8") as f:
        f.write(f"$ {' '.join(args)}\n")
        f.flush()
        return subprocess.run(
            args, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, env=env
        ).returncode


def main(argv: list[str]) -> int:
    pythons = argv or list(DEFAULT_PYTHONS)
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    summary = OUT / "summary.txt"
    base_env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        # PyYAML has no wheel for Python 3.15 yet; build it without the C extension
        # PyYAML に 3.15 の wheel がまだないので、C 拡張なしで作る
        "PYYAML_FORCE_LIBYAML": os.environ.get("PYYAML_FORCE_LIBYAML", "0"),
    }
    uv_version = subprocess.run(["uv", "--version"], capture_output=True, text=True).stdout.strip()
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT
    ).stdout.strip()
    dirty = subprocess.run(["git", "diff", "--quiet"], cwd=ROOT).returncode != 0
    lines = [
        "# elbx の試験の結果 / check results",
        f"date:   {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"os:     {platform.platform()}",
        f"uv:     {uv_version}",
        f"commit: {commit}{' (uncommitted changes / 未 commit の変更あり)' if dirty else ''}",
        "",
    ]
    failed = False

    # ruff / mypy once, with the project's own environment (.venv)
    static = OUT / "static.txt"
    ok = all(
        run(["uv", "run", "--frozen", *cmd], static, base_env) == 0
        for cmd in (["ruff", "check", "."], ["ruff", "format", "--check", "."], ["mypy"])
    )
    lines.append(f"ruff + mypy      : {'ok' if ok else f'NG ({static.relative_to(ROOT)})'}")
    failed |= not ok

    for py in pythons:
        name = f"py{py}"
        out_dir = OUT / name
        out_dir.mkdir()
        env = {**base_env, "UV_PROJECT_ENVIRONMENT": str(VENVS / name), "UV_PYTHON": py}
        setup_log = out_dir / "setup.txt"
        if run(["uv", "sync", "--frozen", "--python", py], setup_log, env) != 0:
            lines.append(
                f"{name:<17}: NG (could not create the environment: {setup_log.relative_to(ROOT)})"
            )
            failed = True
            continue
        versions = subprocess.run(
            ["uv", "run", "--no-sync", "python", "-c", VERSIONS_PROBE],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
        ).stdout.strip()
        # the same options as CI, with the reports of this version kept apart
        # CI と同じオプション。レポートはこの版のディレクトリに分ける
        code = run(
            [
                "uv",
                "run",
                "--no-sync",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-q",
                "-o",
                "addopts=",
                # 言語ごとの追加の表（ini の casebook_html_reports）は reports/ に書くので、
                # 版ごとの実行では出さない（全体の表だけを、この版のディレクトリに出す）
                "-o",
                "casebook_html_reports=",
                f"--junitxml={out_dir / 'junit.xml'}",
                f"--casebook-html={out_dir / 'casebook.html'}",
            ],
            out_dir / "tests.txt",
            env,
        )
        last = (out_dir / "tests.txt").read_text(encoding="utf-8").rstrip().splitlines()[-1]
        result = "ok" if code == 0 else f"NG ({out_dir.relative_to(ROOT)}/tests.txt)"
        lines.append(f"{name:<17}: {result}  [{versions}] {last.strip('= ')}")
        failed |= code != 0

    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
