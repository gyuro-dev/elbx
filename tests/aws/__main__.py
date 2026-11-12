"""
実際の AWS でのテスト環境を操作するコマンド。

  uv run python -m tests.aws preflight --aws-profile <名前> --aws-account-id <12桁>
      何も作らずに、作成前の確認だけを行う
      （アカウント・SSO の残り時間・名前・外部公開の方針・テンプレート検証）
  uv run python -m tests.aws template [--aws-name-prefix ...]
      生成される CloudFormation テンプレートを表示する（AWS には接続しない）
  uv run python -m tests.aws cleanup --aws-profile <名前> --aws-account-id <12桁>
      残ったスタックとインポートした証明書を削除する

テストの実行は pytest から行います（オプションは `uv run pytest --help` の `--aws` の項）:
  uv run pytest tests/casebook_elbx_ja.yaml tests/casebook_elbx_en.yaml --aws \\
      --aws-profile <名前> --aws-account-id <12桁> \\
      -o casebook_golden_dir=golden_aws -o casebook_html_reports= -m "not moto_only"
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Any

from tests.harness import load_infra

from .options import DEFAULT_MIN_SESSION_MINUTES, DEFAULT_REGION, AwsOptions, AwsTestError
from .stack import delete_environment, describe_sso, preflight
from .template import build_template

OPTION_HELP: dict[str, str] = {
    "profile": "AWS のプロファイル名（SSO 可。事前に `aws sso login` しておく）",
    "account-id": "接続先の AWS アカウント ID（12桁）。実際の接続先と違えば何も作らずに止める",
    "region": f"リージョン（デフォルト: {DEFAULT_REGION}）",
    "name-prefix": "作るすべてのリソース名の先頭に付ける文字列",
    "name-suffix": "作るすべてのリソース名の末尾に付ける文字列",
    "permissions-boundary": "作る IAM ロールに付ける Permissions Boundary のポリシー ARN",
    "iam-path": "作る IAM ロールのパス（例: /elbx/）",
    "cfn-role-arn": "CloudFormation がリソースを作るときに使う IAM ロールの ARN",
    "min-session-minutes": (
        f"SSO の残り時間がこれ未満なら始めない（分、デフォルト: {DEFAULT_MIN_SESSION_MINUTES}）"
    ),
}


def add_aws_arguments(add: Any, prefix: str = "--aws-") -> None:
    """
    `--aws-*` オプション群を追加する
    （`argparse.ArgumentParser.add_argument` と `pytest` の `group.addoption` の両方で共用）。

    :param add: オプション追加用の関数（`parser.add_argument` または `group.addoption`）
    :param prefix: オプション名のプレフィックス（デフォルト: `"--aws-"`）
    """
    add(f"{prefix}profile", default=None, help=OPTION_HELP["profile"])
    add(f"{prefix}account-id", default=None, help=OPTION_HELP["account-id"])
    add(f"{prefix}region", default=DEFAULT_REGION, help=OPTION_HELP["region"])
    add(f"{prefix}name-prefix", default="", help=OPTION_HELP["name-prefix"])
    add(f"{prefix}name-suffix", default="", help=OPTION_HELP["name-suffix"])
    add(f"{prefix}permissions-boundary", default=None, help=OPTION_HELP["permissions-boundary"])
    add(f"{prefix}iam-path", default=None, help=OPTION_HELP["iam-path"])
    add(f"{prefix}cfn-role-arn", default=None, help=OPTION_HELP["cfn-role-arn"])
    add(
        f"{prefix}min-session-minutes",
        type=int,
        default=DEFAULT_MIN_SESSION_MINUTES,
        help=OPTION_HELP["min-session-minutes"],
    )


def options_from(get: Any, keep_stack: bool = False) -> AwsOptions:
    """
    `get("aws_profile")` のようにオプション値を取得する関数から `AwsOptions` を生成する。

    :param get: オプション名を引数に取り値を返す関数
    :param keep_stack: テスト終了後もスタックを削除せずに残すかどうか
    :return: 構築された AwsOptions インスタンス
    """
    return AwsOptions(
        profile=get("aws_profile"),
        account_id=get("aws_account_id") or "",
        region=get("aws_region") or DEFAULT_REGION,
        name_prefix=get("aws_name_prefix") or "",
        name_suffix=get("aws_name_suffix") or "",
        permissions_boundary=get("aws_permissions_boundary"),
        iam_path=get("aws_iam_path"),
        cfn_role_arn=get("aws_cfn_role_arn"),
        keep_stack=keep_stack,
        min_session_minutes=get("aws_min_session_minutes"),
    )


def main(argv: list[str] | None = None) -> int:
    """
    `python -m tests.aws` CLI のエントリポイント（`preflight`, `template`, `cleanup` を実行）。

    :param argv: コマンドライン引数リスト（省略時は sys.argv[1:]）
    :return: 終了コード（0=正常終了、1=エラー）
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="python -m tests.aws", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("preflight", "何も作らずに作成前の確認だけを行う"),
        ("template", "生成される CloudFormation テンプレートを表示する"),
        ("cleanup", "残ったスタックと証明書を削除する"),
    ):
        p = sub.add_parser(name, help=help_text)
        add_aws_arguments(p.add_argument)
    args = parser.parse_args(argv)
    opts = options_from(lambda key: getattr(args, key))
    if args.command == "cleanup":
        # 削除は SSO の残り時間が短くても実行する（残したままにしない）
        opts = dataclasses.replace(opts, min_session_minutes=0)
    infra = load_infra()

    try:
        if args.command == "template":
            print(json.dumps(build_template(infra, opts), ensure_ascii=False, indent=2))
            return 0
        check = preflight(opts, infra)
        print(f"アカウント: {check.account_id}（{check.caller_arn}）")
        print(f"リージョン: {opts.region}")
        print(f"スタック名: {opts.stack_name}（現在の状態: {check.stack_status or 'なし'}）")
        if check.sso is not None:
            print(describe_sso(check.sso).lstrip("、"))
        if args.command == "preflight":
            for warning in check.warnings:
                print(f"注意: {warning}")
            print("作成前の確認はすべて問題ありません（何も作成していません）")
            return 0
        delete_environment(check.session, opts)
        print("削除が完了しました")
        return 0
    except AwsTestError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
