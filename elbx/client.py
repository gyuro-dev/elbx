"""
The public client for ALB / NLB (ELBv2): load balancer trees, target lookup and request
routing simulation. Read-only (only Describe* / List* / Get* APIs are called).
The low-level API calls live in fetchers.py, the tree building in hydrator.py and the
rule evaluation in simulator.py.

ELBv2 (ALB/NLB) の公開クライアント。階層構造の取得・逆引き検索・リクエストのルーティング先
シミュレーションを提供する参照専用（読み取り系 API のみを呼び出す）のクライアント。
低レベル API 取得は fetchers.py、ツリー構築は hydrator.py、ルール評価は simulator.py に分けている。
"""

from __future__ import annotations

import functools
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, NoRegionError

from ._ids import canonical_ip
from .errors import AWSAPIError, ELBError, ResourceNotFoundError
from .fetchers import (
    _NOT_FOUND_CODES,
    AWS_ERRORS,
    _decode_route53_name,
    _normalize_dns_name,
    error_code,
    logger,
    tolerate_aws_errors,
)
from .hydrator import ELBHydratorMixin
from .messages import t
from .models import (
    LoadBalancerView,
    RouteSimulationResult,
    TargetGroupView,
    TargetLookupResult,
    TargetView,
)
from .simulator import SimulatedRequest, simulate_hop, wildcard_match

# 以前どおり elbx.client からも import できるように残す
__all__ = ["AWSAPIError", "ELBClient", "ELBError", "ResourceNotFoundError"]


P = ParamSpec("P")
R = TypeVar("R")

# ELB 自身の DNS 名（ALB は `….<region>.elb.amazonaws.com`、NLB は `….elb.<region>.amazonaws.com`）
_ELB_DNS_NAME = re.compile(r"\.elb\.([a-z0-9-]+\.)?amazonaws\.com(\.cn)?\.?$")

# TG ごと・リスナーごとの呼び出しを並列に行うので、スロットリングは botocore の既定
# （legacy・最大 5 回）より粘り強くやり直す。1 回でも残ると、コマンド全体が失敗するため
_RETRY_MODE = "standard"
_MAX_ATTEMPTS = 10


# botocore の知らないリージョン名（打ち間違いが多い）のときの最大試行回数。接続できないときも
# スロットリングと同じ回数だけやり直すので、10 回のままだとエラーになるまで 30 秒以上かかる。
# 新しくできたリージョンを古い botocore で使うこともあるので、弾かずに回数だけ減らす
_MAX_ATTEMPTS_UNKNOWN_REGION = 2


def _retry_config(region_known: bool = True) -> Config:
    """
    AWS のクライアントに付けるリトライの設定を返す。
    利用者が環境変数（`AWS_RETRY_MODE` / `AWS_MAX_ATTEMPTS`）で決めた項目は、そのままにする。

    :param region_known: リージョン名を botocore が知っているか
    """
    retries: dict[str, Any] = {}
    if not os.environ.get("AWS_RETRY_MODE"):
        retries["mode"] = _RETRY_MODE
    if not os.environ.get("AWS_MAX_ATTEMPTS"):
        retries["total_max_attempts"] = (
            _MAX_ATTEMPTS if region_known else _MAX_ATTEMPTS_UNKNOWN_REGION
        )
    return Config(retries=retries) if retries else Config()


def _is_known_region(session: Any, region: str | None) -> bool:
    """
    リージョン名を botocore が知っているかを返す（分からないときは True）。

    :param session: boto3.Session
    :param region: リージョン名（決まっていなければ None）
    """
    if not region:
        return True
    try:
        return any(
            region in session.get_available_regions("elbv2", partition_name=partition)
            for partition in session.get_available_partitions()
        )
    except Exception:  # noqa: BLE001 - 目安なので、調べられなければ知っているものとして扱う
        return True


def _arn_name_part(arn: str) -> str:
    """
    ARN のうち、リソースの名前や ID にあたる部分を返す（`find` の部分一致に使う）。
    ARN 全体と照合すると、リージョン名・アカウント ID・`app`・`task` のような、どの ARN にも
    入っている文字列で無関係なターゲットに当たるため。

    :param arn: ARN（小文字でもよい）
    :return: ELB は `<名前>/<ID>`、Lambda は `<関数名>[:<エイリアス>]`。ARN の形でなければそのまま
    """
    parts = arn.split(":", 5)
    if len(parts) < 6 or parts[0] != "arn":
        return arn
    resource = parts[5]
    for kind in ("loadbalancer/app/", "loadbalancer/net/", "loadbalancer/gwy/", "function:"):
        if resource.startswith(kind):
            return resource[len(kind) :]
    return resource


def _arn_contains(arn: str, q_lower: str) -> bool:
    """
    ARN が検索クエリ（小文字）に一致するかを返す。ARN を貼り付けたクエリ（`arn:` で始まる）は
    ARN 全体と、それ以外は名前の部分だけと照合する。
    """
    arn_lower = arn.lower()
    if q_lower.startswith("arn:"):
        return q_lower in arn_lower
    return q_lower in _arn_name_part(arn_lower)


def _log_api_call(http_response: Any, model: Any, context: dict[str, Any]) -> None:
    """
    呼び出した API と所要時間を DEBUG で記録する。
    リクエストの引数やヘッダーは、認証情報やリソースの中身が混ざらないように記録しない。

    :param http_response: botocore の HTTP 応答
    :param model: 呼び出した API の定義
    :param context: before-call と共有する辞書
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    elapsed_ms = (time.perf_counter() - context["elbx_started_at"]) * 1000
    logger.debug(
        t(
            "log.api_call",
            service=model.service_model.service_name,
            operation=model.name,
            status=http_response.status_code,
            ms=round(elapsed_ms),
        )
    )


def _wrap_aws_errors(
    action: str, one_run: bool = True
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    公開メソッドの中で起きた botocore の例外を `AWSAPIError` に変換するデコレーター。
    利用者には botocore の型やトレースバックではなく、何をしようとして失敗したかを見せるため。
    メソッドの中は 1 回の実行（`_one_run`）として、同じものを 2 回取らないようにする。

    :param action: 失敗したときのメッセージに入れる操作の、messages.py のキー
        （言語は失敗したときに決めるので、ここではキーだけを持つ）
    :param one_run: False なら 1 回の実行として扱わない（クライアントを作る `__init__` 用）
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            client: Any = args[0]
            try:
                if not one_run:
                    return func(*args, **kwargs)
                with client._one_run():
                    return func(*args, **kwargs)
            except AWS_ERRORS as exc:
                raise AWSAPIError(t("client.aws_error", action=t(action), error=exc)) from exc

        return wrapper

    return decorator


class ELBClient(ELBHydratorMixin):
    """
    Read-only client for ALB / NLB.

    Fetches load balancers with their listeners, rules, target groups and targets, resolves
    every target to the real resource (EC2, ECS task, Lambda, ENI, downstream load balancer)
    together with Route 53 records, security groups, subnets and ACM certificates, looks
    targets up, and simulates which listener rule a request matches.

    ```python
    from elbx import ELBClient, format_lb_tree

    client = ELBClient(profile_name="prod", region_name="ap-northeast-1")
    lb = client.get_load_balancer("prod-web-alb")
    print(format_lb_tree(lb))
    for target in lb.all_targets:
        print(target.target_kind, target.display_name, target.health_state)
    ```

    ALB / NLB の参照専用クライアント。ELB をリスナー・ルール・ターゲットグループ・ターゲットごと
    取得し、各ターゲットの実体（EC2・ECS タスク・Lambda・ENI・下流の ELB）を Route 53・SG・
    サブネット・ACM 証明書とともに特定する。逆引き検索と、リクエストがどのリスナールールに
    一致するかのシミュレーションも行う。

    Every public method raises `AWSAPIError` (a subclass of `ELBError`) when an AWS API call
    fails. / 公開メソッドは、AWS API の呼び出しに失敗すると `AWSAPIError`（`ELBError` の
    サブクラス）を送出する。

    Details that cannot be fetched (e.g. no permission for the security groups) are left empty
    and recorded as warnings on the `elbx` logger of the standard `logging` module; every API
    call is recorded at DEBUG. / 取得できなかった詳細（SG の権限がないなど）は空のままにして、標準の
    `logging` の `elbx` ロガーに WARNING で記録する。API の呼び出しは DEBUG で記録する。
    """

    @_wrap_aws_errors("client.action.connect", one_run=False)
    def __init__(
        self,
        profile_name: str | None = None,
        region_name: str | None = None,
        session: Any | None = None,
        max_workers: int = 10,
    ) -> None:
        """
        Create the boto3 clients. Credentials are resolved like the AWS CLI does
        (environment, ~/.aws/config, SSO, ...).

        boto3 のクライアントを作る。認証情報は AWS CLI と同じ順で解決する（環境変数・~/.aws/config・
        SSO）。

        :param profile_name: AWS profile name (default profile when omitted)
            / AWS のプロファイル名（省略時は既定のプロファイル）
        :param region_name: AWS region (the profile's / environment's region when omitted)
            / リージョン（省略時はプロファイルや環境変数の設定）
        :param session: an existing boto3.Session to use instead (tests, shared sessions).
            `region_name` still applies to the clients made from it; `profile_name` cannot be
            combined with it
            / 代わりに使う boto3.Session（テストや既存セッションの共有用）。`region_name` は
            そのセッションから作るクライアントに効く。`profile_name` とは併用できない
        :param max_workers: maximum number of parallel API calls (default 10)
            / API を並列に呼ぶ最大数（既定 10）
        :raises ValueError: `session` and `profile_name` are both given
            / `session` と `profile_name` を両方指定した
        :raises AWSAPIError: no region is configured, or the clients cannot be created
            / リージョンが決まらない、またはクライアントを作れない
        """
        self.max_workers = max(1, max_workers)
        # 同時に呼ぶ AWS API の数の枠。互いに関係のない取得を並行して進めても、合わせて
        # max_workers を超えないようにする（スロットリングを増やさないため）
        self._api_slots = threading.BoundedSemaphore(self.max_workers)
        self._route53_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self.region_name = region_name or None
        if session is not None:
            if profile_name:
                raise ValueError(t("client.session_and_profile"))
            self.session = session
        else:
            session_kwargs: dict[str, Any] = {}
            if profile_name:
                session_kwargs["profile_name"] = profile_name
            if region_name:
                session_kwargs["region_name"] = region_name
            self.session = boto3.Session(**session_kwargs)
        # 空のリージョン（AWS_REGION= のように空で設定されている）は、botocore では NoRegionError
        # ではなく壊れたエンドポイントのエラーになるので、ここで同じ失敗にそろえる
        # （リージョンが None なら、botocore がクライアントを作るときに NoRegionError を出す）
        if not self.region_name and getattr(self.session, "region_name", None) == "":
            raise NoRegionError()
        self._region_known = _is_known_region(self.session, self._resolved_region())

        self.elbv2 = self._create_client("elbv2")
        self.ec2 = self._create_client("ec2")
        self.acm = self._create_client("acm")
        self.ecs = self._safe_create_client("ecs")
        self.lambda_client = self._safe_create_client("lambda")
        self.route53 = self._safe_create_client("route53")

    def _resolved_region(self) -> str | None:
        """クライアントが使うリージョン名を返す（引数で指定がなければセッションの設定）。"""
        return self.region_name or getattr(self.session, "region_name", None) or None

    def _has_target_group(self, name: str) -> bool:
        """
        その名前のターゲットグループがあるかを返す（エラーの案内のため。調べられなければ False）。

        :param name: ターゲットグループの名前
        """
        try:
            return bool(self._fetch_raw_target_groups(names=[name]))
        except AWS_ERRORS:
            return False

    def _has_load_balancer(self, name: str) -> bool:
        """
        その名前のロードバランサーがあるかを返す（エラーの案内のため。調べられなければ False）。

        :param name: ロードバランサーの名前
        """
        try:
            return bool(self._fetch_raw_load_balancers(names=[name]))
        except AWS_ERRORS:
            return False

    def _create_client(self, service_name: str) -> Any:
        """
        Boto3クライアントを作り、API の呼び出しを記録するようにする。

        :param service_name: Boto3サービス名
        :return: 生成されたBoto3クライアント
        """
        kwargs: dict[str, Any] = {"config": _retry_config(self._region_known)}
        if self.region_name:
            kwargs["region_name"] = self.region_name
        client = self.session.client(service_name, **kwargs)
        client.meta.events.register("before-call", self._on_api_call_start)
        client.meta.events.register("after-call", self._on_api_call_end)
        client.meta.events.register("after-call-error", self._on_api_call_error)
        return client

    def _on_api_call_start(self, context: dict[str, Any], **_: Any) -> None:
        """
        botocore の before-call の処理。同時に呼ぶ数の枠を取り、所要時間を測るために開始時刻を
        残す（None を返すこと）。枠は、応答（after-call）か失敗（after-call-error）で返す。
        """
        self._api_slots.acquire()
        context["elbx_slot"] = True
        context["elbx_started_at"] = time.perf_counter()

    def _release_api_slot(self, context: dict[str, Any]) -> None:
        """before-call で取った枠を返す（同じ呼び出しで 2 回返さない）。"""
        if context.pop("elbx_slot", False):
            self._api_slots.release()

    def _on_api_call_end(
        self, http_response: Any, model: Any, context: dict[str, Any], **_: Any
    ) -> None:
        """botocore の after-call の処理。枠を返し、呼び出しを DEBUG で記録する。"""
        self._release_api_slot(context)
        _log_api_call(http_response, model, context)

    def _on_api_call_error(self, context: dict[str, Any], **_: Any) -> None:
        """botocore の after-call-error の処理（接続の失敗など、応答がないとき）。枠を返す。"""
        self._release_api_slot(context)

    def _safe_create_client(self, service_name: str) -> Any | None:
        """
        セッションが対象サービスをサポートしている場合のみクライアントを生成する。
        リージョン未設定（Route 53 以外）やエンドポイント未対応環境でも初期化自体は失敗させない。

        :param service_name: Boto3サービス名（例: `"ecs"`, `"lambda"`, `"route53"`）
        :return: 生成されたBoto3クライアント（生成できない場合はNone）
        """
        with tolerate_aws_errors("log.what.client", service_name):
            return self._create_client(service_name)
        return None

    # =========================================================================
    # 公開API: ロードバランサー・ツリー取得
    # =========================================================================

    @_wrap_aws_errors("client.action.list_load_balancers")
    def list_load_balancers(
        self,
        names: list[str] | None = None,
        search: str | None = None,
        include_relations: bool = True,
        include_network: bool = True,
        include_dns: bool = True,
        include_certificates: bool = True,
        include_attributes: bool = True,
        resolve_targets: bool = True,
    ) -> list[LoadBalancerView]:
        """
        List load balancers with everything behind them (listeners, rules, target groups,
        resolved targets, Route 53 records, subnets, security groups).

        ロードバランサーの一覧を、配下（リスナー・ルール・TG・実体を特定したターゲット・Route 53・
        サブネット・SG）ごと取得する。

        :param names: load balancer names to fetch (exact match)
            / 取得するロードバランサー名（完全一致）
        :param search: keep only load balancers whose name or DNS name contains this text
            / 名前または DNS 名にこの文字列を含むものだけにする
        :param include_relations: False returns only the load balancers themselves
            (no listeners, target groups or targets)
            / False なら ELB 本体だけを返す（リスナー・TG・ターゲットは取得しない）
        :param include_network: fetch subnets and security groups
            / サブネットとセキュリティグループを取得する
        :param include_dns: scan all Route 53 zones for records that point at the load
            balancers (slow in accounts with many zones)
            / Route 53 の全ゾーンを走査して ELB を向くレコードを取得する（ゾーンが多いと遅い）
        :param include_certificates: fetch the certificate details from ACM
            / ACM から証明書の詳細を取得する
        :param include_attributes: fetch the attributes of the load balancers and target groups
            / ELB と TG の属性を取得する
        :param resolve_targets: find the resource behind each target (EC2, ECS, Lambda, ENI,
            downstream load balancer). False leaves only the health of each target, which is
            much faster when only the counts are needed
            / ターゲットの実体（EC2・ECS・Lambda・ENI・下流の ELB）を調べる。False ならヘルス状態
            だけになる（件数だけが必要なら大幅に速い）
        :return: `LoadBalancerView` list / `LoadBalancerView` のリスト
        :raises ResourceNotFoundError: a name in `names` does not exist
            / `names` に存在しない名前がある
        """
        if include_relations and include_dns:
            self._prefetch_hosted_zones()
        try:
            raw_lbs = self._fetch_raw_load_balancers(names=names)
        except ClientError as exc:
            if names is not None and error_code(exc) in _NOT_FOUND_CODES:
                raise ResourceNotFoundError(
                    t("client.lb_not_found_detail", name=", ".join(names), error=exc)
                ) from exc
            raise
        if search:
            keyword = search.lower()
            raw_lbs = [
                lb
                for lb in raw_lbs
                if keyword in lb.get("LoadBalancerName", "").lower()
                or keyword in lb.get("DNSName", "").lower()
            ]

        if not raw_lbs:
            return []

        if not include_relations:
            return [self._build_basic_lb_view(lb) for lb in raw_lbs]

        return self._hydrate_load_balancers(
            raw_lbs,
            include_network=include_network,
            include_dns=include_dns,
            include_certificates=include_certificates,
            include_attributes=include_attributes,
            resolve_targets=resolve_targets,
        )

    @_wrap_aws_errors("client.action.get_load_balancer")
    def get_load_balancer(
        self,
        name_or_arn: str,
        include_network: bool = True,
        include_dns: bool = True,
        include_certificates: bool = True,
        include_attributes: bool = True,
        resolve_targets: bool = True,
    ) -> LoadBalancerView:
        """
        Get one load balancer with everything behind it.

        1つのロードバランサーを配下ごと取得する。

        :param name_or_arn: load balancer name or ARN / ロードバランサー名または ARN
        :param include_network: fetch subnets and security groups
            / サブネットとセキュリティグループを取得する
        :param include_dns: scan Route 53 for records that point at the load balancer
            / Route 53 を走査して ELB を向くレコードを取得する
        :param include_certificates: fetch the certificate details from ACM
            / ACM から証明書の詳細を取得する
        :param include_attributes: fetch the attributes of the load balancers and target groups
            / ELB と TG の属性を取得する
        :param resolve_targets: find the resource behind each target (EC2, ECS, Lambda, ENI,
            downstream load balancer). False leaves only the health of each target, which is
            much faster when only the counts are needed
            / ターゲットの実体（EC2・ECS・Lambda・ENI・下流の ELB）を調べる。False ならヘルス状態
            だけになる（件数だけが必要なら大幅に速い）
        :return: `LoadBalancerView`
        :raises ResourceNotFoundError: the load balancer does not exist
            / ロードバランサーが存在しない
        """
        if include_dns:
            self._prefetch_hosted_zones()
        raw_lb = self._resolve_raw_load_balancer(name_or_arn)
        views = self._hydrate_load_balancers(
            [raw_lb],
            include_network=include_network,
            include_dns=include_dns,
            include_certificates=include_certificates,
            include_attributes=include_attributes,
            resolve_targets=resolve_targets,
        )
        return views[0]

    # =========================================================================
    # 公開API: ターゲットグループ取得・逆引き検索
    # =========================================================================

    @_wrap_aws_errors("client.action.list_target_groups")
    def list_target_groups(
        self,
        lb_name_or_arn: str | None = None,
        names: list[str] | None = None,
        search: str | None = None,
        include_attributes: bool = True,
        expand_downstream: bool = True,
    ) -> list[TargetGroupView]:
        """
        List target groups with the health and the resolved resource of every target.

        ターゲットグループの一覧を、各ターゲットのヘルス状態と実体リソース付きで取得する。

        :param lb_name_or_arn: only the target groups of this load balancer (name or ARN)
            / この ELB（名前または ARN）に紐づくターゲットグループだけにする
        :param names: target group names to fetch (exact match)
            / 取得するターゲットグループ名（完全一致）
        :param search: keep only target groups whose name contains this text
            / 名前にこの文字列を含むものだけにする
        :param include_attributes: fetch the attributes of the target groups
            / TG の属性を取得する
        :param expand_downstream: also fetch what is behind a downstream load balancer. False
            leaves `TargetView.downstream_lb` with the load balancer itself and no listeners or
            target groups, which avoids reading the same target groups again
            / 下流の ELB の配下も取得する。False なら `TargetView.downstream_lb` は ELB 本体だけに
            なり、リスナーと TG は空になる（同じ TG を読み直さなくて済む）
        :return: `TargetGroupView` list / `TargetGroupView` のリスト
        :raises ResourceNotFoundError: the load balancer or a name in `names` does not exist
            / ELB または `names` の名前が存在しない
        """
        lb_arn: str | None = None
        if lb_name_or_arn is not None:
            lb_arn = self._resolve_raw_load_balancer(lb_name_or_arn)["LoadBalancerArn"]

        try:
            raw_tgs = self._fetch_raw_target_groups(lb_arn=lb_arn, names=names)
        except ClientError as exc:
            if names is not None and error_code(exc) in _NOT_FOUND_CODES:
                raise ResourceNotFoundError(
                    t("client.tg_not_found_detail", name=", ".join(names), error=exc)
                ) from exc
            raise
        # describe_target_groups は LoadBalancerArn と Names を同時に指定できないので、ここで絞る
        if lb_arn is not None and names is not None:
            raw_tgs = [tg for tg in raw_tgs if lb_arn in tg.get("LoadBalancerArns", [])]
        if search:
            keyword = search.lower()
            raw_tgs = [tg for tg in raw_tgs if keyword in tg.get("TargetGroupName", "").lower()]

        if not raw_tgs:
            return []

        return self._hydrate_target_groups(
            raw_tgs,
            include_dns=False,
            include_attributes=include_attributes,
            expand_downstream=expand_downstream,
        )

    @_wrap_aws_errors("client.action.get_target_group")
    def get_target_group(
        self, name_or_arn: str, include_attributes: bool = True, expand_downstream: bool = True
    ) -> TargetGroupView:
        """
        Get one target group with the health and the resolved resource of every target.

        1つのターゲットグループを、各ターゲットのヘルス状態と実体リソース付きで取得する。

        :param name_or_arn: target group name or ARN / ターゲットグループ名または ARN
        :param include_attributes: fetch the attributes of the target group
            / TG の属性を取得する
        :param expand_downstream: also fetch what is behind a downstream load balancer. False
            leaves `TargetView.downstream_lb` with the load balancer itself and no listeners or
            target groups, which avoids reading the same target groups again
            / 下流の ELB の配下も取得する。False なら `TargetView.downstream_lb` は ELB 本体だけに
            なり、リスナーと TG は空になる（同じ TG を読み直さなくて済む）
        :return: `TargetGroupView`
        :raises ResourceNotFoundError: the target group does not exist
            / ターゲットグループが存在しない
        """
        raw_tg = self._resolve_raw_target_group(name_or_arn)
        return self._hydrate_target_groups(
            [raw_tg],
            include_dns=False,
            include_attributes=include_attributes,
            expand_downstream=expand_downstream,
        )[0]

    @_wrap_aws_errors("client.action.find_target")
    def find_target(
        self, query: str, include_network: bool = True, expand_downstream: bool = True
    ) -> list[TargetLookupResult]:
        """
        Find which load balancers and target groups a resource is registered in.

        `query` is matched against EC2 instance IDs and Name tags, IP addresses, ENI IDs, ECS
        cluster / service / task names, Lambda function names and downstream load balancer
        names. Names and IDs match as substrings (case-insensitive); an IP address matches
        only as a whole (IPv6 in any notation); an ARN matches by its name part only, unless
        `query` starts with `arn:`. Every target group of every load balancer is read, so
        this takes a while in a large account.

        あるリソースがどの ELB・どのターゲットグループに登録されているかを逆引きする。
        `query` は EC2 のインスタンス ID と Name タグ、IP アドレス、ENI ID、ECS のクラスター・
        サービス・タスク名、Lambda 関数名、下流の ELB 名と照合する。名前と ID は部分一致（大文字
        小文字を区別しない）、IP アドレスは完全一致（IPv6 は表記の違いを吸収）。ARN は名前の部分
        だけと照合する（`query` が `arn:` で始まるときは ARN 全体）。全 ELB の全 TG を読むため、
        大きなアカウントでは時間がかかる。

        :param query: text to look for / 探す文字列
        :param include_network: fetch the subnets and security groups of the targets
            / ターゲットのサブネットと SG を取得する
        :param expand_downstream: also fetch what is behind a downstream load balancer. False
            leaves `TargetView.downstream_lb` with the load balancer itself and no listeners or
            target groups, which avoids reading the same target groups again
            / 下流の ELB の配下も取得する。False なら `TargetView.downstream_lb` は ELB 本体だけに
            なり、リスナーと TG は空になる（同じ TG を読み直さなくて済む）
        :return: one `TargetLookupResult` per (load balancer, target group, target) match;
            a target group that is not attached to any load balancer gives one result with
            an empty load balancer
            / 一致した（ELB・TG・ターゲット）ごとの `TargetLookupResult`。どの ELB にも
            紐づかない TG は ELB が空の結果になる
        :raises ELBError: `query` is empty or only spaces / `query` が空か空白だけ
        """
        # 空の文字列はすべての名前に部分一致し、全ターゲットが「見つかった」ことになるため弾く
        if not query.strip():
            raise ELBError(t("client.empty_query"))
        raw_lbs = self._fetch_raw_load_balancers()
        lb_map = {lb["LoadBalancerArn"]: lb for lb in raw_lbs}

        # 逆引き検索では TG の属性と、下流ELBの証明書・属性・ネットワーク情報、Route 53 走査は
        # 使わないのでスキップして高速化する
        raw_tgs = self._fetch_raw_target_groups()
        shared_raw_tgs = {tg["TargetGroupArn"]: tg for tg in raw_tgs}
        tg_views = self._hydrate_target_groups(
            raw_tgs,
            _shared_raw_tgs=shared_raw_tgs,
            include_network=False,
            include_dns=False,
            include_certificates=False,
            include_attributes=False,
            include_target_network=include_network,
            expand_downstream=expand_downstream,
        )

        q_lower = query.strip().lower()
        results: list[TargetLookupResult] = []

        for tg in tg_views:
            for target in tg.targets:
                if not self._matches_target(target, q_lower):
                    continue
                if tg.load_balancer_arns:
                    for lb_arn in dict.fromkeys(tg.load_balancer_arns):
                        lb_info = lb_map.get(lb_arn, {})
                        results.append(
                            TargetLookupResult(
                                load_balancer_name=lb_info.get("LoadBalancerName", ""),
                                load_balancer_arn=lb_arn,
                                load_balancer_dns=lb_info.get("DNSName", ""),
                                target_group_name=tg.name,
                                target_group_arn=tg.arn,
                                target=target,
                            )
                        )
                else:
                    results.append(
                        TargetLookupResult(
                            load_balancer_name="",
                            load_balancer_arn="",
                            load_balancer_dns="",
                            target_group_name=tg.name,
                            target_group_arn=tg.arn,
                            target=target,
                        )
                    )
        return results

    @staticmethod
    def _matches_target(target: TargetView, q_lower: str) -> bool:
        """
        `TargetView` が検索クエリ（小文字）に一致するかを判定する。
        IPアドレス（IPv4/IPv6）は誤ヒットを避けるため完全一致（IPv6の省略表記揺れは正規化して比較）、
        名前やID類は部分一致で判定する。

        :param target: 判定対象の `TargetView`
        :param q_lower: 小文字化された検索クエリ
        :return: 一致する場合は True、一致しない場合は False
        """
        q_canon_ip = canonical_ip(q_lower)

        def _ip_eq(ip_val: str) -> bool:
            if not ip_val:
                return False
            if ip_val.lower() == q_lower:
                return True
            return bool(q_canon_ip is not None and canonical_ip(ip_val) == q_canon_ip)

        if _ip_eq(target.ip_address):
            return True
        # target_id 自体が IP アドレスの場合は完全一致（正規化一致）のみで判定し、部分一致させない。
        # ARN（Lambda・下流の ELB）は名前の部分だけと照合する
        if canonical_ip(target.target_id) is not None:
            if _ip_eq(target.target_id):
                return True
        elif _arn_contains(target.target_id, q_lower):
            return True

        if target.ec2 and (
            q_lower in target.ec2.name.lower()
            or q_lower in target.ec2.instance_id.lower()
            or _ip_eq(target.ec2.private_ip)
            or _ip_eq(target.ec2.public_ip)
        ):
            return True
        if target.ecs and (
            q_lower in target.ecs.cluster_name.lower()
            or q_lower in target.ecs.service_name.lower()
            or q_lower in target.ecs.task_id.lower()
            or (q_lower.startswith("arn:") and q_lower in target.ecs.task_arn.lower())
            or q_lower in target.ecs.task_definition.lower()
            or q_lower in target.ecs.container_name.lower()
            or _ip_eq(target.ecs.private_ip)
        ):
            return True
        if target.lambda_function and (
            q_lower in target.lambda_function.function_name.lower()
            or _arn_contains(target.lambda_function.function_arn, q_lower)
        ):
            return True
        if target.downstream_lb and (
            q_lower in target.downstream_lb.name.lower()
            or _arn_contains(target.downstream_lb.arn, q_lower)
            or q_lower in target.downstream_lb.dns_name.lower()
        ):
            return True
        return bool(
            target.eni
            and (
                q_lower in target.eni.network_interface_id.lower()
                or q_lower in target.eni.description.lower()
                or _ip_eq(target.eni.private_ip)
            )
        )

    # =========================================================================
    # 公開API: ルーティング先シミュレーション
    # =========================================================================

    @_wrap_aws_errors("client.action.simulate_request")
    def simulate_request(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        source_ip: str | None = None,
        lb_name_or_arn: str | None = None,
    ) -> RouteSimulationResult:
        """
        Simulate which listener rule a request matches and where it is sent, from the
        configuration alone (no request is sent). When the matched rule forwards to a target
        group that contains a downstream load balancer (NLB -> ALB, ALB -> NLB), its rules are
        evaluated too. Only what the request reaches is fetched: the rules of the listener
        on the request's port and the target groups of the matched rule, so
        `RouteHop.load_balancer` is not the whole tree.

        Rules are evaluated in priority order with the ALB semantics: host, header and query
        conditions are case-insensitive, path and method are case-sensitive, `*` and `?` are
        wildcards, the values of one condition are OR-ed and the conditions of one rule are
        AND-ed. The host-header condition uses the `Host` header when `headers` has one
        (without the port), otherwise the host name of `url`.

        リクエストがどのリスナールールに一致し、どこへ届くかを設定だけから評価する（リクエストは
        送らない）。一致したルールの転送先 TG に下流の ELB（NLB -> ALB、ALB -> NLB）があれば、
        その ELB のルールも続けて評価する。取得するのはリクエストの届く範囲（ポートのリスナーの
        ルールと、一致したルールの転送先の TG）だけなので、`RouteHop.load_balancer` は ELB 全体
        ではない。評価は ALB と同じ規則で行う（ホスト・ヘッダー・クエリは
        大文字小文字を区別せず、パスとメソッドは区別する。`*` と `?` はワイルドカード。1つの条件の
        値は OR、1つのルールの条件は AND）。host-header 条件は `headers` に `Host` があればその値
        （ポートを除く）、なければ `url` のホスト名と照合する。

        :param url: the request URL, e.g. `https://api.example.com/v1/users?id=1`
            (`http://` is assumed when the scheme is missing)
            / 評価する URL（スキームがなければ `http://` とみなす）
        :param method: HTTP method (default `"GET"`) / HTTP メソッド（既定 `"GET"`）
        :param headers: request headers for `http-header` conditions
            / `http-header` 条件の評価に使うリクエストヘッダー
        :param source_ip: client IP for `source-ip` conditions; rules with a `source-ip`
            condition do not match when it is omitted
            / `source-ip` 条件の評価に使う送信元 IP（省略すると `source-ip` 条件は不一致）
        :param lb_name_or_arn: the entry load balancer; when omitted it is found from the host
            name (the load balancer's own DNS name, then Route 53 records, then Route 53
            wildcard records)
            / 入口の ELB。省略時はホスト名から探す（ELB 自身の DNS 名 → Route 53 の
            レコード → Route 53 のワイルドカードレコード）
        :return: `RouteSimulationResult`
        :raises ELBError: the URL or source IP cannot be parsed
            / URL または送信元 IP を解釈できない
        :raises ResourceNotFoundError: no load balancer serves the host name
            / ホスト名を向いている ELB がない
        """
        try:
            req = SimulatedRequest.from_url(
                url, method=method, headers=headers, source_ip=source_ip
            )
        except ValueError as exc:
            raise ELBError(t("client.bad_request", url=url, error=exc)) from exc

        if lb_name_or_arn is not None:
            raw_lbs = [self._resolve_raw_load_balancer(lb_name_or_arn)]
        else:
            # ELB 自身の DNS 名なら Route 53 は読まないので、そうでないときだけ先に読み始める
            if not _ELB_DNS_NAME.search(req.host):
                self._prefetch_hosted_zones()
            raw_lbs = self._find_raw_lbs_for_host(req.host)
            if not raw_lbs:
                raise ResourceNotFoundError(t("client.no_lb_for_host", host=req.host))

        # ELB ごとに、評価の直前に、リクエストの届く範囲（ポートのリスナーのルールと、一致した
        # ルールの転送先の TG）だけを取得する。先に ELB の配下をすべて取得すると、TG の多い ELB で
        # 届かない TG のヘルスとターゲットの実体まで調べることになる
        def load(lb: LoadBalancerView, port: int, visited: frozenset[str]) -> LoadBalancerView:
            return self._hydrate_for_route(lb, req, port, visited)

        return RouteSimulationResult(
            url=req.url,
            method=req.method,
            host=req.host,
            port=req.port,
            path=req.path,
            entries=[
                simulate_hop(self._build_basic_lb_view(raw_lb), req, req.port, _load=load)
                for raw_lb in raw_lbs
            ],
        )

    def _find_raw_lbs_for_host(self, host: str) -> list[dict[str, Any]]:
        """
        ホスト名を向いている入口ELBを、
        1. ELB自身のDNS名 → 2. Route 53 レコード完全一致 → 3. Route 53 ワイルドカード一致
        の優先順で探して返す。

        :param host: リクエストURLのホスト名
        :return: 一致したELB生データ辞書のリスト
        """
        raw_lbs = self._fetch_raw_load_balancers()
        host_norm = _normalize_dns_name(host)
        direct = [lb for lb in raw_lbs if _normalize_dns_name(lb.get("DNSName", "")) == host_norm]
        if direct:
            return direct

        # 読めなかったのに「向いている ELB がない」と答えないよう、ここでは失敗を隠さない。
        # 読むのは、このホスト名のレコードを持ちうるゾーンだけ
        zone_results = self._fetch_route53_zones_for_host(host_norm)
        r53_map = self._fetch_route53_records_by_dns(
            [lb.get("DNSName", "") for lb in raw_lbs if lb.get("DNSName")], zone_results
        )
        # ワイルドカードのレコードは、同じゾーンにその名前のレコードがないときだけ使われる
        # （`api.example.com` が CloudFront を向いていれば、`*.example.com` の ELB には届かない）
        zones_with_host = {
            hz.get("Id", "").split("/")[-1]
            for hz, records in zone_results
            if any(_decode_route53_name(r.get("Name", "")).lower() == host_norm for r in records)
        }
        exact: list[dict[str, Any]] = []
        wildcard: list[dict[str, Any]] = []
        for lb in raw_lbs:
            records = r53_map.get(lb.get("DNSName", ""), [])
            if any(r.record_name.lower() == host_norm for r in records):
                exact.append(lb)
            elif any(
                "*" in r.record_name
                and r.zone_id not in zones_with_host
                and wildcard_match(r.record_name, host_norm, ignore_case=True)
                for r in records
            ):
                wildcard.append(lb)
        found = exact or wildcard
        # 読めなかったゾーンにレコードがあるかもしれないので、見つからないときは
        # 「ない」と答えずに、読めなかったことを伝える
        failures = self._route53_zone_failures
        if not found and failures:
            raise next(iter(failures.values()))
        return found

    # =========================================================================
    # 内部メソッド: 名前・ARN解決
    # =========================================================================

    def _resolve_raw_load_balancer(self, name_or_arn: str) -> dict[str, Any]:
        """
        名前またはARNから単一のロードバランサー辞書を解決する。

        :param name_or_arn: ロードバランサー名またはARN
        :return: Boto3が返すLoadBalancer辞書
        :raises ResourceNotFoundError: 対象のロードバランサーが存在しない場合
        """
        try:
            if name_or_arn.startswith("arn:"):
                lbs = self._fetch_raw_load_balancers(lb_arns=[name_or_arn])
            else:
                lbs = self._fetch_raw_load_balancers(names=[name_or_arn])
        except ClientError as exc:
            if error_code(exc) not in _NOT_FOUND_CODES:
                raise
            raise ResourceNotFoundError(
                t("client.lb_not_found_detail", name=name_or_arn, error=exc)
            ) from exc

        if not lbs:
            raise ResourceNotFoundError(t("client.lb_not_found", name=name_or_arn))
        return lbs[0]

    def _resolve_raw_target_group(self, name_or_arn: str) -> dict[str, Any]:
        """
        名前またはARNから単一のターゲットグループ辞書を解決する。

        :param name_or_arn: ターゲットグループ名またはARN
        :return: Boto3が返すTargetGroup辞書
        :raises ResourceNotFoundError: 対象のターゲットグループが存在しない場合
        """
        try:
            if name_or_arn.startswith("arn:"):
                tgs = self._fetch_raw_target_groups(tg_arns=[name_or_arn])
            else:
                tgs = self._fetch_raw_target_groups(names=[name_or_arn])
        except ClientError as exc:
            if error_code(exc) not in _NOT_FOUND_CODES:
                raise
            raise ResourceNotFoundError(
                t("client.tg_not_found_detail", name=name_or_arn, error=exc)
            ) from exc

        if not tgs:
            raise ResourceNotFoundError(t("client.tg_not_found", name=name_or_arn))
        return tgs[0]
