"""
pytest 共通設定。

- moto 環境（tests/data/infra.yaml の構成）はテストセッション全体で1回だけ作る
  （elbx は参照専用で AWS 上の状態を変えないため、全テストで共有できる）
- ゴールデンテスト（tests/casebook_elbx_ja.yaml / casebook_elbx_en.yaml）は pytest-casebook が
  実行する。ここにはその hook を書く（期待出力の作り直しは `--casebook-update`）
- `--aws` を付けたときだけ、実際の AWS に環境を作ってケースを実行する
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from pytest_casebook import Case, CommandResult, run_in_process

from elbx import ELBClient, cli, set_language

from .aws.__main__ import add_aws_arguments, options_from
from .harness import (
    _FALLBACK_PATTERNS,
    TestEnv,
    apply_health_overrides,
    moto_environment,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    """
    実AWS検証用オプション（`--aws*`）を追加する。

    :param parser: pytest のコマンドライン引数パーサー
    """
    group = parser.getgroup("aws", "実際の AWS でのゴールデンテスト（料金がかかります）")
    group.addoption(
        "--aws",
        action="store_true",
        default=False,
        help="実際の AWS に環境を作ってテストする（--aws-profile と --aws-account-id が必須）",
    )
    group.addoption(
        "--aws-keep-stack",
        action="store_true",
        default=False,
        help="テスト後もスタックを削除しない（次回はそのスタックを使い回す。料金がかかり続ける）",
    )
    add_aws_arguments(group.addoption)


def pytest_configure(config: pytest.Config) -> None:
    """
    pytest 起動時にオプションの整合性を検証する（`--aws` 指定時の必須オプション確認）。

    :param config: pytest の設定オブジェクト
    :raises pytest.UsageError: `--aws` 指定時に必須オプションが欠けている場合
    """
    if config.getoption("--aws"):
        missing = [
            name
            for name in ("--aws-profile", "--aws-account-id")
            if not config.getoption(name.lstrip("-").replace("-", "_"))
        ]
        if missing:
            raise pytest.UsageError(f"--aws には {' と '.join(missing)} の指定が必要です")


@pytest.fixture(autouse=True)
def _japanese_output(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    テストの出力の言語を日本語に固定する（既定は OS の言語で決まり、CI の Linux では英語に
    なるため）。英語は、言語を指定するテストで確かめる（ゴールデンテストは YAML の env: で固定）。
    テストの中で `set_language()` を呼んでも、次のテストに残らないように戻す。
    """
    monkeypatch.setenv("ELBX_LANG", "ja")
    yield
    set_language(None)


@pytest.fixture(scope="session")
def moto_env() -> Iterator[TestEnv]:
    """
    `infra.yaml` の構成を moto 上に構築したセッション共有のテスト環境フィクスチャ。

    :return: TestEnv インスタンスを yield するイテレータ
    """
    with moto_environment() as env:
        yield env


@pytest.fixture(scope="session")
def aws_env(request: pytest.FixtureRequest) -> Iterator[TestEnv]:
    """
    `infra.yaml` の構成を実際の AWS（CloudFormation）上に構築したテスト環境フィクスチャ。
    `--aws` オプションが指定されたときのみ実行される。

    :param request: pytest の FixtureRequest オブジェクト
    :return: 実AWS向けの TestEnv インスタンスを yield するイテレータ
    """
    config = request.config
    if not config.getoption("--aws"):
        pytest.skip("実際の AWS でのテストは --aws を付けたときだけ実行します")
    from .aws.stack import aws_environment

    opts = options_from(config.getoption, keep_stack=config.getoption("--aws-keep-stack"))
    with aws_environment(opts) as env:
        yield env


@pytest.fixture
def elb_client(moto_env: TestEnv) -> ELBClient:
    """
    テスト関数ごとに新しい ELBClient を生成して返すフィクスチャ
    （`describe_target_health` 等へのモンキーパッチが他テストへ漏れないようにする）。

    :param moto_env: セッション共有の moto テスト環境
    :return: 新しい ELBClient インスタンス
    """
    return moto_env.client()


# =============================================================================
# pytest-casebook（tests/casebook_elbx_ja.yaml / casebook_elbx_en.yaml）の hook
# =============================================================================


@pytest.fixture(scope="session")
def casebook_env(request: pytest.FixtureRequest) -> TestEnv:
    """
    casebook のケースを実行する環境。`--aws` を付ければ実際の AWS、付けなければ moto。

    moto は `moto_env` を使い回す（同じ構成をもう1つ作ると同じ名前のリソースで失敗するため）。
    使わないほうの環境は作らないよう、`getfixturevalue` で必要なほうだけを作る。

        uv run pytest tests/casebook_elbx_ja.yaml tests/casebook_elbx_en.yaml   # moto
        uv run pytest tests/casebook_elbx_ja.yaml tests/casebook_elbx_en.yaml --aws \\
            --aws-profile <名前> --aws-account-id <12桁> \\
            -o casebook_golden_dir=golden_aws -o casebook_html_reports= \\
            -m "not moto_only"                                      # 実際の AWS
    """
    name = "aws_env" if request.config.getoption("--aws") else "moto_env"
    env: TestEnv = request.getfixturevalue(name)
    return env


def run_case_command(case: Case, env: TestEnv, argv: list[str]) -> CommandResult:
    """
    ケースの `command` を、環境に向けたクライアントで同じプロセスの中で実行する。

    ヘルス状態（`state`）は、実行のたびに作るこのクライアントに当てる必要があるため、
    `pytest_casebook_setup` ではなくここで当てる。playground.py も同じ関数を使う。
    引数は pytest-casebook が渡す `argv`（`command_prefix` とコマンドを合わせたもの）の
    プログラム名のあとを使う（期待出力の `# command:` と実際に動かす引数をそろえるため）。
    YAML の `env:`（出力の言語の `ELBX_LANG` など）は、pytest-casebook が実行の間だけ
    `os.environ` に当てているので、ここでは何もしない。
    実際の AWS で名前に prefix / suffix を付けたときは、コマンドの引数のうちリソースの名前と
    一致するものを、付けた名前に置き換えて実行する（出力は元の名前に戻して比較する）。

    :param case: casebook のケース
    :param env: moto または実際の AWS の環境
    :param argv: `command_prefix` とコマンドを合わせた引数（先頭はプログラム名の `elbx`）
    :return: 終了コード・標準出力・標準エラー出力
    """
    client = env.client()
    apply_health_overrides(client, env, case.state_data or {})
    args = [env.command_names.get(arg, arg) for arg in argv[1:]]
    return run_in_process(lambda a: cli.main(a, client=client), args)


def pytest_casebook_run_command(case: Case, env: TestEnv, argv: list[str]) -> CommandResult:
    """elbx の CLI を別プロセスではなく同じプロセスで実行する（moto の環境を渡すため）。"""
    return run_case_command(case, env, argv)


def pytest_casebook_placeholders(env: TestEnv) -> list[tuple[str, str]]:
    """moto / 実 AWS のランダムな ID を infra.yaml の key を使った表記に置き換える。"""
    return env.placeholders()


def pytest_casebook_patterns(env: TestEnv) -> list[tuple[Any, str]]:
    """対応表にない ID・日時の置き換え（置き換え漏れの保険）。"""
    return [*env.extra_patterns, *_FALLBACK_PATTERNS]
