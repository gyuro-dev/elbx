"""
実際の AWS での環境のライフサイクル:
作成前の確認 → 作成 → ヘルスの安定待ち → テスト用の環境情報 → 削除。

費用と時間を抑えるため、環境は1回だけ作って全ケースで共有し、最後に1回だけ削除する。
途中で失敗しても削除は必ず実行し、それでも残った場合は cleanup コマンドで消せる。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

from tests.harness import TestEnv, check_exposure_policy, for_real_aws, load_infra

from .options import (
    TAG_KEY,
    AwsOptions,
    AwsTestError,
    SsoTokenStatus,
    command_names,
    resource_names,
    sso_token_status,
    validate_options,
)
from .template import logical_id, template_body

_COMPLETE = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
HEALTH_WAIT_SECONDS = 900
# ヘルスが落ち着いたとみなす状態（initial / unhealthy は起動直後に出るため待つ）
_SETTLED_STATES = {"healthy", "unused", "unavailable", "draining"}


def _log(message: str) -> None:
    """
    実AWSテストの進行状況メッセージを即時フラッシュ付きで標準出力へ表示する。

    :param message: 出力するログメッセージ
    """
    print(f"[elbx-aws] {message}", flush=True)


# =============================================================================
# 作成前の確認
# =============================================================================


@dataclass
class Preflight:
    """作成前の確認（preflight チェック）の結果を保持するデータクラス。"""

    session: Any
    account_id: str
    caller_arn: str
    sso: SsoTokenStatus | None
    stack_status: str | None
    template: str
    # 作成は止めないが知っておくとよいこと（期待出力がずれる・初めて使うアカウントで失敗しうる）
    warnings: list[str] = field(default_factory=list)


def preflight(opts: AwsOptions, infra: dict[str, Any] | None = None) -> Preflight:
    """
    AWS 上に何も作らずに、安全に環境を作成してよい状態かを事前確認する。
    確認内容: 外部公開の方針 / 名前とオプション / 接続先アカウント / SSO の残り時間 /
    テンプレートの検証（`ValidateTemplate`） / 既存スタックの状態。
    止めるほどではないこと（リージョンにあるほかの ELB・TG、まだないサービスリンクロール）は
    `Preflight.warnings` に入れる。

    :param opts: AWS テストオプション
    :param infra: infra.yaml のデータ辞書（省略時は自動読み込み）
    :return: 事前確認結果をまとめた Preflight インスタンス
    :raises AwsTestError: いずれかの事前確認で問題が見つかった場合
    """
    infra = for_real_aws(infra or load_infra())
    problems = check_exposure_policy(infra) + validate_options(infra, opts)
    if problems:
        raise AwsTestError("作成前の確認で問題が見つかりました:\n- " + "\n- ".join(problems))

    try:
        session = boto3.Session(profile_name=opts.profile, region_name=opts.region)
        identity = session.client("sts").get_caller_identity()
    except Exception as exc:
        raise AwsTestError(
            f"AWS に接続できません（プロファイル: {opts.profile}）。"
            f" SSO の場合は `aws sso login --profile {opts.profile}` を実行してください: {exc}"
        ) from exc
    if identity["Account"] != opts.account_id:
        raise AwsTestError(
            f"接続先のアカウントが指定と違います（指定: {opts.account_id}、"
            f"実際: {identity['Account']}、{identity['Arn']}）。何も作成していません。"
        )

    # 自動更新できない（旧形式などの）SSO トークンだけ、残り時間で止める
    sso = sso_token_status(session)
    if sso is not None and not sso.refreshable and sso.minutes_left < opts.min_session_minutes:
        minutes = sso.minutes_left
        raise AwsTestError(
            f"SSO の残り時間が {minutes:.0f} 分しかありません"
            f"（{opts.min_session_minutes} 分以上必要）。"
            f" `aws sso login --profile {opts.profile}` でログインし直してください。"
        )

    body = template_body(infra, opts)
    if len(body.encode("utf-8")) > 51_200:
        raise AwsTestError(f"テンプレートが大きすぎます（{len(body)} バイト、上限 51,200）")
    cfn = session.client("cloudformation")
    try:
        cfn.validate_template(TemplateBody=body)
    except Exception as exc:
        raise AwsTestError(f"テンプレートの検証に失敗しました: {exc}") from exc

    return Preflight(
        session=session,
        account_id=identity["Account"],
        caller_arn=identity["Arn"],
        sso=sso,
        stack_status=_stack_status(cfn, opts.stack_name),
        template=body,
        warnings=_other_elb_warnings(session, infra, opts) + _service_linked_role_warnings(session),
    )


def _other_elb_warnings(session: Any, infra: dict[str, Any], opts: AwsOptions) -> list[str]:
    """
    テストで作るもの以外の ELB・TG がリージョンにあれば、その警告を返す。
    `ls` / `targets` / `find` はリージョン内のすべてを対象にするため、期待出力と合わなくなる。

    :param session: boto3.Session インスタンス
    :param infra: infra.yaml のデータ辞書
    :param opts: AWS テストオプション
    :return: 警告メッセージのリスト
    """
    names = resource_names(infra, opts)
    ours = {full for kind in ("elb", "target_group") for _base, full in names[kind]}
    elbv2 = session.client("elbv2")
    others: list[str] = []
    for method, key, name_key in (
        ("describe_load_balancers", "LoadBalancers", "LoadBalancerName"),
        ("describe_target_groups", "TargetGroups", "TargetGroupName"),
    ):
        for page in elbv2.get_paginator(method).paginate():
            others += [r[name_key] for r in page.get(key, []) if r[name_key] not in ours]
    if not others:
        return []
    shown = ", ".join(others[:10]) + (f" ほか {len(others) - 10} 件" if len(others) > 10 else "")
    return [
        (
            f"リージョン {opts.region} に、テストで作るもの以外の ELB・TG があります（{shown}）。"
            " ls / targets / find の出力にも出るため、期待出力と合わなくなります。"
            " ELB・TG のないリージョンかアカウントで実行してください"
        )
    ]


# 初めて ELB・ECS を使うときに AWS が作るサービスリンクロール -> そのサービス
_SERVICE_LINKED_ROLES = {
    "AWSServiceRoleForElasticLoadBalancing": "elasticloadbalancing.amazonaws.com",
    "AWSServiceRoleForECS": "ecs.amazonaws.com",
}


def _service_linked_role_warnings(session: Any) -> list[str]:
    """
    ELB・ECS のサービスリンクロールがまだなければ、その警告を返す。
    作成時に AWS が自動で作るが、`iam:CreateServiceLinkedRole` の権限がないと作成に失敗する。
    ロールを読む権限がなく確かめられないときは、何も返さない。

    :param session: boto3.Session インスタンス
    :return: 警告メッセージのリスト
    """
    iam = session.client("iam")
    warnings: list[str] = []
    for role, service in _SERVICE_LINKED_ROLES.items():
        try:
            iam.get_role(RoleName=role)
        except iam.exceptions.NoSuchEntityException:
            warnings.append(
                f"サービスリンクロール {role} がまだありません。作成時に自動で作られますが、"
                "iam:CreateServiceLinkedRole の権限がないと失敗します。先に作る場合: "
                f"aws iam create-service-linked-role --aws-service-name {service}"
            )
        except ClientError:  # 権限がなく確かめられないときは警告しない
            continue
    return warnings


def describe_sso(sso: SsoTokenStatus | None) -> str:
    """
    SSO トークンの状態を表示用の日本語文字列に整形する。

    :param sso: SsoTokenStatus インスタンス（または None）
    :return: 表示用の説明文字列（SSO 対象外なら空文字列）
    """
    if sso is None:
        return ""
    if sso.refreshable:
        return f"、SSO トークンは自動更新あり（現在のトークンの残り {sso.minutes_left:.0f} 分）"
    return f"、SSO の残り {sso.minutes_left:.0f} 分（自動更新なし）"


def _stack_status(cfn: Any, stack_name: str) -> str | None:
    """
    指定した CloudFormation スタックの現在のステータス文字列を取得する。

    :param cfn: CloudFormation クライアント
    :param stack_name: 対象のスタック名
    :return: スタックステータス文字列（スタックが存在しない場合は None）
    """
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    except Exception as exc:
        if "does not exist" in str(exc):
            return None
        raise
    return stacks[0]["StackStatus"] if stacks else None


# =============================================================================
# 自己署名証明書（ACM にインポートする。DNS 検証が不要で、料金もかからない）
# =============================================================================


def _self_signed_certificate(domain: str, sans: list[str]) -> tuple[bytes, bytes]:
    """
    テスト用の自己署名証明書（PEM）と秘密鍵（PEM）をメモリ上で生成する。

    :param domain: 証明書の Common Name (CN) ドメイン名
    :param sans: Subject Alternative Names (SANs) のドメイン名リスト
    :return: `(証明書PEMバイト列, 秘密鍵PEMバイト列)` のタプル
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=7))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def _import_certificate(session: Any, infra: dict[str, Any], stack_name: str) -> str:
    """
    `infra.yaml` の証明書定義に基づいて自己署名証明書を生成し、ACM へインポートして ARN を返す。

    :param session: boto3.Session インスタンス
    :param infra: infra.yaml のデータ辞書
    :param stack_name: 識別タグに付与するスタック名
    :return: インポートされた ACM 証明書の ARN
    """
    spec = infra["certificates"][0]
    cert_pem, key_pem = _self_signed_certificate(spec["domain_name"], list(spec["san"]))
    resp = session.client("acm").import_certificate(
        Certificate=cert_pem,
        PrivateKey=key_pem,
        Tags=[{"Key": TAG_KEY, "Value": stack_name}],
    )
    return resp["CertificateArn"]


def _tagged_certificates(session: Any, stack_name: str) -> list[str]:
    """
    指定したスタック名のタグが付与されている ACM 証明書の ARN 一覧を検索して返す。

    :param session: boto3.Session インスタンス
    :param stack_name: 検索対象のスタック名（`elbx-test` タグの値）
    :return: 一致した ACM 証明書 ARN のリスト
    """
    acm = session.client("acm")
    arns: list[str] = []
    for page in acm.get_paginator("list_certificates").paginate():
        for cert in page.get("CertificateSummaryList", []):
            tags = acm.list_tags_for_certificate(CertificateArn=cert["CertificateArn"]).get(
                "Tags", []
            )
            if {"Key": TAG_KEY, "Value": stack_name} in tags:
                arns.append(cert["CertificateArn"])
    return arns


# =============================================================================
# 作成・削除
# =============================================================================


def _create_stack(session: Any, opts: AwsOptions, body: str, cert_arn: str) -> None:
    """
    CloudFormation スタックを作成し、`CREATE_COMPLETE` になるまで待機する。

    :param session: boto3.Session インスタンス
    :param opts: AWS テストオプション
    :param body: CloudFormation テンプレート JSON 文字列
    :param cert_arn: パラメータとして渡す ACM 証明書 ARN
    :raises AwsTestError: スタック作成が失敗またはタイムアウトした場合
    """
    cfn = session.client("cloudformation")
    kwargs: dict[str, Any] = {
        "StackName": opts.stack_name,
        "TemplateBody": body,
        "Parameters": [{"ParameterKey": "CertificateArn", "ParameterValue": cert_arn}],
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "Tags": [{"Key": TAG_KEY, "Value": opts.stack_name}],
        # 作成に失敗したら自動で削除する（中途半端なリソースを残さない）
        "OnFailure": "DELETE",
    }
    if opts.cfn_role_arn:
        kwargs["RoleARN"] = opts.cfn_role_arn
    _log(f"スタック {opts.stack_name} を作成しています（5〜10分かかります）")
    # 作成に失敗すると自動で削除されて名前では参照できなくなるため、スタック ID で追う
    stack_id = cfn.create_stack(**kwargs)["StackId"]
    _watch_stack(cfn, stack_id, action="作成", success={"CREATE_COMPLETE"}, timeout=CREATE_TIMEOUT)
    _log("スタックを作成しました")


STACK_POLL_SECONDS = 10
PROGRESS_LOG_SECONDS = 60
CREATE_TIMEOUT = 40 * 60
DELETE_TIMEOUT = 45 * 60


def _watch_stack(
    cfn: Any,
    stack_id: str,
    action: str,
    success: set[str],
    timeout: int,
    since: dt.datetime | None = None,
) -> None:
    """
    スタックのイベントを `STACK_POLL_SECONDS` 秒ごとに監視し、進行状況をログに出力する。

    CloudFormation の標準 waiter は「スタック全体」が終わるまで結果を返さないため、
    作成に失敗すると自動削除が終わるまで数分間気づけない。ここでは個別リソースの失敗
    （巻き添えのキャンセルは除く）を見つけた時点でログに出し、即座に `AwsTestError` を送出する。
    `since` より前のイベント（削除監視時における作成時の過去イベントなど）は無視する。

    :param cfn: CloudFormation クライアント
    :param stack_id: 監視対象のスタック ID またはスタック名
    :param action: ログ表示用の操作名（`"作成"` または `"削除"`）
    :param success: 完了とみなすスタックステータスの集合
    :param timeout: タイムアウト秒数
    :param since: この時刻以降のイベントのみを監視対象とする基準時刻
    :raises AwsTestError: リソース失敗、ロールバック、またはタイムアウトが発生した場合
    """
    seen: set[str] = set()
    completed: set[str] = set()
    deadline = time.monotonic() + timeout
    next_progress = time.monotonic() + PROGRESS_LOG_SECONDS
    while True:
        stack = cfn.describe_stacks(StackName=stack_id)["Stacks"][0]
        status = stack["StackStatus"]
        events = cfn.describe_stack_events(StackName=stack_id)["StackEvents"]  # 新しい順に最大100件
        failures: list[str] = []
        for event in reversed([e for e in events if e["EventId"] not in seen]):
            seen.add(event["EventId"])
            if since is not None and event["Timestamp"] < since:
                continue
            resource, res_status = event["LogicalResourceId"], event["ResourceStatus"]
            reason = event.get("ResourceStatusReason", "")
            if resource == stack["StackName"]:
                continue  # スタック自体のイベントは集計なので、リソースの失敗だけを見る
            if res_status.endswith("_COMPLETE"):
                completed.add(resource)
            if res_status.endswith("_FAILED") and "cancelled" not in reason:
                failures.append(f"{resource}: {reason}")
                _log(f"{action}に失敗したリソース: {resource}: {reason}")
        if failures:
            raise AwsTestError(f"スタックの{action}に失敗しました:\n- " + "\n- ".join(failures))
        if status in success:
            return
        if status.endswith("_FAILED") or "ROLLBACK" in status:
            raise AwsTestError(
                f"スタックの{action}に失敗しました（状態: {status}）: "
                f"{stack.get('StackStatusReason', '')}"
            )
        now = time.monotonic()
        if now >= deadline:
            raise AwsTestError(
                f"スタックの{action}が {timeout // 60} 分で終わりませんでした（状態: {status}）"
            )
        if now >= next_progress:
            _log(f"{action}中: {len(completed)} 件のリソースが完了（状態: {status}）")
            next_progress = now + PROGRESS_LOG_SECONDS
        time.sleep(STACK_POLL_SECONDS)


def delete_environment(session: Any, opts: AwsOptions) -> None:
    """
    CloudFormation スタックと、インポートした ACM 証明書を削除する（存在しなければ何もしない）。

    :param session: boto3.Session インスタンス
    :param opts: AWS テストオプション
    """
    cfn = session.client("cloudformation")
    if _stack_status(cfn, opts.stack_name) is not None:
        stack_id = cfn.describe_stacks(StackName=opts.stack_name)["Stacks"][0]["StackId"]
        kwargs: dict[str, Any] = {"StackName": opts.stack_name}
        if opts.cfn_role_arn:
            kwargs["RoleARN"] = opts.cfn_role_arn
        _log(
            f"スタック {opts.stack_name} を削除しています"
            "（VPC 内の Lambda があると 10〜20分かかります）"
        )
        started = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)
        cfn.delete_stack(**kwargs)
        _watch_stack(
            cfn,
            stack_id,
            action="削除",
            success={"DELETE_COMPLETE"},
            timeout=DELETE_TIMEOUT,
            since=started,
        )
        _log("スタックを削除しました")

    acm = session.client("acm")
    for arn in _tagged_certificates(session, opts.stack_name):
        # ELB の削除直後は「使用中」で消せないことがあるため、少し待って再試行する
        for attempt in range(10):
            try:
                acm.delete_certificate(CertificateArn=arn)
                _log(f"証明書を削除しました: {arn}")
                break
            except Exception:
                if attempt == 9:
                    raise
                time.sleep(15)


# =============================================================================
# ヘルスの安定待ち・テスト用の環境情報
# =============================================================================


def _outputs(session: Any, stack_name: str) -> dict[str, str]:
    """
    CloudFormation スタックの `Outputs` を `OutputKey -> OutputValue` の辞書として取得する。

    :param session: boto3.Session インスタンス
    :param stack_name: 対象のスタック名
    :return: OutputKey をキー、OutputValue を値とする辞書
    """
    stack = session.client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def never_healthy_targets(infra: dict[str, Any], env: TestEnv) -> set[str]:
    """
    healthy にならないと分かっているターゲットの ID（実際の AWS のアドレス）を返す。
    どのリソースのものでもない IP（「ENI のない IP」を確かめるための IPv6 のターゲット）は、
    ヘルスチェックに応える相手がいないので、いつまでも `unhealthy` のままになる。

    :param infra: infra.yaml のデータ辞書
    :param env: 実際の AWS の環境（IPv6 の読み替えに使う）
    :return: ターゲットの ID の集合
    """
    owned = {ip for inst in infra["ec2_instances"] for ip in inst.get("ipv6_ips", [])}
    return {
        env.real_ipv6(ip)
        for tg in infra["target_groups"]
        for ip in tg.get("ips", [])
        if ":" in ip and ip not in owned
    }


def wait_for_health(
    session: Any,
    tg_arns: list[str],
    ecs_tg_arns: list[str],
    never_healthy: set[str] | None = None,
) -> dict[str, str]:
    """
    全ターゲットグループのヘルス状態が落ち着くまで待機する
    （`initial` / `unhealthy` がなくなり、ECS サービスのタスクがターゲット登録されるまで）。
    制限時間内に落ち着かなくても例外では止めずに続行し、最後の状態辞書を返す（ゴールデン差分として確認できるため）。

    :param session: boto3.Session インスタンス
    :param tg_arns: 監視対象の全ターゲットグループ ARN のリスト
    :param ecs_tg_arns: ECS タスクが登録されるターゲットグループ ARN のリスト
    :param never_healthy: healthy にならないと分かっているターゲットの ID
        （`unhealthy` になった時点で落ち着いたとみなす）
    :return: `"<TG名> <ターゲットID>"` -> ヘルス状態文字列の辞書
    """
    never_healthy = never_healthy or set()
    elbv2 = session.client("elbv2")
    deadline = time.monotonic() + HEALTH_WAIT_SECONDS
    _log("ターゲットのヘルスが落ち着くのを待っています")
    while True:
        states: dict[str, str] = {}
        for arn in tg_arns:
            for desc in elbv2.describe_target_health(TargetGroupArn=arn)[
                "TargetHealthDescriptions"
            ]:
                states[f"{arn.split('/')[-2]} {desc['Target']['Id']}"] = desc["TargetHealth"][
                    "State"
                ]
        ecs_ready = all(
            elbv2.describe_target_health(TargetGroupArn=arn)["TargetHealthDescriptions"]
            for arn in ecs_tg_arns
        )
        unsettled = {
            k: v
            for k, v in states.items()
            if v not in _SETTLED_STATES
            and not (v == "unhealthy" and k.split(" ", 1)[1] in never_healthy)
        }
        if not unsettled and ecs_ready:
            _log("ヘルスが落ち着きました")
            return states
        if time.monotonic() >= deadline:
            _log(f"時間内に落ち着かなかったターゲットがあります（そのまま続けます）: {unsettled}")
            return states
        time.sleep(15)


def _build_test_env(
    session: Any, opts: AwsOptions, infra: dict[str, Any], cert_arn: str
) -> TestEnv:
    """
    CloudFormation スタックの出力と EC2/ECS への追加問い合わせから、
    ゴールデン比較用の `TestEnv` を組み立てる。

    :param session: boto3.Session インスタンス
    :param opts: AWS テストオプション
    :param infra: infra.yaml のデータ辞書
    :param cert_arn: インポートした ACM 証明書の ARN
    :return: 実AWS上の実IDとプレースホルダ対応表を持つ TestEnv インスタンス
    """
    out = _outputs(session, opts.stack_name)
    env = TestEnv(session=session, region=opts.region, infra=infra)
    env.vpc_id = out["VpcId"]
    env.real_vpc_ipv6_cidr = out.get("VpcIpv6Cidr", "")
    env.subnet_ids = {s["key"]: out[logical_id("Subnet", s["key"])] for s in infra["subnets"]}
    env.sg_ids = {sg["key"]: out[logical_id("Sg", sg["key"])] for sg in infra["security_groups"]}
    env.instance_ids = {
        i["key"]: out[logical_id("Instance", i["key"])] for i in infra["ec2_instances"]
    }
    env.lambda_arns = {
        fn["key"]: out[f"{logical_id('Lambda', fn['key'])}Arn"]
        for fn in infra.get("lambda_functions", [])
    }
    env.tg_arns = _tg_arns(out, infra)
    env.lb_arns = {
        lb["key"]: out[f"{logical_id('Lb', lb['key'])}Arn"] for lb in infra["load_balancers"]
    }
    env.zone_ids = {
        z["name"]: out[f"Zone{idx}Id"] for idx, z in enumerate(infra.get("route53_zones", []))
    }
    env.cert_arns = {infra["certificates"][0]["key"]: cert_arn}

    ec2 = session.client("ec2")
    for sg in ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [env.vpc_id]},
            {"Name": "group-name", "Values": ["default"]},
        ]
    )["SecurityGroups"]:
        env.sg_ids["default"] = sg["GroupId"]
    for res in ec2.describe_instances(InstanceIds=list(env.instance_ids.values()))["Reservations"]:
        for inst in res["Instances"]:
            key = next(k for k, v in env.instance_ids.items() if v == inst["InstanceId"])
            for ni in inst.get("NetworkInterfaces", []):
                env.eni_ids[key] = ni["NetworkInterfaceId"]
    for eni in infra.get("load_balancer_enis", []):
        for ni in ec2.describe_network_interfaces(
            Filters=[
                {"Name": "vpc-id", "Values": [env.vpc_id]},
                {"Name": "addresses.private-ip-address", "Values": [eni["private_ip"]]},
            ]
        )["NetworkInterfaces"]:
            env.eni_ids[eni["key"]] = ni["NetworkInterfaceId"]

    extra: list[tuple[str, str]] = [(opts.account_id, "<account>")]
    extra += [
        (out[f"{logical_id('Lb', lb['key'])}Dns"], f"<dns:{lb['key']}>")
        for lb in infra["load_balancers"]
    ]
    # ECS タスク（ID / IP / ENI は毎回変わる）
    ecs = session.client("ecs")
    for idx, spec in enumerate(infra.get("ecs_clusters", [])):
        cluster, service = out[f"EcsCluster{idx}Name"], out[f"EcsService{idx}Name"]
        task_arns = ecs.list_tasks(cluster=cluster, serviceName=service)["taskArns"]
        for t_idx, task in enumerate(
            ecs.describe_tasks(cluster=cluster, tasks=task_arns)["tasks"] if task_arns else []
        ):
            label = spec["service_name"] if t_idx == 0 else f"{spec['service_name']}#{t_idx}"
            extra.append((task["taskArn"].split("/")[-1], f"<ecs-task:{label}>"))
            for att in task.get("attachments", []):
                for detail in att.get("details", []):
                    if detail["name"] == "privateIPv4Address":
                        env.extra_patterns.append(
                            (
                                re.compile(rf"(?<![\d.]){re.escape(detail['value'])}(?![\d.])"),
                                f"<ecs-ip:{label}>",
                            )
                        )
                    elif detail["name"] == "networkInterfaceId":
                        extra.append((detail["value"], f"<eni:ecs-task:{label}>"))
                extra.append((att.get("id", ""), f"<ecs-attachment:{label}>"))
        # タスク定義のリビジョンは、同じファミリーを作り直すたびに増える
        env.extra_patterns.append(
            (re.compile(rf"\b({re.escape(spec['task_family'])}):\d+\b"), r"\1:<rev>")
        )
    # prefix / suffix を付けた名前は、元の名前に戻して比較する（誰が実行しても同じ期待出力になる）
    for pairs in resource_names(infra, opts).values():
        extra += [(full, base) for base, full in pairs if full != base]
    env.extra_placeholders = extra
    # ケースのコマンドに書いた元の名前は、prefix / suffix を付けた名前にして実行する
    env.command_names = command_names(infra, opts)
    return env


def _tg_arns(out: dict[str, str], infra: dict[str, Any]) -> dict[str, str]:
    """
    スタックの出力から、TG の key -> ARN の辞書を作る。

    :param out: `_outputs` の結果
    :param infra: infra.yaml のデータ辞書
    :return: TG の key -> ARN の辞書
    """
    return {tg["key"]: out[f"{logical_id('Tg', tg['key'])}Arn"] for tg in infra["target_groups"]}


@contextlib.contextmanager
def aws_environment(opts: AwsOptions, infra: dict[str, Any] | None = None) -> Iterator[TestEnv]:
    """
    実際の AWS に CloudFormation でテスト環境を構築して `TestEnv` を返し、
    終了時に削除するコンテキストマネージャ。
    `--aws-keep-stack` 指定時は、既存の完成済みスタックがあれば使い回し、終了時も削除しない。

    :param opts: AWS テストオプション
    :param infra: infra.yaml のデータ辞書（省略時は自動読み込み）
    :return: 実AWS環境の TestEnv インスタンスを yield するイテレータ
    :raises AwsTestError: 事前確認失敗・スタック作成失敗・削除失敗時
    """
    infra = for_real_aws(infra or load_infra())
    check = preflight(opts, infra)
    session = check.session
    _log(
        f"アカウント {check.account_id}（{check.caller_arn}）、リージョン {opts.region}、"
        f"スタック {opts.stack_name}" + describe_sso(check.sso)
    )
    for warning in check.warnings:
        _log(f"注意: {warning}")

    reuse = opts.keep_stack and check.stack_status in _COMPLETE
    if check.stack_status is not None and not reuse:
        raise AwsTestError(
            f"スタック {opts.stack_name} が既にあります（状態: {check.stack_status}）。"
            " `uv run python -m tests.aws cleanup ...` で削除するか、"
            "--aws-keep-stack で使い回してください。"
        )

    created = False
    try:
        if reuse:
            _log("既存のスタックを使い回します")
            certs = _tagged_certificates(session, opts.stack_name)
            if not certs:
                raise AwsTestError(
                    "使い回すスタックの証明書が見つかりません。cleanup で削除し直してください"
                )
            cert_arn = certs[0]
        else:
            created = True
            cert_arn = _import_certificate(session, infra, opts.stack_name)
            _create_stack(session, opts, check.template, cert_arn)

        # ECS のタスクの ID・IP・ENI は、タスクが TG に登録されてから集める
        # （CloudFormation はタスクの起動を待たずにサービスを作成済みにすることがある）
        out = _outputs(session, opts.stack_name)
        tg_arns = _tg_arns(out, infra)
        ecs_tgs = [tg_arns[c["target_group"]] for c in infra.get("ecs_clusters", [])]
        ipv6_env = TestEnv(session=session, region=opts.region, infra=infra)
        ipv6_env.real_vpc_ipv6_cidr = out.get("VpcIpv6Cidr", "")
        wait_for_health(
            session, list(tg_arns.values()), ecs_tgs, never_healthy_targets(infra, ipv6_env)
        )
        yield _build_test_env(session, opts, infra, cert_arn)
    finally:
        if opts.keep_stack:
            _log(
                f"--aws-keep-stack のため、スタック {opts.stack_name} を残します"
                "（料金がかかり続けます）"
            )
        elif created or reuse:
            try:
                delete_environment(session, opts)
            except Exception as exc:
                _log(
                    f"削除に失敗しました: {exc}\n"
                    "  次のコマンドで削除し直してください（SSO が切れた場合は先にログイン）:\n"
                    f"  uv run python -m tests.aws cleanup --aws-profile {opts.profile} "
                    f"--aws-account-id {opts.account_id} --aws-region {opts.region}"
                    + (f" --aws-name-prefix {opts.name_prefix}" if opts.name_prefix else "")
                    + (f" --aws-name-suffix {opts.name_suffix}" if opts.name_suffix else "")
                )
                raise
