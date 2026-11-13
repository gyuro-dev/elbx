"""
Data model: the load balancer tree (LoadBalancerView -> ListenerView -> RuleView ->
TargetGroupView -> TargetView) and the related resources (Route 53, EC2, ECS, Lambda,
downstream load balancers, ENI, subnets, security groups, ACM certificates).
Every class is a dataclass with `to_dict()`; `LoadBalancerView` also has `to_json()`.

データモデル: ELB のツリー（LoadBalancerView -> ListenerView -> RuleView -> TargetGroupView ->
TargetView）と関連リソース（Route 53・EC2・ECS・Lambda・下流の ELB・ENI・サブネット・SG・
ACM 証明書）。すべて dataclass で `to_dict()` を持ち、`LoadBalancerView` は `to_json()` も持つ。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ._ids import canonical_ip, is_arn_of
from .messages import t

# `TargetView.target_kind` の値 / what `TargetView.target_kind` can be
TargetKind = Literal["alb", "nlb", "lambda", "ecs", "ec2", "eni", "ip", "instance"]
# `RouteHop.note_code` の値（note があるとき） / what `RouteHop.note_code` can be with a note
RouteNoteCode = Literal["no_listener", "no_matching_rule", "too_many_hops"]

# `RuleView.actions_summary` の転送アクションの書き出し（hydrator の `_summarize_action` が作る）
_FORWARD_PREFIX = "Forward -> "

# json.dumps(ensure_ascii=False) がエスケープしない制御文字（DEL・C1）と、表示の向きを変える文字
_JSON_RAW_CONTROLS = re.compile(r"[\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _dumps_json(data: object, indent: int = 2) -> str:
    """
    日本語はそのまま、制御文字はすべて `\\uXXXX` にした JSON の文字列を返す。
    U+009B（CSI）などは、そのまま端末に届くと制御シーケンスとして解釈されることがあるため。

    :param data: JSON にできる値
    :param indent: インデントの幅
    :return: JSON の文字列
    """
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    return _JSON_RAW_CONTROLS.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


@dataclass
class Route53RecordInfo:
    """A Route 53 record that points at the load balancer (A / AAAA alias or CNAME).

    ELB の DNS 名を向いている Route 53 のレコード（A / AAAA の Alias または CNAME）。
    """

    zone_id: str
    zone_name: str
    record_name: str
    record_type: str  # A, AAAA, CNAME
    is_alias: bool = True
    private_zone: bool = False
    ttl: int | None = None
    evaluate_target_health: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the fields as a dict (JSON-serializable). / フィールドを辞書にして返す。"""
        return asdict(self)


@dataclass
class SubnetInfo:
    """A subnet (and its availability zone) of a load balancer or a target.

    `cidr_block` is the IPv4 CIDR (the IPv6 CIDR on an IPv6-only subnet) and
    `ipv6_cidr_blocks` the IPv6 CIDRs; `cidr_blocks` has both.

    ELB またはターゲットのサブネット（と AZ）。`cidr_block` は IPv4 の CIDR（IPv6 専用サブネットでは
    IPv6 の CIDR）、`ipv6_cidr_blocks` は IPv6 の CIDR。`cidr_blocks` は両方をまとめたもの。
    """

    subnet_id: str
    availability_zone: str
    name: str = ""
    cidr_block: str = ""
    # デュアルスタック / IPv6 専用サブネットの IPv6 CIDR
    ipv6_cidr_blocks: list[str] = field(default_factory=list)
    load_balancer_addresses: list[str] = field(default_factory=list)

    @property
    def cidr_blocks(self) -> list[str]:
        """
        The IPv4 and IPv6 CIDRs of the subnet, without duplicates.

        IPv4 と IPv6 の CIDR をまとめたもの（IPv6 専用サブネットでは重複を除く）。
        """
        return list(dict.fromkeys(c for c in (self.cidr_block, *self.ipv6_cidr_blocks) if c))

    def to_dict(self) -> dict[str, Any]:
        """Return the fields as a dict (JSON-serializable). / フィールドを辞書にして返す。"""
        return asdict(self)


@dataclass
class SecurityGroupRuleSummary:
    """One security group rule: `protocol` (`TCP` / `UDP` / `ICMP` / `ALL`), `port_range` (`443`,
    `80-8080` or `ALL`; for ICMP the type, or type/code such as `3/4`) and `source_or_dest` (CIDR,
    security group ID or prefix list ID).

    SG のルール1件。`protocol`（`TCP` / `UDP` / `ICMP` / `ALL`）、`port_range`（`443`、`80-8080`、
    `ALL`。ICMP はタイプ、または `3/4` のようなタイプ/コード）、`source_or_dest`（CIDR、SG の ID、
    プレフィックスリストの ID）。
    """

    protocol: str
    port_range: str
    source_or_dest: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the fields as a dict (JSON-serializable). / フィールドを辞書にして返す。"""
        return asdict(self)


@dataclass
class SecurityGroupInfo:
    """A security group with its inbound and outbound rules.

    SG とそのインバウンド・アウトバウンドのルール。
    """

    group_id: str
    group_name: str = ""
    description: str = ""
    inbound_rules: list[SecurityGroupRuleSummary] = field(default_factory=list)
    outbound_rules: list[SecurityGroupRuleSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the fields as a dict (JSON-serializable). / フィールドを辞書にして返す。"""
        return asdict(self)


@dataclass
class CertificateInfo:
    """An ACM certificate of an HTTPS / TLS listener (`is_default` marks the listener's
    default certificate; `not_after` is the expiry date in ISO 8601).

    HTTPS / TLS リスナーの ACM 証明書（`is_default` はリスナーの既定の証明書、`not_after` は
    ISO 8601 の有効期限）。
    """

    certificate_arn: str
    domain_name: str = ""
    subject_alternative_names: list[str] = field(default_factory=list)
    status: str = ""
    not_after: str | None = None
    in_use: bool = True
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the fields as a dict (JSON-serializable). / フィールドを辞書にして返す。"""
        return asdict(self)


@dataclass
class EC2InstanceInfo:
    """The EC2 instance behind a target (registered by instance ID or by IP).

    ターゲットの実体の EC2 インスタンス（インスタンス ID または IP で登録されたもの）。
    """

    instance_id: str
    name: str = ""
    state: str = ""
    private_ip: str = ""
    public_ip: str = ""
    availability_zone: str = ""
    instance_type: str = ""
    subnet_id: str = ""
    security_group_ids: list[str] = field(default_factory=list)
    subnet: SubnetInfo | None = None
    security_groups: list[SecurityGroupInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the fields as a dict (JSON-serializable). / フィールドを辞書にして返す。"""
        return asdict(self)


@dataclass
class ECSTaskInfo:
    """The ECS task (or service) behind a target.

    `task_id`, `task_arn` and `private_ip` are empty when only the service could be
    identified (e.g. several tasks on one host that cannot be told apart by port).

    ターゲットの実体の ECS タスク（またはサービス）。サービスまでしか特定できなかったとき
    （1台のホストに複数のタスクがあり、ポートで区別できない場合など）は `task_id`・`task_arn`・
    `private_ip` が空。
    """

    cluster_name: str
    cluster_arn: str = ""
    service_name: str = ""
    task_arn: str = ""
    task_id: str = ""
    task_definition: str = ""
    launch_type: str = ""  # FARGATE, EC2, EXTERNAL
    last_status: str = ""
    private_ip: str = ""
    container_name: str = ""
    availability_zone: str = ""
    subnet_ids: list[str] = field(default_factory=list)
    security_group_ids: list[str] = field(default_factory=list)
    subnets: list[SubnetInfo] = field(default_factory=list)
    security_groups: list[SecurityGroupInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the fields as a dict (JSON-serializable). / フィールドを辞書にして返す。"""
        return asdict(self)


@dataclass
class LambdaFunctionInfo:
    """The Lambda function behind a target of a `lambda` target group (with its VPC
    configuration when it has one).

    `lambda` タイプの TG のターゲットの実体の Lambda 関数（VPC の設定があればそれも）。
    """

    function_name: str
    function_arn: str
    runtime: str = ""
    handler: str = ""
    state: str = ""
    last_modified: str = ""
    description: str = ""
    vpc_id: str = ""
    subnet_ids: list[str] = field(default_factory=list)
    security_group_ids: list[str] = field(default_factory=list)
    subnets: list[SubnetInfo] = field(default_factory=list)
    security_groups: list[SecurityGroupInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the fields as a dict (JSON-serializable). / フィールドを辞書にして返す。"""
        return asdict(self)


@dataclass
class ENIInfo:
    """The network interface (ENI) that owns the IP of an `ip` target.

    `instance_id` is set when the ENI belongs to an EC2 instance; `load_balancer_name` /
    `load_balancer_arn` when it is a node of a load balancer (its description is
    `ELB app/<name>/<id>` or `ELB net/<name>/<id>`).

    `ip` ターゲットの IP を持つ ENI。EC2 のものなら `instance_id`、ELB のノードのもの（説明が
    `ELB app/<名前>/<ID>` / `ELB net/<名前>/<ID>`）なら `load_balancer_name` / `load_balancer_arn`
    が入る。
    """

    network_interface_id: str
    private_ip: str
    subnet_id: str = ""
    availability_zone: str = ""
    interface_type: str = ""
    description: str = ""
    instance_id: str = ""
    security_group_ids: list[str] = field(default_factory=list)
    subnet: SubnetInfo | None = None
    security_groups: list[SecurityGroupInfo] = field(default_factory=list)
    # ELB のノードの ENI（説明が "ELB net/<名前>/<ID>" / "ELB app/<名前>/<ID>"）の場合、その ELB
    load_balancer_name: str = ""
    load_balancer_arn: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the fields as a dict (JSON-serializable). / フィールドを辞書にして返す。"""
        return asdict(self)


@dataclass(repr=False)
class TargetView:
    """
    One target of a target group: its health and the real resource behind it.

    Exactly one of `ec2`, `ecs`, `lambda_function`, `downstream_lb` and `eni` is usually set
    (an ECS task on EC2 has both `ecs` and `ec2`; `downstream_lb` is the load balancer behind
    the target, an ALB or an NLB). The properties `target_kind`, `display_name`,
    `ip_address`, `subnets` and `security_groups` give the same information whatever the
    kind of target, so callers do not have to branch on it.

    ターゲットグループの中の1つのターゲット。ヘルス状態と、その実体のリソースを持つ。
    `ec2`・`ecs`・`lambda_function`・`downstream_lb`・`eni` は通常どれか1つが入る（EC2 上の ECS
    タスクは `ecs` と `ec2` の両方。`downstream_lb` はターゲットの先にある ELB で、ALB も NLB も
    ある）。`target_kind`・`display_name`・`ip_address`・`subnets`・
    `security_groups` のプロパティは、種別によらず同じ形で情報を返す。
    """

    target_id: str
    port: int
    health_state: str  # healthy, unhealthy, initial, draining, unused, unavailable
    health_reason: str = ""
    health_description: str = ""
    availability_zone: str = ""
    ec2: EC2InstanceInfo | None = None
    ecs: ECSTaskInfo | None = None
    lambda_function: LambdaFunctionInfo | None = None
    downstream_lb: LoadBalancerView | None = None
    eni: ENIInfo | None = None
    # IP ターゲットの IP をサブネットの CIDR と照合して特定したサブネット（ENI 等がないときに使う）
    ip_subnet: SubnetInfo | None = None

    def __repr__(self) -> str:
        """A short repr: the nested resources and a downstream load balancer's tree are left out."""
        return (
            f"TargetView(target_id={self.target_id!r}, port={self.port!r}, "
            f"health_state={self.health_state!r}, target_kind={self.target_kind!r})"
        )

    @property
    def target_kind(self) -> TargetKind:
        """
        The kind of resource behind the target: `"alb"`, `"nlb"`, `"lambda"`, `"ecs"`,
        `"ec2"`, `"eni"`, or `"ip"` / `"instance"` when nothing could be resolved
        (the `TargetKind` literal, so a `match` on it can be checked for exhaustiveness).

        ターゲットの実体の種別。`"alb"`・`"nlb"`・`"lambda"`・`"ecs"`・`"ec2"`・`"eni"`、
        実体を特定できなかったときは `"ip"` または `"instance"`。
        """
        # 1. 下流ELB（alb タイプの TG、または ENI 説明から特定した IP 登録の下流 NLB/ALB）
        if self.downstream_lb is not None:
            return "nlb" if self.downstream_lb.type == "network" else "alb"
        if is_arn_of(self.target_id, "elasticloadbalancing"):
            return "nlb" if ":loadbalancer/net/" in self.target_id else "alb"
        # 2. Lambda 関数
        if self.lambda_function is not None or is_arn_of(self.target_id, "lambda"):
            return "lambda"
        # 3. ECS タスク（EC2 起動タイプは ec2 と ecs の両方を持つため ecs を優先）
        if self.ecs is not None:
            return "ecs"
        # 4. EC2 インスタンス（instance 登録、または IP 登録から逆引きできた EC2）
        if self.ec2 is not None:
            return "ec2"
        # 5. VPC エンドポイント等の ENI
        if self.eni is not None:
            return "eni"
        # 6. 実体が特定できなかった IP アドレス（IPv4 / IPv6）とインスタンス ID
        if canonical_ip(self.target_id) is not None:
            return "ip"
        return "instance"

    @property
    def display_name(self) -> str:
        """
        A readable name of the target, e.g. `web-01 (i-0123...)`, `ECS:cluster/service/task`,
        `Lambda:name`, `NLB:name`, or the target ID when nothing could be resolved.

        人が読みやすいターゲットの名前（例: `web-01 (i-0123...)`、`ECS:cluster/service/task`、
        `Lambda:name`、`NLB:name`。実体を特定できなければターゲット ID）。
        """
        if self.downstream_lb:
            prefix = "NLB" if self.downstream_lb.type == "network" else "ALB"
            return f"{prefix}:{self.downstream_lb.name}"
        if self.lambda_function:
            return f"Lambda:{self.lambda_function.function_name}"
        if self.ecs:
            svc = f"{self.ecs.service_name}/" if self.ecs.service_name else ""
            # サービス単位でしか特定できなかった場合は task_id が空のため target_id を使う
            tid = self.ecs.task_id or self.target_id
            return f"ECS:{self.ecs.cluster_name}/{svc}{tid}"
        if self.ec2 and self.ec2.name:
            return f"{self.ec2.name} ({self.target_id})"
        if self.eni and self.eni.description:
            return f"{self.target_id} ({self.eni.network_interface_id})"
        return self.target_id

    @property
    def ip_address(self) -> str:
        """
        The private IP (IPv4 or IPv6) of the target: the target ID itself for `ip` targets,
        otherwise the ECS task IP, the EC2 private IP or the ENI IP; `""` when unknown.

        ターゲットのプライベート IP（IPv4 / IPv6）。`ip` ターゲットはターゲット ID そのもの、
        それ以外は ECS タスクの IP → EC2 のプライベート IP → ENI の IP の順。不明なら `""`。
        """
        if canonical_ip(self.target_id) is not None:
            return self.target_id
        if self.ecs and self.ecs.private_ip:
            return self.ecs.private_ip
        if self.ec2 and self.ec2.private_ip:
            return self.ec2.private_ip
        if self.eni and self.eni.private_ip:
            return self.eni.private_ip
        return ""

    @property
    def subnets(self) -> list[SubnetInfo]:
        """
        The subnets of the target, whatever its kind (EC2 / ENI: one subnet; ECS / Lambda /
        downstream load balancer: possibly several; an IP alone: the subnet whose CIDR
        contains it). Empty when unknown.

        ターゲットのサブネット（種別を問わない。EC2 / ENI は1つ、ECS / Lambda / 下流の ELB は複数の
        こともある。IP だけのターゲットは CIDR にその IP を含むサブネット）。不明なら空。
        """
        # 単一サブネットの EC2 / ENI を先に確認し、ECS / Lambda / ALB に続く
        if self.ec2 and self.ec2.subnet:
            return [self.ec2.subnet]
        if self.eni and self.eni.subnet:
            return [self.eni.subnet]
        if self.ecs and self.ecs.subnets:
            return list(self.ecs.subnets)
        if self.lambda_function and self.lambda_function.subnets:
            return list(self.lambda_function.subnets)
        if self.downstream_lb and self.downstream_lb.subnets:
            return list(self.downstream_lb.subnets)
        if self.ip_subnet:
            return [self.ip_subnet]
        return []

    @property
    def subnet(self) -> SubnetInfo | None:
        """The first of `subnets`, or None. / `subnets` の先頭（なければ None）。"""
        subs = self.subnets
        return subs[0] if subs else None

    @property
    def security_groups(self) -> list[SecurityGroupInfo]:
        """
        The security groups of the target, whatever its kind. Empty when unknown.

        ターゲットのセキュリティグループ（種別を問わない）。不明なら空。
        """
        if self.ec2 and self.ec2.security_groups:
            return list(self.ec2.security_groups)
        if self.ecs and self.ecs.security_groups:
            return list(self.ecs.security_groups)
        if self.eni and self.eni.security_groups:
            return list(self.eni.security_groups)
        if self.lambda_function and self.lambda_function.security_groups:
            return list(self.lambda_function.security_groups)
        if self.downstream_lb and self.downstream_lb.security_groups:
            return list(self.downstream_lb.security_groups)
        return []

    def to_dict(self) -> dict[str, Any]:
        """
        Return the target as a dict, including the computed properties (`target_kind`,
        `display_name`, `ip_address`, `subnets`, `security_groups`) and the nested resources.

        ターゲットを辞書にして返す（計算したプロパティと、入れ子のリソースを含む）。
        """
        return {
            "target_id": self.target_id,
            "port": self.port,
            "target_kind": self.target_kind,
            "display_name": self.display_name,
            "ip_address": self.ip_address,
            "health_state": self.health_state,
            "health_reason": self.health_reason,
            "health_description": self.health_description,
            "availability_zone": self.availability_zone,
            "subnet": self.subnet.to_dict() if self.subnet else None,
            "subnets": [s.to_dict() for s in self.subnets],
            "security_groups": [sg.to_dict() for sg in self.security_groups],
            "ec2": self.ec2.to_dict() if self.ec2 else None,
            "ecs": self.ecs.to_dict() if self.ecs else None,
            "lambda_function": (self.lambda_function.to_dict() if self.lambda_function else None),
            "downstream_lb": self.downstream_lb.to_dict() if self.downstream_lb else None,
            "eni": self.eni.to_dict() if self.eni else None,
        }


@dataclass(repr=False)
class TargetGroupView:
    """
    A target group: its settings (protocol, port, target type, health check, attributes)
    and its targets. `health_summary` and the `*_count` properties aggregate the targets'
    health.

    ターゲットグループ。設定（プロトコル・ポート・ターゲットの種別・ヘルスチェック・属性）と
    ターゲットの一覧を持ち、`health_summary` と `*_count` でヘルス状態を集計する。
    """

    arn: str
    name: str
    protocol: str
    port: int | None
    target_type: str  # instance, ip, lambda, alb
    vpc_id: str = ""
    health_check_protocol: str = ""
    health_check_port: str = ""
    health_check_path: str = ""
    health_check_interval_seconds: int = 30
    health_check_timeout_seconds: int = 5
    healthy_threshold_count: int = 5
    unhealthy_threshold_count: int = 2
    matcher: str = ""
    ip_address_type: str = "ipv4"  # ipv4, ipv6（ip / instance タイプの TG）
    load_balancer_arns: list[str] = field(default_factory=list)
    targets: list[TargetView] = field(default_factory=list)
    # describe_target_group_attributes の Key/Value（deregistration_delay.timeout_seconds 等）
    attributes: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        """A short repr: the targets are counted, not listed."""
        return (
            f"TargetGroupView(name={self.name!r}, target_type={self.target_type!r}, "
            f"targets={len(self.targets)})"
        )

    @property
    def total_count(self) -> int:
        """Number of targets. / ターゲットの数。"""
        return len(self.targets)

    @property
    def healthy_count(self) -> int:
        """Number of `healthy` targets. / `healthy` のターゲットの数。"""
        return sum(1 for t in self.targets if t.health_state == "healthy")

    @property
    def unhealthy_count(self) -> int:
        """Number of `unhealthy` targets. / `unhealthy` のターゲットの数。"""
        return sum(1 for t in self.targets if t.health_state == "unhealthy")

    @property
    def draining_count(self) -> int:
        """Number of `draining` targets. / `draining` のターゲットの数。"""
        return sum(1 for t in self.targets if t.health_state == "draining")

    def needs_attention(self, target: TargetView) -> bool:
        """
        Whether the target is in a state worth looking into: anything but `healthy`, except
        the states the configuration itself causes. Those are `unavailable` because health
        checks are disabled (`Target.HealthCheckDisabled`, the default for a Lambda target
        group) and `unused` in a target group that no load balancer uses. A stopped instance
        (`unused`) in a target group in use still needs attention. `elbx targets --unhealthy`
        shows the targets for which this is true.

        ターゲットが調べるべき状態かどうか。`healthy` 以外が対象だが、設定どおりに出る状態は
        除く。除くのは、ヘルスチェックが無効なための `unavailable`（`Target.HealthCheckDisabled`。
        Lambda の TG の既定）と、どの ELB にも使われていない TG の `unused`。使われている TG の
        停止したインスタンス（`unused`）は対象のまま。`elbx targets --unhealthy` はこれが真の
        ターゲットを表示する。
        """
        state = target.health_state
        if state == "healthy":
            return False
        if state == "unavailable" and target.health_reason == "Target.HealthCheckDisabled":
            return False
        return not (state == "unused" and not self.load_balancer_arns)

    @property
    def attention_count(self) -> int:
        """
        Number of targets for which `needs_attention` is true.

        `needs_attention` が真のターゲットの数。
        """
        return sum(1 for t in self.targets if self.needs_attention(t))

    @property
    def health_summary(self) -> str:
        """
        One-line health summary such as `2/3 healthy, 1 unhealthy`. States other than
        `healthy` are counted when present, in the order `unhealthy`, `initial`, `draining`,
        `unused`, `unavailable`, then any other state.

        ヘルス状態の1行の集計（例: `2/3 healthy, 1 unhealthy`）。`healthy` 以外の状態は、
        あるものだけを `unhealthy`・`initial`・`draining`・`unused`・`unavailable`・その他の順に
        数える。
        """
        counts: dict[str, int] = {}
        for target in self.targets:
            if target.health_state != "healthy":
                counts[target.health_state] = counts.get(target.health_state, 0) + 1
        order = ["unhealthy", "initial", "draining", "unused", "unavailable"]
        states = [s for s in order if s in counts] + [s for s in counts if s not in order]
        parts = [f"{self.healthy_count}/{self.total_count} healthy"]
        parts += [f"{counts[s]} {s}" for s in states]
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """
        Return the target group as a dict: settings, `targets` and a `summary` of their health.

        ターゲットグループを辞書にして返す（設定・`targets`・ヘルス状態の `summary`）。
        """
        return {
            "arn": self.arn,
            "name": self.name,
            "protocol": self.protocol,
            "port": self.port,
            "target_type": self.target_type,
            "vpc_id": self.vpc_id,
            "health_check_protocol": self.health_check_protocol,
            "health_check_port": self.health_check_port,
            "health_check_path": self.health_check_path,
            "health_check_interval_seconds": self.health_check_interval_seconds,
            "health_check_timeout_seconds": self.health_check_timeout_seconds,
            "healthy_threshold_count": self.healthy_threshold_count,
            "unhealthy_threshold_count": self.unhealthy_threshold_count,
            "matcher": self.matcher,
            "ip_address_type": self.ip_address_type,
            "load_balancer_arns": list(self.load_balancer_arns),
            "attributes": dict(self.attributes),
            "targets": [t.to_dict() for t in self.targets],
            "summary": {
                "total": self.total_count,
                "healthy": self.healthy_count,
                "unhealthy": self.unhealthy_count,
                "draining": self.draining_count,
                "attention": self.attention_count,
                "by_state": dict(Counter(t.health_state for t in self.targets)),
            },
        }


@dataclass
class RuleTargetGroupRef:
    """A target group a rule forwards to, with its weight (None unless the rule forwards to
    several target groups) and the `TargetGroupView` when it was fetched.

    ルールの転送先の TG。重み（複数の TG に転送するルール以外は None）と、取得できていれば
    `TargetGroupView` を持つ。
    """

    target_group_arn: str
    target_group_name: str = ""
    weight: int | None = None
    target_group: TargetGroupView | None = None

    def to_dict(self) -> dict[str, Any]:
        """
        Return the reference as a dict (with the nested target group).

        転送先の参照を辞書にして返す（TG も入れ子で含む）。
        """
        return {
            "target_group_arn": self.target_group_arn,
            "target_group_name": self.target_group_name,
            "weight": self.weight,
            "target_group": self.target_group.to_dict() if self.target_group else None,
        }


@dataclass
class RuleView:
    """
    A listener rule: priority (`"default"` for the default rule), conditions, actions and
    the target groups it forwards to. `conditions` / `actions_summary` are readable texts;
    `condition_specs` are the raw `Conditions` of `describe_rules`, used by `route`.

    リスナールール。優先度（default ルールは `"default"`）・条件・アクション・転送先の TG を持つ。
    `conditions` / `actions_summary` は表示用の文字列、`condition_specs` は `describe_rules` の
    `Conditions` そのもの（`route` の評価に使う）。
    """

    arn: str
    priority: str  # '1', '2', ..., 'default'
    is_default: bool
    conditions: list[str] = field(default_factory=list)
    actions_summary: list[str] = field(default_factory=list)
    target_group_refs: list[RuleTargetGroupRef] = field(default_factory=list)
    # describe_rules の Conditions そのもの（route シミュレーションの評価に使用）
    condition_specs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def non_forward_actions(self) -> list[str]:
        """
        The actions of `actions_summary` other than the forward: an authentication that runs
        before the forward, a redirect, a fixed response. The forward itself is in
        `target_group_refs`.

        `actions_summary` のうち、転送以外のアクション（転送の前に行う認証、リダイレクト、固定
        レスポンス）。転送は `target_group_refs` にある。
        """
        return [a for a in self.actions_summary if not a.startswith(_FORWARD_PREFIX)]

    @property
    def priority_order(self) -> tuple[int, int]:
        """
        Sort key in evaluation order: numeric priorities ascending, the default rule last.

        評価順のソートキー（優先度の小さい順。default ルールは最後）。
        """
        # 数値ルールは (0, 優先度)、default ルールは (1, 0) にすることで常に末尾に並べる
        if self.is_default or not self.priority.isdigit():
            return (1, 0)
        return (0, int(self.priority))

    @property
    def condition_text(self) -> str:
        """
        The conditions as one line, e.g. `Host in [api.example.com] AND Path in [/v1/*]`
        (a fixed text for the default rule).

        条件を1行にしたもの（例: `Host in [api.example.com] AND Path in [/v1/*]`。default ルールは
        固定の文）。
        """
        if self.is_default or not self.conditions:
            return t("model.default_rule")
        return " AND ".join(self.conditions)

    def to_dict(self) -> dict[str, Any]:
        """
        Return the rule as a dict (with the nested target group references).

        ルールを辞書にして返す（転送先の参照も入れ子で含む）。
        """
        return {
            "arn": self.arn,
            "priority": self.priority,
            "is_default": self.is_default,
            "conditions": self.conditions,
            "actions_summary": self.actions_summary,
            "target_group_refs": [ref.to_dict() for ref in self.target_group_refs],
            "condition_specs": list(self.condition_specs),
        }


@dataclass
class ListenerView:
    """
    A listener: port, protocol, SSL policy, ACM certificates and rules.

    リスナー。ポート・プロトコル・SSL ポリシー・ACM 証明書・ルールを持つ。
    """

    arn: str
    port: int
    protocol: str
    ssl_policy: str = ""
    certificates: list[CertificateInfo] = field(default_factory=list)
    rules: list[RuleView] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """
        Return the listener as a dict (with the nested certificates and rules).

        リスナーを辞書にして返す（証明書とルールも入れ子で含む）。
        """
        return {
            "arn": self.arn,
            "port": self.port,
            "protocol": self.protocol,
            "ssl_policy": self.ssl_policy,
            "certificates": [c.to_dict() for c in self.certificates],
            "rules": [r.to_dict() for r in self.rules],
        }


@dataclass(repr=False)
class LoadBalancerView:
    """
    A load balancer (ALB / NLB) with everything behind it: Route 53 records, subnets,
    security groups, attributes, listeners (-> rules -> target groups -> targets) and
    `target_groups` (every target group attached to the load balancer).

    ロードバランサー（ALB / NLB）とその配下すべて。Route 53 のレコード・サブネット・SG・属性・
    リスナー（→ ルール → TG → ターゲット）と、`target_groups`（この ELB に紐づく全 TG）を持つ。
    """

    arn: str
    name: str
    dns_name: str
    type: str  # application, network, gateway
    scheme: str  # internet-facing, internal
    state: str  # active, provisioning, active_impaired, failed
    vpc_id: str
    ip_address_type: str = "ipv4"
    created_time: str | None = None
    route53_records: list[Route53RecordInfo] = field(default_factory=list)
    subnets: list[SubnetInfo] = field(default_factory=list)
    security_groups: list[SecurityGroupInfo] = field(default_factory=list)
    listeners: list[ListenerView] = field(default_factory=list)
    target_groups: list[TargetGroupView] = field(default_factory=list)
    # describe_load_balancer_attributes の Key/Value（idle_timeout.timeout_seconds 等）
    attributes: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        """A short repr: the listeners and target groups are counted, not listed."""
        return (
            f"LoadBalancerView(name={self.name!r}, type={self.type!r}, "
            f"listeners={len(self.listeners)}, target_groups={len(self.target_groups)})"
        )

    @property
    def all_targets(self) -> list[TargetView]:
        """
        Every target of every target group of the load balancer, as one flat list.

        この ELB の全 TG のターゲットを1つのリストにしたもの。
        """
        result: list[TargetView] = []
        for tg in self.target_groups:
            result.extend(tg.targets)
        return result

    def to_dict(self) -> dict[str, Any]:
        """
        Return the load balancer and everything behind it as a dict (JSON-serializable).

        ロードバランサーと配下すべてを辞書にして返す（JSON にできる）。
        """
        return {
            "arn": self.arn,
            "name": self.name,
            "dns_name": self.dns_name,
            "type": self.type,
            "scheme": self.scheme,
            "state": self.state,
            "vpc_id": self.vpc_id,
            "ip_address_type": self.ip_address_type,
            "created_time": self.created_time,
            "route53_records": [r.to_dict() for r in self.route53_records],
            "subnets": [s.to_dict() for s in self.subnets],
            "security_groups": [sg.to_dict() for sg in self.security_groups],
            "attributes": dict(self.attributes),
            "listeners": [listener.to_dict() for listener in self.listeners],
            "target_groups": [tg.to_dict() for tg in self.target_groups],
        }

    def to_json(self, indent: int = 2) -> str:
        """
        Return `to_dict()` as a JSON string (non-ASCII text is kept as it is; control characters
        are escaped).

        `to_dict()` を JSON の文字列にして返す（日本語などはそのまま、制御文字はエスケープする）。

        :param indent: indentation width / インデントの幅
        """
        return _dumps_json(self.to_dict(), indent=indent)


@dataclass
class TargetLookupResult:
    """
    One match of `ELBClient.find_target`: the load balancer (empty name / ARN when the
    target group is attached to none), the target group and the target.

    `ELBClient.find_target` の一致1件。ELB（TG がどの ELB にも紐づいていなければ名前と ARN は空）、
    TG、ターゲットを持つ。
    """

    load_balancer_name: str
    load_balancer_arn: str
    load_balancer_dns: str
    target_group_name: str
    target_group_arn: str
    target: TargetView

    def to_dict(self) -> dict[str, Any]:
        """
        Return the match as a dict (with the nested target).

        一致を辞書にして返す（ターゲットも入れ子で含む）。
        """
        return {
            "load_balancer_name": self.load_balancer_name,
            "load_balancer_arn": self.load_balancer_arn,
            "load_balancer_dns": self.load_balancer_dns,
            "target_group_name": self.target_group_name,
            "target_group_arn": self.target_group_arn,
            "target": self.target.to_dict(),
        }


@dataclass
class RuleEvaluation:
    """The evaluation of one rule in a route simulation: whether it matched and why
    (every condition when it matched, otherwise the failing conditions).

    route シミュレーションでの1つのルールの評価。一致したかと、その理由（一致したときは全条件、
    不一致のときは不一致だった条件）。
    """

    rule: RuleView
    matched: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """
        Return the evaluation as a dict (priority, condition text, matched, reasons).

        評価を辞書にして返す（優先度・条件の文・一致したか・理由）。
        """
        return {
            "priority": self.rule.priority,
            "condition_text": self.rule.condition_text,
            "matched": self.matched,
            "reasons": list(self.reasons),
        }


@dataclass(repr=False)
class RouteHop:
    """
    The evaluation at one load balancer of a route simulation: the listener for the port,
    the rules evaluated, the matched rule, a `note` when something stopped the evaluation
    (no listener, too many hops) with its `note_code` (`"no_listener"`, `"no_matching_rule"`,
    `"too_many_hops"`; `""` when there is no note), and `downstream` for the load balancers it
    forwards to.

    In a result of `ELBClient.simulate_request`, `load_balancer` has only what the evaluation
    fetched: every listener, the rules of the listener on `port`, and the target groups of
    the matched rule.

    route シミュレーションでの1台の ELB の評価。ポートのリスナー、評価したルール、一致した
    ルール、評価を止めた理由の `note`（リスナーがない、段数が多すぎる）とその種類の `note_code`
    （`"no_listener"`・`"no_matching_rule"`・`"too_many_hops"`。note がなければ `""`）、
    転送先の ELB の `downstream` を持つ。

    `ELBClient.simulate_request` の結果では、`load_balancer` には評価のために取得した分だけが
    入る（リスナーの一覧、`port` のリスナーのルール、一致したルールの転送先の TG）。
    """

    load_balancer: LoadBalancerView
    port: int
    listener: ListenerView | None = None
    evaluations: list[RuleEvaluation] = field(default_factory=list)
    matched_rule: RuleView | None = None
    note: str = ""
    # note の種類（言語によらない短いコード。note が空なら空）
    note_code: RouteNoteCode | Literal[""] = ""
    downstream: list[RouteHop] = field(default_factory=list)

    def __repr__(self) -> str:
        """A short repr: the load balancer by name, the downstream hops counted."""
        rule = self.matched_rule.priority if self.matched_rule else None
        return (
            f"RouteHop(load_balancer={self.load_balancer.name!r}, port={self.port!r}, "
            f"matched_rule={rule!r}, note_code={self.note_code!r}, "
            f"downstream={len(self.downstream)})"
        )

    def to_dict(self) -> dict[str, Any]:
        """
        Return the hop as a dict with only what matters for the routing decision (not the
        whole load balancer tree): the evaluations, the matched rule's actions and target
        groups with their targets' health, and the downstream hops.

        判定に関わる情報だけを辞書にして返す（ELB のツリー全体は含めない）。評価、一致したルールの
        アクションと転送先 TG（ターゲットのヘルス状態つき）、下流のホップ。
        """
        rule = self.matched_rule
        return {
            "load_balancer_name": self.load_balancer.name,
            "load_balancer_arn": self.load_balancer.arn,
            "load_balancer_type": self.load_balancer.type,
            "port": self.port,
            "listener": (
                f"{self.listener.protocol}:{self.listener.port}" if self.listener else None
            ),
            "evaluations": [e.to_dict() for e in self.evaluations],
            "matched_rule_priority": rule.priority if rule else None,
            "actions_summary": list(rule.actions_summary) if rule else [],
            "target_groups": [
                {
                    "name": ref.target_group_name,
                    "arn": ref.target_group_arn,
                    "weight": ref.weight,
                    "health_summary": (
                        ref.target_group.health_summary if ref.target_group else None
                    ),
                    "targets": (
                        [
                            {
                                "display_name": t.display_name,
                                "target_kind": t.target_kind,
                                "port": t.port,
                                "health_state": t.health_state,
                                "health_reason": t.health_reason,
                            }
                            for t in ref.target_group.targets
                        ]
                        if ref.target_group
                        else []
                    ),
                }
                for ref in (rule.target_group_refs if rule else [])
            ],
            "note": self.note,
            "note_code": self.note_code,
            "downstream": [d.to_dict() for d in self.downstream],
        }


@dataclass
class RouteSimulationResult:
    """The result of `ELBClient.simulate_request`: the request as parsed and one `RouteHop`
    per entry load balancer (several when more than one load balancer serves the host).

    `ELBClient.simulate_request` の結果。解釈したリクエストと、入口の ELB ごとの `RouteHop`
    （ホスト名を向いている ELB が複数あれば複数）。
    """

    url: str
    method: str
    host: str
    port: int
    path: str
    entries: list[RouteHop] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """
        Return the result as a dict (the request and the hops).

        結果を辞書にして返す（リクエストとホップ）。
        """
        return {
            "url": self.url,
            "method": self.method,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "entries": [e.to_dict() for e in self.entries],
        }
