"""
実際の AWS 用の仕組みのうち、AWS に接続せずに確認できる部分のテスト（料金はかからない）。

- infra.yaml が外部公開の方針を守っているか
- オプション・リソース名の確認
- 生成した CloudFormation テンプレートの安全面（パブリック IP、SG、IAM の Boundary / パス、名前）
- SSO トークンの残り時間の読み取り、自己署名証明書の生成
- prefix / suffix を付けた名前でのケースのコマンドの実行、作成前の確認の警告
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from .aws.options import (
    AwsOptions,
    command_names,
    sso_token_status,
    validate_options,
)
from .aws.stack import _self_signed_certificate, never_healthy_targets
from .aws.template import build_template, template_body
from .harness import TestEnv, check_exposure_policy, for_real_aws, load_infra

ACCOUNT = "123456789012"
# Amazon が VPC に割り当てた IPv6 の CIDR を指す、CloudFormation の式
_VPC_IPV6_CIDR = {"Fn::Select": [0, {"Fn::GetAtt": ["Vpc", "Ipv6CidrBlocks"]}]}


def _opts(**kwargs: Any) -> AwsOptions:
    return AwsOptions(profile="test", account_id=ACCOUNT, **kwargs)


def _resources(template: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [r for r in template["Resources"].values() if r["Type"] == resource_type]


def _by_key(infra: dict[str, Any], kind: str, key: str) -> dict[str, Any]:
    return next(r for r in infra[kind] if r["key"] == key)


# =============================================================================
# 外部公開の方針
# =============================================================================


def test_infra_follows_exposure_policy() -> None:
    """infra.yaml は外部公開の方針を守っている（守れていないと実際の AWS では作成前に止まる）。"""
    assert check_exposure_policy(load_infra()) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda i: i["security_groups"][0]["ingress"].append(
                {"protocol": "tcp", "port": 443, "cidr": "0.0.0.0/0"}
            ),
            "VPC 外の 0.0.0.0/0",
        ),
        (
            lambda i: i["security_groups"][0]["ingress"].append(
                {"protocol": "tcp", "port": 443, "ipv6": "::/0"}
            ),
            "IPv6",
        ),
        (lambda i: i["load_balancers"][1].update(scheme="internet-facing"), "SG がありません"),
        (
            lambda i: _by_key(i, "load_balancers", "v6_alb").pop("security_groups"),
            "SG がありません",
        ),
        (
            lambda i: _by_key(i, "security_groups", "v6_alb_sg")["ingress"].append(
                {"protocol": "tcp", "port": 80, "ipv6": "2001:db8:2::/64"}
            ),
            "VPC 外の IPv6 2001:db8:2::/64",
        ),
    ],
    ids=[
        "public_ipv4_source",
        "ipv6_source",
        "internet_facing_without_sg",
        "dualstack_without_sg",
        "ipv6_source_outside_vpc",
    ],
)
def test_exposure_policy_detects_violations(mutate: Any, expected: str) -> None:
    """外部からのアクセスを許可する構成は、違反として検出される。"""
    infra = copy.deepcopy(load_infra())
    mutate(infra)
    assert any(expected in v for v in check_exposure_policy(infra))


# =============================================================================
# オプション・名前
# =============================================================================


def test_default_options_are_valid() -> None:
    assert validate_options(load_infra(), _opts()) == []


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"account_id": "12345"}, "12桁"),
        ({"name_prefix": "a-very-long-prefix-"}, "名前が長すぎます"),
        ({"name_prefix": "internal-"}, "'internal-' で始められません"),
        ({"name_prefix": "bad_prefix"}, "英数字とハイフンだけ"),
        ({"name_suffix": "-x-"}, "ハイフンで終えられません"),
        ({"iam_path": "elbx"}, "/ で始まり / で終わる"),
        ({"permissions_boundary": "my-boundary"}, "ARN を指定"),
    ],
    ids=[
        "account_id",
        "too_long",
        "internal_prefix",
        "bad_chars",
        "suffix_hyphen",
        "iam_path",
        "boundary",
    ],
)
def test_invalid_options_are_rejected(kwargs: dict[str, Any], expected: str) -> None:
    opts = AwsOptions(profile="test", **{"account_id": ACCOUNT, **kwargs})
    assert any(expected in e for e in validate_options(load_infra(), opts))


# =============================================================================
# CloudFormation テンプレート
# =============================================================================


def test_template_never_assigns_public_ip_to_ec2_or_subnets() -> None:
    template = build_template(load_infra(), _opts())
    for inst in _resources(template, "AWS::EC2::Instance"):
        assert all(
            ni["AssociatePublicIpAddress"] is False
            for ni in inst["Properties"]["NetworkInterfaces"]
        )
    for subnet in _resources(template, "AWS::EC2::Subnet"):
        props = subnet["Properties"]
        if "CidrBlock" in props:
            assert props["MapPublicIpOnLaunch"] is False
        else:  # IPv6 専用のサブネットには、IPv4 のアドレスそのものがない
            assert props["Ipv6Native"] is True


def test_template_has_no_ipv6_route_to_the_internet() -> None:
    """IPv6 のアドレスはグローバルなので、インターネットへの IPv6 の経路（::/0）を作らない。"""
    template = build_template(load_infra(), _opts())
    for route in _resources(template, "AWS::EC2::Route"):
        assert "DestinationIpv6CidrBlock" not in route["Properties"]
    assert not _resources(template, "AWS::EC2::EgressOnlyInternetGateway")


def test_template_security_groups_allow_only_vpc_sources() -> None:
    template = build_template(load_infra(), _opts())
    for sg in _resources(template, "AWS::EC2::SecurityGroup"):
        for rule in sg["Properties"]["SecurityGroupIngress"]:
            # IPv4 は VPC の CIDR、IPv6 は Amazon が VPC に割り当てた CIDR だけを許可する
            if "CidrIpv6" in rule:
                assert rule["CidrIpv6"] == _VPC_IPV6_CIDR
                assert "CidrIp" not in rule
            else:
                assert rule["CidrIp"].startswith("10.0.")


def test_ipv6_is_built_from_the_cidr_amazon_assigns() -> None:
    """
    IPv6 の構成は、infra.yaml の文書用アドレスそのものではなく、Amazon が割り当てる /56 の中の
    同じ位置のアドレスで作る。
    """
    infra = load_infra()
    template = build_template(infra, _opts())
    resources = template["Resources"]
    assert "2001:db8" not in template_body(infra, _opts())
    assert resources["VpcIpv6"]["Properties"]["AmazonProvidedIpv6CidrBlock"] is True

    # サブネットは、割り当てられた /56 を /64 に分けた、infra.yaml と同じ番号のもの
    def subnet_number(lid: str) -> int:
        select = resources[lid]["Properties"]["Ipv6CidrBlock"]["Fn::Select"]
        assert select[1] == {"Fn::Cidr": [_VPC_IPV6_CIDR, 256, "64"]}
        return int(select[0])

    assert [
        subnet_number(lid) for lid in ("SubnetPublic1a", "SubnetPublic1c", "SubnetV6only1a")
    ] == [
        1,
        2,
        3,
    ]
    assert all(
        subnet["DependsOn"] == "VpcIpv6" for subnet in _resources(template, "AWS::EC2::Subnet")
    )

    # アドレスは「そのサブネットの CIDR の先頭 + 同じ下位」で作る（2001:db8:1:1::31 -> ::31）
    def address(lid: str, suffix: str) -> dict[str, Any]:
        subnet_cidr = {"Fn::Select": [0, {"Fn::GetAtt": [lid, "Ipv6CidrBlocks"]}]}
        prefix = {"Fn::Select": [0, {"Fn::Split": ["::/64", subnet_cidr]}]}
        return {"Fn::Join": ["", [prefix, suffix]]}

    interface = resources["InstanceV6Web01"]["Properties"]["NetworkInterfaces"][0]
    assert interface["Ipv6Addresses"] == [{"Ipv6Address": address("SubnetPublic1a", "::31")}]
    assert interface["GroupSet"] == [{"Ref": "SgAlbSg"}, {"Ref": "SgV6WebSg"}]
    tg = resources["TgV6IpTg"]["Properties"]
    assert tg["IpAddressType"] == "ipv6"
    assert [t["Id"] for t in tg["Targets"]] == [
        address("SubnetPublic1a", "::31"),
        address("SubnetPublic1c", "::50"),
        address("SubnetV6only1a", "::50"),
    ]
    assert resources["LbV6Alb"]["Properties"]["IpAddressType"] == "dualstack"
    rule = resources["LbV6AlbListener80Rule10"]
    assert rule["Properties"]["Conditions"][0]["SourceIpConfig"]["Values"] == [_VPC_IPV6_CIDR]
    assert rule["DependsOn"] == "VpcIpv6"
    # IPv6 のアドレスを持つインスタンスは、IPv6 でもヘルスチェックに応える
    user_data = resources["InstanceV6Web01"]["Properties"]["UserData"]["Fn::Base64"]
    assert 'http.server "$port" --bind ::' in user_data


def test_listener_with_authentication_gets_ordered_actions() -> None:
    """認証してから転送するリスナーは、書いた順に Order を付けた複数のアクションになる。"""
    template = build_template(load_infra(), _opts())
    actions = template["Resources"]["LbV6AlbListener443"]["Properties"]["DefaultActions"]
    assert [(a["Type"], a["Order"]) for a in actions] == [("authenticate-oidc", 1), ("forward", 2)]
    assert actions[0]["AuthenticateOidcConfig"]["Issuer"] == "https://idp.elbx-test.internal"
    assert actions[0]["AuthenticateOidcConfig"]["OnUnauthenticatedRequest"] == "authenticate"


def _real_env() -> TestEnv:
    """Amazon が `2406:da14:e5a:6f00::/56` を割り当てた、実際の AWS の環境（接続はしない）。"""
    env = TestEnv(session=None, region="ap-northeast-1", infra=load_infra())
    env.real_vpc_ipv6_cidr = "2406:da14:e5a:6f00::/56"
    return env


@pytest.mark.parametrize(
    ("written", "real"),
    [
        ("2001:db8:1:1::31", "2406:da14:e5a:6f01::31"),
        ("2001:0DB8:0001:0001:0000:0000:0000:0031", "2406:DA14:0E5A:6F01:0000:0000:0000:0031"),
        ("2001:db8:1:2::50", "2406:da14:e5a:6f02::50"),
        ("2001:db8:1:1::99", "2406:da14:e5a:6f01::99"),
        ("2001:db8:9::1", "2001:db8:9::1"),
        ("10.0.1.5", "10.0.1.5"),
        ("http://v6.elbx-test.internal/app", "http://v6.elbx-test.internal/app"),
    ],
    ids=["address", "written_in_full", "other_subnet", "source_ip", "outside_vpc", "ipv4", "url"],
)
def test_ipv6_addresses_in_commands_are_read_as_the_real_ones(written: str, real: str) -> None:
    """ケースのコマンドの IPv6 アドレスは、実際の AWS では割り当てられた /56 の同じ位置に読む。"""
    assert _real_env().command_arg(written) == real
    moto = TestEnv(session=None, region="ap-northeast-1", infra=load_infra())
    assert moto.command_arg(written) == written


def test_real_ipv6_addresses_are_shown_as_placeholders() -> None:
    """実際の AWS の IPv6 は環境を作るたびに変わるので、サブネットの key を使った表記で比べる。"""
    text = (
        "ip=2406:da14:e5a:6f01::31 [10.0.1.0/24, 2406:da14:e5a:6f01::/64] "
        "from 2406:da14:e5a:6f00::/56, 2406:da14:e5a:6f03::50"
    )
    for real, shown in _real_env().placeholders():
        text = text.replace(real, shown)
    assert text == (
        "ip=<ipv6:public_1a>::31 [10.0.1.0/24, <ipv6:public_1a>::/64] "
        "from <vpc-ipv6-cidr>, <ipv6:v6only_1a>::50"
    )


def test_targets_that_cannot_become_healthy_are_known_before_waiting() -> None:
    """どのリソースのものでもない IPv6 のターゲットは、ヘルスの安定待ちで待ち続けない。"""
    assert never_healthy_targets(load_infra(), _real_env()) == {
        "2406:da14:e5a:6f02::50",
        "2406:da14:e5a:6f03::50",
    }


def test_internet_facing_load_balancers_have_security_groups() -> None:
    template = build_template(load_infra(), _opts())
    for lb in _resources(template, "AWS::ElasticLoadBalancingV2::LoadBalancer"):
        if lb["Properties"]["Scheme"] == "internet-facing":
            assert lb["Properties"].get("SecurityGroups")


def test_template_applies_name_prefix_suffix_boundary_and_iam_path() -> None:
    opts = _opts(
        name_prefix="ishi-",
        name_suffix="-t1",
        permissions_boundary="arn:aws:iam::123456789012:policy/boundary",
        iam_path="/elbx/",
    )
    template = build_template(load_infra(), opts)
    names = [
        lb["Properties"]["Name"]
        for lb in _resources(template, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    ]
    assert names == [
        "ishi-prod-web-alb-t1",
        "ishi-edge-ingress-nlb-t1",
        "ishi-batch-nlb-t1",
        "ishi-v6-dualstack-alb-t1",
    ]
    tg_names = [
        tg["Properties"]["Name"]
        for tg in _resources(template, "AWS::ElasticLoadBalancingV2::TargetGroup")
    ]
    assert all(n.startswith("ishi-") and n.endswith("-t1") for n in tg_names)
    for role in _resources(template, "AWS::IAM::Role"):
        assert (
            role["Properties"]["PermissionsBoundary"] == "arn:aws:iam::123456789012:policy/boundary"
        )
        assert role["Properties"]["Path"] == "/elbx/"
        assert role["Properties"]["RoleName"] == "ishi-elbx-test-lambda-role-t1"


def test_template_without_boundary_has_no_boundary() -> None:
    template = build_template(load_infra(), _opts())
    for role in _resources(template, "AWS::IAM::Role"):
        assert "PermissionsBoundary" not in role["Properties"]
        assert role["Properties"]["Path"] == "/"


def test_template_fixes_nlb_node_ip_and_lets_ecs_register_task_ips() -> None:
    template = build_template(load_infra(), _opts())
    batch = template["Resources"]["LbBatchNlb"]["Properties"]
    assert batch["SubnetMappings"][0]["PrivateIPv4Address"] == "10.0.1.90"
    ecs_tg = template["Resources"]["TgEcsIpTg"]["Properties"]
    assert "Targets" not in ecs_tg  # ECS サービスがタスクの IP を登録する


def test_every_resource_is_tagged_and_template_fits_size_limit() -> None:
    opts = _opts()
    template = build_template(load_infra(), opts)
    tag_lists = [
        r["Properties"].get("Tags") or r["Properties"].get("HostedZoneTags")
        for r in template["Resources"].values()
        if "Tags" in r["Properties"] or "HostedZoneTags" in r["Properties"]
    ]
    assert tag_lists
    assert all({"Key": "elbx-test", "Value": opts.stack_name} in tags for tags in tag_lists)
    assert len(template_body(load_infra(), opts).encode("utf-8")) <= 51_200


# =============================================================================
# スタックの見張り（失敗をすぐに検知する）
# =============================================================================


class _StubCfn:
    """呼ばれるたびに次の状態を返す CloudFormation のスタブ（イベントは新しい順）。"""

    def __init__(self, polls: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self.polls = polls
        self.calls = 0

    def describe_stacks(self, StackName: str) -> dict[str, Any]:
        status, _ = self.polls[min(self.calls, len(self.polls) - 1)]
        return {"Stacks": [{"StackName": "elbx-test", "StackStatus": status}]}

    def describe_stack_events(self, StackName: str) -> dict[str, Any]:
        _, events = self.polls[min(self.calls, len(self.polls) - 1)]
        self.calls += 1
        return {"StackEvents": events}


def _event(
    eid: str, resource: str, status: str, reason: str = "", minutes_ago: int = 0
) -> dict[str, Any]:
    return {
        "EventId": eid,
        "LogicalResourceId": resource,
        "ResourceStatus": status,
        "ResourceStatusReason": reason,
        "Timestamp": datetime.now(UTC) - timedelta(minutes=minutes_ago),
    }


@pytest.fixture
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    from .aws import stack

    monkeypatch.setattr(stack, "STACK_POLL_SECONDS", 0)


def test_watch_stack_reports_first_resource_failure_immediately(fast_poll: None) -> None:
    """リソースの失敗は、スタック全体が失敗・自動削除される前の最初の見張りで検知する。"""
    from .aws.stack import _watch_stack

    cfn = _StubCfn(
        [
            (
                "CREATE_IN_PROGRESS",  # スタック全体はまだ進行中
                [
                    _event("3", "Vpc", "CREATE_FAILED", "Resource creation cancelled"),
                    _event(
                        "2", "LbEdgeNlb", "CREATE_FAILED", "account does not support load balancers"
                    ),
                    _event("1", "Vpc", "CREATE_IN_PROGRESS"),
                ],
            )
        ]
    )
    with pytest.raises(Exception, match="LbEdgeNlb: account does not support") as info:
        _watch_stack(cfn, "id", action="作成", success={"CREATE_COMPLETE"}, timeout=60)
    assert "cancelled" not in str(info.value)  # 巻き添えの取り消しは原因として出さない
    assert cfn.calls == 1


def test_watch_stack_succeeds_and_ignores_events_before_since(fast_poll: None) -> None:
    """削除の見張りでは、開始前（作成時など）の失敗イベントを無視して完了まで待つ。"""
    from .aws.stack import _watch_stack

    old_failure = _event("1", "Zone0", "CREATE_FAILED", "reserved", minutes_ago=10)
    cfn = _StubCfn(
        [
            ("DELETE_IN_PROGRESS", [old_failure]),
            ("DELETE_COMPLETE", [_event("2", "Vpc", "DELETE_COMPLETE"), old_failure]),
        ]
    )
    since = datetime.now(UTC) - timedelta(minutes=1)
    _watch_stack(cfn, "id", action="削除", success={"DELETE_COMPLETE"}, timeout=60, since=since)
    assert cfn.calls == 2


# =============================================================================
# SSO / 証明書
# =============================================================================


class _FakeSession:
    def __init__(self, config: dict[str, str]) -> None:
        self._session = type("S", (), {"get_scoped_config": lambda _self: config})()


@pytest.mark.parametrize(
    ("key_name", "with_refresh_token", "refreshable"),
    [
        ("sso_session", True, True),  # sso_session 形式は botocore が自動で更新する
        ("sso_session", False, False),
        ("sso_start_url", True, False),  # 旧形式は自動で更新されない
    ],
)
def test_sso_token_status_reads_token_cache(
    tmp_path: Path, key_name: str, with_refresh_token: bool, refreshable: bool
) -> None:
    key = "my-sso" if key_name == "sso_session" else "https://example.awsapps.com/start"
    expires = (datetime.now(UTC) + timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache = tmp_path / f"{hashlib.sha1(key.encode()).hexdigest()}.json"
    token = {"expiresAt": expires, **({"refreshToken": "dummy"} if with_refresh_token else {})}
    cache.write_text(json.dumps(token), encoding="utf-8")

    status = sso_token_status(_FakeSession({key_name: key}), cache_dir=tmp_path)
    assert status is not None
    assert 85 < status.minutes_left <= 90
    assert status.refreshable is refreshable


def test_sso_token_status_is_none_for_non_sso_profile(tmp_path: Path) -> None:
    assert sso_token_status(_FakeSession({}), cache_dir=tmp_path) is None


def test_self_signed_certificate_covers_san() -> None:
    from cryptography import x509

    cert_pem, key_pem = _self_signed_certificate("*.example.com", ["*.example.com", "example.com"])
    cert = x509.load_pem_x509_certificate(cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["*.example.com", "example.com"]
    assert b"PRIVATE KEY" in key_pem


# =============================================================================
# prefix / suffix を付けた名前でケースのコマンドを実行する
# =============================================================================


def test_command_names_map_base_names_to_prefixed_names() -> None:
    """ケースのコマンドに書く元の名前を、prefix / suffix を付けた名前に対応させる。"""
    names = command_names(load_infra(), _opts(name_prefix="ishi-", name_suffix="-t1"))
    assert names["prod-web-alb"] == "ishi-prod-web-alb-t1"
    assert names["nlb-to-alb-tg"] == "ishi-nlb-to-alb-tg-t1"
    assert names["web-prod-01"] == "ishi-web-prod-01-t1"
    assert names["prod-auth-fn"] == "ishi-prod-auth-fn-t1"
    assert names["api-fargate-svc"] == "ishi-api-fargate-svc-t1"
    assert command_names(load_infra(), _opts()) == {}


def test_run_case_command_replaces_resource_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """名前と一致する引数だけを置き換える（部分一致の検索語や URL はそのまま）。"""
    from elbx import cli

    from .conftest import run_case_command

    captured: list[list[str]] = []

    def fake_main(args: list[str], client: Any = None) -> int:
        captured.append(list(args))
        return 0

    monkeypatch.setattr(cli, "main", fake_main)
    env = TestEnv(session=None, region="ap-northeast-1", infra=load_infra())
    env.command_names = {"prod-web-alb": "x-prod-web-alb-y"}
    env.client = lambda: None  # type: ignore[method-assign,assignment,return-value]
    case = SimpleNamespace(state_data=None)
    # pytest-casebook が hook に渡す argv（command_prefix の elbx とコマンドを合わせたもの）
    argv = ["elbx", "route", "https://api.elbx-test.internal/", "--lb", "prod-web-alb"]
    argv += ["--search", "prod-web"]
    run_case_command(case, env, argv)  # type: ignore[arg-type]
    assert captured == [
        [
            "route",
            "https://api.elbx-test.internal/",
            "--lb",
            "x-prod-web-alb-y",
            "--search",
            "prod-web",
        ]
    ]


# =============================================================================
# 作成前の確認の警告（止めずに知らせる）
# =============================================================================


class _StubPaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages

    def paginate(self) -> list[dict[str, Any]]:
        return self.pages


class _StubElbv2:
    def __init__(self, lbs: list[str], tgs: list[str]) -> None:
        self.pages = {
            "describe_load_balancers": [{"LoadBalancers": [{"LoadBalancerName": n} for n in lbs]}],
            "describe_target_groups": [{"TargetGroups": [{"TargetGroupName": n} for n in tgs]}],
        }

    def get_paginator(self, name: str) -> _StubPaginator:
        return _StubPaginator(self.pages[name])


class _StubIam:
    class exceptions:  # noqa: N801 - boto3 と同じ名前
        class NoSuchEntityException(Exception):  # noqa: N818 - boto3 と同じ名前
            pass

    def __init__(self, missing: set[str], denied: set[str]) -> None:
        self.missing = missing
        self.denied = denied

    def get_role(self, RoleName: str) -> dict[str, Any]:
        if RoleName in self.missing:
            raise self.exceptions.NoSuchEntityException(RoleName)
        if RoleName in self.denied:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetRole")
        return {"Role": {"RoleName": RoleName}}


def test_preflight_warns_about_other_load_balancers_in_the_region() -> None:
    """テストで作るもの以外の ELB・TG があれば、期待出力がずれることを知らせる。"""
    from .aws.stack import _other_elb_warnings

    opts = _opts(name_prefix="ishi-")
    ours = SimpleNamespace(client=lambda _: _StubElbv2(["ishi-prod-web-alb"], ["ishi-prod-web-tg"]))
    assert _other_elb_warnings(ours, for_real_aws(load_infra()), opts) == []

    others = SimpleNamespace(
        client=lambda _: _StubElbv2(["ishi-prod-web-alb", "team-alb"], ["prod-web-tg"])
    )
    warnings = _other_elb_warnings(others, for_real_aws(load_infra()), opts)
    assert len(warnings) == 1
    # prefix のない同じ名前は、ほかの人のもの
    assert "team-alb, prod-web-tg" in warnings[0]


def test_preflight_warns_about_missing_service_linked_roles() -> None:
    """サービスリンクロールがなければ作り方を知らせ、確かめる権限がなければ何も言わない。"""
    from .aws.stack import _service_linked_role_warnings

    iam = _StubIam(
        missing={"AWSServiceRoleForECS"}, denied={"AWSServiceRoleForElasticLoadBalancing"}
    )
    warnings = _service_linked_role_warnings(SimpleNamespace(client=lambda _: iam))
    assert len(warnings) == 1
    assert "aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com" in warnings[0]

    iam_all = _StubIam(missing=set(), denied=set())
    assert _service_linked_role_warnings(SimpleNamespace(client=lambda _: iam_all)) == []
