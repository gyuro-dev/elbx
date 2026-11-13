"""
elbx: a read-only CLI and Python library for AWS ALB / NLB (ELBv2).

- Fetches a load balancer with everything behind it (listeners, rules, target groups,
  targets) and the related resources (Route 53, EC2, ECS, Lambda, ENI, security groups,
  subnets, ACM) in one call, resolving each target to the real resource
- Looks targets up the other way round (which load balancer / target group is this
  EC2, IP, ECS service or Lambda in?)
- Simulates which listener rule a request matches and where it ends up
- Calls only Describe* / List* / Get* APIs; nothing in the account is changed

elbx: AWS の ALB / NLB（ELBv2）を調べる参照専用の CLI と Python ライブラリ。

- ELB の配下（リスナー・ルール・ターゲットグループ・ターゲット）と関連リソース
  （Route 53・EC2・ECS・Lambda・ENI・SG・サブネット・ACM）を1回で取得し、
  ターゲットの実体を特定する
- EC2・IP・ECS サービス・Lambda から、どの ELB / TG に登録されているかを逆引きする
- リクエストがどのリスナールールに一致し、どこへ届くかをシミュレーションする
- 呼び出すのは Describe* / List* / Get* の API だけで、アカウントの中は変更しない
"""

from __future__ import annotations

import logging

from .client import ELBClient
from .errors import AWSAPIError, ELBError, ResourceNotFoundError
from .formatter import (
    format_lb_map,
    format_lb_summary_table,
    format_lb_tree,
    format_lookup_results_table,
    format_route_simulation,
    format_targets_table,
    print_lb_tree,
)
from .messages import get_language, set_language
from .models import (
    CertificateInfo,
    EC2InstanceInfo,
    ECSTaskInfo,
    ENIInfo,
    LambdaFunctionInfo,
    ListenerView,
    LoadBalancerView,
    Route53RecordInfo,
    RouteHop,
    RouteNoteCode,
    RouteSimulationResult,
    RuleEvaluation,
    RuleTargetGroupRef,
    RuleView,
    SecurityGroupInfo,
    SecurityGroupRuleSummary,
    SubnetInfo,
    TargetGroupView,
    TargetKind,
    TargetLookupResult,
    TargetView,
)

__version__ = "0.1.1"

# ログの出し方は利用する側が決める（何も設定しなければ何も出さない）
logging.getLogger("elbx").addHandler(logging.NullHandler())

__all__ = [
    "AWSAPIError",
    "CertificateInfo",
    "EC2InstanceInfo",
    "ECSTaskInfo",
    "ELBClient",
    "ELBError",
    "ENIInfo",
    "LambdaFunctionInfo",
    "ListenerView",
    "LoadBalancerView",
    "ResourceNotFoundError",
    "Route53RecordInfo",
    "RouteHop",
    "RouteNoteCode",
    "RouteSimulationResult",
    "RuleEvaluation",
    "RuleTargetGroupRef",
    "RuleView",
    "SecurityGroupInfo",
    "SecurityGroupRuleSummary",
    "SubnetInfo",
    "TargetGroupView",
    "TargetKind",
    "TargetLookupResult",
    "TargetView",
    "__version__",
    "format_lb_map",
    "format_lb_summary_table",
    "format_lb_tree",
    "format_lookup_results_table",
    "format_route_simulation",
    "format_targets_table",
    "get_language",
    "print_lb_tree",
    "set_language",
]
