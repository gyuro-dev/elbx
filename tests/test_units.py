"""
ロジック単体のテスト。

infra.yaml の構成（moto）では再現できない状況（ページネーション、bridge モードの複数タスク、
SG のプレフィックスリスト、API エラーなど）を、スタブのクライアントに差し替えて確認する。
出力の見た目は test_scenarios.py（ゴールデンテスト）で確認する。
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from typing import Any

import pytest
from botocore.exceptions import ClientError

from elbx import (
    ECSTaskInfo,
    ELBClient,
    ENIInfo,
    LambdaFunctionInfo,
    ListenerView,
    LoadBalancerView,
    ResourceNotFoundError,
    Route53RecordInfo,
    RouteSimulationResult,
    RuleTargetGroupRef,
    RuleView,
    SubnetInfo,
    TargetGroupView,
    TargetLookupResult,
    TargetView,
    cli,
    format_lb_map,
    format_lb_summary_table,
    format_lb_tree,
    format_lookup_results_table,
    format_route_simulation,
    format_targets_table,
)
from elbx.cli import _only_unhealthy
from elbx.fetchers import (
    Deferred,
    ECSMaps,
    RunCache,
    _decode_route53_name,
    _parse_elb_eni_description,
)
from elbx.formatter import _AnsiColor, _display_width, _health_summary_badge, _render_table
from elbx.hydrator import (
    _ip_targets_outside_load_balancers,
    _LBBatch,
    _link_downstream_lbs,
    _RawTargetGroups,
    _target_groups_outside_load_balancers,
    _TargetIds,
)
from elbx.simulator import SimulatedRequest, simulate_hop

from .harness import TestEnv

ARN_P = "arn:aws:ecs:ap-northeast-1:123456789012"


def _paged(pages: list[dict[str, Any]], token_key: str) -> Any:
    """token_key でページをたどるスタブ API を作る（最終ページ以外にトークンを付ける）。"""

    def _call(**kwargs: Any) -> dict[str, Any]:
        idx = int(kwargs.get(token_key) or 0)
        page = dict(pages[idx])
        if idx + 1 < len(pages):
            page[token_key] = str(idx + 1)
        return page

    return _call


# =============================================================================
# EC2 / ENI の一括取得
# =============================================================================


def test_fetch_ec2_instances_map_ignores_missing_instance_ids(
    elb_client: ELBClient, moto_env: TestEnv
) -> None:
    """存在しないインスタンス ID が混ざっていても、同じチャンクの他の EC2 を取りこぼさない。"""
    real_ids = [moto_env.instance_ids[k] for k in ("web_01", "web_02")]
    ec2_map = elb_client._fetch_ec2_instances_map([real_ids[0], "i-0000000000000dead", real_ids[1]])
    assert set(ec2_map) == set(real_ids)
    assert ec2_map[real_ids[0]].name == "web-prod-01"


def test_ec2_and_eni_fetch_follow_pagination(elb_client: ELBClient) -> None:
    """describe_instances / describe_network_interfaces の2ページ目以降も取得する。"""

    def _inst(i_id: str, ip: str) -> dict[str, Any]:
        return {"Instances": [{"InstanceId": i_id, "PrivateIpAddress": ip}]}

    def _eni(eni_id: str, ip: str) -> dict[str, Any]:
        return {"NetworkInterfaces": [{"NetworkInterfaceId": eni_id, "PrivateIpAddress": ip}]}

    class _StubEC2:
        describe_instances = staticmethod(
            _paged(
                [
                    {"Reservations": [_inst("i-page1", "10.9.0.1")]},
                    {"Reservations": [_inst("i-page2", "10.9.0.2")]},
                ],
                "NextToken",
            )
        )
        describe_network_interfaces = staticmethod(
            _paged([_eni("eni-1", "10.9.0.1"), _eni("eni-2", "10.9.0.2")], "NextToken")
        )

    elb_client.ec2 = _StubEC2()
    assert set(elb_client._fetch_ec2_instances_map(["i-page1", "i-page2"])) == {
        "i-page1",
        "i-page2",
    }
    assert set(elb_client._fetch_ec2_instances_by_ips(["10.9.0.1", "10.9.0.2"])) == {
        "10.9.0.1",
        "10.9.0.2",
    }
    assert set(elb_client._fetch_eni_map_by_ips(["10.9.0.1", "10.9.0.2"])) == {
        "10.9.0.1",
        "10.9.0.2",
    }


@pytest.mark.parametrize(
    ("ips", "expected"),
    [(["10.0.1.10"], []), (["10.0.1.10", "10.0.1.99"], [["10.0.1.99"]])],
    ids=["all_have_an_eni", "one_without_eni"],
)
def test_ip_targets_with_an_eni_are_not_asked_to_ec2_again(
    elb_client: ELBClient, ips: list[str], expected: list[list[str]]
) -> None:
    """
    ENI が見つかった IP は、EC2 の IP として照会し直さない（EC2 のものなら ENI に EC2 の ID が
    入っている。Fargate のタスクの IP で毎回呼ばないため）。照会するのは ENI のない IP だけ。
    """
    asked: list[list[str]] = []

    def by_ips(private_ips: list[str]) -> dict[str, Any]:
        asked.append(sorted(private_ips))
        return {}

    task_eni = ENIInfo(network_interface_id="eni-task", private_ip="10.0.1.10")
    elb_client._fetch_eni_map_by_ips = lambda _ips: {  # type: ignore[method-assign]
        "10.0.1.10": {"vpc-1": task_eni}
    }
    elb_client._fetch_ec2_instances_by_ips = by_ips  # type: ignore[method-assign]
    ids = _TargetIds(ip_targets=set(ips), ip_target_keys={("vpc-1", ip) for ip in ips})

    eni_by_ip = elb_client._resolve_enis(ids)
    _ec2_by_id, ec2_by_ip = elb_client._resolve_ec2(ids, eni_by_ip)
    assert eni_by_ip == {("vpc-1", "10.0.1.10"): task_eni}
    assert ec2_by_ip == {}
    assert asked == expected


# =============================================================================
# ELB のノードの ENI から ELB を特定する
# =============================================================================


def test_parse_elb_eni_description() -> None:
    """ELB のノードの ENI の説明から ELB 名と ID を取り出す。それ以外の説明は None。"""
    assert _parse_elb_eni_description("ELB net/my-nlb/50dc6c495c0c9188") == (
        "my-nlb",
        "50dc6c495c0c9188",
    )
    assert _parse_elb_eni_description("ELB app/my-alb/50dc6c495c0c9188") == (
        "my-alb",
        "50dc6c495c0c9188",
    )
    assert _parse_elb_eni_description("ELB my-classic-elb") is None
    assert _parse_elb_eni_description("Primary network interface") is None


def test_resolve_elb_enis_checks_name_and_id(elb_client: ELBClient, moto_env: TestEnv) -> None:
    """名前と ID の両方が一致する ELB だけを対応付ける（同名で作り直された ELB を取り違えない）。"""
    nlb_arn = moto_env.lb_arns["batch_nlb"]
    nlb_id = nlb_arn.split("/")[-1]

    def _eni(eni_id: str, description: str) -> ENIInfo:
        return ENIInfo(network_interface_id=eni_id, private_ip="10.0.9.9", description=description)

    current = _eni("eni-current", f"ELB net/batch-nlb/{nlb_id}")
    recreated = _eni("eni-old", "ELB net/batch-nlb/0000000000000000")
    missing = _eni("eni-missing", "ELB net/no-such-nlb/1111111111111111")
    other = _eni("eni-other", "Primary network interface")

    elb_client._resolve_elb_enis([current, recreated, missing, other])

    assert (current.load_balancer_name, current.load_balancer_arn) == (
        "batch-nlb",
        nlb_arn,
    )
    for eni in (recreated, missing, other):
        assert (eni.load_balancer_name, eni.load_balancer_arn) == ("", "")


# =============================================================================
# ECS タスクとターゲットの対応付け
# =============================================================================


def test_ecs_maps_follow_pagination(elb_client: ELBClient) -> None:
    """ECS のクラスター・サービス・タスク・コンテナインスタンス一覧の2ページ目以降も走査する。"""
    tg_arn = "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:targetgroup/paged/1"
    ci_arn = f"{ARN_P}:container-instance/c2/ci-page2"

    def _task(cluster: str, task: str, ip: str) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "taskArn": f"{ARN_P}:task/{cluster}/{cluster}-{task}",
            "group": "service:svc-page2",
            "attachments": [{"details": [{"name": "privateIPv4Address", "value": ip}]}],
        }
        if (cluster, task) == ("c2", "t2"):
            spec["containerInstanceArn"] = ci_arn
        return spec

    class _StubECS:
        list_clusters = staticmethod(
            _paged(
                [
                    {"clusterArns": [f"{ARN_P}:cluster/c1"]},
                    {"clusterArns": [f"{ARN_P}:cluster/c2"]},
                ],
                "nextToken",
            )
        )
        list_services = staticmethod(
            _paged([{"serviceArns": ["svc-page1"]}, {"serviceArns": ["svc-page2"]}], "nextToken")
        )
        list_container_instances = staticmethod(
            _paged(
                [{"containerInstanceArns": []}, {"containerInstanceArns": [ci_arn]}], "nextToken"
            )
        )
        list_tasks = staticmethod(_paged([{"taskArns": ["t1"]}, {"taskArns": ["t2"]}], "nextToken"))

        @staticmethod
        def describe_services(cluster: str, services: list[str]) -> dict[str, Any]:
            return {
                "services": [
                    {
                        "serviceName": s,
                        "loadBalancers": [{"targetGroupArn": tg_arn}] if s == "svc-page2" else [],
                    }
                    for s in services
                ]
            }

        @staticmethod
        def describe_container_instances(
            cluster: str, containerInstances: list[str]
        ) -> dict[str, Any]:
            return {
                "containerInstances": [
                    {"containerInstanceArn": c, "ec2InstanceId": "i-ecs-page2"}
                    for c in containerInstances
                ]
            }

        @staticmethod
        def describe_tasks(cluster: str, tasks: list[str]) -> dict[str, Any]:
            c_name = cluster.rsplit("/", 1)[-1]
            ips = {"t1": "10.8.0.1", "t2": "10.8.0.2"}
            return {"tasks": [_task(c_name, t, ips[t]) for t in tasks]}

    elb_client.ecs = _StubECS()
    maps = elb_client._fetch_ecs_maps({"10.8.0.1", "10.8.0.2"}, {"i-ecs-page2"}, {tg_arn})

    # 2ページ目のクラスター (c2) の2ページ目のタスク (t2) まで解決されている
    task = maps.task_for_ip("10.8.0.2", "")
    assert task is not None
    assert task.cluster_name == "c2"
    assert task.task_id == "c2-t2"
    assert maps.by_instance["i-ecs-page2"].task_id == "c2-t2"
    # 2ページ目のサービス (svc-page2) の TG 対応はサービス単位の情報（タスクで上書きしない）
    assert maps.by_tg_arn[tg_arn].service_name == "svc-page2"
    assert maps.by_tg_arn[tg_arn].task_id == ""


def test_ecs_bridge_mode_multiple_tasks_per_host_resolved_by_port(elb_client: ELBClient) -> None:
    """bridge モードで1台に複数タスクがあるとき、(ホスト, ポート) で区別する。"""
    ci_arn = f"{ARN_P}:container-instance/c/ci-1"

    def _bridge_task(task_id: str, host_port: int) -> dict[str, Any]:
        return {
            "taskArn": f"{ARN_P}:task/c/{task_id}",
            "group": "service:web",
            "launchType": "EC2",
            "containerInstanceArn": ci_arn,
            "containers": [{"name": "app", "networkBindings": [{"hostPort": host_port}]}],
        }

    class _StubECS:
        list_clusters = staticmethod(lambda **_: {"clusterArns": [f"{ARN_P}:cluster/c"]})
        list_services = staticmethod(lambda **_: {"serviceArns": []})
        list_tasks = staticmethod(lambda **_: {"taskArns": ["a", "b"]})
        list_container_instances = staticmethod(lambda **_: {"containerInstanceArns": [ci_arn]})

        @staticmethod
        def describe_container_instances(**_: Any) -> dict[str, Any]:
            return {
                "containerInstances": [{"containerInstanceArn": ci_arn, "ec2InstanceId": "i-host"}]
            }

        @staticmethod
        def describe_tasks(**_: Any) -> dict[str, Any]:
            return {"tasks": [_bridge_task("task-a", 32768), _bridge_task("task-b", 32769)]}

    elb_client.ecs = _StubECS()
    maps = elb_client._fetch_ecs_maps(set(), {"i-host"}, set())

    assert "i-host" not in maps.by_instance
    assert maps.by_instance_port[("i-host", 32768)].task_id == "task-a"
    assert maps.by_instance_port[("i-host", 32769)].task_id == "task-b"


def test_ecs_task_with_known_host_port_is_matched_only_by_that_port(
    elb_client: ELBClient,
) -> None:
    """
    hostPort が分かっているタスクは、そのホストで 1 つだけでも (ホスト, ポート) でだけ照合する。
    同じホストを別のポートで登録したターゲット（ECS ではないプロセス）を、そのタスクにしない。
    """
    ci_arn = f"{ARN_P}:container-instance/c/ci-1"

    class _StubECS:
        list_clusters = staticmethod(lambda **_: {"clusterArns": [f"{ARN_P}:cluster/c"]})
        list_services = staticmethod(lambda **_: {"serviceArns": []})
        list_tasks = staticmethod(lambda **_: {"taskArns": ["a"]})
        list_container_instances = staticmethod(lambda **_: {"containerInstanceArns": [ci_arn]})

        @staticmethod
        def describe_container_instances(**_: Any) -> dict[str, Any]:
            return {
                "containerInstances": [{"containerInstanceArn": ci_arn, "ec2InstanceId": "i-host"}]
            }

        @staticmethod
        def describe_tasks(**_: Any) -> dict[str, Any]:
            task = {
                "taskArn": f"{ARN_P}:task/c/agent",
                "group": "service:agent",
                "containerInstanceArn": ci_arn,
                "containers": [{"name": "agent", "networkBindings": [{"hostPort": 8125}]}],
            }
            return {"tasks": [task]}

    elb_client.ecs = _StubECS()
    maps = elb_client._fetch_ecs_maps(set(), {"i-host"}, set())

    assert maps.by_instance_port[("i-host", 8125)].task_id == "agent"
    assert "i-host" not in maps.by_instance


# =============================================================================
# セキュリティグループ / API エラー
# =============================================================================


def test_security_group_rules_include_prefix_lists_and_outbound(elb_client: ELBClient) -> None:
    """SG ルールにプレフィックスリストの参照とアウトバウンドルールが含まれる。"""

    class _StubEC2:
        @staticmethod
        def describe_security_groups(**_: Any) -> dict[str, Any]:
            return {
                "SecurityGroups": [
                    {
                        "GroupId": "sg-cf",
                        "GroupName": "from-cloudfront",
                        "IpPermissions": [
                            {
                                "IpProtocol": "tcp",
                                "FromPort": 443,
                                "ToPort": 443,
                                "PrefixListIds": [
                                    {"PrefixListId": "pl-58a04531", "Description": "CloudFront"}
                                ],
                            }
                        ],
                        "IpPermissionsEgress": [
                            {
                                "IpProtocol": "tcp",
                                "FromPort": 8080,
                                "ToPort": 8080,
                                "UserIdGroupPairs": [{"GroupId": "sg-target"}],
                            }
                        ],
                    }
                ]
            }

    elb_client.ec2 = _StubEC2()
    sg = elb_client._fetch_security_groups_map(["sg-cf"])["sg-cf"]

    assert [(r.port_range, r.source_or_dest) for r in sg.inbound_rules] == [("443", "pl-58a04531")]
    assert [(r.port_range, r.source_or_dest) for r in sg.outbound_rules] == [("8080", "sg-target")]


def test_describe_target_health_error_is_not_hidden(elb_client: ELBClient) -> None:
    """ヘルス取得の失敗を「ターゲット未登録」として隠さず、例外として通知する。"""

    def _denied(**_: Any) -> dict[str, Any]:
        raise RuntimeError("AccessDenied: elasticloadbalancing:DescribeTargetHealth")

    elb_client.elbv2.describe_target_health = _denied
    with pytest.raises(RuntimeError, match="AccessDenied"):
        elb_client.get_target_group("prod-web-tg")


# =============================================================================
# ライブラリ API / フォーマッタ
# =============================================================================


def test_list_load_balancers_without_relations_returns_basic_view(elb_client: ELBClient) -> None:
    """include_relations=False では ELB 本体だけを返す（リスナー・TG は取得しない）。"""
    lbs = elb_client.list_load_balancers(names=["prod-web-alb"], include_relations=False)
    assert [lb.name for lb in lbs] == ["prod-web-alb"]
    assert lbs[0].listeners == []
    assert lbs[0].target_groups == []


def test_tree_with_color_and_eni_target(elb_client: ELBClient) -> None:
    """カラー出力と、ENI に解決された IP ターゲットの表示（moto では作れない構成）。"""
    lb = elb_client.get_load_balancer("prod-web-alb", include_network=False, include_dns=False)
    lb.target_groups.append(
        TargetGroupView(
            arn="arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:targetgroup/custom/1",
            name="custom-eni-tg",
            protocol="HTTP",
            port=80,
            target_type="ip",
            targets=[
                TargetView(
                    target_id="10.0.9.1",
                    port=80,
                    health_state="unhealthy",
                    health_reason="Target.FailedHealthChecks",
                    health_description="502 Bad Gateway",
                    eni=ENIInfo(
                        network_interface_id="eni-0123456789",
                        private_ip="10.0.9.1",
                        availability_zone="ap-northeast-1a",
                        description="VPC Endpoint ENI",
                    ),
                ),
            ],
        )
    )
    out = format_lb_tree(lb, use_color=True)
    assert "\033[" in out
    assert "[IP/ENI]" in out
    assert "eni=eni-0123456789" in out
    assert "1 unhealthy" in out


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--profile", "before", "ls"], {"profile": "before"}),
        (["ls", "--profile", "after"], {"profile": "after"}),
        (["--region", "us-east-1", "tree", "x"], {"region": "us-east-1"}),
        (["-j", "3", "find", "q"], {"workers": 3}),
        (["--no-color", "route", "https://a.example.org/"], {"no_color": True}),
        (["ls"], {"profile": None, "region": None, "workers": 10, "no_color": False}),
    ],
    ids=[
        "before_sub",
        "after_sub",
        "region_before",
        "workers_before",
        "no_color_before",
        "defaults",
    ],
)
def test_common_options_work_before_and_after_subcommand(
    argv: list[str], expected: dict[str, Any]
) -> None:
    """共通オプションはサブコマンドの前後どちらに書いても効く（前に書いた値が既定値で消えない）。"""
    from elbx.cli import build_parser

    args = build_parser().parse_args(argv)
    for key, value in expected.items():
        assert getattr(args, key) == value


@pytest.mark.parametrize("partition", ["aws", "aws-cn", "aws-us-gov"])
def test_arn_targets_are_recognized_in_every_partition(partition: str) -> None:
    """中国（aws-cn）や GovCloud（aws-us-gov）の ARN でも、ELB・Lambda のターゲットと判定する。"""
    region = {"aws": "ap-northeast-1", "aws-cn": "cn-north-1", "aws-us-gov": "us-gov-west-1"}
    prefix = f"arn:{partition}:%s:{region[partition]}:123456789012"
    lambda_target = TargetView(
        target_id=f"{prefix % 'lambda'}:function:fn", port=0, health_state="healthy"
    )
    nlb_target = TargetView(
        target_id=f"{prefix % 'elasticloadbalancing'}:loadbalancer/net/nlb/0123456789abcdef",
        port=80,
        health_state="healthy",
    )
    assert lambda_target.target_kind == "lambda"
    assert nlb_target.target_kind == "nlb"
    assert lambda_target.ip_address == nlb_target.ip_address == ""


def test_table_columns_line_up_with_full_width_characters() -> None:
    """全角文字（日本語の Name タグなど）は 2 カラムと数え、どの行も次の列が同じ位置から始まる。"""
    table = _render_table(["NAME", "PORT"], [["本番Web01", "80"], ["web-02", "8080"]], False)
    lines = table.splitlines()
    # 2 列目の文字列（見出し・区切り線・各行）が始まるまでの表示幅
    starts = [
        _display_width(line[: line.rindex(cell)])
        for line, cell in zip(lines, ["PORT", "----", "80", "8080"], strict=True)
    ]
    assert starts == [11, 11, 11, 11]
    assert _display_width("本番Web01") == 9
    assert _display_width("é") == 1  # 結合文字は幅 0


def test_target_group_only_in_rules_belongs_to_every_load_balancer(elb_client: ELBClient) -> None:
    """ルールだけから参照される TG は、参照するすべての ELB の TG に入る（先の ELB だけでなく）。"""
    tg = {"TargetGroupArn": "tg-shared", "LoadBalancerArns": []}
    raw_tgs = _RawTargetGroups(by_lb={"lb-1": [], "lb-2": []}, pool={"tg-shared": tg})
    forward = {"Actions": [{"Type": "forward", "TargetGroupArn": "tg-shared"}]}
    batch = _LBBatch(
        listeners_by_lb={"lb-1": [{"ListenerArn": "l-1"}], "lb-2": [{"ListenerArn": "l-2"}]},
        rules_by_listener={"l-1": [forward, forward], "l-2": [forward]},
    )
    elb_client._add_target_groups_of_rules(raw_tgs, batch)
    assert raw_tgs.by_lb == {"lb-1": [tg], "lb-2": [tg]}


def test_route_does_not_fetch_certificates_or_attributes(
    elb_client: ELBClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """route の評価では使わない ACM 証明書と ELB・TG の属性は取得しない（下流の ELB でも）。"""

    def _unused(*_: Any) -> Any:
        raise AssertionError("not needed by route")

    for name in ("_fetch_certificates_map", "_fetch_lb_attributes", "_fetch_tg_attributes"):
        monkeypatch.setattr(elb_client, name, _unused)
    result = elb_client.simulate_request("https://api.elbx-test.internal/v1/users")
    assert result.entries


def test_parallel_map(elb_client: ELBClient) -> None:
    """並列実行ヘルパーが入力順を保って結果を返す。"""
    assert elb_client._parallel_map(lambda x: x * 2, []) == []
    assert elb_client._parallel_map(lambda x: x * 2, [1, 2, 3]) == [2, 4, 6]


# =============================================================================
# IPv6 ターゲットの解決と検索
# =============================================================================


def test_ipv6_targets_eni_ec2_ecs_and_subnet_resolution(elb_client: ELBClient) -> None:
    """
    IPv6 の ip ターゲットについて、ENI・デュアルスタックEC2・ECS Fargateタスク・
    デュアルスタックサブネット（Ipv6CidrBlockAssociationSet）が正しく紐づくことを確認する。
    また、省略形と非省略形（2001:0db8:1::10 と 2001:db8:1::10）の表記揺れも吸収する。
    """
    tg_ec2_arn = "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:targetgroup/v6-ec2-tg/1"
    tg_ecs_arn = "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:targetgroup/v6-ecs-tg/2"
    raw_tgs = [
        {
            "TargetGroupArn": tg_ec2_arn,
            "TargetGroupName": "v6-ec2-tg",
            "Protocol": "HTTP",
            "Port": 80,
            "TargetType": "ip",
            "VpcId": "vpc-v6",
            "HealthCheckProtocol": "HTTP",
            "HealthCheckPort": "traffic-port",
            "HealthCheckPath": "/healthz",
        },
        {
            "TargetGroupArn": tg_ecs_arn,
            "TargetGroupName": "v6-ecs-tg",
            "Protocol": "HTTP",
            "Port": 80,
            "TargetType": "ip",
            "VpcId": "vpc-v6",
            "HealthCheckProtocol": "HTTP",
            "HealthCheckPort": "traffic-port",
            "HealthCheckPath": "/healthz",
        },
    ]

    class _StubELBv2:
        @staticmethod
        def describe_target_health(TargetGroupArn: str) -> dict[str, Any]:
            if TargetGroupArn == tg_ec2_arn:
                return {
                    "TargetHealthDescriptions": [
                        {
                            # 非省略形の IPv6 アドレスで登録されている EC2 ターゲットのケース
                            "Target": {"Id": "2001:0db8:1::10", "Port": 80},
                            "TargetHealth": {"State": "healthy"},
                        }
                    ]
                }
            assert TargetGroupArn == tg_ecs_arn
            return {
                "TargetHealthDescriptions": [
                    {
                        # AZ が応答に含まれない ECS Fargate IPv6 タスクのケース
                        "Target": {"Id": "2001:db8:1::20", "Port": 80},
                        "TargetHealth": {"State": "healthy"},
                    }
                ]
            }

        @staticmethod
        def describe_target_group_attributes(**_: Any) -> dict[str, Any]:
            return {"Attributes": []}

    class _StubEC2:
        @staticmethod
        def describe_network_interfaces(Filters: list[dict[str, Any]]) -> dict[str, Any]:
            # ipv6-addresses.ipv6-address フィルタで照会されることを確認
            if Filters[0]["Name"] != "ipv6-addresses.ipv6-address":
                return {"NetworkInterfaces": []}
            return {
                "NetworkInterfaces": [
                    {
                        "NetworkInterfaceId": "eni-v6-ec2",
                        "SubnetId": "subnet-v6",
                        "VpcId": "vpc-v6",
                        "AvailabilityZone": "ap-northeast-1a",
                        "PrivateIpAddress": "10.0.1.10",
                        "Ipv6Addresses": [{"Ipv6Address": "2001:db8:1::10"}],
                        "Attachment": {"InstanceId": "i-v6host"},
                        "Groups": [{"GroupId": "sg-v6"}],
                    }
                ]
            }

        @staticmethod
        def describe_instances(Filters: list[dict[str, Any]]) -> dict[str, Any]:
            if Filters[0]["Name"] == "instance-id" and "i-v6host" in Filters[0]["Values"]:
                return {
                    "Reservations": [
                        {
                            "Instances": [
                                {
                                    "InstanceId": "i-v6host",
                                    "PrivateIpAddress": "10.0.1.10",
                                    "InstanceType": "t4g.nano",
                                    "State": {"Name": "running"},
                                    "Placement": {"AvailabilityZone": "ap-northeast-1a"},
                                    "SubnetId": "subnet-v6",
                                    "VpcId": "vpc-v6",
                                    "SecurityGroups": [{"GroupId": "sg-v6"}],
                                    "Tags": [{"Key": "Name", "Value": "web-v6-01"}],
                                    "NetworkInterfaces": [
                                        {
                                            "PrivateIpAddress": "10.0.1.10",
                                            "Ipv6Addresses": [{"Ipv6Address": "2001:db8:1::10"}],
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                }
            return {"Reservations": []}

        @staticmethod
        def describe_subnets(**_: Any) -> dict[str, Any]:
            return {
                "Subnets": [
                    {
                        "SubnetId": "subnet-v6",
                        "VpcId": "vpc-v6",
                        "AvailabilityZone": "ap-northeast-1a",
                        "CidrBlock": "10.0.1.0/24",
                        "Ipv6CidrBlockAssociationSet": [{"Ipv6CidrBlock": "2001:db8:1::/64"}],
                        "Tags": [{"Key": "Name", "Value": "dualstack-subnet-1a"}],
                    }
                ]
            }

        @staticmethod
        def describe_security_groups(**_: Any) -> dict[str, Any]:
            return {
                "SecurityGroups": [
                    {
                        "GroupId": "sg-v6",
                        "GroupName": "v6-sg",
                        "Description": "allow v6",
                        "IpPermissions": [],
                        "IpPermissionsEgress": [],
                    }
                ]
            }

    class _StubECS:
        list_clusters = staticmethod(lambda **_: {"clusterArns": [f"{ARN_P}:cluster/v6-cluster"]})
        list_services = staticmethod(lambda **_: {"serviceArns": ["v6-svc"]})
        list_container_instances = staticmethod(lambda **_: {"containerInstanceArns": []})
        list_tasks = staticmethod(lambda **_: {"taskArns": ["task-v6"]})

        @staticmethod
        def describe_services(**_: Any) -> dict[str, Any]:
            return {
                "services": [
                    {
                        "serviceName": "v6-svc",
                        "taskDefinition": f"{ARN_P}:task-definition/v6-app:1",
                        "launchType": "FARGATE",
                        "status": "ACTIVE",
                        "networkConfiguration": {
                            "awsvpcConfiguration": {
                                "subnets": ["subnet-v6"],
                                "securityGroups": ["sg-v6"],
                            }
                        },
                        "loadBalancers": [{"targetGroupArn": tg_ecs_arn, "containerName": "app"}],
                    }
                ]
            }

        @staticmethod
        def describe_tasks(**_: Any) -> dict[str, Any]:
            return {
                "tasks": [
                    {
                        "taskArn": f"{ARN_P}:task/v6-cluster/task-v6",
                        "group": "service:v6-svc",
                        "taskDefinitionArn": f"{ARN_P}:task-definition/v6-app:1",
                        "launchType": "FARGATE",
                        "lastStatus": "RUNNING",
                        "attachments": [
                            {
                                "details": [
                                    {"name": "privateIPv4Address", "value": "10.0.1.20"},
                                    {"name": "privateIPv6Address", "value": "2001:db8:1::20"},
                                ]
                            }
                        ],
                        "containers": [
                            {
                                "name": "app",
                                "networkInterfaces": [
                                    {
                                        "privateIpv4Address": "10.0.1.20",
                                        "ipv6Address": "2001:db8:1::20",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }

    elb_client.elbv2 = _StubELBv2()
    elb_client.ec2 = _StubEC2()
    elb_client.ecs = _StubECS()

    views = elb_client._hydrate_target_groups(raw_tgs)
    assert len(views) == 2
    by_tid = {t.target_id: t for tg_view in views for t in tg_view.targets}

    # 1. 非省略形 2001:0db8:1::10 で登録された EC2 IPv6 ターゲット
    ec2_target = by_tid["2001:0db8:1::10"]
    assert ec2_target.target_kind == "ec2"
    assert ec2_target.ip_address == "2001:0db8:1::10"
    assert ec2_target.ec2 is not None
    assert ec2_target.ec2.name == "web-v6-01"
    assert ec2_target.subnet is not None
    assert ec2_target.subnet.subnet_id == "subnet-v6"
    assert [sg.group_id for sg in ec2_target.security_groups] == ["sg-v6"]

    # 2. ECS Fargate IPv6 タスクターゲット（IPv6 CIDR からサブネットと AZ も特定される）
    ecs_target = by_tid["2001:db8:1::20"]
    assert ecs_target.target_kind == "ecs"
    assert ecs_target.ip_address == "2001:db8:1::20"
    assert ecs_target.ecs is not None
    assert ecs_target.ecs.task_id == "task-v6"
    assert ecs_target.ecs.private_ip == "2001:db8:1::20"
    assert ecs_target.availability_zone == "ap-northeast-1a"
    assert ecs_target.subnet is not None
    assert ecs_target.subnet.subnet_id == "subnet-v6"


def test_ipv6_only_ec2_eni_ecs_and_subnet_resolution(elb_client: ELBClient) -> None:
    """
    IPv6 only（IPv4 アドレスを持たない）の EC2・ENI・ECS Fargate タスクと、IPv6 専用サブネットが
    解決できることを確認する。

    実際の AWS と同じく、応答に PrivateIpAddress やサブネットの CidrBlock を含めない。
    （moto は IPv6 専用サブネットの EC2 にも PrivateIpAddress を返すなど実際の AWS と違うため、
    スタブで再現する）
    """
    tg_inst_arn = f"{ARN_P.replace('ecs', 'elasticloadbalancing')}:targetgroup/v6only-inst/1"
    tg_ip_arn = f"{ARN_P.replace('ecs', 'elasticloadbalancing')}:targetgroup/v6only-ip/2"
    tg_ecs_arn = f"{ARN_P.replace('ecs', 'elasticloadbalancing')}:targetgroup/v6only-ecs/3"
    ec2_ip, ecs_ip = "2001:db8:3::10", "2001:db8:3::20"

    def raw_tg(arn: str, name: str, target_type: str) -> dict[str, Any]:
        return {
            "TargetGroupArn": arn,
            "TargetGroupName": name,
            "Protocol": "HTTP",
            "Port": 80,
            "TargetType": target_type,
            "IpAddressType": "ipv6",
            "VpcId": "vpc-v6only",
            "HealthCheckProtocol": "HTTP",
            "HealthCheckPort": "traffic-port",
            "HealthCheckPath": "/healthz",
        }

    raw_tgs = [
        raw_tg(tg_inst_arn, "v6only-inst", "instance"),
        raw_tg(tg_ip_arn, "v6only-ip", "ip"),
        raw_tg(tg_ecs_arn, "v6only-ecs", "ip"),
    ]
    registered = {tg_inst_arn: ["i-v6only"], tg_ip_arn: [ec2_ip], tg_ecs_arn: [ecs_ip]}
    instance = {
        "InstanceId": "i-v6only",
        "InstanceType": "t4g.nano",
        "State": {"Name": "running"},
        "Placement": {"AvailabilityZone": "ap-northeast-1c"},
        "SubnetId": "subnet-v6only",
        "VpcId": "vpc-v6only",
        "SecurityGroups": [{"GroupId": "sg-v6only"}],
        "Tags": [{"Key": "Name", "Value": "web-v6only-01"}],
        "Ipv6Address": ec2_ip,
        "NetworkInterfaces": [
            {
                "PrivateIpAddresses": [],
                "Ipv6Addresses": [{"Ipv6Address": ec2_ip, "IsPrimaryIpv6": True}],
            }
        ],
    }

    class _StubELBv2:
        @staticmethod
        def describe_target_health(TargetGroupArn: str) -> dict[str, Any]:
            return {
                "TargetHealthDescriptions": [
                    {"Target": {"Id": t, "Port": 80}, "TargetHealth": {"State": "healthy"}}
                    for t in registered[TargetGroupArn]
                ]
            }

        @staticmethod
        def describe_target_group_attributes(**_: Any) -> dict[str, Any]:
            return {"Attributes": []}

    class _StubEC2:
        @staticmethod
        def describe_network_interfaces(Filters: list[dict[str, Any]]) -> dict[str, Any]:
            if (
                Filters[0]["Name"] != "ipv6-addresses.ipv6-address"
                or ec2_ip not in Filters[0]["Values"]
            ):
                return {"NetworkInterfaces": []}
            return {
                "NetworkInterfaces": [
                    {
                        "NetworkInterfaceId": "eni-v6only",
                        "SubnetId": "subnet-v6only",
                        "VpcId": "vpc-v6only",
                        "AvailabilityZone": "ap-northeast-1c",
                        "PrivateIpAddresses": [],
                        "Ipv6Addresses": [{"Ipv6Address": ec2_ip}],
                        "Attachment": {"InstanceId": "i-v6only"},
                        "Groups": [{"GroupId": "sg-v6only"}],
                    }
                ]
            }

        @staticmethod
        def describe_instances(Filters: list[dict[str, Any]]) -> dict[str, Any]:
            name, values = Filters[0]["Name"], Filters[0]["Values"]
            if (name == "instance-id" and "i-v6only" in values) or (
                name == "ipv6-address" and ec2_ip in values
            ):
                return {"Reservations": [{"Instances": [instance]}]}
            return {"Reservations": []}

        @staticmethod
        def describe_subnets(**_: Any) -> dict[str, Any]:
            return {
                "Subnets": [
                    {
                        "SubnetId": "subnet-v6only",
                        "VpcId": "vpc-v6only",
                        "AvailabilityZone": "ap-northeast-1c",
                        "Ipv6Native": True,
                        "Ipv6CidrBlockAssociationSet": [{"Ipv6CidrBlock": "2001:db8:3::/64"}],
                        "Tags": [{"Key": "Name", "Value": "v6only-subnet-1c"}],
                    }
                ]
            }

        @staticmethod
        def describe_security_groups(**_: Any) -> dict[str, Any]:
            return {
                "SecurityGroups": [
                    {
                        "GroupId": "sg-v6only",
                        "GroupName": "v6only-sg",
                        "Description": "allow v6",
                        "IpPermissions": [],
                        "IpPermissionsEgress": [],
                    }
                ]
            }

    class _StubECS:
        list_clusters = staticmethod(lambda **_: {"clusterArns": [f"{ARN_P}:cluster/v6only"]})
        list_services = staticmethod(lambda **_: {"serviceArns": ["v6only-svc"]})
        list_container_instances = staticmethod(lambda **_: {"containerInstanceArns": []})
        list_tasks = staticmethod(lambda **_: {"taskArns": ["task-v6only"]})

        @staticmethod
        def describe_services(**_: Any) -> dict[str, Any]:
            return {
                "services": [
                    {
                        "serviceName": "v6only-svc",
                        "taskDefinition": f"{ARN_P}:task-definition/v6only-app:1",
                        "launchType": "FARGATE",
                        "status": "ACTIVE",
                        "networkConfiguration": {
                            "awsvpcConfiguration": {
                                "subnets": ["subnet-v6only"],
                                "securityGroups": ["sg-v6only"],
                            }
                        },
                        "loadBalancers": [{"targetGroupArn": tg_ecs_arn, "containerName": "app"}],
                    }
                ]
            }

        @staticmethod
        def describe_tasks(**_: Any) -> dict[str, Any]:
            # IPv4 の privateIPv4Address / privateIpv4Address を持たず、AZ も応答に含まれないタスク
            return {
                "tasks": [
                    {
                        "taskArn": f"{ARN_P}:task/v6only/task-v6only",
                        "group": "service:v6only-svc",
                        "taskDefinitionArn": f"{ARN_P}:task-definition/v6only-app:1",
                        "launchType": "FARGATE",
                        "lastStatus": "RUNNING",
                        "attachments": [
                            {
                                "details": [
                                    {"name": "subnetId", "value": "subnet-v6only"},
                                    {"name": "ipv6Address", "value": ecs_ip},
                                ]
                            }
                        ],
                        "containers": [
                            {"name": "app", "networkInterfaces": [{"ipv6Address": ecs_ip}]}
                        ],
                    }
                ]
            }

    elb_client.elbv2 = _StubELBv2()
    elb_client.ec2 = _StubEC2()
    elb_client.ecs = _StubECS()

    views = elb_client._hydrate_target_groups(raw_tgs)
    assert [v.ip_address_type for v in views] == ["ipv6", "ipv6", "ipv6"]
    by_tid = {t.target_id: t for tg_view in views for t in tg_view.targets}

    # 1. instance 登録の IPv6 only EC2: PrivateIpAddress がなくても IPv6 アドレスが IP になる
    inst_target = by_tid["i-v6only"]
    assert inst_target.target_kind == "ec2"
    assert inst_target.ip_address == ec2_ip
    assert inst_target.availability_zone == "ap-northeast-1c"
    assert inst_target.subnet is not None
    assert inst_target.subnet.name == "v6only-subnet-1c"
    assert inst_target.subnet.cidr_blocks == ["2001:db8:3::/64"]

    # 2. IP 登録の IPv6 only EC2: IPv4 のない ENI から EC2 を特定する
    ip_target = by_tid[ec2_ip]
    assert ip_target.target_kind == "ec2"
    assert ip_target.ec2 is not None
    assert ip_target.ec2.name == "web-v6only-01"
    assert ip_target.eni is not None
    assert ip_target.eni.network_interface_id == "eni-v6only"
    assert [sg.group_id for sg in ip_target.security_groups] == ["sg-v6only"]

    # 3. IPv6 only の ECS タスク: IPv6 アドレスだけで照合し、AZ はサブネットの IPv6 CIDR から補う
    ecs_target = by_tid[ecs_ip]
    assert ecs_target.target_kind == "ecs"
    assert ecs_target.ecs is not None
    assert ecs_target.ecs.task_id == "task-v6only"
    assert ecs_target.ip_address == ecs_ip
    assert ecs_target.availability_zone == "ap-northeast-1c"
    assert ecs_target.subnet is not None
    assert ecs_target.subnet.subnet_id == "subnet-v6only"

    # 4. 表示: IPv4 の値がなくても IPv6 アドレスとサブネットが出る
    table = format_targets_table(views, use_color=False)
    assert ec2_ip in table
    assert ecs_ip in table
    assert "v6only-subnet-1c (subnet-v6only)" in table


def test_matches_target_ipv6_canonical_and_exact_ip() -> None:
    """
    逆引き検索の判定（_matches_target）で、IPv6 の省略表記・大文字小文字の違いを吸収して一致し、
    IP アドレスの部分文字列（例: '10.0.1' や '2001:db8'）では誤ヒットしないことを確認する。
    """
    v6_target = TargetView(
        target_id="2001:db8:1::10",
        port=80,
        health_state="healthy",
    )
    # 完全一致・非省略形・大文字表記はいずれも一致する
    assert ELBClient._matches_target(v6_target, "2001:db8:1::10")
    assert ELBClient._matches_target(v6_target, "2001:0db8:0001:0000:0000:0000:0000:0010")
    assert ELBClient._matches_target(v6_target, "2001:db8:1::10".lower())
    # 部分文字列や別アドレスには一致しない
    assert not ELBClient._matches_target(v6_target, "2001:db8")
    assert not ELBClient._matches_target(v6_target, "2001:db8:1::1")

    v4_target = TargetView(
        target_id="10.0.1.11",
        port=80,
        health_state="healthy",
    )
    assert ELBClient._matches_target(v4_target, "10.0.1.11")
    assert not ELBClient._matches_target(v4_target, "10.0.1")
    assert not ELBClient._matches_target(v4_target, "10.0.1.1")


def test_matches_target_looks_only_at_the_name_part_of_an_arn() -> None:
    """
    ARN は名前の部分だけと部分一致で照合する。リージョン名・アカウント ID・`app`・`function` の
    ような、どの ARN にも入っている文字列では当たらない。ARN を貼り付けたクエリは ARN 全体と
    照合する。
    """
    fn_arn = "arn:aws:lambda:us-east-1:123456789012:function:orders-api"
    lambda_target = TargetView(
        target_id=fn_arn,
        port=0,
        health_state="healthy",
        lambda_function=LambdaFunctionInfo(function_name="orders-api", function_arn=fn_arn),
    )
    assert ELBClient._matches_target(lambda_target, "orders")
    assert ELBClient._matches_target(lambda_target, fn_arn)
    assert ELBClient._matches_target(lambda_target, "arn:aws:lambda:us-east-1")
    for common in ("east", "123456789012", "function", "lambda", "aws"):
        assert not ELBClient._matches_target(lambda_target, common), common

    alb_arn = (
        "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/internal-web/50dc6c49"
    )
    alb_target = TargetView(target_id=alb_arn, port=80, health_state="healthy")
    assert ELBClient._matches_target(alb_target, "internal-web")
    assert ELBClient._matches_target(alb_target, "50dc6c49")
    for common in ("app", "loadbalancer", "east", "elasticloadbalancing"):
        assert not ELBClient._matches_target(alb_target, common), common

    task_arn = f"{ARN_P}:task/prod/0123456789abcdef"
    ecs_target = TargetView(
        target_id="10.0.1.20",
        port=8080,
        health_state="healthy",
        ecs=ECSTaskInfo(
            cluster_name="prod", service_name="web", task_arn=task_arn, task_id="0123456789abcdef"
        ),
    )
    assert ELBClient._matches_target(ecs_target, "0123456789abcdef")
    assert ELBClient._matches_target(ecs_target, task_arn)
    for common in ("task", "northeast", "ecs"):
        assert not ELBClient._matches_target(ecs_target, common), common


def test_subnet_cidr_blocks_for_dualstack_and_ipv6_only() -> None:
    """
    サブネットの CIDR 表示用の一覧は、デュアルスタックでは IPv4 と IPv6 の両方、
    IPv6 専用（cidr_block にも IPv6 CIDR が入る）では重複なしの IPv6 だけになる。
    """
    dualstack = SubnetInfo(
        subnet_id="subnet-ds",
        availability_zone="ap-northeast-1a",
        cidr_block="10.0.1.0/24",
        ipv6_cidr_blocks=["2001:db8:1:1::/64"],
    )
    assert dualstack.cidr_blocks == ["10.0.1.0/24", "2001:db8:1:1::/64"]
    ipv6_only = SubnetInfo(
        subnet_id="subnet-v6",
        availability_zone="ap-northeast-1a",
        cidr_block="2001:db8:1:3::/64",
        ipv6_cidr_blocks=["2001:db8:1:3::/64"],
    )
    assert ipv6_only.cidr_blocks == ["2001:db8:1:3::/64"]


# =============================================================================
# 同じ CIDR の VPC が複数あるとき（IP ターゲットの実体を TG の VPC で選ぶ）
# =============================================================================


def test_ip_targets_resolve_in_the_target_group_vpc(elb_client: ELBClient) -> None:
    """
    同じ IP（10.0.1.5）の ENI・EC2 が別々の VPC にあっても、TG の VPC のものに紐づく。
    AZ が all（TG の VPC の外の IP）なら、どの VPC のものでも1つに決まるときだけ紐づく。
    """

    def raw_tg(name: str, vpc_id: str) -> dict[str, Any]:
        return {
            "TargetGroupArn": (
                f"arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:targetgroup/{name}/1"
            ),
            "TargetGroupName": name,
            "Protocol": "HTTP",
            "Port": 80,
            "TargetType": "ip",
            "VpcId": vpc_id,
        }

    raw_tgs = [raw_tg("tg-a", "vpc-a"), raw_tg("tg-b", "vpc-b"), raw_tg("tg-outside", "vpc-c")]
    # TG ごとの登録ターゲット（tg-outside の 2 件は AZ を all にした、TG の VPC の外の IP）
    registered = {
        "tg-a": [{"Id": "10.0.1.5", "Port": 80}],
        "tg-b": [{"Id": "10.0.1.5", "Port": 80}],
        "tg-outside": [
            {"Id": "10.0.1.5", "Port": 80, "AvailabilityZone": "all"},
            {"Id": "10.9.0.7", "Port": 80, "AvailabilityZone": "all"},
        ],
    }
    # (ENI, VPC, サブネット, IP, インスタンス)
    enis = [
        ("eni-a", "vpc-a", "subnet-a", "10.0.1.5", "i-a"),
        ("eni-b", "vpc-b", "subnet-b", "10.0.1.5", "i-b"),
        ("eni-peer", "vpc-peer", "subnet-peer", "10.9.0.7", "i-peer"),
    ]
    # (サブネット, VPC, CIDR, AZ)
    subnets = [
        ("subnet-a", "vpc-a", "10.0.1.0/24", "ap-northeast-1a"),
        ("subnet-b", "vpc-b", "10.0.1.0/24", "ap-northeast-1c"),
        ("subnet-peer", "vpc-peer", "10.9.0.0/24", "ap-northeast-1d"),
    ]

    def values(filters: list[dict[str, Any]], name: str) -> set[str]:
        return next((set(f["Values"]) for f in filters if f["Name"] == name), set())

    class _StubELBv2:
        @staticmethod
        def describe_target_health(TargetGroupArn: str) -> dict[str, Any]:
            name = TargetGroupArn.split("/")[-2]
            return {
                "TargetHealthDescriptions": [
                    {"Target": t, "TargetHealth": {"State": "healthy"}} for t in registered[name]
                ]
            }

        @staticmethod
        def describe_target_group_attributes(**_: Any) -> dict[str, Any]:
            return {"Attributes": []}

    class _StubEC2:
        @staticmethod
        def describe_network_interfaces(Filters: list[dict[str, Any]]) -> dict[str, Any]:
            ips = values(Filters, "addresses.private-ip-address")
            return {
                "NetworkInterfaces": [
                    {
                        "NetworkInterfaceId": eni_id,
                        "VpcId": vpc,
                        "SubnetId": subnet,
                        "PrivateIpAddress": ip,
                        "PrivateIpAddresses": [{"PrivateIpAddress": ip}],
                        "Attachment": {"InstanceId": inst},
                    }
                    for eni_id, vpc, subnet, ip, inst in enis
                    if ip in ips
                ]
            }

        @staticmethod
        def describe_instances(Filters: list[dict[str, Any]]) -> dict[str, Any]:
            ids = values(Filters, "instance-id")
            ips = values(Filters, "private-ip-address")
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": inst,
                                "VpcId": vpc,
                                "SubnetId": subnet,
                                "PrivateIpAddress": ip,
                                "Tags": [{"Key": "Name", "Value": f"web-{vpc}"}],
                            }
                        ]
                    }
                    for _eni, vpc, subnet, ip, inst in enis
                    if inst in ids or ip in ips
                ]
            }

        @staticmethod
        def describe_subnets(Filters: list[dict[str, Any]]) -> dict[str, Any]:
            ids = values(Filters, "subnet-id")
            vpcs = values(Filters, "vpc-id")
            return {
                "Subnets": [
                    {"SubnetId": s, "VpcId": vpc, "CidrBlock": cidr, "AvailabilityZone": az}
                    for s, vpc, cidr, az in subnets
                    if s in ids or vpc in vpcs
                ]
            }

        @staticmethod
        def describe_security_groups(**_: Any) -> dict[str, Any]:
            return {"SecurityGroups": []}

    elb_client.elbv2 = _StubELBv2()
    elb_client.ec2 = _StubEC2()
    elb_client.ecs = None

    views = {v.name: v for v in elb_client._hydrate_target_groups(raw_tgs)}
    for name, suffix in (("tg-a", "a"), ("tg-b", "b")):
        target = views[name].targets[0]
        assert target.ec2 is not None
        assert target.ec2.instance_id == f"i-{suffix}"
        assert target.eni is not None
        assert target.eni.network_interface_id == f"eni-{suffix}"
        assert target.subnet is not None
        assert target.subnet.subnet_id == f"subnet-{suffix}"

    outside = {t.target_id: t for t in views["tg-outside"].targets}
    # 2 つの VPC に同じ IP があり、どちらか決められないので紐づけない
    assert outside["10.0.1.5"].ec2 is None
    assert outside["10.0.1.5"].eni is None
    assert outside["10.0.1.5"].target_kind == "ip"
    # ピアリング先の VPC に1つだけあるものは紐づける
    peer = outside["10.9.0.7"].ec2
    assert peer is not None
    assert peer.instance_id == "i-peer"


def test_ecs_task_for_ip_prefers_the_task_of_the_target_eni() -> None:
    """同じ IP の ECS タスクが別々の VPC にあるときは、ターゲットの ENI のタスクを選ぶ。"""
    task_a = ECSTaskInfo(cluster_name="a", task_id="task-a")
    task_b = ECSTaskInfo(cluster_name="b", task_id="task-b")
    maps = ECSMaps(by_ip={"10.0.1.5": {"eni-a": task_a, "eni-b": task_b}})
    assert maps.task_for_ip("10.0.1.5", "eni-b") is task_b
    # ENI が分からなければ決められない
    assert maps.task_for_ip("10.0.1.5", "") is None
    # 別の ENI（同じ IP の EC2 など）のターゲットにはタスクを紐づけない
    assert maps.task_for_ip("10.0.1.5", "eni-other") is None

    single = ECSMaps(by_ip={"10.0.1.6": {"eni-c": task_a}})
    assert single.task_for_ip("10.0.1.6", "") is task_a


# =============================================================================
# 調べるべきターゲット（--unhealthy の対象、色）
# =============================================================================

_LB_ARN = "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:loadbalancer/app/x/1"


def _tg_with(states: list[tuple[str, str]], attached: bool = True) -> TargetGroupView:
    """`(状態, 理由)` のターゲットを持つ TG を作る（`attached` なら ELB に使われている）。"""
    return TargetGroupView(
        arn="arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:targetgroup/t/1",
        name="t",
        protocol="HTTP",
        port=80,
        target_type="instance",
        load_balancer_arns=[_LB_ARN] if attached else [],
        targets=[
            TargetView(target_id=f"i-{n}", port=80, health_state=s, health_reason=r)
            for n, (s, r) in enumerate(states)
        ],
    )


@pytest.mark.parametrize(
    ("state", "reason", "attached", "expected"),
    [
        ("healthy", "", True, False),
        ("unhealthy", "Target.FailedHealthChecks", True, True),
        ("initial", "Elb.RegistrationInProgress", True, True),
        ("draining", "Target.DeregistrationInProgress", True, True),
        ("unavailable", "Target.HealthCheckDisabled", True, False),
        ("unavailable", "Elb.InternalError", True, True),
        ("unused", "Target.NotInUse", False, False),
        ("unused", "Target.InvalidState", True, True),
        ("unused", "Target.NotInUse", True, True),
    ],
    ids=[
        "healthy",
        "unhealthy",
        "initial",
        "draining",
        "health_check_disabled",
        "unavailable_other",
        "unused_unattached_tg",
        "unused_stopped_instance",
        "unused_az_not_enabled",
    ],
)
def test_needs_attention(state: str, reason: str, attached: bool, expected: bool) -> None:
    """設定どおりの状態（ヘルスチェック無効・使われていない TG）だけは調べる対象から外す。"""
    tg = _tg_with([(state, reason)], attached=attached)
    assert tg.needs_attention(tg.targets[0]) is expected
    assert tg.attention_count == int(expected)


def test_health_summary_counts_every_state() -> None:
    """集計には、ある状態だけを決まった順で並べる。"""
    tg = _tg_with(
        [
            ("healthy", ""),
            ("unavailable", "Target.HealthCheckDisabled"),
            ("unused", "Target.InvalidState"),
            ("unhealthy", "Target.Timeout"),
            ("unhealthy", "Target.Timeout"),
        ]
    )
    assert tg.health_summary == "1/5 healthy, 2 unhealthy, 1 unused, 1 unavailable"
    assert _tg_with([("healthy", "")]).health_summary == "1/1 healthy"


def test_only_unhealthy_skips_states_caused_by_the_configuration() -> None:
    """`targets --unhealthy` は、ヘルスチェック無効の Lambda・使われていない TG を出さない。"""
    lambda_tg = _tg_with([("unavailable", "Target.HealthCheckDisabled")])
    orphan_tg = _tg_with([("unused", "Target.NotInUse")], attached=False)
    stopped_tg = _tg_with([("healthy", ""), ("unused", "Target.InvalidState")])
    result = _only_unhealthy([lambda_tg, orphan_tg, stopped_tg])
    assert [[t.target_id for t in tg.targets] for tg in result] == [["i-1"]]


def test_health_colors_do_not_alarm_on_states_caused_by_the_configuration() -> None:
    """ヘルスチェック無効の Lambda は赤にせず、集計のバッジも目立たせない。"""
    c = _AnsiColor(enabled=True)

    def badge(tg: TargetGroupView) -> str:
        return c.render([_health_summary_badge(tg, c)])

    disabled = _tg_with([("unavailable", "Target.HealthCheckDisabled")])
    assert badge(disabled) == f"{c.DIM}[0/1 healthy, 1 unavailable]{c.RESET}"
    assert badge(_tg_with([("unhealthy", "Target.Timeout")])).startswith(c.RED)
    stopped = _tg_with([("unused", "Target.InvalidState")])
    assert badge(stopped).startswith(c.YELLOW)
    assert badge(_tg_with([])).startswith(c.YELLOW)
    assert badge(_tg_with([("healthy", "")])).startswith(c.GREEN)


# =============================================================================
# 下流 ELB の循環
# =============================================================================


def _lb_with_targets(name: str, target_ids: list[str], tg_name: str = "") -> LoadBalancerView:
    """`target_ids` をターゲットに持つ TG が 1 つある ELB を作る。"""
    tg = TargetGroupView(
        arn=f"tg-{tg_name or name}",
        name=f"tg-{tg_name or name}",
        protocol="TCP",
        port=80,
        target_type="alb",
        targets=[TargetView(target_id=t, port=80, health_state="healthy") for t in target_ids],
    )
    return LoadBalancerView(
        arn=f"lb-{name}",
        name=name,
        dns_name=f"{name}.example.com",
        type="network",
        scheme="internal",
        state="active",
        vpc_id="vpc-1",
        target_groups=[tg],
    )


def _link(*lbs: LoadBalancerView) -> None:
    _link_downstream_lbs(list(lbs), [tg for lb in lbs for tg in lb.target_groups])


def _downstream_names(lb: LoadBalancerView) -> list[str | None]:
    return [t.downstream_lb.name if t.downstream_lb else None for t in lb.all_targets]


@pytest.mark.parametrize(
    "lbs",
    [
        lambda: [_lb_with_targets("a", ["lb-b"]), _lb_with_targets("b", ["lb-a"])],
        lambda: [_lb_with_targets("a", ["lb-a"])],
        lambda: [
            _lb_with_targets("a", ["lb-b"]),
            _lb_with_targets("b", ["lb-c"]),
            _lb_with_targets("c", ["lb-a"]),
        ],
    ],
    ids=["two_lbs", "self", "three_lbs"],
)
def test_downstream_cycle_is_not_linked(lbs: Any) -> None:
    """ELB の参照が輪になる紐付けはしない。JSON とツリーが最後まで作れる。"""
    views = lbs()
    _link(*views)
    for lb in views:
        json.dumps(lb.to_dict())
        assert format_lb_tree(lb, use_color=False)
        assert format_lb_map(lb, use_color=False)


def test_downstream_links_that_make_no_cycle_are_kept() -> None:
    """輪にならない紐付け（直列と、同じ下流を複数の ELB が使う形）はこれまでどおり。"""
    a = _lb_with_targets("a", ["lb-c"])
    b = _lb_with_targets("b", ["lb-c"])
    c = _lb_with_targets("c", ["lb-d"])
    d = _lb_with_targets("d", ["10.0.0.1"])
    _link(a, b, c, d)
    assert _downstream_names(a) == ["c"]
    assert _downstream_names(b) == ["c"]
    assert _downstream_names(c) == ["d"]
    assert _downstream_names(d) == [None]


def test_downstream_cycle_keeps_the_first_link_and_the_target() -> None:
    """輪を閉じるターゲットだけを紐付けず、ターゲット自体は ARN のまま残す。"""
    a = _lb_with_targets("a", ["lb-b"])
    b = _lb_with_targets("b", ["lb-a"])
    _link(a, b)
    assert _downstream_names(a) == ["b"]
    assert _downstream_names(b) == [None]
    assert b.all_targets[0].target_id == "lb-a"


def test_cycle_through_a_shared_target_group_is_not_linked() -> None:
    """2 つの ELB が同じ TG を使うとき、どちらの ELB に戻る輪も作らない。"""
    a = _lb_with_targets("a", ["lb-c"], tg_name="shared")
    b = _lb_with_targets("b", [])
    b.target_groups = a.target_groups
    c = _lb_with_targets("c", ["lb-b"])
    _link(a, b, c)
    for lb in (a, b, c):
        json.dumps(lb.to_dict())


# =============================================================================
# route の下流
# =============================================================================


def test_route_does_not_follow_a_target_group_of_weight_zero() -> None:
    """重み 0 の TG にはリクエストが届かないので、その下流の ELB は評価しない。"""
    downstream = _lb_with_targets("down", [])
    downstream.listeners = [ListenerView(arn="l-down", port=80, protocol="HTTP")]
    tg = TargetGroupView(
        arn="tg-1",
        name="tg-1",
        protocol="TCP",
        port=80,
        target_type="alb",
        targets=[
            TargetView(
                target_id="lb-down", port=80, health_state="healthy", downstream_lb=downstream
            )
        ],
    )
    entry = _lb_with_targets("entry", [])
    rule = RuleView(
        arn="r",
        priority="default",
        is_default=True,
        target_group_refs=[
            RuleTargetGroupRef(target_group_arn="tg-1", weight=0, target_group=tg),
        ],
    )
    entry.listeners = [ListenerView(arn="l", port=80, protocol="HTTP", rules=[rule])]
    req = SimulatedRequest.from_url("http://x.example.com/")
    assert simulate_hop(entry, req, 80).downstream == []
    rule.target_group_refs[0].weight = 1
    assert [hop.load_balancer.name for hop in simulate_hop(entry, req, 80).downstream] == ["down"]


# =============================================================================
# SG のルールの表示
# =============================================================================


@pytest.mark.parametrize(
    ("perm", "expected"),
    [
        ({"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443}, ("TCP", "443")),
        ({"IpProtocol": "tcp", "FromPort": 80, "ToPort": 8080}, ("TCP", "80-8080")),
        ({"IpProtocol": "udp"}, ("UDP", "ALL")),
        ({"IpProtocol": "-1"}, ("ALL", "ALL")),
        ({"IpProtocol": "icmp", "FromPort": -1, "ToPort": -1}, ("ICMP", "ALL")),
        ({"IpProtocol": "icmp", "FromPort": 8, "ToPort": -1}, ("ICMP", "8")),
        ({"IpProtocol": "icmp", "FromPort": 3, "ToPort": 4}, ("ICMP", "3/4")),
        ({"IpProtocol": "icmp", "FromPort": 3, "ToPort": 3}, ("ICMP", "3/3")),
        ({"IpProtocol": "icmpv6", "FromPort": 128, "ToPort": -1}, ("ICMPV6", "128")),
        ({"IpProtocol": "58", "FromPort": 1, "ToPort": 4}, ("58", "1/4")),
    ],
    ids=[
        "tcp_one",
        "tcp_range",
        "udp_all",
        "all",
        "icmp_all",
        "icmp_type",
        "icmp_type_code",
        "icmp_same_type_code",
        "icmpv6_type",
        "icmpv6_by_number",
    ],
)
def test_sg_port_range(perm: dict[str, Any], expected: tuple[str, str]) -> None:
    """ICMP の FromPort / ToPort はタイプとコードなので、ポートの範囲の形にしない。"""
    rule = ELBClient._parse_sg_permissions([{**perm, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])[0]
    assert (rule.protocol, rule.port_range) == expected


# =============================================================================
# Lambda の取得
# =============================================================================

_LAMBDA_ARN = "arn:aws:lambda:ap-northeast-1:123456789012:function:my-fn"


def test_lambda_details_come_from_the_configuration_only(elb_client: ELBClient) -> None:
    """コードの署名付き URL を返す get_function は呼ばず、設定だけを取得する。"""

    class _StubLambda:
        def get_function_configuration(self, FunctionName: str) -> dict[str, Any]:
            return {"FunctionName": "my-fn", "FunctionArn": FunctionName, "Runtime": "python3.13"}

        def get_function(self, **_: Any) -> dict[str, Any]:
            pytest.fail("get_function を呼んだ")

    elb_client.lambda_client = _StubLambda()
    _, info = elb_client._fetch_single_lambda_function(_LAMBDA_ARN)
    assert (info.function_name, info.runtime) == ("my-fn", "python3.13")


@pytest.mark.parametrize(
    ("arn", "name"),
    [
        (_LAMBDA_ARN, "my-fn"),
        (f"{_LAMBDA_ARN}:prod", "my-fn"),
        (f"{_LAMBDA_ARN}:7", "my-fn"),
    ],
    ids=["plain", "alias", "version"],
)
def test_lambda_name_when_the_details_cannot_be_fetched(
    elb_client: ELBClient, arn: str, name: str
) -> None:
    """取得できないときの名前は関数名（エイリアスやバージョンを関数名にしない）。"""

    class _DeniedLambda:
        def get_function_configuration(self, **_: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException"}}, "GetFunctionConfiguration"
            )

    elb_client.lambda_client = _DeniedLambda()
    assert elb_client._fetch_single_lambda_function(arn)[1].function_name == name


# =============================================================================
# 取得しないと決めたものは API を呼ばない
# =============================================================================


def _count_calls(monkeypatch: pytest.MonkeyPatch, client: Any, *apis: str) -> dict[str, int]:
    """API の呼び出しを数えるようにする（呼び出し自体は本物に渡す）。"""
    counts = dict.fromkeys(apis, 0)
    for api in apis:
        real = getattr(client, api)

        def _counted(api: str = api, real: Any = real, **kwargs: Any) -> Any:
            counts[api] += 1
            return real(**kwargs)

        monkeypatch.setattr(client, api, _counted)
    return counts


def test_no_network_makes_no_subnet_or_security_group_calls(
    elb_client: ELBClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-network` と既定の関連図は、ターゲットの分も含めてサブネットと SG を取得しない。"""
    counts = _count_calls(
        monkeypatch, elb_client.ec2, "describe_subnets", "describe_security_groups"
    )
    elb_client.get_load_balancer("prod-web-alb", include_network=False, include_dns=False)
    assert counts == {"describe_subnets": 0, "describe_security_groups": 0}
    elb_client.get_load_balancer("prod-web-alb", include_network=True, include_dns=False)
    assert counts["describe_subnets"] > 0
    assert counts["describe_security_groups"] > 0


def test_find_still_resolves_the_network_of_the_targets(elb_client: ELBClient) -> None:
    """`find --json` はターゲットのサブネットと SG を出す（下流 ELB の分だけを省く）。"""
    target = elb_client.find_target("canary-prod-01")[0].target
    assert target.subnets
    assert target.security_groups


# =============================================================================
# Route 53 の名前
# =============================================================================


def test_same_record_name_is_shown_once() -> None:
    """デュアルスタックの A と AAAA は同じ名前なので、関連図と一覧には 1 回だけ出す。"""
    lb = _lb_with_targets("a", [])
    lb.route53_records = [
        Route53RecordInfo("Z1", "example.com", "api.example.com", "A"),
        Route53RecordInfo("Z1", "example.com", "api.example.com", "AAAA"),
    ]
    assert format_lb_map(lb, use_color=False).count("api.example.com") == 1
    assert format_lb_summary_table([lb], use_color=False).count("api.example.com") == 1


# =============================================================================
# 下流の ELB を同じ（ELB, ポート）で繰り返さない
# =============================================================================


def _entry_to_downstream(
    downstream: LoadBalancerView, targets: list[TargetView]
) -> LoadBalancerView:
    """`targets`（下流の ELB を指す）の TG へ転送する入口の ELB を作る。"""
    tg = TargetGroupView(
        arn="tg-entry",
        name="tg-entry",
        protocol="HTTP",
        port=80,
        target_type="ip",
        targets=targets,
    )
    entry = _lb_with_targets("entry", [])
    entry.target_groups = [tg]
    rule = RuleView(
        arn="r",
        priority="default",
        is_default=True,
        target_group_refs=[RuleTargetGroupRef(target_group_arn=tg.arn, target_group=tg)],
    )
    entry.listeners = [ListenerView(arn="l", port=80, protocol="HTTP", rules=[rule])]
    return entry


def _node(downstream: LoadBalancerView, ip: str, port: int = 80) -> TargetView:
    """ノードの IP で登録された下流の ELB のターゲットを作る。"""
    eni = ENIInfo(
        network_interface_id=f"eni-{ip}",
        private_ip=ip,
        load_balancer_name=downstream.name,
        load_balancer_arn=downstream.arn,
    )
    return TargetView(
        target_id=ip, port=port, health_state="healthy", downstream_lb=downstream, eni=eni
    )


def _downstream_with_listeners(*ports: int) -> LoadBalancerView:
    """`ports` のリスナーを持つ下流の ELB を作る。"""
    downstream = _lb_with_targets("down", [])
    downstream.listeners = [
        ListenerView(arn=f"l-{port}", port=port, protocol="TCP") for port in ports
    ]
    return downstream


def test_route_evaluates_a_multi_az_downstream_once() -> None:
    """AZ の数だけ IP が登録された下流の ELB を、同じ内容で繰り返し評価しない。"""
    down = _downstream_with_listeners(80)
    entry = _entry_to_downstream(down, [_node(down, "10.0.1.5"), _node(down, "10.0.2.5")])
    hops = simulate_hop(entry, SimulatedRequest.from_url("http://x.example.com/"), 80).downstream
    assert [(hop.load_balancer.name, hop.port) for hop in hops] == [("down", 80)]


def test_route_evaluates_a_downstream_once_per_port() -> None:
    """同じ下流の ELB でも、ポートが違えば別のリスナーなので、どちらも評価する。"""
    down = _downstream_with_listeners(80, 8080)
    entry = _entry_to_downstream(down, [_node(down, "10.0.1.5"), _node(down, "10.0.2.5", 8080)])
    hops = simulate_hop(entry, SimulatedRequest.from_url("http://x.example.com/"), 80).downstream
    assert [hop.port for hop in hops] == [80, 8080]


def test_full_tree_expands_a_downstream_once() -> None:
    """`tree --full` も同じ ELB は 1 回だけ展開し、2 行目も別のノードと分かるように IP を出す。"""
    down = _downstream_with_listeners(80)
    entry = _entry_to_downstream(down, [_node(down, "10.0.1.5"), _node(down, "10.0.2.5")])
    text = format_lb_tree(entry, use_color=False, show_network=False)
    assert text.count("Listener TCP:80") == 1
    assert text.count("(上に展開済み)") == 1
    assert "ip=10.0.1.5" in text
    assert "ip=10.0.2.5" in text


def test_map_tells_the_nodes_of_a_downstream_apart() -> None:
    """関連図でも、同じ名前の行が何を指すのかが IP で分かる。"""
    down = _downstream_with_listeners(80)
    entry = _entry_to_downstream(down, [_node(down, "10.0.1.5"), _node(down, "10.0.2.5")])
    text = format_lb_map(entry, use_color=False)
    assert "NLB down (10.0.1.5)" in text
    assert "NLB down (10.0.2.5) (上に展開済み)" in text


# =============================================================================
# Route 53 の名前
# =============================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\\052.example.com.", "*.example.com"),
        ("api.example.com.", "api.example.com"),
        ("a\\100b.example.com", "a@b.example.com"),
    ],
)
def test_route53_names_are_decoded(raw: str, expected: str) -> None:
    """API が 8 進エスケープで返す文字を戻し、末尾のドットを除く。"""
    assert _decode_route53_name(raw) == expected


def _stub_route53(
    client: ELBClient, zones: list[tuple[dict[str, Any], list[dict[str, Any]] | Exception]]
) -> list[str]:
    """
    Route 53 のゾーンとレコードを差し替え、レコードを読んだゾーンの ID を記録するリストを返す。
    レコードの代わりに例外を渡すと、そのゾーンのレコードは読めない。
    """
    records_by_id = {zone["Id"].split("/")[-1]: records for zone, records in zones}
    read: list[str] = []

    class _Paginator:
        def __init__(self, name: str) -> None:
            self.name = name

        def paginate(self, HostedZoneId: str = "") -> list[dict[str, Any]]:
            if self.name == "list_hosted_zones":
                return [{"HostedZones": [zone for zone, _ in zones]}]
            read.append(HostedZoneId)
            records = records_by_id[HostedZoneId]
            if isinstance(records, Exception):
                raise records
            return [{"ResourceRecordSets": records}]

    client.route53.get_paginator = _Paginator
    return read


def _zone(zone_id: str, name: str, private: bool = False) -> dict[str, Any]:
    """`list_hosted_zones` が返す形のゾーン。"""
    return {"Id": f"/hostedzone/{zone_id}", "Name": f"{name}.", "Config": {"PrivateZone": private}}


def test_wildcard_record_is_shown_and_matched_as_written(elb_client: ELBClient) -> None:
    """ワイルドカードのレコードは `*.example.com` の形で表示し、ホスト名とも照合できる。"""
    raw_lb = elb_client._fetch_raw_load_balancers()[0]
    record = {
        "Name": "\\052.example.com.",
        "Type": "A",
        "AliasTarget": {
            "DNSName": f"dualstack.{raw_lb['DNSName']}.",
            "EvaluateTargetHealth": False,
        },
    }
    _stub_route53(elb_client, [(_zone("Z1", "example.com"), [record])])
    records = elb_client._fetch_route53_records_by_dns([raw_lb["DNSName"]])
    assert [r.record_name for r in records[raw_lb["DNSName"]]] == ["*.example.com"]
    found = elb_client._find_raw_lbs_for_host("api.example.com")
    assert [lb["LoadBalancerArn"] for lb in found] == [raw_lb["LoadBalancerArn"]]


def _wildcard_alias(dns_name: str) -> dict[str, Any]:
    """ELB を向く `*.example.com` の Alias レコード。"""
    return {
        "Name": "\\052.example.com.",
        "Type": "A",
        "AliasTarget": {"DNSName": f"dualstack.{dns_name}.", "EvaluateTargetHealth": False},
    }


def test_wildcard_record_is_not_used_for_a_name_that_has_its_own_record(
    elb_client: ELBClient,
) -> None:
    """
    同じゾーンにその名前のレコードがあれば、ワイルドカードのレコードは使われない
    （`api.example.com` が CloudFront を向いていれば、`*.example.com` の ELB には届かない）。
    """
    raw_lb = elb_client._fetch_raw_load_balancers()[0]
    zone = _zone("Z1", "example.com")
    other_zone = _zone("Z2", "example.com", private=True)
    to_cloudfront = {
        "Name": "API.example.com.",
        "Type": "CNAME",
        "TTL": 60,
        "ResourceRecords": [{"Value": "d111111abcdef8.cloudfront.net"}],
    }
    wildcard = _wildcard_alias(raw_lb["DNSName"])

    _stub_route53(elb_client, [(zone, [wildcard, to_cloudfront])])
    assert elb_client._find_raw_lbs_for_host("api.example.com") == []
    assert elb_client._find_raw_lbs_for_host("www.example.com") == [raw_lb]

    # 別のゾーンにある同じ名前のレコードは、このゾーンのワイルドカードを止めない
    _stub_route53(elb_client, [(zone, [wildcard]), (other_zone, [to_cloudfront])])
    assert elb_client._find_raw_lbs_for_host("api.example.com") == [raw_lb]


def test_host_lookup_reports_a_zone_that_could_not_be_read(elb_client: ELBClient) -> None:
    """
    レコードを読めなかったゾーンがあるときは、「向いている ELB がない」と答えずにその失敗を出す。
    同じ名前のほかのゾーンで見つかれば、それを返す。
    """
    raw_lb = elb_client._fetch_raw_load_balancers()[0]
    denied = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "ListResourceRecordSets"
    )
    _stub_route53(
        elb_client,
        [
            (_zone("Z1", "example.com"), [_wildcard_alias(raw_lb["DNSName"])]),
            (_zone("Z2", "example.com", private=True), denied),
            (_zone("Z9", "example.org"), denied),
        ],
    )

    assert elb_client._find_raw_lbs_for_host("api.example.com") == [raw_lb]
    with pytest.raises(ClientError) as exc_info:
        elb_client._find_raw_lbs_for_host("api.example.org")
    assert exc_info.value is denied


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("api.sub.example.com", ["Z-com", "Z-sub"]),
        ("example.com", ["Z-com"]),
        ("API.Example.COM", ["Z-com"]),
        # ゾーン名が文字列として末尾に一致するだけ（ラベルの途中）のゾーンは読まない
        ("api.badexample.com", ["Z-bad"]),
        ("nothing.example.net", []),
    ],
)
def test_host_lookup_reads_only_the_zones_that_can_hold_the_name(
    elb_client: ELBClient, host: str, expected: list[str]
) -> None:
    """
    ホスト名から ELB を探すときは、名前がそのホスト名と同じか、その親のドメインのゾーンだけを
    読む（レコードの名前はゾーンの名前で終わるので、ほかのゾーンに一致するレコードはない）。
    読めないゾーンも、関係がなければ結果を変えない。
    """
    denied = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "ListResourceRecordSets"
    )
    read = _stub_route53(
        elb_client,
        [
            (_zone("Z-com", "example.com"), []),
            (_zone("Z-sub", "sub.example.com"), []),
            (_zone("Z-bad", "badexample.com"), []),
            (_zone("Z-org", "example.org"), denied),
        ],
    )
    assert elb_client._find_raw_lbs_for_host(host) == []
    assert sorted(read) == expected


# =============================================================================
# find の ELB 名と、JSON の集計
# =============================================================================


def test_find_gives_an_empty_name_for_a_target_group_without_load_balancer(
    elb_client: ELBClient,
) -> None:
    """ELB 名は言語によらず空で、表のときだけ `(ELB未アタッチ)` と出す。"""
    fetch = elb_client._fetch_raw_target_groups
    elb_client._fetch_raw_target_groups = lambda: [  # type: ignore[method-assign,assignment,misc]
        {**tg, "LoadBalancerArns": []} for tg in fetch()
    ]
    results = elb_client.find_target("web-prod-01")
    assert results
    assert {r.load_balancer_name for r in results} == {""}
    assert {r.load_balancer_arn for r in results} == {""}
    assert "(ELB未アタッチ)" in format_lookup_results_table(results, use_color=False)


def test_find_table_says_when_the_load_balancer_is_unknown() -> None:
    """ARN があるのに名前が分からない ELB は、未アタッチとは別の表示にする。"""
    target = TargetView(target_id="i-1", port=80, health_state="healthy")
    result = TargetLookupResult("", "lb-arn", "", "tg", "tg-arn", target)
    assert "(不明なELB)" in format_lookup_results_table([result], use_color=False)


def test_target_group_summary_counts_every_state() -> None:
    """JSON の集計は、すべての状態の内訳が `total` と一致し、調べるべき数も分かる。"""
    states = ["healthy", "unhealthy", "initial", "unused", "unavailable", "unhealthy.draining"]
    tg = TargetGroupView(
        arn="tg",
        name="tg",
        protocol="HTTP",
        port=80,
        target_type="instance",
        load_balancer_arns=["lb"],
        targets=[
            TargetView(target_id=f"i-{n}", port=80, health_state=state)
            for n, state in enumerate(states)
        ],
    )
    summary = tg.to_dict()["summary"]
    assert sum(summary["by_state"].values()) == summary["total"] == len(states)
    assert summary["by_state"]["initial"] == 1
    assert summary["attention"] == len(states) - 1


# =============================================================================
# ls の異常の表示
# =============================================================================


def test_ls_points_out_only_the_targets_that_need_attention() -> None:
    """ヘルスチェックが無効な Lambda だけなら目立たせず、調べるべきものがあれば数と赤で知らせる。"""
    lb = _lb_with_targets("calm", [])
    lb.target_groups[0].load_balancer_arns = [lb.arn]
    lb.target_groups[0].targets = [
        TargetView(
            target_id="fn",
            port=0,
            health_state="unavailable",
            health_reason="Target.HealthCheckDisabled",
        )
    ]
    calm = format_lb_summary_table([lb], use_color=True)
    assert "0/1 (1 TGs)" in calm
    assert "要確認" not in calm
    assert _AnsiColor.RED not in calm

    lb.target_groups[0].targets.append(TargetView("i-1", 80, "unhealthy"))
    assert "0/2 (1 TGs, 要確認: 1)" in format_lb_summary_table([lb], use_color=False)
    assert _AnsiColor.RED in format_lb_summary_table([lb], use_color=True)


# =============================================================================
# 使わないものを取得しない
# =============================================================================


_RESOLVING_TARGETS = {
    "DescribeInstances",
    "DescribeNetworkInterfaces",
    "DescribeSubnets",
    "DescribeSecurityGroups",
    "GetFunctionConfiguration",
    "ListClusters",
    "ListServices",
    "ListTasks",
}
_ATTRIBUTES = {"DescribeLoadBalancerAttributes", "DescribeTargetGroupAttributes"}


def test_ls_table_reads_only_what_the_table_shows(elb_client: ELBClient) -> None:
    """`ls` の表は、ターゲットの実体・証明書・属性を読まない。"""
    calls = _count_api_calls(elb_client)
    assert cli.main(["ls", "--no-dns", "--no-color"], client=elb_client) == 0
    assert "DescribeTargetHealth" in calls
    assert "DescribeRules" in calls
    assert not calls.keys() & (_RESOLVING_TARGETS | _ATTRIBUTES | {"DescribeCertificate"})


def test_ls_json_still_reads_everything(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ls --json` は、これまでどおりターゲットの実体・証明書・属性まで返す。"""
    calls = _count_api_calls(elb_client)
    assert cli.main(["ls", "--no-dns", "--json"], client=elb_client) == 0
    assert _RESOLVING_TARGETS & calls.keys()
    assert calls.keys() >= _ATTRIBUTES
    assert "DescribeCertificate" in calls
    assert '"ec2"' in capsys.readouterr().out


def test_tree_map_reads_certificates_but_not_attributes(elb_client: ELBClient) -> None:
    """関連図は証明書のドメインを出すが、属性は出さないので読まない。"""
    calls = _count_api_calls(elb_client)
    assert cli.main(["tree", "prod-web-alb", "--no-dns", "--no-color"], client=elb_client) == 0
    assert "DescribeCertificate" in calls
    assert not calls.keys() & _ATTRIBUTES


def test_tree_full_reads_attributes(elb_client: ELBClient) -> None:
    """`tree --full` は属性を出すので読む。"""
    calls = _count_api_calls(elb_client)
    argv = ["tree", "prod-web-alb", "--full", "--no-dns", "--no-color"]
    assert cli.main(argv, client=elb_client) == 0
    assert calls.keys() >= _ATTRIBUTES


@pytest.mark.parametrize(
    "argv",
    [["targets", "--no-color"], ["targets", "--tg", "prod-web-tg", "--no-color"], ["find", "web"]],
    ids=["targets", "targets_tg", "find"],
)
def test_tables_of_targets_do_not_read_attributes(elb_client: ELBClient, argv: list[str]) -> None:
    """`targets` と `find` の表は属性を出さないので読まない。"""
    calls = _count_api_calls(elb_client)
    assert cli.main(argv, client=elb_client) == 0
    assert not calls.keys() & _ATTRIBUTES


def test_targets_json_reads_target_group_attributes(elb_client: ELBClient) -> None:
    """`targets --json` は TG の属性を含むので読む。"""
    calls = _count_api_calls(elb_client)
    assert cli.main(["targets", "--json"], client=elb_client) == 0
    assert "DescribeTargetGroupAttributes" in calls


def _ecs_with_hosts(hosts: dict[str, str]) -> tuple[Any, list[str]]:
    """クラスター名 -> EC2 の ID のスタブ ECS と、タスクを読んだクラスターの記録を返す。"""
    tasks_read: list[str] = []

    class _StubECS:
        list_clusters = staticmethod(
            lambda **_: {"clusterArns": [f"{ARN_P}:cluster/{c}" for c in hosts]}
        )
        list_services = staticmethod(lambda **_: {"serviceArns": []})

        @staticmethod
        def list_container_instances(cluster: str, **_: Any) -> dict[str, Any]:
            return {"containerInstanceArns": [cluster]}

        @staticmethod
        def describe_container_instances(cluster: str, **_: Any) -> dict[str, Any]:
            host = hosts[cluster.rsplit("/", 1)[-1]]
            return {
                "containerInstances": [
                    {"containerInstanceArn": cluster, "ec2InstanceId": host},
                ]
            }

        @staticmethod
        def list_tasks(cluster: str, **_: Any) -> dict[str, Any]:
            tasks_read.append(cluster.rsplit("/", 1)[-1])
            return {"taskArns": []}

    return _StubECS(), tasks_read


def test_ecs_tasks_are_read_only_from_clusters_with_the_target_instances(
    elb_client: ELBClient,
) -> None:
    """調べている EC2 がいないクラスターのタスクは読まない。"""
    elb_client.ecs, tasks_read = _ecs_with_hosts({"mine": "i-mine", "other": "i-other"})
    elb_client._fetch_ecs_maps(set(), {"i-mine"}, {"tg"})
    assert tasks_read == ["mine"]


def test_ecs_tasks_are_read_from_every_cluster_for_ip_targets(elb_client: ELBClient) -> None:
    """IP ターゲットはどのクラスターのタスクか分からないので、すべて読む。"""
    elb_client.ecs, tasks_read = _ecs_with_hosts({"a": "i-a", "b": "i-b"})
    elb_client._fetch_ecs_maps({"10.0.0.1"}, set(), {"tg"})
    assert sorted(tasks_read) == ["a", "b"]


def test_ecs_clusters_are_merged_in_order_when_read_in_parallel(elb_client: ELBClient) -> None:
    """並列に読んでも、結果はクラスターの順に足す（同じ TG を指すサービスは後のものが残る）。"""
    tg_arn = "tg-shared"

    class _StubECS:
        list_clusters = staticmethod(
            lambda **_: {"clusterArns": [f"{ARN_P}:cluster/{n}" for n in range(8)]}
        )
        list_services = staticmethod(lambda **_: {"serviceArns": ["svc"]})

        @staticmethod
        def describe_services(cluster: str, services: list[str]) -> dict[str, Any]:
            return {
                "services": [
                    {
                        "serviceName": f"svc-{cluster.rsplit('/', 1)[-1]}",
                        "loadBalancers": [{"targetGroupArn": tg_arn}],
                    }
                ]
            }

    elb_client.ecs = _StubECS()
    maps = elb_client._fetch_ecs_maps(set(), set(), {tg_arn})
    assert maps.by_tg_arn[tg_arn].service_name == "svc-7"


def test_ip_of_a_load_balancer_node_is_not_matched_with_ecs_tasks() -> None:
    """ELB のノードの IP だけを ECS の照合から外す（EC2 の ENI の IP はタスクかもしれない）。"""
    ids = _TargetIds(
        ip_target_keys={
            ("vpc-1", "10.0.0.1"),  # ELB のノード
            ("vpc-1", "10.0.0.2"),  # EC2 にアタッチされた ENI（awsvpc のタスクかもしれない）
            ("vpc-1", "10.0.0.3"),  # ENI が分からない
            ("vpc-1", "10.0.0.4"),  # この VPC では ELB のノード
            ("vpc-2", "10.0.0.4"),  # 同じ CIDR の別の VPC では、ELB のノードではない
        }
    )
    enis = {
        ("vpc-1", "10.0.0.1"): ENIInfo("eni-1", "10.0.0.1", load_balancer_arn="lb"),
        ("vpc-1", "10.0.0.2"): ENIInfo("eni-2", "10.0.0.2", instance_id="i-host"),
        ("vpc-1", "10.0.0.4"): ENIInfo("eni-4", "10.0.0.4", load_balancer_arn="lb"),
        ("vpc-2", "10.0.0.4"): ENIInfo("eni-5", "10.0.0.4"),
    }
    assert _ip_targets_outside_load_balancers(ids, enis) == {"10.0.0.2", "10.0.0.3", "10.0.0.4"}


# =============================================================================
# targets と find の表は、下流の ELB の配下を読み直さない
# =============================================================================


def _count_api_calls(client: ELBClient) -> Counter[str]:
    """`client` がこのあと呼ぶ AWS の操作の回数を数える `Counter` を返す。"""
    calls: Counter[str] = Counter()

    def on_call(model: Any, **_: Any) -> None:
        calls[model.name] += 1

    for service in (
        client.elbv2,
        client.ec2,
        client.acm,
        client.ecs,
        client.lambda_client,
        client.route53,
    ):
        service.meta.events.register("before-call", on_call)
    return calls


@pytest.mark.parametrize(
    "argv", [["targets", "--no-color"], ["find", "web"]], ids=["targets", "find"]
)
def test_tables_read_each_target_group_once(elb_client: ELBClient, argv: list[str]) -> None:
    """下流の ELB があっても、TG のヘルスと ECS を読むのは 1 周だけで、証明書も読まない。"""
    target_groups = len(elb_client._fetch_raw_target_groups())
    calls = _count_api_calls(elb_client)
    assert cli.main(argv, client=elb_client) == 0
    assert calls["DescribeTargetHealth"] == target_groups
    assert calls["ListClusters"] == 1
    assert calls["DescribeListeners"] == 0
    assert calls["DescribeCertificate"] == 0


def test_route_reads_only_what_the_request_reaches(elb_client: ELBClient) -> None:
    """
    `route` は、ポートのリスナーのルールと、一致したルールの転送先の TG だけを読む
    （ELB のすべての TG のヘルスとターゲットの実体は調べない）。
    """
    calls = _count_api_calls(elb_client)
    result = elb_client.simulate_request("https://api.elbx-test.internal/v1/users")
    hop = result.entries[0]
    assert hop.matched_rule is not None
    reached = [ref.target_group_arn for ref in hop.matched_rule.target_group_refs]
    assert calls["DescribeListeners"] == 1
    assert calls["DescribeRules"] == 1
    assert calls["DescribeTargetHealth"] == len(reached)
    assert len(reached) < len(elb_client._fetch_raw_target_groups(lb_arn=hop.load_balancer.arn))
    assert sorted(tg.arn for tg in hop.load_balancer.target_groups) == sorted(reached)
    # ほかのリスナーは、ルールを読まないまま一覧に残る
    assert len(hop.load_balancer.listeners) > 1
    assert [bool(lst.rules) for lst in hop.load_balancer.listeners] == [
        lst.port == 443 for lst in hop.load_balancer.listeners
    ]


def test_route_reads_no_target_group_when_the_rule_does_not_forward(
    elb_client: ELBClient,
) -> None:
    """リダイレクトや固定レスポンスに一致したときは、TG もターゲットも読まない。"""
    calls = _count_api_calls(elb_client)
    result = elb_client.simulate_request("http://api.elbx-test.internal/anything")
    hop = result.entries[0]
    assert hop.matched_rule is not None
    assert hop.matched_rule.target_group_refs == []
    assert not calls.keys() & (
        _RESOLVING_TARGETS | {"DescribeTargetGroups", "DescribeTargetHealth"}
    )


def test_route_follows_a_downstream_load_balancer_one_hop_at_a_time(
    elb_client: ELBClient,
) -> None:
    """下流の ELB も、評価する段になってから、届くポートのルールと TG だけを読む。"""
    calls = _count_api_calls(elb_client)
    result = elb_client.simulate_request("https://edge.elbx-test.internal/auth/login")
    entry = result.entries[0]
    assert [down.load_balancer.name for down in entry.downstream] == ["prod-web-alb"]
    down = entry.downstream[0]
    assert down.matched_rule is not None
    hops = [entry, down]
    reached = [
        ref.target_group_arn
        for hop in hops
        if hop.matched_rule is not None
        for ref in hop.matched_rule.target_group_refs
    ]
    assert calls["DescribeListeners"] == len(hops)
    assert calls["DescribeRules"] == len(hops)
    assert calls["DescribeTargetHealth"] == len(reached)


def test_route_does_not_read_the_records_of_unrelated_zones(elb_client: ELBClient) -> None:
    """ホスト名が入りえないゾーンのレコードは読まない。"""
    calls = _count_api_calls(elb_client)
    with pytest.raises(ResourceNotFoundError):
        elb_client.simulate_request("https://nothing.example.com/")
    assert calls["ListHostedZones"] == 1
    assert calls["ListResourceRecordSets"] == 0


def test_find_table_does_not_read_subnets_and_security_groups(elb_client: ELBClient) -> None:
    """`find` の表はサブネットと SG を出さないので読まない。"""
    calls = _count_api_calls(elb_client)
    assert cli.main(["find", "web"], client=elb_client) == 0
    assert calls["DescribeSubnets"] == 0
    assert calls["DescribeSecurityGroups"] == 0


def test_targets_table_keeps_the_network_of_a_downstream(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """下流の ELB の配下を読まなくても、`targets` の表にはそのサブネットと SG を出す。"""
    argv = ["targets", "--tg", "nlb-to-alb-tg", "--no-color"]
    assert cli.main(argv, client=elb_client) == 0
    out = capsys.readouterr().out
    assert "ALB: prod-web-alb" in out
    assert "prod-alb-sg" in out
    assert "public-subnet-1a" in out


@pytest.mark.parametrize(
    "argv",
    [["targets", "--tg", "nlb-to-alb-tg", "--json"], ["find", "prod-web-alb", "--json"]],
    ids=["targets", "find"],
)
def test_json_still_expands_a_downstream(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """`--json` は、これまでどおり下流の ELB の配下まで返す。"""
    assert cli.main(argv, client=elb_client) == 0
    data = json.loads(capsys.readouterr().out)
    target = data[0]["targets"][0] if "targets" in data[0] else data[0]["target"]
    assert target["downstream_lb"]["listeners"]


def test_library_expands_a_downstream_by_default(elb_client: ELBClient) -> None:
    """ライブラリの既定は配下まで取得し、`expand_downstream=False` なら ELB 本体だけにする。"""
    expanded = elb_client.get_target_group("nlb-to-alb-tg").targets[0].downstream_lb
    assert expanded is not None
    assert expanded.listeners
    unexpanded = (
        elb_client.get_target_group("nlb-to-alb-tg", expand_downstream=False)
        .targets[0]
        .downstream_lb
    )
    assert unexpanded is not None
    assert unexpanded.name == expanded.name
    assert unexpanded.listeners == []
    assert unexpanded.target_groups == []
    assert unexpanded.subnets


def test_find_matches_a_downstream_without_expanding_it(elb_client: ELBClient) -> None:
    """配下を取得しなくても、下流の ELB の名前で逆引きできる。"""
    results = elb_client.find_target("batch-nlb", include_network=False, expand_downstream=False)
    assert any(
        r.target.downstream_lb is not None and r.target.downstream_lb.name == "batch-nlb"
        for r in results
    )


# =============================================================================
# ターゲットがノードの IP だけの TG
# =============================================================================


def test_target_group_of_only_node_ips_does_not_read_ecs(elb_client: ELBClient) -> None:
    """ターゲットが下流の ELB のノードの IP だけなら、ECS を 1 回も読まない。"""
    calls = _count_api_calls(elb_client)
    argv = ["targets", "--tg", "alb-to-nlb-tg", "--no-color"]
    assert cli.main(argv, client=elb_client) == 0
    assert calls["ListClusters"] == 0
    assert calls["ListServices"] == 0


def test_only_target_groups_with_other_targets_are_matched_with_ecs() -> None:
    """ノードの IP 以外が 1 つでもある TG は、ECS のサービスと照合する。"""
    node = {"Target": {"Id": "10.0.0.1"}}
    other = {"Target": {"Id": "10.0.0.2"}}
    raw_tgs = [
        {"TargetGroupArn": arn, "VpcId": "vpc-1"}
        for arn in ("only-nodes", "mixed", "not-candidate")
    ]
    health = {"only-nodes": [node], "mixed": [node, other], "not-candidate": [other]}
    ids = _TargetIds(ecs_candidate_tg_arns={"only-nodes", "mixed"})
    enis = {("vpc-1", "10.0.0.1"): ENIInfo("eni-1", "10.0.0.1", load_balancer_arn="lb")}
    assert _target_groups_outside_load_balancers(ids, raw_tgs, health, enis) == {"mixed"}


# =============================================================================
# route の重み 0
# =============================================================================


def _route_text(
    zero_weight_targets: list[TargetView],
    weights: tuple[tuple[str, int], ...] = (("blue", 100), ("green", 0)),
) -> str:
    """重み 100 の TG と重み 0 の TG へ転送する `route` の出力を返す（`weights` で変えられる）。"""
    tg_of = {
        name: TargetGroupView(
            arn=name, name=name, protocol="HTTP", port=80, target_type="ip", targets=targets
        )
        for name, targets in (
            ("blue", [TargetView("10.0.0.1", 80, "healthy")]),
            ("green", zero_weight_targets),
        )
    }
    entry = _lb_with_targets("entry", [])
    rule = RuleView(
        arn="r",
        priority="default",
        is_default=True,
        target_group_refs=[
            RuleTargetGroupRef(name, name, weight, tg_of[name]) for name, weight in weights
        ],
    )
    entry.listeners = [ListenerView(arn="l", port=80, protocol="HTTP", rules=[rule])]
    req = SimulatedRequest.from_url("http://x.example.com/")
    return format_route_simulation(
        RouteSimulationResult(
            url=req.url,
            method=req.method,
            host=req.host,
            port=req.port,
            path=req.path,
            entries=[simulate_hop(entry, req, 80)],
        ),
        use_color=False,
    )


def test_route_says_a_target_group_of_weight_zero_receives_no_requests() -> None:
    """重み 0 の TG はヘルスの集計だけを出し、届かないターゲットは並べない。"""
    text = _route_text([TargetView("10.0.0.2", 80, "unhealthy")])
    assert "TG: green (weight=0) [0/1 healthy, 1 unhealthy] (リクエストは届きません)" in text
    assert "10.0.0.2" not in text
    assert "TG: blue (weight=100) [1/1 healthy]\n" in text
    assert "10.0.0.1" in text


def test_route_says_so_when_the_target_of_weight_zero_is_a_downstream() -> None:
    """下流の ELB を持つ重み 0 の TG も同じ表示にし、転送先には出さない。"""
    down = _downstream_with_listeners(80)
    text = _route_text([_node(down, "10.0.0.3")])
    assert "TG: green (weight=0) [1/1 healthy] (リクエストは届きません)" in text
    assert "down" not in text


def test_route_hides_the_weight_of_a_single_target_group() -> None:
    """TG が 1 つの forward に実際の AWS が付ける weight=1 は出さない（tree と同じ）。"""
    text = _route_text([], weights=(("blue", 1),))
    assert "TG: blue [1/1 healthy]\n" in text
    assert "weight" not in text


def test_route_shows_weight_zero_even_for_a_single_target_group() -> None:
    """TG が 1 つでも、重み 0 は届かない理由なので出す。"""
    text = _route_text([TargetView("10.0.0.2", 80, "healthy")], weights=(("green", 0),))
    assert "TG: green (weight=0) [1/1 healthy] (リクエストは届きません)" in text


# =============================================================================
# ライブラリの使い勝手: note_code と repr
# =============================================================================


def test_route_hop_has_a_note_code(elb_client: ELBClient) -> None:
    """評価が止まった理由は、言語によらない note_code でも分かる（JSON にも入る）。"""
    hop = elb_client.simulate_request("https://api.elbx-test.internal:8443/").entries[0]
    assert hop.matched_rule is None
    assert hop.note_code == "no_listener"
    assert hop.to_dict()["note_code"] == "no_listener"

    matched = elb_client.simulate_request("https://api.elbx-test.internal/v1/users").entries[0]
    assert matched.matched_rule is not None
    assert matched.note_code == ""
    assert matched.note == ""


def test_reprs_are_one_short_line(elb_client: ELBClient) -> None:
    """ELB・TG・ターゲット・ホップの repr は 1 行（入れ子は件数だけ。REPL で読めるように）。"""
    lb = elb_client.get_load_balancer("prod-web-alb")
    hop = elb_client.simulate_request("https://api.elbx-test.internal/v1/users").entries[0]
    for obj in (lb, lb.target_groups[0], lb.all_targets[0], hop):
        text = repr(obj)
        assert "\n" not in text
        assert len(text) < 200, text
    assert repr(lb) == (
        f"LoadBalancerView(name='prod-web-alb', type='application', "
        f"listeners={len(lb.listeners)}, target_groups={len(lb.target_groups)})"
    )
    assert repr(hop) == (
        "RouteHop(load_balancer='prod-web-alb', port=443, matched_rule='10', note_code='', "
        "downstream=0)"
    )


# =============================================================================
# 互いに関係のない取得を並行して進める / 同じものを 2 回取らない
# =============================================================================


@pytest.mark.parametrize("background", [True, False], ids=["thread", "serial"])
def test_deferred_returns_the_result_or_raises_the_same_error(background: bool) -> None:
    """`Deferred` は結果を返し、取得の失敗は `result()` を呼んだ側に同じ例外で伝える。"""
    assert Deferred(lambda: 1 + 1, background=background).result() == 2

    error = ValueError("boom")

    def fail() -> int:
        raise error

    failed = Deferred(fail, background=background)
    with pytest.raises(ValueError, match="boom") as exc_info:
        failed.result()
    assert exc_info.value is error


def test_deferred_without_a_thread_runs_only_when_asked() -> None:
    """`-j 1` のときは別のスレッドを使わず、`result()` を呼んだときに実行する（直列のまま）。"""
    ran: list[str] = []
    deferred = Deferred(lambda: ran.append("x"), background=False)
    assert ran == []
    deferred.result()
    assert ran == ["x"]


def test_api_calls_at_once_never_exceed_max_workers(moto_env: TestEnv) -> None:
    """並行して進める取得を合わせても、同時に呼ぶ AWS API の数は `max_workers` を超えない。"""
    client = ELBClient(region_name=moto_env.region, session=moto_env.session, max_workers=2)
    lock = threading.Lock()
    running = 0
    most = 0

    def started(**_: Any) -> None:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        time.sleep(0.002)  # moto はすぐ返すので、重なりが見えるように少し待つ

    def ended(**_: Any) -> None:
        nonlocal running
        with lock:
            running -= 1

    for service in (client.elbv2, client.ec2, client.acm, client.ecs, client.lambda_client):
        service.meta.events.register("before-call", started)
        service.meta.events.register("after-call", ended)
    assert client.list_load_balancers()
    assert 1 <= most <= 2
    assert client._api_slots._value == 2  # 枠はすべて返っている


def test_api_slot_is_returned_when_a_call_fails(elb_client: ELBClient) -> None:
    """応答のない失敗（接続できないなど）でも枠を返し、同じ呼び出しで 2 回は返さない。"""
    free = elb_client._api_slots._value
    context: dict[str, Any] = {}
    elb_client._on_api_call_start(context)
    assert elb_client._api_slots._value == free - 1
    elb_client._on_api_call_error(context)
    elb_client._on_api_call_error(context)
    assert elb_client._api_slots._value == free

    # include_dns=False: Route 53 のゾーン一覧の先読みが、別のスレッドで枠を持っている間に数えない
    with pytest.raises(ResourceNotFoundError):
        elb_client.get_load_balancer("no-such-lb", include_dns=False)
    assert elb_client._api_slots._value == free


def test_one_run_reads_each_thing_once(elb_client: ELBClient) -> None:
    """下流の ELB をたどっても、同じ ECS クラスターと同じ ELB は 1 回の実行で 1 回しか読まない。"""
    calls = _count_api_calls(elb_client)
    assert cli.main(["tree", "prod-web-alb", "--no-color"], client=elb_client) == 0
    assert calls["ListClusters"] == 1
    assert calls["ListServices"] == 1
    assert calls["ListContainerInstances"] == 1
    # 入口の ELB と、ENI から特定した下流の ELB（その ELB を ARN で取り直さない）
    assert calls["DescribeLoadBalancers"] == 2

    calls.clear()
    assert cli.main(["find", "web", "--no-color"], client=elb_client) == 0
    assert calls["DescribeLoadBalancers"] == 1  # 最初の全件の一覧だけ


def test_run_cache_does_not_outlive_the_call(elb_client: ELBClient) -> None:
    """置き場は呼び出しが終われば捨てる（長く使うクライアントが古い結果を返さない）。"""
    calls = _count_api_calls(elb_client)
    first = elb_client.list_load_balancers(include_relations=False)
    assert elb_client._run_cache is None
    second = elb_client.list_load_balancers(include_relations=False)
    assert calls["DescribeLoadBalancers"] == 2
    assert [lb.arn for lb in first] == [lb.arn for lb in second]

    # 失敗して抜けても残さない
    with pytest.raises(ResourceNotFoundError):
        elb_client.get_load_balancer("no-such-lb")
    assert elb_client._run_cache is None
    assert elb_client._run_depth == 0


def test_run_cache_fetches_once_even_when_asked_at_the_same_time() -> None:
    """並行して同じものを求めても取得は 1 回で、待った側も同じ結果（か同じ例外）を受け取る。"""
    cache = RunCache()
    started = threading.Event()
    release = threading.Event()
    count = 0

    def slow() -> str:
        nonlocal count
        count += 1
        started.set()
        release.wait(5)
        return "value"

    first = Deferred(lambda: cache.once("key", slow))
    started.wait(5)
    second = Deferred(lambda: cache.once("key", slow))
    release.set()
    assert (first.result(), second.result(), count) == ("value", "value", 1)

    def fail() -> str:
        raise ValueError("boom")

    for _ in range(2):
        with pytest.raises(ValueError, match="boom"):
            cache.once("bad", fail)


def test_route53_zone_list_is_read_ahead_only_when_route53_is_used(
    elb_client: ELBClient,
) -> None:
    """Route 53 のゾーン一覧を先に読み始めるのは、Route 53 を使うと分かっているときだけ。"""
    dns_name = elb_client.list_load_balancers(include_relations=False)[0].dns_name
    calls = _count_api_calls(elb_client)
    for argv in (
        ["ls", "--no-dns"],
        ["tree", "prod-web-alb", "--no-dns"],
        ["targets"],
        ["route", f"https://{dns_name}/", "--no-color"],
        ["route", "https://api.elbx-test.internal/", "--lb", "prod-web-alb"],
    ):
        assert cli.main(argv, client=elb_client) == 0
        assert calls["ListHostedZones"] == 0, argv

    assert cli.main(["ls"], client=elb_client) == 0
    assert calls["ListHostedZones"] == 1


def test_serial_and_parallel_runs_print_the_same(
    moto_env: TestEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-j 1`（直列）でも、並行して取得したときと同じ出力になる。"""
    outputs = []
    for workers in (1, 10):
        client = ELBClient(
            region_name=moto_env.region, session=moto_env.session, max_workers=workers
        )
        assert cli.main(["tree", "--full", "--no-color"], client=client) == 0
        assert cli.main(["targets", "--no-color"], client=client) == 0
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1]
