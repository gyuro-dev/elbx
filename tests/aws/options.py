"""
実際の AWS で構築するときのオプション、リソース名の付け方、作成前の確認（名前・SSO の残り時間）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_REGION = "ap-northeast-1"
DEFAULT_MIN_SESSION_MINUTES = 60
# すべてのリソース（とスタック）に付けるタグのキー。値はスタック名。残ったリソースを探す目印にする
TAG_KEY = "elbx-test"
STACK_BASE_NAME = "elbx-test"
LAMBDA_ROLE_BASE_NAME = "elbx-test-lambda-role"
VPC_BASE_NAME = "elbx-test-vpc"


class AwsTestError(Exception):
    """実際の AWS でのテストを始められない・続けられないときの例外。"""


@dataclass(frozen=True)
class AwsOptions:
    """`--aws-*` オプションの内容。"""

    profile: str | None
    account_id: str
    region: str = DEFAULT_REGION
    name_prefix: str = ""
    name_suffix: str = ""
    permissions_boundary: str | None = None
    iam_path: str | None = None
    cfn_role_arn: str | None = None
    keep_stack: bool = False
    min_session_minutes: int = DEFAULT_MIN_SESSION_MINUTES

    def name(self, base: str) -> str:
        """
        ベースのリソース名に `name_prefix` と `name_suffix` を付与した名前を返す。

        :param base: ベースとなるリソース名
        :return: prefix / suffix を付与したリソース名
        """
        return f"{self.name_prefix}{base}{self.name_suffix}"

    @property
    def stack_name(self) -> str:
        """
        CloudFormation スタック名（`elbx-test` に prefix / suffix を付与した名前）を返す。

        :return: CloudFormation スタック名
        """
        return self.name(STACK_BASE_NAME)


# =============================================================================
# 名前・オプションの確認
# =============================================================================

_ELB_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")


def resource_names(infra: dict[str, Any], opts: AwsOptions) -> dict[str, list[tuple[str, str]]]:
    """
    リソース種別ごとに `(元のベース名, prefix/suffix を付与した実名)` のタプル一覧を返す。

    :param infra: infra.yaml のデータ辞書
    :param opts: AWS テストオプション
    :return: リソース種別 -> `(元の名前, 実名)` のリストの辞書
    """

    def pair(base: str) -> tuple[str, str]:
        return base, opts.name(base)

    ecs = infra.get("ecs_clusters", [])
    return {
        "elb": [pair(lb["name"]) for lb in infra["load_balancers"]],
        "target_group": [pair(tg["name"]) for tg in infra["target_groups"]],
        "security_group": [pair(sg["name"]) for sg in infra["security_groups"]],
        "ec2": [pair(i["name"]) for i in infra["ec2_instances"]],
        "subnet": [pair(s["name"]) for s in infra["subnets"]],
        "lambda": [pair(fn["function_name"]) for fn in infra.get("lambda_functions", [])],
        "ecs": [pair(c[k]) for c in ecs for k in ("cluster_name", "service_name", "task_family")],
        "iam_role": [pair(LAMBDA_ROLE_BASE_NAME)],
        "vpc": [pair(VPC_BASE_NAME)],
        "stack": [pair(STACK_BASE_NAME)],
    }


# ケースのコマンドに名前を書くことのあるリソースの種別
_COMMAND_NAME_KINDS = ("elb", "target_group", "ec2", "lambda", "ecs", "security_group", "subnet")


def command_names(infra: dict[str, Any], opts: AwsOptions) -> dict[str, str]:
    """
    ケースのコマンドに書く元の名前 -> 実際の AWS に作った名前（prefix / suffix 付き）の辞書を返す。
    prefix / suffix がなければ空の辞書。

    :param infra: infra.yaml のデータ辞書
    :param opts: AWS テストオプション
    :return: 元の名前 -> 実名の辞書
    """
    names = resource_names(infra, opts)
    return {
        base: full for kind in _COMMAND_NAME_KINDS for base, full in names[kind] if full != base
    }


def validate_options(infra: dict[str, Any], opts: AwsOptions) -> list[str]:
    """
    オプションの形式および prefix / suffix を付与した全リソース名が
    AWS の命名制約（文字数上限・使用可能文字）を満たしているかを検証する。

    :param infra: infra.yaml のデータ辞書
    :param opts: 検証対象の AWS テストオプション
    :return: 違反メッセージのリスト（問題なければ空リスト）
    """
    errors: list[str] = []
    if not re.fullmatch(r"\d{12}", opts.account_id or ""):
        errors.append(f"--aws-account-id は12桁の数字で指定してください: {opts.account_id!r}")
    for label, value in (("prefix", opts.name_prefix), ("suffix", opts.name_suffix)):
        if value and not re.fullmatch(r"[A-Za-z0-9-]+", value):
            errors.append(f"--aws-name-{label} に使えるのは英数字とハイフンだけです: {value!r}")
    if opts.name_prefix.startswith("-"):
        errors.append("--aws-name-prefix はハイフンで始められません")
    if opts.name_suffix.endswith("-"):
        errors.append("--aws-name-suffix はハイフンで終えられません")
    if opts.iam_path and not (opts.iam_path.startswith("/") and opts.iam_path.endswith("/")):
        errors.append(
            f"--aws-iam-path は / で始まり / で終わる形で指定してください: {opts.iam_path!r}"
        )
    for label, arn in (
        ("--aws-permissions-boundary", opts.permissions_boundary),
        ("--aws-cfn-role-arn", opts.cfn_role_arn),
    ):
        if arn and not arn.startswith("arn:"):
            errors.append(f"{label} には ARN を指定してください: {arn!r}")

    limits = {"elb": 32, "target_group": 32, "lambda": 64, "iam_role": 64, "ecs": 255, "stack": 128}
    names = resource_names(infra, opts)
    for kind, limit in limits.items():
        for _base, full in names[kind]:
            if len(full) > limit:
                errors.append(
                    f"名前が長すぎます（{kind} は {limit} 文字まで）: {full}（{len(full)} 文字）。"
                    " prefix / suffix を短くしてください"
                )
    for kind in ("elb", "target_group"):
        for _base, full in names[kind]:
            if not _ELB_NAME.match(full):
                errors.append(f"{kind} の名前に使えない文字があります: {full}")
            if kind == "elb" and full.startswith("internal-"):
                errors.append(f"ELB の名前は 'internal-' で始められません: {full}")
    stack = opts.stack_name
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", stack):
        errors.append(f"スタック名は英字で始まり、英数字とハイフンだけにしてください: {stack}")
    return errors


# =============================================================================
# SSO トークンの残り時間
# =============================================================================


def _parse_sso_expiry(value: str) -> datetime:
    """
    AWS SSO キャッシュ内の `expiresAt`（ISO 8601 文字列）を UTC 付きの `datetime` に変換する。

    :param value: SSO キャッシュ内の有効期限文字列（例: `"2026-10-17T12:00:00Z"`）
    :return: UTC タイムゾーン付きの datetime オブジェクト
    """
    text = value.strip().replace("UTC", "+00:00").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class SsoTokenStatus:
    """キャッシュされた SSO トークンの状態。"""

    minutes_left: float
    # sso_session 形式で refreshToken があれば、botocore が期限前に自動で更新する
    # （その場合、アクセストークンの期限は実行時間の上限にならない）
    refreshable: bool


def sso_token_status(session: Any, cache_dir: Path | None = None) -> SsoTokenStatus | None:
    """
    プロファイルが SSO の場合、キャッシュされた SSO トークンの残り時間と自動更新可否を返す。
    SSO 以外のプロファイルや、トークンファイルが見つからないときは `None` を返す。
    （botocore と同じく、`sso_session` 名または `sso_start_url` の SHA-1 ハッシュを
    キャッシュファイル名として参照し、トークンの値そのものは読み取らない）

    :param session: boto3.Session インスタンス
    :param cache_dir: SSO キャッシュディレクトリ（省略時は `~/.aws/sso/cache`）
    :return: SsoTokenStatus インスタンス、または SSO 対象外の場合は None
    """
    config = session._session.get_scoped_config()
    key = config.get("sso_session") or config.get("sso_start_url")
    if not key:
        return None
    cache_dir = cache_dir or Path.home() / ".aws" / "sso" / "cache"
    path = cache_dir / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("expiresAt"):
        return None
    minutes = (_parse_sso_expiry(data["expiresAt"]) - datetime.now(UTC)).total_seconds()
    return SsoTokenStatus(
        minutes_left=minutes / 60,
        refreshable=bool(config.get("sso_session") and data.get("refreshToken")),
    )
