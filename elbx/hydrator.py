"""
ELBv2の階層構造（LoadBalancer -> Route53 / Listener -> Rule -> TargetGroup -> Target）と
関連リソース（EC2, ECS, Lambda, 下流ELB, ENI, Subnet, SG, ACM）を統合・構築するモジュール。
並列フェッチ・一括取得・再帰キャッシュ共有によりAPIコール数と待機時間を最小化する。
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ._ids import canonical_ip, is_arn_of
from .fetchers import (
    AWSFetcherMixin,
    Deferred,
    ECSMaps,
    _format_datetime,
    pick_in_vpc,
    tolerate_aws_errors,
)
from .models import (
    CertificateInfo,
    EC2InstanceInfo,
    ECSTaskInfo,
    ENIInfo,
    LambdaFunctionInfo,
    ListenerView,
    LoadBalancerView,
    Route53RecordInfo,
    RuleTargetGroupRef,
    RuleView,
    SecurityGroupInfo,
    SubnetInfo,
    TargetGroupView,
    TargetView,
)
from .simulator import (
    SimulatedRequest,
    _regex_values,
    _values,
    evaluate_listener,
    query_pair_label,
)


def _match_subnet_for_ip(
    ip_str: str,
    subnet_info_map: dict[str, SubnetInfo],
    subnet_vpcs: dict[str, str] | None = None,
    vpc_id: str = "",
) -> SubnetInfo | None:
    """
    IPアドレス（IPv4 / IPv6）が属するサブネットをCIDRブロックの包含判定から特定する。

    ECS Fargate 等の IP ターゲットでは `describe_target_health` の応答に AvailabilityZone が
    含まれないことがあるため、所属VPCのサブネット一覧のCIDR（IPv4およびIPv6 CIDR）と
    照合してサブネットとAZを決定論的に逆引きする。
    `vpc_id` を渡すと、その VPC のサブネットだけと照合する（別々の VPC が同じ CIDR を
    使っていても、ほかの VPC のサブネットと取り違えない）。

    :param ip_str: 判定対象の文字列（IP でなければ None を返す）
    :param subnet_info_map: `{subnet_id: SubnetInfo}` のマップ
    :param subnet_vpcs: `{subnet_id: VPC ID}`（`vpc_id` で絞り込むときに使う）
    :param vpc_id: 照合する VPC の ID（空ならすべてのサブネット）
    :return: IPが含まれる `SubnetInfo`（一致するサブネットがない場合や不正なIPの場合はNone）
    """
    canon = canonical_ip(ip_str)
    if canon is None:
        return None
    ip_obj = ipaddress.ip_address(canon)
    for s_id, s_info in subnet_info_map.items():
        if vpc_id and (subnet_vpcs or {}).get(s_id) != vpc_id:
            continue
        for cidr in s_info.cidr_blocks:
            with contextlib.suppress(ValueError):
                if ip_obj in ipaddress.ip_network(cidr):
                    return s_info
    return None


def _security_groups(
    sg_info_map: dict[str, SecurityGroupInfo], sg_ids: list[str]
) -> list[SecurityGroupInfo]:
    """SG ID のリストを SecurityGroupInfo のリストにする（取得できなかった SG は ID だけ）。"""
    return [sg_info_map.get(sg_id, SecurityGroupInfo(group_id=sg_id)) for sg_id in sg_ids]


@dataclass
class _TargetLookups:
    """`_hydrate_target_groups` で一括取得した、ターゲットの実体リソースを引くための対応表。"""

    ec2_by_id: dict[str, EC2InstanceInfo]
    # IP ターゲットの `_ip_target_key` -> その IP の EC2 / ENI
    ec2_by_ip: dict[tuple[str, str], EC2InstanceInfo]
    eni_by_ip: dict[tuple[str, str], ENIInfo]
    ecs_maps: ECSMaps
    lambda_map: dict[str, LambdaFunctionInfo]
    # 以下は実体を取得したあとに入れる（サブネット・SG、下流 ELB）
    alb_view_map: dict[str, LoadBalancerView] = field(default_factory=dict)
    subnet_info_map: dict[str, SubnetInfo] = field(default_factory=dict)
    sg_info_map: dict[str, SecurityGroupInfo] = field(default_factory=dict)
    # サブネット ID -> VPC ID
    subnet_vpcs: dict[str, str] = field(default_factory=dict)

    def security_groups(self, sg_ids: list[str]) -> list[SecurityGroupInfo]:
        """SG ID のリストを SecurityGroupInfo のリストにする（取得できなかった SG は ID だけ）。"""
        return _security_groups(self.sg_info_map, sg_ids)


def _ip_target_key(tg: dict[str, Any], desc: dict[str, Any]) -> tuple[str, str]:
    """
    IP ターゲットの実体（ENI・EC2）を探すときのキー `(探す VPC の ID, IP)` を返す。

    AZ が `all` のターゲットは TG の VPC の外の IP（ピアリング先の VPC など）なので、
    VPC を空にしてすべての VPC から探す。それ以外は TG の VPC の中だけで探す
    （別々の VPC が同じ CIDR を使っていても、ほかの VPC の同じ IP と取り違えない）。

    :param tg: 生TG辞書
    :param desc: TargetHealthDescriptions の要素
    :return: `(VPC ID, IP)`
    """
    target = desc.get("Target", {})
    vpc_id = "" if target.get("AvailabilityZone") == "all" else tg.get("VpcId", "")
    return vpc_id, target.get("Id", "")


@dataclass
class _TargetIds:
    """TG のターゲットを種別ごとに振り分けたもの（後続の API をまとめて呼ぶため）。"""

    instance_ids: set[str] = field(default_factory=set)
    ip_targets: set[str] = field(default_factory=set)
    # IP ターゲットの `_ip_target_key`
    ip_target_keys: set[tuple[str, str]] = field(default_factory=set)
    lambda_arns: set[str] = field(default_factory=set)
    downstream_lb_arns: set[str] = field(default_factory=set)
    # ECS のタスクかもしれないターゲットを持つ TG
    ecs_candidate_tg_arns: set[str] = field(default_factory=set)
    vpc_ids: set[str] = field(default_factory=set)


def _classify_targets(
    raw_tgs: list[dict[str, Any]], health_by_tg: dict[str, list[dict[str, Any]]]
) -> _TargetIds:
    """
    TG のターゲットを種別（EC2・IP・Lambda・下流 ELB）ごとに振り分ける。

    :param raw_tgs: 生TG辞書
    :param health_by_tg: TG ARN -> TargetHealthDescriptions
    :return: 振り分けたターゲット
    """
    ids = _TargetIds()
    for tg in raw_tgs:
        tg_arn = tg["TargetGroupArn"]
        target_type = tg.get("TargetType", "instance")
        if tg.get("VpcId"):
            ids.vpc_ids.add(tg["VpcId"])
        descriptions = health_by_tg.get(tg_arn, [])

        if target_type in ("ip", "instance") and descriptions:
            ids.ecs_candidate_tg_arns.add(tg_arn)

        for desc in descriptions:
            t_id = desc.get("Target", {}).get("Id", "")
            if not t_id:
                continue
            if is_arn_of(t_id, "elasticloadbalancing") or target_type == "alb":
                ids.downstream_lb_arns.add(t_id)
            elif is_arn_of(t_id, "lambda") or target_type == "lambda":
                ids.lambda_arns.add(t_id)
            elif target_type == "instance" or t_id.startswith("i-"):
                ids.instance_ids.add(t_id)
            elif target_type == "ip":
                ids.ip_targets.add(t_id)
                ids.ip_target_keys.add(_ip_target_key(tg, desc))
    return ids


def _ip_targets_outside_load_balancers(
    ids: _TargetIds, eni_by_ip: dict[tuple[str, str], ENIInfo]
) -> set[str]:
    """
    IP ターゲットのうち、ELB のノード以外の IP を返す（ECS のタスクと照合する意味があるもの）。

    EC2 に解決できた IP は除かない。awsvpc の ECS on EC2 では、タスクの ENI が EC2 に
    アタッチされているので、EC2 に解決できてもタスクの IP のことがある。

    :param ids: 振り分けたターゲット
    :param eni_by_ip: IP ターゲットの ENI（キーは `_ip_target_key`）
    :return: ECS のタスクと照合する IP
    """
    ips: set[str] = set()
    for key in ids.ip_target_keys:
        eni = eni_by_ip.get(key)
        if eni is None or not eni.load_balancer_arn:
            ips.add(key[1])
    return ips


def _target_groups_outside_load_balancers(
    ids: _TargetIds,
    raw_tgs: list[dict[str, Any]],
    health_by_tg: dict[str, list[dict[str, Any]]],
    eni_by_ip: dict[tuple[str, str], ENIInfo],
) -> set[str]:
    """
    ECS のタスクかもしれないターゲットを持つ TG のうち、ELB のノード以外のターゲットがあるものを
    返す（ノードの IP だけの TG のために、ECS のサービスを読まないため）。

    :param ids: 振り分けたターゲット
    :param raw_tgs: 生TG辞書
    :param health_by_tg: TG ARN -> TargetHealthDescriptions
    :param eni_by_ip: IP ターゲットの ENI（キーは `_ip_target_key`）
    :return: ECS と照合する TG の ARN
    """
    tg_arns: set[str] = set()
    for tg in raw_tgs:
        tg_arn = tg["TargetGroupArn"]
        if tg_arn not in ids.ecs_candidate_tg_arns:
            continue
        for desc in health_by_tg.get(tg_arn, []):
            eni = eni_by_ip.get(_ip_target_key(tg, desc))
            if eni is None or not eni.load_balancer_arn:
                tg_arns.add(tg_arn)
                break
    return tg_arns


def _resolve_ecs_for_target(
    t_id: str,
    t_port: int,
    tg: dict[str, Any],
    lookups: _TargetLookups,
    ec2_info: EC2InstanceInfo | None,
    eni_info: ENIInfo | None,
    matched_ip_subnet: SubnetInfo | None,
) -> ECSTaskInfo | None:
    """
    ターゲットに対応する ECS タスク（またはサービス単位の情報）を探し、
    このターゲット用にサブネット・SG・AZ を補った `ECSTaskInfo` を返す。

    探す順: タスクIP → (ホスト, ポート) → ホスト単独 → TG に紐づくサービス単位の情報

    :param t_id: ターゲットID（IP またはインスタンスID）
    :param t_port: ターゲットのポート
    :param tg: 親の生TG辞書
    :param lookups: 一括取得済みのリソースの対応表
    :param ec2_info: このターゲットに対応する EC2（あれば）
    :param eni_info: このターゲットに対応する ENI（あれば）
    :param matched_ip_subnet: IP をサブネットの CIDR と照合して特定したサブネット（あれば）
    :return: `ECSTaskInfo`（ECS のターゲットでなければ None）
    """
    maps = lookups.ecs_maps
    raw_ecs = (
        maps.task_for_ip(t_id, eni_info.network_interface_id if eni_info else "")
        or maps.by_instance_port.get((t_id, t_port))
        or maps.by_instance.get(t_id)
        or maps.by_tg_arn.get(tg["TargetGroupArn"])
    )
    if raw_ecs is None:
        return None

    # 複数ターゲットが同じサービス情報を共有していても書き換えないよう deepcopy する
    ecs_info = copy.deepcopy(raw_ecs)
    if not ecs_info.private_ip:
        if tg.get("TargetType") == "ip":
            ecs_info.private_ip = t_id
        elif ec2_info:
            ecs_info.private_ip = ec2_info.private_ip

    # IPからCIDR照合で特定したサブネットをサービス全体のサブネットより優先する
    if matched_ip_subnet:
        ecs_info.subnets = [matched_ip_subnet]
        if matched_ip_subnet.subnet_id not in ecs_info.subnet_ids:
            ecs_info.subnet_ids = [matched_ip_subnet.subnet_id, *ecs_info.subnet_ids]
    else:
        ecs_info.subnets = [
            lookups.subnet_info_map[s] for s in ecs_info.subnet_ids if s in lookups.subnet_info_map
        ]

    if eni_info:
        for sg_id in eni_info.security_group_ids:
            if sg_id not in ecs_info.security_group_ids:
                ecs_info.security_group_ids.append(sg_id)
    ecs_info.security_groups = lookups.security_groups(ecs_info.security_group_ids)

    # AZ不明時は ENI → CIDR一致サブネット → 先頭サブネット → EC2 の順で補完
    if not ecs_info.availability_zone:
        candidates = (
            eni_info.availability_zone if eni_info else "",
            matched_ip_subnet.availability_zone if matched_ip_subnet else "",
            ecs_info.subnets[0].availability_zone if ecs_info.subnets else "",
            ec2_info.availability_zone if ec2_info else "",
        )
        ecs_info.availability_zone = next((az for az in candidates if az), "")
    return ecs_info


# 値のリストだけを持つ条件の表示名と、値が入る *Config のキー
_VALUE_CONDITIONS = {
    "host-header": ("Host", "HostHeaderConfig"),
    "path-pattern": ("Path", "PathPatternConfig"),
    "http-request-method": ("Method", "HttpRequestMethodConfig"),
    "source-ip": ("SourceIP", "SourceIpConfig"),
}


def _condition_text(cond: dict[str, Any]) -> str:
    """
    ルール条件1件を `Host in [api.example.com]` のような表示用の文字列にする。
    新形式（`*Config.Values`）と旧形式（`Values`）の両方に対応する。
    正規表現の値（`RegexValues`）は `regex:` を付けて並べる。

    :param cond: describe_rules の Condition 辞書
    :return: 表示用の条件文字列
    """
    field_name = cond.get("Field", "")
    regexes = [f"regex:{r}" for r in _regex_values(cond)]
    if field_name in _VALUE_CONDITIONS:
        label, config_key = _VALUE_CONDITIONS[field_name]
        return f"{label} in [{', '.join([*_values(cond, config_key), *regexes])}]"
    if field_name == "http-header":
        cfg = cond.get("HttpHeaderConfig", {})
        vals = [*cfg.get("Values", []), *regexes]
        return f"Header({cfg.get('HttpHeaderName', 'Header')}) in [{', '.join(vals)}]"
    if field_name == "query-string":
        pairs = cond.get("QueryStringConfig", {}).get("Values", [])
        vals = [query_pair_label(q) for q in pairs]
        return f"Query in [{', '.join(vals)}]"
    return f"{field_name}: {', '.join(cond.get('Values', []))}"


def _tg_ref(
    t_arn: str, weight: int | None, tg_view_map: dict[str, TargetGroupView]
) -> RuleTargetGroupRef:
    """
    転送先TGの参照を作る。TGを取得できていない場合も、ARN からTG名を取り出して表示できるようにする。

    :param t_arn: 転送先TGのARN（`.../targetgroup/<名前>/<ID>`）
    :param weight: 重み（ForwardConfig で指定されていない場合は None）
    :param tg_view_map: `{tg_arn: TargetGroupView}` のマップ
    :return: `RuleTargetGroupRef`
    """
    tg_obj = tg_view_map.get(t_arn)
    name = tg_obj.name if tg_obj else (t_arn.split("/")[-2] if "/" in t_arn else t_arn)
    return RuleTargetGroupRef(
        target_group_arn=t_arn, target_group_name=name, weight=weight, target_group=tg_obj
    )


# RedirectConfig の Query の既定値（リクエストのクエリをそのまま付ける）
_REDIRECT_KEEPS_QUERY = "#{query}"


def _auth_summary(label: str, **details: str) -> str:
    """
    認証アクションの要約（`AuthenticateOidc (issuer=…, on_unauthenticated=…)`）を作る。
    どこで認証するかと、未認証のリクエストをどうするか（deny / allow / authenticate）が分かる
    ようにする。値のない項目は出さない。クライアントのシークレットは出さない。

    :param label: アクションの名前
    :param details: 出す項目（名前 -> 値）
    :return: 要約
    """
    shown = ", ".join(f"{name}={value}" for name, value in details.items() if value)
    return f"{label} ({shown})" if shown else label


def _summarize_action(
    act: dict[str, Any], tg_view_map: dict[str, TargetGroupView]
) -> tuple[str, list[RuleTargetGroupRef]]:
    """
    ルールのアクション1件を、表示用の要約と転送先TGの参照にする。

    :param act: describe_rules の Action 辞書
    :param tg_view_map: `{tg_arn: TargetGroupView}` のマップ
    :return: `(要約文字列, 転送先TGの参照のリスト)`（要約がない場合は空文字列）
    """
    act_type = act.get("Type", "")
    if act_type == "forward":
        fwd_tgs = act.get("ForwardConfig", {}).get("TargetGroups", [])
        if fwd_tgs:
            refs = [
                _tg_ref(f.get("TargetGroupArn", ""), f.get("Weight"), tg_view_map) for f in fwd_tgs
            ]
        elif act.get("TargetGroupArn"):
            refs = [_tg_ref(act["TargetGroupArn"], None, tg_view_map)]
        else:
            return "", []
        # 単一TGの ForwardConfig では weight=1 が自動付与されるため、複数TGのときだけ重みを出す
        parts = [
            f"{r.target_group_name} (weight={r.weight})"
            if r.weight is not None and len(refs) > 1
            else r.target_group_name
            for r in refs
        ]
        return f"Forward -> {', '.join(parts)}", refs
    if act_type == "redirect":
        rc = act.get("RedirectConfig", {})
        target = (
            f"{rc.get('Protocol', '#{protocol}')}://{rc.get('Host', '#{host}')}:"
            f"{rc.get('Port', '#{port}')}{rc.get('Path', '/#{path}')}"
        )
        # Query の既定（リクエストのクエリをそのまま付ける）は出さず、変えているときだけ出す
        query = rc.get("Query", _REDIRECT_KEEPS_QUERY)
        if query != _REDIRECT_KEEPS_QUERY:
            target += f"?{query}"
        return f"Redirect ({rc.get('StatusCode', 'HTTP_301')}) -> {target}", []
    if act_type == "authenticate-cognito":
        cognito = act.get("AuthenticateCognitoConfig", {})
        return _auth_summary(
            "AuthenticateCognito",
            # ARN は `…:userpool/<プールの ID>`
            user_pool=cognito.get("UserPoolArn", "").rpartition("/")[2],
            on_unauthenticated=cognito.get("OnUnauthenticatedRequest", ""),
        ), []
    if act_type == "authenticate-oidc":
        oidc = act.get("AuthenticateOidcConfig", {})
        return _auth_summary(
            "AuthenticateOidc",
            issuer=oidc.get("Issuer", ""),
            on_unauthenticated=oidc.get("OnUnauthenticatedRequest", ""),
        ), []
    if act_type == "fixed-response":
        fc = act.get("FixedResponseConfig", {})
        return (
            f"FixedResponse ({fc.get('StatusCode', '200')}, {fc.get('ContentType', 'text/plain')})",
            [],
        )
    return act_type, []


@dataclass
class _RawTargetGroups:
    """`_hydrate_load_balancers` で集める生 TG（ELB ごとと ARN ごと）と、共有プール。"""

    # ELB ARN -> その ELB の生 TG（同じ TG が重なることがある）
    by_lb: dict[str, list[dict[str, Any]]]
    # 共有プール `{tg_arn: raw_tg}`（アカウント内の TG を一度に取得したもの。下流 ELB でも使う）
    pool: dict[str, dict[str, Any]]
    # TG ARN -> 生 TG（この一括で解決する TG）
    by_arn: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, lb_arn: str, raw_tg: dict[str, Any]) -> None:
        """ELB の TG として足す。"""
        self.by_lb[lb_arn].append(raw_tg)
        self.by_arn[raw_tg["TargetGroupArn"]] = raw_tg


@dataclass
class _LBBatch:
    """`_hydrate_load_balancers` で一括取得した、`LoadBalancerView` を組み立てる材料。"""

    # ELB ARN -> 生リスナー
    listeners_by_lb: dict[str, list[dict[str, Any]]]
    # リスナー ARN -> 生ルール
    rules_by_listener: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tg_views: dict[str, TargetGroupView] = field(default_factory=dict)
    subnets: dict[str, dict[str, Any]] = field(default_factory=dict)
    security_groups: dict[str, SecurityGroupInfo] = field(default_factory=dict)
    certificates: dict[str, CertificateInfo] = field(default_factory=dict)
    # ELB の DNS 名 -> Route 53 レコード
    route53: dict[str, list[Any]] = field(default_factory=dict)
    # ELB ARN -> ELB 属性
    attributes: dict[str, dict[str, str]] = field(default_factory=dict)

    def all_listeners(self) -> list[dict[str, Any]]:
        """すべての ELB の生リスナー（ELB の順）。"""
        return [lst for listeners in self.listeners_by_lb.values() for lst in listeners]


@dataclass
class _LBSurroundings:
    """
    ELB の周りのリソース（サブネット・SG・ACM 証明書・ELB 属性・Route 53 レコード）の、進行中の
    取得。ELB だけで決まるものは TG やターゲットを待たずに取り始め、`fill` で結果を受け取る。
    取得しないものは None。
    """

    subnets: Deferred[dict[str, dict[str, Any]]] | None = None
    security_groups: Deferred[dict[str, SecurityGroupInfo]] | None = None
    certificates: Deferred[dict[str, CertificateInfo]] | None = None
    attributes: Deferred[dict[str, dict[str, str]]] | None = None
    route53: Deferred[dict[str, list[Route53RecordInfo]]] | None = None

    def fill(self, batch: _LBBatch) -> None:
        """取得が終わるのを待って、結果を `batch` に入れる。"""
        if self.subnets is not None:
            batch.subnets = self.subnets.result()
        if self.security_groups is not None:
            batch.security_groups = self.security_groups.result()
        if self.certificates is not None:
            batch.certificates = self.certificates.result()
        if self.attributes is not None:
            batch.attributes = self.attributes.result()
        if self.route53 is not None:
            batch.route53 = dict(self.route53.result())


def _lb_subnet(az: dict[str, Any], s_detail: dict[str, Any]) -> SubnetInfo:
    """
    ELB の AvailabilityZones の 1 件から、ELB のノードの IP 付きの `SubnetInfo` を作る。

    :param az: `describe_load_balancers` の AvailabilityZones の要素
    :param s_detail: そのサブネットの詳細（`_fetch_subnets_map` の値。取得できなければ空）
    :return: `SubnetInfo`
    """
    lb_addrs = [
        addr[key]
        for addr in az.get("LoadBalancerAddresses", [])
        for key in ("IpAddress", "IPv6Address")
        if addr.get(key)
    ]
    return SubnetInfo(
        subnet_id=az.get("SubnetId", ""),
        availability_zone=az.get("ZoneName", s_detail.get("availability_zone", "")),
        name=s_detail.get("name", ""),
        cidr_block=s_detail.get("cidr_block", ""),
        ipv6_cidr_blocks=list(s_detail.get("ipv6_cidr_blocks", [])),
        load_balancer_addresses=lb_addrs,
    )


def _reaches(start: LoadBalancerView, lb_arns: set[str]) -> bool:
    """
    `start` か、その下流の ELB（`TargetView.downstream_lb` でたどる）が `lb_arns` に含まれるかを
    返す。

    :param start: たどり始める ELB
    :param lb_arns: 探す ELB の ARN
    :return: 含まれれば True
    """
    seen: set[str] = set()
    stack = [start]
    while stack:
        lb = stack.pop()
        if lb.arn in lb_arns:
            return True
        if lb.arn not in seen:
            seen.add(lb.arn)
            stack += [
                target.downstream_lb
                for target in lb.all_targets
                if target.downstream_lb is not None
            ]
    return False


def _link_downstream_lbs(
    lb_views: list[LoadBalancerView], tg_views: Iterable[TargetGroupView]
) -> None:
    """
    同じ一括で組み立てた ELB が下流 ELB として登録されていれば、追加の API 呼び出しなしで
    `TargetView.downstream_lb` に紐付ける。

    紐付けると ELB の参照が輪になるターゲット（自分のノードの IP を登録した TG や、A -> B -> A の
    構成）は紐付けない。輪があると `to_dict()` やツリーの描画が終わらないため。そのターゲットは
    ARN や ENI のまま表示される。

    :param lb_views: 組み立てた ELB
    :param tg_views: その ELB たちの TG
    """
    by_arn = {v.arn: v for v in lb_views}
    # TG の ARN -> その TG を持つ ELB の ARN（同じ TG を複数の ELB が使うことがある）
    owners: dict[str, set[str]] = {}
    for lb_v in lb_views:
        for owned in lb_v.target_groups:
            owners.setdefault(owned.arn, set()).add(lb_v.arn)
    for tg_v in tg_views:
        for t_v in tg_v.targets:
            if t_v.downstream_lb is not None:
                continue
            # ELB の ARN を直接登録（alb タイプ）、または ELB のノードの IP を登録（ip タイプ）
            lb_arn = t_v.target_id if t_v.target_id in by_arn else ""
            if not lb_arn and t_v.eni and t_v.eni.load_balancer_arn in by_arn:
                lb_arn = t_v.eni.load_balancer_arn
            if lb_arn and not _reaches(by_arn[lb_arn], owners.get(tg_v.arn, set())):
                t_v.downstream_lb = by_arn[lb_arn]


class ELBHydratorMixin(AWSFetcherMixin):
    """
    生のAWS API辞書群とリソースマップを統合し、
    ネストされたドメインモデル（LoadBalancerView / TargetGroupView / RuleView）を
    構築するMixinクラス。
    """

    def _hydrate_load_balancers(
        self,
        raw_lbs: list[dict[str, Any]],
        _visited_lb_arns: set[str] | None = None,
        include_network: bool = True,
        _shared_raw_tgs: dict[str, dict[str, Any]] | None = None,
        include_dns: bool = True,
        include_certificates: bool = True,
        include_attributes: bool = True,
        resolve_targets: bool = True,
    ) -> list[LoadBalancerView]:
        """
        生のELB辞書リストから関連リソースを一括・並列解決して `LoadBalancerView` を構築する。

        :param raw_lbs: `describe_load_balancers` が返す生ELB辞書のリスト
        :param _visited_lb_arns: 多段ELBの再帰取得時に循環参照を防ぐための訪問済みELB ARN集合
        :param include_network: ELB・ターゲット・下流ELBのサブネット/SGを取得するか
        :param _shared_raw_tgs: 複数ELB取得時に共有するTG生データのプール（`{tg_arn: raw_tg}`）
        :param include_dns: Route 53 の全ゾーンを走査して ELB を向くレコードを取得するか
        :param include_certificates: ACM 証明書の詳細を取得するか（下流ELBにも引き継ぐ）
        :param include_attributes: ELB・TG の属性を取得するか（下流ELBにも引き継ぐ）
        :param resolve_targets: ターゲットの実体（EC2・ECS・Lambda・ENI・下流ELB）を調べるか。
            False ならターゲットはヘルス状態だけになる
        :return: 構築された `LoadBalancerView` のリスト
        """
        if not raw_lbs:
            return []
        lb_arns = [lb["LoadBalancerArn"] for lb in raw_lbs]
        visited = {*(_visited_lb_arns or ()), *lb_arns}

        # 0. ELB だけで決まる周りのリソース（サブネット・SG・ELB 属性・Route 53 レコード）は、
        #    リスナーやターゲットを待たずに取り始める（結果は 4. で受け取る）
        surroundings = self._start_lb_surroundings(
            raw_lbs, include_network, include_dns, include_attributes
        )

        # 1. ELB ごとのターゲットグループ（リスナーとルールを待たずに取り始める）
        raw_tgs_started = self._start(
            lambda: self._collect_raw_target_groups(lb_arns, _shared_raw_tgs)
        )

        # 2. 全ELBのリスナーと、全リスナーのルール（ルールの転送先にだけ出てくる TG も足す）
        listeners_lists = self._parallel_map(self._fetch_raw_listeners, lb_arns)
        batch = _LBBatch(listeners_by_lb=dict(zip(lb_arns, listeners_lists, strict=True)))
        if include_certificates:
            cert_arns = {
                cert["CertificateArn"]
                for listener in batch.all_listeners()
                for cert in listener.get("Certificates", [])
                if cert.get("CertificateArn")
            }
            surroundings.certificates = self._start(
                lambda: self._fetch_certificates_map(list(cert_arns))
            )
        listener_arns = [lst["ListenerArn"] for lst in batch.all_listeners()]
        rules_lists = self._parallel_map(self._fetch_raw_rules, listener_arns)
        batch.rules_by_listener = dict(zip(listener_arns, rules_lists, strict=True))
        raw_tgs = raw_tgs_started.result()
        self._add_target_groups_of_rules(raw_tgs, batch)

        # 3. 全ターゲットグループのヘルス情報とEC2/ECS/Lambda/ENI/Subnet/SG情報を一括解決
        hydrated_tgs = self._hydrate_target_groups(
            list(raw_tgs.by_arn.values()),
            _visited_lb_arns=visited,
            _shared_raw_tgs=raw_tgs.pool or None,
            include_network=include_network,
            include_dns=include_dns,
            include_certificates=include_certificates,
            include_attributes=include_attributes,
            include_target_network=include_network,
            resolve_targets=resolve_targets,
        )
        batch.tg_views = {tg.arn: tg for tg in hydrated_tgs}

        # 4. サブネット・セキュリティグループ・ACM証明書・Route53レコード・ELB属性
        surroundings.fill(batch)

        # 5. LoadBalancerView ツリーを構築
        result = [
            self._build_lb_view(
                lb, raw_tgs.by_lb.get(lb["LoadBalancerArn"], []), batch, include_network
            )
            for lb in raw_lbs
        ]

        # 6. 同じ一括で組み立てた下流 ELB を、追加の API 呼び出しなしで
        #    TargetView.downstream_lb に紐付ける
        _link_downstream_lbs(result, batch.tg_views.values())
        return result

    def _build_unexpanded_lbs(
        self, raw_lbs: list[dict[str, Any]], include_network: bool
    ) -> list[LoadBalancerView]:
        """
        配下を取得せずに `LoadBalancerView` を作る（ターゲットの行に下流の ELB を出すだけのとき）。

        :param raw_lbs: 生ELB辞書
        :param include_network: サブネットと SG を付けるか
        :return: リスナーと TG が空の `LoadBalancerView` のリスト
        """
        batch = _LBBatch(listeners_by_lb={})
        self._start_lb_surroundings(
            raw_lbs, include_network, include_dns=False, include_attributes=False
        ).fill(batch)
        return [self._build_lb_view(lb, [], batch, include_network) for lb in raw_lbs]

    def _hydrate_for_route(
        self, lb: LoadBalancerView, req: SimulatedRequest, port: int, visited: frozenset[str]
    ) -> LoadBalancerView:
        """
        `route` の 1 段の評価に使うものだけを取得して、`lb` に入れた `LoadBalancerView` を返す。

        取得するのは、リスナーの一覧、`port` のリスナーのルール、`req` が一致するルールの転送先の
        TG。ほかのリスナーのルールと、リクエストの届かない TG は取得しない（TG の多い ELB で、
        すべての TG のヘルスとターゲットの実体を調べないため）。下流の ELB は本体だけにして、
        次の段を評価するときにまたこのメソッドで取得する。

        :param lb: 対象の ELB（本体だけでよい）
        :param req: 評価するリクエスト
        :param port: リクエストが届くリスナーのポート
        :param visited: ここまでにたどった ELB の ARN（`lb` を含む。下流として付けない）
        :return: リスナーと、届く TG だけを入れた `LoadBalancerView`
        """
        batch = _LBBatch(listeners_by_lb={lb.arn: self._fetch_raw_listeners(lb.arn)})
        on_port = next(
            (raw_l for raw_l in batch.all_listeners() if raw_l.get("Port") == port), None
        )
        if on_port is not None:
            batch.rules_by_listener[on_port["ListenerArn"]] = self._fetch_raw_rules(
                on_port["ListenerArn"]
            )
            # 評価に TG は要らないので、先に一致するルールを決めて、その転送先だけを取得する
            _, matched = evaluate_listener(self._build_listener_view(on_port, batch), req)
            tg_arns = [ref.target_group_arn for ref in matched.target_group_refs] if matched else []
            hydrated_tgs = self._hydrate_target_groups(
                self._fetch_raw_target_groups(tg_arns=tg_arns),
                _visited_lb_arns=set(visited),
                include_network=False,
                include_attributes=False,
                include_target_network=False,
                expand_downstream=False,
            )
            batch.tg_views = {tg.arn: tg for tg in hydrated_tgs}

        listeners = [self._build_listener_view(raw_l, batch) for raw_l in batch.all_listeners()]
        listeners.sort(key=lambda x: x.port)
        return dataclasses.replace(
            lb, listeners=listeners, target_groups=list(batch.tg_views.values())
        )

    def _collect_raw_target_groups(
        self, lb_arns: list[str], shared_raw_tgs: dict[str, dict[str, Any]] | None
    ) -> _RawTargetGroups:
        """
        ELB ごとの生 TG を集める。

        複数ELBを対象とする場合はアカウント内の全TGを1回だけ一括取得して共有プールを作り、
        ELBごとの describe_target_groups(LoadBalancerArn=...) 呼び出しを削減する。

        :param lb_arns: 対象の ELB の ARN
        :param shared_raw_tgs: 呼び出し元から引き継いだ共有プール（なければ None）
        :return: 集めた TG
        """
        raw_tgs = _RawTargetGroups(
            by_lb={arn: [] for arn in lb_arns},
            pool=dict(shared_raw_tgs) if shared_raw_tgs is not None else {},
        )
        if len(lb_arns) > 1 and not raw_tgs.pool:
            for tg in self._fetch_raw_target_groups():
                raw_tgs.pool[tg["TargetGroupArn"]] = tg

        if raw_tgs.pool:
            lb_arns_set = set(lb_arns)
            for tg in raw_tgs.pool.values():
                for attached_lb_arn in tg.get("LoadBalancerArns", []):
                    if attached_lb_arn in lb_arns_set:
                        raw_tgs.add(attached_lb_arn, tg)
        else:
            for tg in self._fetch_raw_target_groups(lb_arn=lb_arns[0]):
                raw_tgs.add(lb_arns[0], tg)
        return raw_tgs

    def _add_target_groups_of_rules(self, raw_tgs: _RawTargetGroups, batch: _LBBatch) -> None:
        """
        ルールの ForwardConfig 内でのみ参照されているターゲットグループを `raw_tgs` に補う
        （共有プールになければ、まとめて取得する）。

        :param raw_tgs: 補う先の TG
        :param batch: リスナーとルールを取得済みの一括の材料
        """
        missing_tg_arns: dict[str, list[str]] = {}
        for lb_arn, listeners in batch.listeners_by_lb.items():
            # 「どの ELB の TG か」は ELB ごとに見る（ほかの ELB で足した TG も、この ELB に足す）
            own_tg_arns = {tg["TargetGroupArn"] for tg in raw_tgs.by_lb[lb_arn]}
            for listener in listeners:
                for rule in batch.rules_by_listener.get(listener["ListenerArn"], []):
                    for action in rule.get("Actions", []):
                        for tg_arn in self._extract_tg_arns_from_action(action):
                            if tg_arn in own_tg_arns:
                                continue
                            own_tg_arns.add(tg_arn)
                            known = raw_tgs.by_arn.get(tg_arn) or raw_tgs.pool.get(tg_arn)
                            if known is not None:
                                raw_tgs.add(lb_arn, known)
                            else:
                                missing_tg_arns.setdefault(tg_arn, []).append(lb_arn)

        if missing_tg_arns:
            with tolerate_aws_errors("log.what.target_groups"):
                for etg in self._fetch_raw_target_groups(tg_arns=list(missing_tg_arns)):
                    t_arn = etg["TargetGroupArn"]
                    raw_tgs.by_arn[t_arn] = etg
                    raw_tgs.pool[t_arn] = etg
                    for lb_arn in missing_tg_arns.get(t_arn, []):
                        raw_tgs.by_lb[lb_arn].append(etg)

    def _start_lb_surroundings(
        self,
        raw_lbs: list[dict[str, Any]],
        include_network: bool,
        include_dns: bool,
        include_attributes: bool,
    ) -> _LBSurroundings:
        """
        ELB の周りのリソースのうち、ELB だけで決まるもの（サブネット・SG・ELB 属性・Route 53
        レコード）を取り始める。証明書はリスナーを取得してから、呼び出し側が足す。

        :param raw_lbs: 対象の生ELB辞書
        :param include_network: サブネットと SG を取得するか
        :param include_dns: Route 53 の全ゾーンを走査するか
        :param include_attributes: ELB 属性を取得するか
        :return: 進行中の取得（`fill` で結果を受け取る）
        """
        surroundings = _LBSurroundings()
        if include_network:
            subnet_ids = {
                az["SubnetId"]
                for lb in raw_lbs
                for az in lb.get("AvailabilityZones", [])
                if az.get("SubnetId")
            }
            sg_ids = {sg_id for lb in raw_lbs for sg_id in lb.get("SecurityGroups", [])}
            surroundings.subnets = self._start(
                lambda: self._fetch_subnets_map(subnet_ids=list(subnet_ids))
            )
            surroundings.security_groups = self._start(
                lambda: self._fetch_security_groups_map(list(sg_ids))
            )
        if include_attributes:
            lb_arns = [lb["LoadBalancerArn"] for lb in raw_lbs]
            surroundings.attributes = self._start(
                lambda: dict(self._parallel_map(self._fetch_lb_attributes, lb_arns))
            )
        if include_dns:
            lb_dns_names = [lb.get("DNSName", "") for lb in raw_lbs if lb.get("DNSName")]
            surroundings.route53 = self._start(
                lambda: self._fetch_route53_records_by_dns(lb_dns_names)
            )
        return surroundings

    def _build_lb_view(
        self,
        lb: dict[str, Any],
        raw_tgs: list[dict[str, Any]],
        batch: _LBBatch,
        include_network: bool,
    ) -> LoadBalancerView:
        """
        1 つの ELB の `LoadBalancerView` を、一括で取得した材料から組み立てる。

        :param lb: 生ELB辞書
        :param raw_tgs: この ELB の生 TG（重複を含んでよい）
        :param batch: 一括の材料
        :param include_network: サブネットと SG を付けるか
        :return: `LoadBalancerView`
        """
        lb_arn = lb["LoadBalancerArn"]
        lb_view = self._build_basic_lb_view(lb)
        lb_view.route53_records = list(batch.route53.get(lb_view.dns_name, []))
        lb_view.attributes = batch.attributes.get(lb_arn, {})
        if include_network:
            lb_view.subnets = [
                _lb_subnet(az, batch.subnets.get(az.get("SubnetId", ""), {}))
                for az in lb.get("AvailabilityZones", [])
            ]
            lb_view.security_groups = _security_groups(
                batch.security_groups, lb.get("SecurityGroups", [])
            )

        tg_arns = dict.fromkeys(raw_tg["TargetGroupArn"] for raw_tg in raw_tgs)
        lb_view.target_groups = [batch.tg_views[arn] for arn in tg_arns if arn in batch.tg_views]

        listener_views = [
            self._build_listener_view(raw_l, batch)
            for raw_l in batch.listeners_by_lb.get(lb_arn, [])
        ]
        listener_views.sort(key=lambda x: x.port)
        lb_view.listeners = listener_views
        return lb_view

    def _build_listener_view(self, raw_l: dict[str, Any], batch: _LBBatch) -> ListenerView:
        """
        生のリスナー辞書から、証明書とルールを付けた `ListenerView` を作る。

        :param raw_l: `describe_listeners` が返すリスナー辞書
        :param batch: 一括の材料（証明書・ルール・TG）
        :return: `ListenerView`
        """
        l_arn = raw_l["ListenerArn"]
        certs = []
        for c in raw_l.get("Certificates", []):
            c_arn = c.get("CertificateArn", "")
            base_cert = batch.certificates.get(c_arn, CertificateInfo(certificate_arn=c_arn))
            certs.append(
                dataclasses.replace(
                    base_cert, certificate_arn=c_arn, is_default=bool(c.get("IsDefault", False))
                )
            )
        return ListenerView(
            arn=l_arn,
            port=raw_l.get("Port", 0),
            protocol=raw_l.get("Protocol", ""),
            ssl_policy=raw_l.get("SslPolicy", ""),
            certificates=certs,
            rules=[
                self._build_rule_view(raw_r, batch.tg_views)
                for raw_r in batch.rules_by_listener.get(l_arn, [])
            ],
        )

    def _hydrate_target_groups(
        self,
        raw_tgs: list[dict[str, Any]],
        _visited_lb_arns: set[str] | None = None,
        _shared_raw_tgs: dict[str, dict[str, Any]] | None = None,
        include_network: bool = True,
        include_dns: bool = True,
        include_certificates: bool = True,
        include_attributes: bool = True,
        include_target_network: bool = True,
        resolve_targets: bool = True,
        expand_downstream: bool = True,
    ) -> list[TargetGroupView]:
        """
        生のターゲットグループリストからヘルス状態および
        実体リソース（EC2 / ECS / Lambda / 下流ELB / ENI / Subnet / SG）を並列・一括解決する。
        `include_network` / `include_dns` / `include_certificates` / `include_attributes` は
        下流ELBを再帰取得する際にそのまま引き継ぐ。

        :param raw_tgs: `describe_target_groups` が返す生TG辞書のリスト
        :param _visited_lb_arns: 下流ELBの再帰取得時に循環参照を防ぐための訪問済みELB ARN集合
        :param _shared_raw_tgs: 複数ELB取得時に共有するターゲットグループ生データのプール
        :param include_network: 下流ELB再帰取得時にサブネット/SGを含めるか
        :param include_dns: 下流ELB再帰取得時にRoute 53レコードを含めるか
        :param include_certificates: 下流ELB再帰取得時に ACM 証明書の詳細を含めるか
        :param include_attributes: TG の属性を取得するか（下流ELBの属性も）
        :param include_target_network: このターゲットたちのサブネット/SGを取得するか
        :param resolve_targets: ターゲットの実体を調べるか。False ならヘルス状態だけにして、
            実体・サブネット・SG・下流ELBは取得しない
        :param expand_downstream: 下流ELBの配下（リスナー・ルール・TG・ターゲット）まで取得するか。
            False なら下流ELBは本体だけ（`include_network` ならサブネットと SG も）
        :return: 構築された `TargetGroupView` のリスト
        """
        if not raw_tgs:
            return []

        # 1. 全ターゲットグループのヘルス情報と属性を並列取得（属性は 2. も待たずに進める）
        tg_arns = [tg["TargetGroupArn"] for tg in raw_tgs]
        tg_attrs_started = (
            self._start(lambda: dict(self._parallel_map(self._fetch_tg_attributes, tg_arns)))
            if include_attributes
            else None
        )
        health_by_tg: dict[str, list[dict[str, Any]]] = dict(
            self._parallel_map(self._fetch_single_tg_health, tg_arns)
        )

        # 2. ターゲットの実体を調べる（調べないなら、対応表が空のまま組み立てる）
        lookups = (
            self._resolve_target_resources(
                raw_tgs,
                health_by_tg,
                _visited_lb_arns,
                _shared_raw_tgs,
                include_network,
                include_dns,
                include_certificates,
                include_attributes,
                include_target_network,
                expand_downstream,
            )
            if resolve_targets
            else _TargetLookups({}, {}, {}, ECSMaps(), {})
        )

        # 3. TargetGroupView と TargetView の構築
        tg_attrs_map = tg_attrs_started.result() if tg_attrs_started is not None else {}
        tg_views: list[TargetGroupView] = []
        for tg in raw_tgs:
            tg_arn = tg["TargetGroupArn"]
            targets = [
                self._build_target_view(desc, tg, lookups) for desc in health_by_tg.get(tg_arn, [])
            ]
            targets.sort(key=lambda t: (t.display_name, t.target_id))
            tg_views.append(
                self._build_target_group_view(tg, targets, tg_attrs_map.get(tg_arn, {}))
            )
        return tg_views

    def _resolve_target_resources(
        self,
        raw_tgs: list[dict[str, Any]],
        health_by_tg: dict[str, list[dict[str, Any]]],
        _visited_lb_arns: set[str] | None,
        _shared_raw_tgs: dict[str, dict[str, Any]] | None,
        include_network: bool,
        include_dns: bool,
        include_certificates: bool,
        include_attributes: bool,
        include_target_network: bool,
        expand_downstream: bool,
    ) -> _TargetLookups:
        """
        ターゲットの実体（EC2 / ECS / Lambda / ENI / Subnet / SG / 下流ELB）を一括で取得する。
        引数の意味は `_hydrate_target_groups` と同じ。

        :param raw_tgs: `describe_target_groups` が返す生TG辞書のリスト
        :param health_by_tg: TG ARN -> TargetHealthDescriptions
        :return: 取得したリソースの対応表
        """
        # 1. 各ターゲットを種別ごとに振り分け、後続のAPIをバッチ実行できるように集約する
        ids = _classify_targets(raw_tgs, health_by_tg)

        # 2. Lambda 関数は、ほかの実体と関係なく決まるので、先に取り始める
        lambda_arns = list(ids.lambda_arns)
        lambdas = self._start(lambda: self._fetch_lambda_functions_map(lambda_arns))

        # 3. IPターゲットの ENI（EC2 と、ELB のノードなら下流の ELB が決まる）
        eni_by_ip = self._resolve_enis(ids)

        # 4. ここから先は互いを待たずに進められる: 下流 ELB の再帰取得・ECS・EC2
        #    下流 ELB は次のどちらか
        #    - NLB の alb タイプTGに登録された ALB
        #    - ip タイプTGに登録された、ELB のノードの IP（ENI の説明から特定した ELB）
        visited_lbs = set(_visited_lb_arns) if _visited_lb_arns else set()
        unvisited_lb_arns = [arn for arn in ids.downstream_lb_arns if arn not in visited_lbs]
        downstream = self._start(
            lambda: self._hydrate_downstream_lbs(
                unvisited_lb_arns,
                visited_lbs,
                _shared_raw_tgs,
                include_network,
                include_dns,
                include_certificates,
                include_attributes,
                expand_downstream,
            )
        )
        ecs_ips = _ip_targets_outside_load_balancers(ids, eni_by_ip)
        ecs_instance_ids = set(ids.instance_ids)
        ecs_tg_arns = _target_groups_outside_load_balancers(ids, raw_tgs, health_by_tg, eni_by_ip)
        ecs = self._start(
            lambda: self._fetch_ecs_maps(
                relevant_ips=ecs_ips,
                relevant_instance_ids=ecs_instance_ids,
                relevant_tg_arns=ecs_tg_arns,
            )
        )
        ec2_by_id, ec2_by_ip = self._resolve_ec2(ids, eni_by_ip)

        lookups = _TargetLookups(
            ec2_by_id=ec2_by_id,
            ec2_by_ip=ec2_by_ip,
            eni_by_ip=eni_by_ip,
            ecs_maps=ecs.result(),
            lambda_map=lambdas.result(),
        )

        # 5. ターゲットに紐づくサブネット・セキュリティグループ（出さないときは取得しない）
        if include_target_network:
            self._resolve_target_networks(lookups, ids)

        lookups.alb_view_map = downstream.result()
        return lookups

    def _hydrate_downstream_lbs(
        self,
        lb_arns: list[str],
        visited_lbs: set[str],
        _shared_raw_tgs: dict[str, dict[str, Any]] | None,
        include_network: bool,
        include_dns: bool,
        include_certificates: bool,
        include_attributes: bool,
        expand_downstream: bool,
    ) -> dict[str, LoadBalancerView]:
        """
        下流の ELB を取得する。引数の意味は `_hydrate_target_groups` と同じ。

        :param lb_arns: 下流の ELB の ARN（まだたどっていないもの）
        :param visited_lbs: ここまでにたどった ELB の ARN
        :return: ELB の ARN -> `LoadBalancerView`（取得できなければ空）
        """
        if not lb_arns:
            return {}
        with tolerate_aws_errors("log.what.downstream_lbs"):
            raw_downstream_lbs = self._fetch_raw_load_balancers(lb_arns=lb_arns)
            hydrated_downs = (
                self._hydrate_load_balancers(
                    raw_downstream_lbs,
                    _visited_lb_arns=visited_lbs,
                    include_network=include_network,
                    _shared_raw_tgs=_shared_raw_tgs,
                    include_dns=include_dns,
                    include_certificates=include_certificates,
                    include_attributes=include_attributes,
                )
                if expand_downstream
                else self._build_unexpanded_lbs(raw_downstream_lbs, include_network)
            )
            return {d_lb.arn: d_lb for d_lb in hydrated_downs}
        return {}

    def _resolve_enis(self, ids: _TargetIds) -> dict[tuple[str, str], ENIInfo]:
        """
        IP ターゲットの ENI を重複なく逆引きする（TG の VPC のものを選ぶ）。
        EC2 の ENI ならその EC2 を `ids.instance_ids` に、ELB のノードの ENI ならその ELB を
        `ids.downstream_lb_arns` に足す（ALB -> NLB の IP 登録など）。

        :param ids: 振り分けたターゲット（ENI の EC2 と ELB を足す）
        :return: ENI（キーは `_ip_target_key`）
        """
        eni_candidates = self._fetch_eni_map_by_ips(list(ids.ip_targets))
        eni_by_ip: dict[tuple[str, str], ENIInfo] = {}
        for key in ids.ip_target_keys:
            picked_eni = pick_in_vpc(eni_candidates, key[1], key[0])
            if picked_eni is not None:
                eni_by_ip[key] = picked_eni
        for eni in eni_by_ip.values():
            if eni.instance_id:
                ids.instance_ids.add(eni.instance_id)

        self._resolve_elb_enis(eni_by_ip.values())
        for eni in eni_by_ip.values():
            if eni.load_balancer_arn:
                ids.downstream_lb_arns.add(eni.load_balancer_arn)
        return eni_by_ip

    def _resolve_ec2(
        self, ids: _TargetIds, eni_by_ip: dict[tuple[str, str], ENIInfo]
    ) -> tuple[dict[str, EC2InstanceInfo], dict[tuple[str, str], EC2InstanceInfo]]:
        """
        EC2 をまとめて取得し、IP ターゲットの EC2 を ENI から決める。

        :param ids: 振り分けたターゲット（`_resolve_enis` のあと）
        :param eni_by_ip: IP ターゲットの ENI
        :return: `(EC2（キーはインスタンス ID）, IP ターゲットの EC2（キーは _ip_target_key）)`
        """
        ec2_by_id = self._fetch_ec2_instances_map(list(ids.instance_ids))
        ec2_by_ip: dict[tuple[str, str], EC2InstanceInfo] = {
            key: ec2_by_id[eni.instance_id]
            for key, eni in eni_by_ip.items()
            if eni.instance_id and eni.instance_id in ec2_by_id
        }

        # ENI が見つからなかった IP ターゲットだけを、EC2 の IP で照会し直す（ENI を読む権限が
        # ないときのため）。ENI が見つかった IP は、EC2 のものならその ENI に EC2 の ID が入って
        # いるので、照会し直しても結果は変わらない（Fargate のタスクの IP で毎回呼ばない）
        unresolved = [key for key in ids.ip_target_keys if key not in eni_by_ip]
        if unresolved:
            ec2_candidates = self._fetch_ec2_instances_by_ips(
                list(dict.fromkeys(ip for _vpc, ip in unresolved))
            )
            for key in unresolved:
                picked_ec2 = pick_in_vpc(ec2_candidates, key[1], key[0])
                if picked_ec2 is not None:
                    ec2_by_ip[key] = ec2_by_id.setdefault(picked_ec2.instance_id, picked_ec2)
        return ec2_by_id, ec2_by_ip

    def _resolve_target_networks(self, lookups: _TargetLookups, ids: _TargetIds) -> None:
        """
        ターゲットの実体（EC2・ENI・ECS・Lambda）のサブネットと SG をまとめて取得し、
        `lookups` と各リソースのモデルに付ける。

        IP ターゲットがある場合は所属VPCの全サブネットも取得し、CIDR 照合でサブネット/AZを特定できる
        ようにする。

        :param lookups: 実体を取得済みの対応表（サブネットと SG を入れる）
        :param ids: 振り分けたターゲット
        """
        target_subnet_ids: set[str] = set()
        target_sg_ids: set[str] = set()
        single_subnet: list[EC2InstanceInfo | ENIInfo] = [
            *lookups.ec2_by_id.values(),
            *lookups.eni_by_ip.values(),
        ]
        for item in single_subnet:
            if item.subnet_id:
                target_subnet_ids.add(item.subnet_id)
            target_sg_ids.update(item.security_group_ids)
        multi_subnet: list[ECSTaskInfo | LambdaFunctionInfo] = [
            *lookups.ecs_maps.all_infos(),
            *lookups.lambda_map.values(),
        ]
        for multi_item in multi_subnet:
            target_subnet_ids.update(multi_item.subnet_ids)
            target_sg_ids.update(multi_item.security_group_ids)

        sg_started = self._start(lambda: self._fetch_security_groups_map(list(target_sg_ids)))
        subnet_raw_map = self._fetch_subnets_map(
            subnet_ids=list(target_subnet_ids),
            vpc_ids=list(ids.vpc_ids) if ids.ip_targets else None,
        )
        lookups.subnet_info_map = {
            s_id: SubnetInfo(
                subnet_id=s_id,
                availability_zone=s_data.get("availability_zone", ""),
                name=s_data.get("name", ""),
                cidr_block=s_data.get("cidr_block", ""),
                ipv6_cidr_blocks=list(s_data.get("ipv6_cidr_blocks", [])),
            )
            for s_id, s_data in subnet_raw_map.items()
        }
        lookups.subnet_vpcs = {s_id: s.get("vpc_id", "") for s_id, s in subnet_raw_map.items()}
        lookups.sg_info_map = sg_started.result()

        # 各リソースモデルへ SubnetInfo / SecurityGroupInfo を付与
        subnets = lookups.subnet_info_map
        for inst in lookups.ec2_by_id.values():
            inst.subnet = subnets.get(inst.subnet_id)
            inst.security_groups = lookups.security_groups(inst.security_group_ids)
        for eni in lookups.eni_by_ip.values():
            eni.subnet = subnets.get(eni.subnet_id)
            eni.security_groups = lookups.security_groups(eni.security_group_ids)
        for fn_item in lookups.lambda_map.values():
            fn_item.subnets = [subnets[s] for s in fn_item.subnet_ids if s in subnets]
            fn_item.security_groups = lookups.security_groups(fn_item.security_group_ids)

    @staticmethod
    def _build_target_group_view(
        tg: dict[str, Any], targets: list[TargetView], attributes: dict[str, str]
    ) -> TargetGroupView:
        """
        生のTG辞書と構築済みのターゲット一覧から `TargetGroupView` を作る。

        :param tg: `describe_target_groups` が返すTG辞書
        :param targets: このTGの `TargetView` のリスト
        :param attributes: TG属性（`describe_target_group_attributes` の Key/Value）
        :return: `TargetGroupView`
        """
        matcher = tg.get("Matcher", {})
        return TargetGroupView(
            arn=tg["TargetGroupArn"],
            name=tg.get("TargetGroupName", ""),
            protocol=tg.get("Protocol", ""),  # Lambda TG にはプロトコルがない
            port=tg.get("Port"),
            target_type=tg.get("TargetType", "instance"),
            vpc_id=tg.get("VpcId", ""),
            health_check_protocol=tg.get("HealthCheckProtocol", ""),
            health_check_port=str(tg.get("HealthCheckPort", "")),
            health_check_path=tg.get("HealthCheckPath", ""),
            health_check_interval_seconds=tg.get("HealthCheckIntervalSeconds", 30),
            health_check_timeout_seconds=tg.get("HealthCheckTimeoutSeconds", 5),
            healthy_threshold_count=tg.get("HealthyThresholdCount", 5),
            unhealthy_threshold_count=tg.get("UnhealthyThresholdCount", 2),
            matcher=matcher.get("HttpCode") or matcher.get("GrpcCode") or "",
            ip_address_type=tg.get("IpAddressType", "ipv4"),
            load_balancer_arns=tg.get("LoadBalancerArns", []),
            targets=targets,
            attributes=attributes,
        )

    @staticmethod
    def _build_target_view(
        desc: dict[str, Any], tg: dict[str, Any], lookups: _TargetLookups
    ) -> TargetView:
        """
        `describe_target_health` の1件から、実体リソース（EC2 / ECS / Lambda / 下流ELB / ENI）を
        対応付けた `TargetView` を作る。

        :param desc: TargetHealthDescriptions の要素
        :param tg: 親の生TG辞書
        :param lookups: 一括取得済みのリソースの対応表
        :return: `TargetView`
        """
        t_obj = desc.get("Target", {})
        th_obj = desc.get("TargetHealth", {})
        t_id = t_obj.get("Id", "")
        t_port = int(t_obj.get("Port", tg.get("Port") or 0) or 0)
        # VPC外のIPターゲット等で返る "all" は個別AZではないため空に正規化する
        t_az = t_obj.get("AvailabilityZone", "")
        if t_az == "all":
            t_az = ""

        ip_key = _ip_target_key(tg, desc)
        eni_info = lookups.eni_by_ip.get(ip_key)
        ec2_info = lookups.ec2_by_id.get(t_id) or lookups.ec2_by_ip.get(ip_key)
        if not ec2_info and eni_info and eni_info.instance_id:
            ec2_info = lookups.ec2_by_id.get(eni_info.instance_id)
        matched_ip_subnet = _match_subnet_for_ip(
            t_id, lookups.subnet_info_map, lookups.subnet_vpcs, ip_key[0]
        )

        ecs_info = _resolve_ecs_for_target(
            t_id, t_port, tg, lookups, ec2_info, eni_info, matched_ip_subnet
        )

        # ENI単体やIP単体の場合のサブネット補完
        if eni_info and not eni_info.subnet and matched_ip_subnet:
            eni_info.subnet = matched_ip_subnet
            if not eni_info.subnet_id:
                eni_info.subnet_id = matched_ip_subnet.subnet_id

        alb_info = lookups.alb_view_map.get(t_id)
        if alb_info is None and eni_info and eni_info.load_balancer_arn:
            alb_info = lookups.alb_view_map.get(eni_info.load_balancer_arn)

        # TargetHealth に AZ が入っていない場合は紐づいた実体リソースから補完する
        if not t_az:
            t_az = next(
                (
                    az
                    for az in (
                        ec2_info.availability_zone if ec2_info else "",
                        ecs_info.availability_zone if ecs_info else "",
                        eni_info.availability_zone if eni_info else "",
                        matched_ip_subnet.availability_zone if matched_ip_subnet else "",
                    )
                    if az
                ),
                "",
            )

        return TargetView(
            target_id=t_id,
            port=t_port,
            health_state=th_obj.get("State", "unknown"),
            health_reason=th_obj.get("Reason", ""),
            health_description=th_obj.get("Description", ""),
            availability_zone=t_az,
            ec2=ec2_info,
            ecs=ecs_info,
            lambda_function=lookups.lambda_map.get(t_id),
            downstream_lb=alb_info,
            eni=eni_info,
            ip_subnet=matched_ip_subnet,
        )

    def _build_basic_lb_view(self, lb: dict[str, Any]) -> LoadBalancerView:
        """
        生のELB辞書から基本属性のみの `LoadBalancerView` を生成する。

        :param lb: `describe_load_balancers` が返すLoadBalancer辞書
        :return: 基本フィールドのみ設定された `LoadBalancerView`
        """
        return LoadBalancerView(
            arn=lb["LoadBalancerArn"],
            name=lb.get("LoadBalancerName", ""),
            dns_name=lb.get("DNSName", ""),
            type=lb.get("Type", "application"),
            scheme=lb.get("Scheme", ""),
            state=lb.get("State", {}).get("Code", "unknown"),
            vpc_id=lb.get("VpcId", ""),
            ip_address_type=lb.get("IpAddressType", "ipv4"),
            created_time=_format_datetime(lb.get("CreatedTime")),
        )

    def _build_rule_view(
        self,
        raw_rule: dict[str, Any],
        tg_view_map: dict[str, TargetGroupView],
    ) -> RuleView:
        """
        生のRule辞書から人間が読みやすい条件・アクション・TG参照付きの `RuleView` を生成する。

        :param raw_rule: `describe_rules` が返すRule辞書
        :param tg_view_map: `{tg_arn: TargetGroupView}` のマップ
        :return: 構築された `RuleView`
        """
        conditions = [_condition_text(cond) for cond in raw_rule.get("Conditions", [])]
        actions_summary: list[str] = []
        tg_refs: list[RuleTargetGroupRef] = []
        for act in sorted(raw_rule.get("Actions", []), key=lambda a: a.get("Order", 1)):
            summary, refs = _summarize_action(act, tg_view_map)
            if summary:
                actions_summary.append(summary)
            tg_refs += refs

        return RuleView(
            arn=raw_rule.get("RuleArn", ""),
            priority=str(raw_rule.get("Priority", "default")),
            is_default=bool(raw_rule.get("IsDefault", False)),
            conditions=conditions,
            actions_summary=actions_summary,
            target_group_refs=tg_refs,
            condition_specs=list(raw_rule.get("Conditions", [])),
        )

    @staticmethod
    def _extract_tg_arns_from_action(action: dict[str, Any]) -> list[str]:
        """
        アクション辞書から参照されているすべてのターゲットグループARNを抽出する。

        :param action: リスナールールのAction辞書
        :return: ターゲットグループARNのリスト
        """
        arns: list[str] = []
        if action.get("TargetGroupArn"):
            arns.append(action["TargetGroupArn"])
        arns += [
            ftg["TargetGroupArn"]
            for ftg in action.get("ForwardConfig", {}).get("TargetGroups", [])
            if ftg.get("TargetGroupArn")
        ]
        return arns
