"""
AWS各サービス (ELBv2, EC2, ENI, ECS, Lambda, ACM, Route53) からの低レベルAPI一括取得モジュール。
ページネーション処理、チャンク分割クエリ、およびスレッドプールによる並列API取得を提供する。
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import re
import threading
import time
from collections.abc import Callable, Hashable, Iterable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any, Generic, TypeVar

from botocore.exceptions import BotoCoreError, ClientError

from ._ids import canonical_ip, is_arn_of
from .messages import t
from .models import (
    CertificateInfo,
    EC2InstanceInfo,
    ECSTaskInfo,
    ENIInfo,
    LambdaFunctionInfo,
    Route53RecordInfo,
    SecurityGroupInfo,
    SecurityGroupRuleSummary,
)

T = TypeVar("T")
R = TypeVar("R")

# AWS API の呼び出しで起きる例外（権限不足・リソースなし・接続失敗など）。
# 「権限がなければその情報は空にして続ける」のはこれらだけで、コードの誤り（TypeError 等）は隠さない
AWS_ERRORS = (BotoCoreError, ClientError)

# 指定した ELB・TG がない（形が正しくない名前や ARN を含む）ときの AWS のエラーコード。
# 権限不足や期限切れの認証情報を「見つからない」と伝えないように、これらだけを見分ける
_NOT_FOUND_CODES = ("LoadBalancerNotFound", "TargetGroupNotFound", "ValidationError")

logger = logging.getLogger("elbx")


def note_skipped(what: str, exc: Exception, target: str = "") -> None:
    """
    AWS の失敗で情報の一部を取得できず、それなしで続けることを WARNING で記録する。
    出力からは、情報が欠けた理由（権限がないのか、そういう構成なのか）が分からないため。

    :param what: 取得しようとしたものの、messages.py のキー
    :param exc: 起きた例外
    :param target: 対象の名前や ARN（1 つに決まるとき）
    """
    target_part = f" ({target})" if target else ""
    logger.warning(t("log.skipped", what=t(what), target=target_part, error=exc))


@contextlib.contextmanager
def tolerate_aws_errors(what: str, target: str = "") -> Iterator[None]:
    """
    `with` の中の AWS の失敗を `note_skipped` で記録して、続ける。

    :param what: 取得しようとしたものの、messages.py のキー
    :param target: 対象の名前や ARN（1 つに決まるとき）
    """
    try:
        yield
    except AWS_ERRORS as exc:
        note_skipped(what, exc, target)


# Route53 ゾーン走査結果のキャッシュ有効秒数（同一クライアント内の再帰取得で再利用）
ROUTE53_CACHE_TTL_SECONDS = 60.0

# ELBv2 の describe_* で Names / ARN を一度に指定できる件数の上限
ELBV2_BATCH_SIZE = 20

# 1 つの ECS クラスターを読んだ結果: (サービス名 -> サービス, コンテナインスタンス -> EC2, タスク)
_ECSClusterScan = tuple[dict[str, dict[str, Any]], dict[str, str], list[dict[str, Any]]]

# Route 53 API が a-z 0-9 - _ 以外の文字を返すときの形（例: ワイルドカードの `*` は `\052`）
_ROUTE53_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")

# IpProtocol の ICMP の表記（EC2 は icmpv6 を番号で返すことがある）
_ICMP_PROTOCOLS = ("icmp", "icmpv6", "1", "58")


def _chunked(items: list[str], size: int) -> Iterable[list[str]]:
    """
    リストを指定サイズのチャンク（部分リスト）に分割するジェネレータ。
    AWS API の 1 回あたり引数上限（ELBv2 の 20 件、EC2/ECS の 100 件など）に収めるために使用する。

    :param items: 分割対象の文字列リスト
    :param size: 1チャンクあたりの最大要素数
    :return: 分割された部分リストを順次返すイテラブル
    """
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _collect_pages(
    call: Callable[..., dict[str, Any]],
    result_key: str,
    token_key: str = "NextToken",
    next_token_key: str | None = None,
    **kwargs: Any,
) -> list[Any]:
    """
    トークン方式のページネーションAPIを最後のページまで呼び出し、result_key の要素を連結して返す。

    - EC2 の "NextToken"、ECS の "nextToken" のように、要求と応答で同じキーを使う API
    - ELBv2 のように、要求は "Marker"、応答は "NextMarker" と別のキーを使う API（`next_token_key`）

    boto3 の get_paginator を使わずに直接呼び出すことで、単体テストでメソッドをスタブ関数に
    差し替えた場合でもそのままページネーションが動作する。

    :param call: 呼び出す boto3 クライアントメソッド
    :param result_key: レスポンス辞書から結果リストを取り出すキー名
    :param token_key: 要求に付けるページネーショントークンのキー名（デフォルト: "NextToken"）
    :param next_token_key: 応答のトークンのキー名（省略時は `token_key` と同じ）
    :param kwargs: API呼び出しにそのまま渡す追加パラメータ
    :return: 全ページから収集した要素のリスト
    """
    items: list[Any] = []
    token: str | None = None
    while True:
        params = dict(kwargs)
        if token:
            params[token_key] = token
        resp = call(**params)
        items.extend(resp.get(result_key, []))
        token = resp.get(next_token_key or token_key)
        if not token:
            return items


def _collect_in_chunks(
    call: Callable[..., dict[str, Any]],
    result_key: str,
    param_name: str,
    values: Iterable[str],
    size: int = ELBV2_BATCH_SIZE,
) -> list[Any]:
    """
    ID や名前のリストを API の上限件数ごとに分けて照会し、result_key の要素を連結して返す。

    :param call: 呼び出す boto3 クライアントメソッド
    :param result_key: レスポンス辞書から結果リストを取り出すキー名
    :param param_name: リストを渡す引数名（例: `"Names"`, `"LoadBalancerArns"`）
    :param values: 照会する値（重複は除く）
    :param size: 1回に指定できる最大件数
    :return: 全チャンクの結果を連結したリスト
    """
    items: list[Any] = []
    for chunk in _chunked(list(dict.fromkeys(values)), size):
        items.extend(call(**{param_name: chunk}).get(result_key, []))
    return items


def pick_in_vpc(candidates: dict[str, dict[str, T]], ip: str, vpc_id: str) -> T | None:
    """
    `{IP: {VPC ID: 値}}` から、IP ターゲットの実体（ENI・EC2）を選ぶ。

    別々の VPC が同じ CIDR を使っていると、同じプライベート IP がリージョン内に複数ある。
    `vpc_id`（TG の VPC）があれば、その VPC のものだけを選ぶ。空のとき（TG の VPC の外の IP。
    ピアリング先の VPC などで、AZ を `all` にして登録したもの）は、どの VPC のものでも、
    1つに決まるときだけ選ぶ。

    :param candidates: `{IP: {VPC ID: 値}}`（IP は元の表記と正規化表記の両方がキー）
    :param ip: ターゲットの IP
    :param vpc_id: 探す VPC の ID（空ならすべての VPC）
    :return: 選んだ値（ないか、1つに決まらなければ None）
    """
    for key in dict.fromkeys((ip, canonical_ip(ip) or ip)):
        by_vpc = candidates.get(key)
        if not by_vpc:
            continue
        if vpc_id:
            if vpc_id in by_vpc:
                return by_vpc[vpc_id]
            continue
        return next(iter(by_vpc.values())) if len(by_vpc) == 1 else None
    return None


def _split_ips_for_filters(ips: Iterable[str]) -> tuple[list[str], list[str], dict[str, set[str]]]:
    """
    EC2 のフィルタ照会用に、IP を IPv4 と IPv6 に分ける。

    IPv6 は省略表記の揺れ（`2001:0db8::1` と `2001:db8::1`）があるため、正規化表記と元の表記の
    両方で照会する。IP として解釈できない値は IPv4 側にそのまま入れる（フィルタで一致しないだけ）。

    :param ips: 照会する IP アドレスのリスト
    :return: `(IPv4 のリスト, IPv6 のリスト, {正規化表記: 元の表記の集合})`
    """
    v4_ips: list[str] = []
    v6_ips: list[str] = []
    canon_to_originals: dict[str, set[str]] = {}
    for ip in dict.fromkeys(ips):
        canon = canonical_ip(ip)
        if canon is None:
            v4_ips.append(ip)
            continue
        canon_to_originals.setdefault(canon, set()).add(ip)
        if ":" in canon:
            v6_ips += [canon, ip] if ip != canon else [canon]
        else:
            v4_ips.append(canon)
    return list(dict.fromkeys(v4_ips)), list(dict.fromkeys(v6_ips)), canon_to_originals


def _lambda_name_from_arn(arn: str) -> str:
    """
    Lambda の ARN から関数名を取り出す（`...:function:名前:エイリアス` の形でも名前を返す）。

    :param arn: Lambda 関数の ARN
    :return: 関数名（Lambda の ARN の形でなければ ARN の末尾）
    """
    parts = arn.split(":")
    return parts[6] if len(parts) > 6 and parts[5] == "function" else _short_arn_name(arn)


def _extract_name_tag(tags: list[dict[str, str]] | None) -> str:
    """
    AWSリソースのTagリスト（`[{"Key": "Name", "Value": "..."}]`）からNameタグの値を抽出する。

    :param tags: AWS APIが返すタグ辞書のリスト
    :return: Nameタグの値（存在しない場合は空文字列）
    """
    if not tags:
        return ""
    for tag in tags:
        if tag.get("Key") == "Name":
            return tag.get("Value", "")
    return ""


def _format_datetime(dt: Any) -> str | None:
    """
    datetimeオブジェクトまたは日時表現をISO8601文字列に変換する。

    :param dt: datetimeオブジェクトまたは文字列（None可）
    :return: ISO8601形式の文字列（dtがNoneの場合はNone）
    """
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def error_code(exc: ClientError) -> str:
    """
    ClientError の AWS のエラーコードを返す（メッセージの文面に頼らずに種類を見分けるため）。

    :param exc: botocore の ClientError
    :return: エラーコード（例: `"LoadBalancerNotFound"`）
    """
    return str(exc.response.get("Error", {}).get("Code", ""))


def _short_arn_name(arn: str) -> str:
    """
    ARNの末尾から短いリソース名またはIDを取り出す。

    :param arn: AWS ARN文字列
    :return: '/' および ':' で分割した末尾の文字列
    """
    if not arn:
        return ""
    return arn.split("/")[-1].split(":")[-1]


# ELB が各サブネットに自動で作る ENI の説明（例: "ELB net/my-nlb/50dc6c495c0c9188"）
_ELB_ENI_DESCRIPTION = re.compile(r"^ELB (?:app|net)/([^/\s]+)/([0-9a-f]+)$")


def _parse_elb_eni_description(description: str) -> tuple[str, str] | None:
    """
    ELB のノードの ENI の説明から `(ELB名, ELBのID)` を取り出す。該当しなければ None を返す。

    AWS は ALB/NLB の各サブネット用 ENI に `"ELB app/<名前>/<ID>"` または `"ELB net/<名前>/<ID>"`
    という形式の Description を自動設定するため、IP ターゲットから下流 ELB を逆引きするのに使う。

    :param description: ENI の Description 文字列
    :return: `(ELB名, ARN末尾のID)` のタプル、または ELB の ENI でない場合は None
    """
    match = _ELB_ENI_DESCRIPTION.match(description.strip())
    return (match.group(1), match.group(2)) if match else None


def _decode_route53_name(name: str) -> str:
    """
    Route 53 API が返す名前を、人が書く形にする（8進エスケープを戻し、末尾のドットを除く）。

    API は `a-z 0-9 - _` 以外の文字を `\\ooo` の形で返す（ワイルドカードの `*` は `\\052`）。

    :param name: `ResourceRecordSet` や `HostedZone` の `Name`
    :return: `*.example.com` の形の名前
    """
    return _ROUTE53_OCTAL_ESCAPE.sub(lambda m: chr(int(m[1], 8)), name).rstrip(".")


def _normalize_dns_name(dns: str) -> str:
    """
    ELBやRoute53のDNS名を比較用に正規化する（`dualstack.` 除去・末尾ドット除去・小文字化）。

    Route 53 の AliasTarget では ELB の DNS 名の先頭に `dualstack.`、末尾に `.` が付くことがあるため
    それらを取り除いてから比較できるようにする。

    :param dns: 正規化対象のDNS名
    :return: 正規化された小文字のDNS名
    """
    d = dns.strip().rstrip(".").lower()
    if d.startswith("dualstack."):
        d = d[len("dualstack.") :]
    return d


@dataclasses.dataclass
class ECSMaps:
    """
    ターゲットから ECS タスク・サービスを引くための対応表（`_fetch_ecs_maps` の結果）。

    ターゲットごとに `by_ip` → `by_instance_port` → `by_instance` → `by_tg_arn` の順で探す。
    """

    # タスクの IP（awsvpc / Fargate） -> {タスクの ENI の ID（分からなければ空）: タスク}
    # （別々の VPC が同じ CIDR を使っていると、同じ IP のタスクが複数ある）
    by_ip: dict[str, dict[str, ECSTaskInfo]] = dataclasses.field(default_factory=dict)
    # EC2 インスタンス ID（そのホストで動くタスクが1つだけで、hostPort が分からない場合のみ）
    by_instance: dict[str, ECSTaskInfo] = dataclasses.field(default_factory=dict)
    # (EC2 インスタンス ID, hostPort)（bridge / host モードで1台に複数タスクがある場合）
    by_instance_port: dict[tuple[str, int], ECSTaskInfo] = dataclasses.field(default_factory=dict)
    # TG の ARN（サービス単位の情報。タスク固有の ID や IP は持たない）
    by_tg_arn: dict[str, ECSTaskInfo] = dataclasses.field(default_factory=dict)

    def all_infos(self) -> list[ECSTaskInfo]:
        """すべての対応表に入っている ECSTaskInfo を返す（重複あり）。"""
        return [
            *(info for by_eni in self.by_ip.values() for info in by_eni.values()),
            *self.by_instance.values(),
            *self.by_instance_port.values(),
            *self.by_tg_arn.values(),
        ]

    def task_for_ip(self, ip: str, eni_id: str) -> ECSTaskInfo | None:
        """
        IP のタスクを返す。同じ IP のタスクが別の VPC にもあるときは、ターゲットの ENI
        （TG の VPC で IP から見つけたもの）のタスクを選ぶ。

        :param ip: ターゲットの IP
        :param eni_id: ターゲットの IP を持つ ENI の ID（分からなければ空）
        :return: タスク（ないか、1つに決まらなければ None）
        """
        for key in dict.fromkeys((ip, canonical_ip(ip) or ip)):
            by_eni = self.by_ip.get(key)
            if not by_eni:
                continue
            if eni_id:
                # ENI の ID が分からないタスクは、その ENI のものかもしれないので候補に残す
                return by_eni.get(eni_id) or by_eni.get("")
            return next(iter(by_eni.values())) if len(by_eni) == 1 else None
        return None


def _ecs_service_info(c_arn: str, svc: dict[str, Any], lb_cfg: dict[str, Any]) -> ECSTaskInfo:
    """
    ECS サービスの設定から、TG 単位のフォールバック用 `ECSTaskInfo` を作る
    （タスク固有の ID・IP は空）。

    :param c_arn: ECSクラスターのARN
    :param svc: describe_services のサービス辞書
    :param lb_cfg: サービスの loadBalancers の要素（targetGroupArn / containerName）
    :return: サービス単位の `ECSTaskInfo`
    """
    awsvpc = svc.get("networkConfiguration", {}).get("awsvpcConfiguration", {})
    return ECSTaskInfo(
        cluster_name=_short_arn_name(c_arn),
        cluster_arn=c_arn,
        service_name=svc.get("serviceName", ""),
        task_definition=svc.get("taskDefinition", "").split("/")[-1],
        launch_type=svc.get("launchType", ""),
        last_status=svc.get("status", "ACTIVE"),
        container_name=lb_cfg.get("containerName", ""),
        subnet_ids=list(awsvpc.get("subnets", [])),
        security_group_ids=list(awsvpc.get("securityGroups", [])),
    )


# awsvpc の ENI アタッチメントの details で IP を表す項目名
_ECS_ATTACHMENT_IP_KEYS = ("privateIPv4Address", "privateIPv6Address", "ipv6Address")


def _ecs_task_eni_id(task: dict[str, Any]) -> str:
    """describe_tasks のタスク辞書から、awsvpc の ENI の ID を返す（なければ空）。"""
    for att in task.get("attachments", []):
        for detail in att.get("details", []):
            if detail.get("name") == "networkInterfaceId" and detail.get("value"):
                return str(detail["value"])
    return ""


def _parse_ecs_task(
    c_arn: str, task: dict[str, Any], services: dict[str, dict[str, Any]]
) -> tuple[ECSTaskInfo, list[str], list[int]]:
    """
    describe_tasks のタスク辞書を `ECSTaskInfo` に変換し、照合に使う IP と hostPort も取り出す。

    :param c_arn: ECSクラスターのARN
    :param task: describe_tasks のタスク辞書
    :param services: `{サービス名: サービス辞書}`（サブネット・SG をサービス設定から補う）
    :return: `(ECSTaskInfo, タスクの IP（IPv4/IPv6、重複なし）, bridge/host の hostPort)`
    """
    group = task.get("group", "")
    svc_name = group.split("service:", 1)[1] if group.startswith("service:") else ""

    # awsvpc の ENI アタッチメントから IP とサブネット ID を、コンテナから IP と hostPort を取り出す
    ips: list[str] = []
    subnet_ids: list[str] = []
    for att in task.get("attachments", []):
        for detail in att.get("details", []):
            name, value = detail.get("name"), detail.get("value")
            if value and name in _ECS_ATTACHMENT_IP_KEYS:
                ips.append(value)
            elif value and name == "subnetId":
                subnet_ids.append(value)
    container_names: list[str] = []
    host_ports: list[int] = []
    for cont in task.get("containers", []):
        if cont.get("name"):
            container_names.append(cont["name"])
        for ni in cont.get("networkInterfaces", []):
            ips += [ni[k] for k in ("privateIpv4Address", "ipv6Address") if ni.get(k)]
        host_ports += [
            int(nb["hostPort"]) for nb in cont.get("networkBindings", []) if nb.get("hostPort")
        ]

    awsvpc = (
        services.get(svc_name, {}).get("networkConfiguration", {}).get("awsvpcConfiguration", {})
    )
    ips = list(dict.fromkeys(ips))
    info = ECSTaskInfo(
        cluster_name=_short_arn_name(c_arn),
        cluster_arn=c_arn,
        service_name=svc_name,
        task_arn=task.get("taskArn", ""),
        task_id=_short_arn_name(task.get("taskArn", "")),
        task_definition=task.get("taskDefinitionArn", "").split("/")[-1],
        launch_type=task.get("launchType", ""),
        last_status=task.get("lastStatus", ""),
        private_ip=ips[0] if ips else "",
        container_name=", ".join(dict.fromkeys(container_names)),
        availability_zone=task.get("availabilityZone", ""),
        subnet_ids=list(dict.fromkeys([*subnet_ids, *awsvpc.get("subnets", [])])),
        security_group_ids=list(awsvpc.get("securityGroups", [])),
    )
    return info, ips, host_ports


class Deferred(Generic[R]):
    """
    ほかの取得と並行して進める取得。作ったときに別のスレッドで始め、`result()` で結果を待つ。

    互いに関係のない取得（Route 53 のレコードとターゲットの実体など）を、順番に待たずに進める
    ために使う。失敗は `result()` を呼んだ側に、同じ例外で伝わる。
    """

    def __init__(self, fn: Callable[[], R], background: bool = True) -> None:
        """
        :param fn: 実行する取得
        :param background: False なら別のスレッドを使わず、`result()` のときに実行する（直列）
        """
        self._fn = fn
        self._future: Future[R] | None = None
        if background:
            self._future = Future()
            threading.Thread(target=self._run, args=(self._future,), daemon=True).start()

    def _run(self, future: Future[R]) -> None:
        """別のスレッドで `fn` を実行し、結果か例外を `future` に入れる。"""
        try:
            future.set_result(self._fn())
        except BaseException as exc:  # noqa: BLE001 - result() を呼んだ側に、そのまま伝える
            future.set_exception(exc)

    def result(self) -> R:
        """結果を返す（まだなら終わるまで待つ）。取得が失敗していれば、その例外を送出する。"""
        return self._fn() if self._future is None else self._future.result()


class RunCache:
    """
    公開メソッド 1 回の呼び出しのあいだだけ使う、取得済みのものの置き場。

    下流の ELB をたどると、同じ ECS クラスター・同じ ELB・同じサブネットや SG をもう一度読む
    ことになるので、ここに置いて読み直さない。呼び出しが終われば捨てるので、長く使う
    クライアントが古い結果を返すことはない。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[Hashable, Future[Any]] = {}
        # ARN -> 生ELB辞書 / 名前 -> 生ELB辞書
        self._lbs_by_arn: dict[str, dict[str, Any]] = {}
        self._lbs_by_name: dict[str, dict[str, Any]] = {}
        self._all_lbs: list[dict[str, Any]] | None = None
        # サブネット ID -> `_parse_subnet_dict` の辞書 / SG ID -> SecurityGroupInfo
        self.subnets: dict[str, dict[str, Any]] = {}
        self.security_groups: dict[str, SecurityGroupInfo] = {}

    def once(self, key: Hashable, fn: Callable[[], R]) -> R:
        """
        `key` の取得を 1 回だけ実行して、結果を返す。並行して同じ `key` を求めた側は、最初の
        実行が終わるのを待って同じ結果（失敗なら同じ例外）を受け取る。

        :param key: 取得するものを表すキー
        :param fn: 実行する取得
        """
        with self._lock:
            future = self._results.get(key)
            owner = future is None
            if future is None:
                future = self._results[key] = Future()
        if owner:
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - 待っている側にも同じ例外を伝える
                future.set_exception(exc)
        result: R = future.result()
        return result

    def add_load_balancers(self, raw_lbs: list[dict[str, Any]], everything: bool) -> None:
        """
        取得した ELB を置く。

        :param raw_lbs: 生ELB辞書
        :param everything: 絞り込まずに取得した全件か
        """
        with self._lock:
            for lb in raw_lbs:
                self._lbs_by_arn[lb["LoadBalancerArn"]] = lb
                if lb.get("LoadBalancerName"):
                    self._lbs_by_name[lb["LoadBalancerName"]] = lb
            if everything:
                self._all_lbs = list(raw_lbs)

    def load_balancers(
        self, names: list[str] | None, lb_arns: list[str] | None
    ) -> list[dict[str, Any]] | None:
        """
        求められた ELB がすべて置いてあれば返す（1 つでもなければ None。AWS に問い合わせる）。

        :param names: ELB の名前（指定がなければ None）
        :param lb_arns: ELB の ARN（指定がなければ None）
        """
        with self._lock:
            if names is not None:
                wanted, known = names, self._lbs_by_name
            elif lb_arns is not None:
                wanted, known = lb_arns, self._lbs_by_arn
            else:
                return None if self._all_lbs is None else list(self._all_lbs)
            if not wanted or any(key not in known for key in wanted):
                return None
            return [known[key] for key in dict.fromkeys(wanted)]


class AWSFetcherMixin:
    """
    boto3の各サービスクライアント (elbv2, ec2, acm, ecs, lambda, route53) を用いて
    関連リソースを一括・並列取得するMixinクラス。
    """

    elbv2: Any
    ec2: Any
    acm: Any
    ecs: Any | None
    lambda_client: Any | None
    route53: Any | None = None
    max_workers: int = 10
    # Route 53 の全ゾーン走査結果のキャッシュ（取得時刻, [(ホストゾーン, レコード一覧)]）
    _route53_zone_cache: tuple[float, list[tuple[dict[str, Any], list[dict[str, Any]]]]] | None = (
        None
    )
    # 直近のゾーン走査でレコードを読めなかったゾーン（ゾーン ID -> 例外）。走査の前は None
    _route53_zone_failures: dict[str, Exception] | None = None
    # ゾーン走査を 1 つずつにする（並行して進む下流の ELB の取得が、同じ走査を重ねて始めない
    # ように）。ELBClient は自分のものを作る
    _route53_lock: threading.Lock = threading.Lock()
    # 公開メソッドの呼び出しのあいだだけある置き場（`_one_run`）。外では None
    _run_cache: RunCache | None = None
    _run_depth: int = 0
    _run_lock: threading.Lock = threading.Lock()

    @contextlib.contextmanager
    def _one_run(self) -> Iterator[None]:
        """
        `with` の中を 1 回の実行として、同じものを 2 回取らないための置き場（`RunCache`）を
        用意する。入れ子や、ほかのスレッドからの同時の呼び出しでは同じ置き場を使い、最後の
        1 つが終わったときに捨てる。
        """
        with self._run_lock:
            self._run_depth += 1
            if self._run_cache is None:
                self._run_cache = RunCache()
        try:
            yield
        finally:
            with self._run_lock:
                self._run_depth -= 1
                if self._run_depth == 0:
                    self._run_cache = None

    def _once(self, key: Hashable, fn: Callable[[], R]) -> R:
        """
        1 回の実行の中では、`key` の取得を 1 回だけにする（実行の外では、毎回 `fn` を呼ぶ）。

        :param key: 取得するものを表すキー
        :param fn: 実行する取得
        """
        cache = self._run_cache
        return fn() if cache is None else cache.once(key, fn)

    def _start(self, fn: Callable[[], R]) -> Deferred[R]:
        """
        ほかの取得と並行して進められる取得を始める。`max_workers <= 1` なら別のスレッドを使わず、
        `result()` のときに実行する。

        :param fn: 実行する取得
        :return: 結果を `result()` で受け取る `Deferred`
        """
        return Deferred(fn, background=self.max_workers > 1)

    def _parallel_map(self, fn: Callable[[T], R], items: Sequence[T]) -> list[R]:
        """
        複数要素に対するI/O関数呼び出しをThreadPoolExecutorで並列実行し、入力順を保って結果を返す。
        要素数が1件以下の場合や `max_workers <= 1` の場合はスレッド生成コストを避けて逐次実行する。

        :param fn: 各要素に適用する関数
        :param items: 入力要素のシーケンス
        :return: 各要素に対する `fn(item)` の結果リスト（入力と同じ順序）
        """
        if not items:
            return []
        if len(items) == 1 or self.max_workers <= 1:
            return [fn(item) for item in items]
        workers = min(self.max_workers, len(items))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(fn, items))

    # =========================================================================
    # ELBv2 生データ取得（ページネーション・チャンク分割対応）
    # =========================================================================

    def _fetch_raw_load_balancers(
        self,
        names: list[str] | None = None,
        lb_arns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        ELBv2の `describe_load_balancers` を、名前・ARN 指定時は20件ずつ、
        それ以外はページネーションで最後まで取得する。

        :param names: 絞り込むELB名のリスト（省略時は名前フィルタなし）
        :param lb_arns: 絞り込むELB ARNのリスト（省略時はARNフィルタなし）
        :return: Boto3が返すLoadBalancer辞書のリスト
        """
        # 同じ実行の中でもう取得した ELB（全件の一覧や、ENI から特定した下流の ELB）は読み直さない
        cache = self._run_cache
        if cache is not None:
            known = cache.load_balancers(names, lb_arns)
            if known is not None:
                return known

        call = self.elbv2.describe_load_balancers
        # 空のリストは「0件を指定した」ので、全件の取得にしない（_collect_in_chunks は [] を返す）
        if names is not None:
            raw_lbs = _collect_in_chunks(call, "LoadBalancers", "Names", names)
        elif lb_arns is not None:
            raw_lbs = _collect_in_chunks(call, "LoadBalancers", "LoadBalancerArns", lb_arns)
        else:
            raw_lbs = _collect_pages(call, "LoadBalancers", "Marker", "NextMarker")
        if cache is not None:
            cache.add_load_balancers(raw_lbs, everything=names is None and lb_arns is None)
        return raw_lbs

    def _fetch_raw_target_groups(
        self,
        lb_arn: str | None = None,
        names: list[str] | None = None,
        tg_arns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        ELBv2の `describe_target_groups` を、名前・ARN 指定時は20件ずつ、
        それ以外はページネーションで最後まで取得する。

        :param lb_arn: 特定のELBに紐づくターゲットグループのみ取得する場合のELB ARN
        :param names: 絞り込むターゲットグループ名のリスト
        :param tg_arns: 絞り込むターゲットグループARNのリスト
        :return: Boto3が返すTargetGroup辞書のリスト
        """
        call = self.elbv2.describe_target_groups
        if names is not None:
            return _collect_in_chunks(call, "TargetGroups", "Names", names)
        if tg_arns is not None:
            return _collect_in_chunks(call, "TargetGroups", "TargetGroupArns", tg_arns)

        kwargs: dict[str, Any] = {"LoadBalancerArn": lb_arn} if lb_arn else {}
        try:
            return _collect_pages(call, "TargetGroups", "Marker", "NextMarker", **kwargs)
        except ClientError as exc:
            # 削除直後のELBなどで TG が見つからない場合は空として扱う。権限エラー等は再送出する
            if lb_arn and error_code(exc) == "TargetGroupNotFound":
                return []
            raise

    def _fetch_raw_listeners(self, lb_arn: str) -> list[dict[str, Any]]:
        """
        指定ELBのリスナー一覧をページネーション込みで取得する。

        :param lb_arn: 対象ロードバランサーのARN
        :return: Boto3が返すListener辞書のリスト
        """
        return _collect_pages(
            self.elbv2.describe_listeners,
            "Listeners",
            "Marker",
            "NextMarker",
            LoadBalancerArn=lb_arn,
        )

    def _fetch_raw_rules(self, listener_arn: str) -> list[dict[str, Any]]:
        """
        指定リスナーのルール一覧をページネーション込みで取得する。

        :param listener_arn: 対象リスナーのARN
        :return: Boto3が返すRule辞書のリスト
        """
        return _collect_pages(
            self.elbv2.describe_rules, "Rules", "Marker", "NextMarker", ListenerArn=listener_arn
        )

    def _fetch_single_tg_health(self, tg_arn: str) -> tuple[str, list[dict[str, Any]]]:
        """
        単一ターゲットグループの `TargetHealthDescriptions` を取得する。
        権限不足などのエラーを握りつぶすと「ターゲット未登録」と誤認させるため、例外はそのまま送出する。

        :param tg_arn: 対象ターゲットグループのARN
        :return: `(tg_arn, TargetHealthDescriptionsリスト)` のタプル
        """
        resp = self.elbv2.describe_target_health(TargetGroupArn=tg_arn)
        return tg_arn, resp.get("TargetHealthDescriptions", [])

    def _fetch_lb_attributes(self, lb_arn: str) -> tuple[str, dict[str, str]]:
        """
        ロードバランサー属性（アイドルタイムアウト・削除保護・アクセスログ・クロスゾーン等）を取得する。

        :param lb_arn: 対象ロードバランサーのARN
        :return: `(lb_arn, {属性キー: 属性値})` のタプル（取得失敗時は空辞書）
        """
        with tolerate_aws_errors("log.what.lb_attributes", lb_arn):
            resp = self.elbv2.describe_load_balancer_attributes(LoadBalancerArn=lb_arn)
            return lb_arn, {a["Key"]: a.get("Value", "") for a in resp.get("Attributes", [])}
        return lb_arn, {}

    def _fetch_tg_attributes(self, tg_arn: str) -> tuple[str, dict[str, str]]:
        """
        ターゲットグループ属性（登録解除の遅延・スティッキー・スロースタート等）を取得する。

        :param tg_arn: 対象ターゲットグループのARN
        :return: `(tg_arn, {属性キー: 属性値})` のタプル（取得失敗時は空辞書）
        """
        with tolerate_aws_errors("log.what.tg_attributes", tg_arn):
            resp = self.elbv2.describe_target_group_attributes(TargetGroupArn=tg_arn)
            return tg_arn, {a["Key"]: a.get("Value", "") for a in resp.get("Attributes", [])}
        return tg_arn, {}

    # =========================================================================
    # EC2 インスタンス / ENI 解決
    # =========================================================================

    def _fetch_ec2_instances_map(self, instance_ids: list[str]) -> dict[str, EC2InstanceInfo]:
        """
        インスタンスIDのリストから `EC2InstanceInfo` のマップを一括取得する。

        `describe_instances(InstanceIds=...)` だと、存在しないID（終了済みでTGに残っているもの等）が
        1件でも混ざるとチャンク全体が `InvalidInstanceID.NotFound` エラーになるため、
        `Filters=[{"Name": "instance-id", ...}]` で照会して存在するインスタンスだけを取得する。

        :param instance_ids: 取得対象のEC2インスタンスIDリスト
        :return: `{instance_id: EC2InstanceInfo}` の辞書
        """
        if not instance_ids:
            return {}

        unique_ids = list(dict.fromkeys(instance_ids))
        ec2_map: dict[str, EC2InstanceInfo] = {}
        for chunk in _chunked(unique_ids, 100):
            try:
                reservations = _collect_pages(
                    self.ec2.describe_instances,
                    "Reservations",
                    Filters=[{"Name": "instance-id", "Values": chunk}],
                )
            except AWS_ERRORS as exc:
                note_skipped("log.what.ec2_instances", exc)
                continue

            for res in reservations:
                for inst in res.get("Instances", []):
                    info = self._parse_ec2_instance(inst)
                    if info:
                        ec2_map[info.instance_id] = info
        return ec2_map

    def _fetch_ec2_instances_by_ips(
        self, private_ips: list[str]
    ) -> dict[str, dict[str, EC2InstanceInfo]]:
        """
        プライベートIP（IPv4 / IPv6）のリストから `EC2InstanceInfo` を一括逆引きする。
        同じ IP のインスタンスが別々の VPC にありうるため、VPC ごとに返す
        （選ぶのは `pick_in_vpc`）。

        :param private_ips: 逆引き対象のプライベートIPアドレス（IPv4 または IPv6）のリスト
        :return: `{ip: {VPC ID: EC2InstanceInfo}}` の辞書（元の表記と正規化表記の両方をキーに登録）
        """
        if not private_ips:
            return {}

        v4_ips, v6_ips, canon_to_originals = _split_ips_for_filters(private_ips)
        ip_map: dict[str, dict[str, EC2InstanceInfo]] = {}
        for filter_name, ip_list in (("private-ip-address", v4_ips), ("ipv6-address", v6_ips)):
            for chunk in _chunked(ip_list, 100):
                try:
                    reservations = _collect_pages(
                        self.ec2.describe_instances,
                        "Reservations",
                        Filters=[{"Name": filter_name, "Values": chunk}],
                    )
                except AWS_ERRORS as exc:
                    note_skipped("log.what.ec2_instances", exc)
                    continue

                for res in reservations:
                    for inst in res.get("Instances", []):
                        info = self._parse_ec2_instance(inst)
                        if not info:
                            continue
                        inst_ips = self._extract_instance_ips(inst)
                        if info.private_ip and info.private_ip not in inst_ips:
                            inst_ips.append(info.private_ip)
                        vpc_id = inst.get("VpcId", "")
                        for addr in inst_ips:
                            canon = canonical_ip(addr) or addr
                            for key in {addr, canon, *canon_to_originals.get(canon, ())}:
                                ip_map.setdefault(key, {})[vpc_id] = info
        return ip_map

    @staticmethod
    def _extract_instance_ips(inst: dict[str, Any]) -> list[str]:
        """
        Boto3のEC2 Instance辞書から割り当てられている全プライベートIP（IPv4およびIPv6）を抽出する。

        :param inst: Boto3のInstance辞書
        :return: IPアドレス文字列のリスト
        """
        ips = [inst.get("PrivateIpAddress", ""), inst.get("Ipv6Address", "")]
        for ni in inst.get("NetworkInterfaces", []):
            ips.append(ni.get("PrivateIpAddress", ""))
            ips += [a.get("PrivateIpAddress", "") for a in ni.get("PrivateIpAddresses", [])]
            ips.append(ni.get("Ipv6Address", ""))
            ips += [a.get("Ipv6Address", "") for a in ni.get("Ipv6Addresses", [])]
        return [ip for ip in dict.fromkeys(ips) if ip]

    @staticmethod
    def _parse_ec2_instance(inst: dict[str, Any]) -> EC2InstanceInfo | None:
        """
        Boto3のEC2 Instance辞書を `EC2InstanceInfo` に変換する。
        IPv6のみで `PrivateIpAddress` が空の場合は、割り当てられているIPv6アドレスを使用する。

        :param inst: Boto3のInstance辞書
        :return: 変換された `EC2InstanceInfo`（InstanceIdが空の場合はNone）
        """
        i_id = inst.get("InstanceId", "")
        if not i_id:
            return None
        private_ip = inst.get("PrivateIpAddress", "")
        if not private_ip:
            all_ips = AWSFetcherMixin._extract_instance_ips(inst)
            if all_ips:
                private_ip = all_ips[0]
        return EC2InstanceInfo(
            instance_id=i_id,
            name=_extract_name_tag(inst.get("Tags")),
            state=inst.get("State", {}).get("Name", ""),
            private_ip=private_ip,
            public_ip=inst.get("PublicIpAddress", ""),
            availability_zone=inst.get("Placement", {}).get("AvailabilityZone", ""),
            instance_type=inst.get("InstanceType", ""),
            subnet_id=inst.get("SubnetId", ""),
            security_group_ids=[
                sg.get("GroupId", "") for sg in inst.get("SecurityGroups", []) if sg.get("GroupId")
            ],
        )

    def _fetch_eni_map_by_ips(self, private_ips: list[str]) -> dict[str, dict[str, ENIInfo]]:
        """
        IPアドレス（IPv4 / IPv6）のリストからENI（Elastic Network Interface）情報を一括取得する。

        - IPv4は `addresses.private-ip-address` フィルタで照会する。
        - IPv6は `ipv6-addresses.ipv6-address` フィルタで照会する。
        - IPv6の省略表記揺れを吸収し、正規化表記と元の表記の両方でマップに登録する。
        - 同じ IP の ENI が別々の VPC にありうるため、VPC ごとに返す（選ぶのは `pick_in_vpc`）。

        :param private_ips: 照会対象のIPアドレス（IPv4 または IPv6）のリスト
        :return: `{ip: {VPC ID: ENIInfo}}` の辞書
        """
        if not private_ips or not hasattr(self.ec2, "describe_network_interfaces"):
            return {}

        v4_ips, v6_ips, canon_to_originals = _split_ips_for_filters(private_ips)
        eni_map: dict[str, dict[str, ENIInfo]] = {}
        for filter_name, ip_list in (
            ("addresses.private-ip-address", v4_ips),
            ("ipv6-addresses.ipv6-address", v6_ips),
        ):
            for chunk in _chunked(ip_list, 100):
                chunk_canon_set = {canonical_ip(x) or x for x in chunk}
                try:
                    interfaces = _collect_pages(
                        self.ec2.describe_network_interfaces,
                        "NetworkInterfaces",
                        Filters=[{"Name": filter_name, "Values": chunk}],
                    )
                except AWS_ERRORS as exc:
                    note_skipped("log.what.network_interfaces", exc)
                    continue

                for ni in interfaces:
                    sg_ids = [g["GroupId"] for g in ni.get("Groups", []) if g.get("GroupId")]
                    for ip in self._extract_eni_ips(ni):
                        canon = canonical_ip(ip) or ip
                        if canon not in chunk_canon_set and ip not in chunk_canon_set:
                            continue
                        # 照会した表記・ENI が返した表記・正規化表記のどれでも引けるように登録する
                        originals = canon_to_originals.get(canon, {ip})
                        for key_ip in {ip, canon, *originals}:
                            eni_map.setdefault(key_ip, {})[ni.get("VpcId", "")] = ENIInfo(
                                network_interface_id=ni.get("NetworkInterfaceId", ""),
                                private_ip=key_ip if key_ip in originals else ip,
                                subnet_id=ni.get("SubnetId", ""),
                                availability_zone=ni.get("AvailabilityZone", ""),
                                interface_type=ni.get("InterfaceType", "interface"),
                                description=ni.get("Description", ""),
                                instance_id=ni.get("Attachment", {}).get("InstanceId", ""),
                                security_group_ids=list(sg_ids),
                            )
        return eni_map

    @staticmethod
    def _extract_eni_ips(ni: dict[str, Any]) -> list[str]:
        """
        Boto3のNetworkInterface辞書から割り当てられている全IP（IPv4およびIPv6）を抽出する。

        :param ni: Boto3のNetworkInterface辞書
        :return: IPアドレス文字列のリスト（重複なし）
        """
        ips = [
            a["PrivateIpAddress"]
            for a in ni.get("PrivateIpAddresses", [])
            if a.get("PrivateIpAddress")
        ]
        ips += [ni.get("PrivateIpAddress", "")]
        ips += [a["Ipv6Address"] for a in ni.get("Ipv6Addresses", []) if a.get("Ipv6Address")]
        ips += [ni.get("Ipv6Address", "")]
        return [ip for ip in dict.fromkeys(ips) if ip]

    def _resolve_elb_enis(self, enis: Iterable[ENIInfo]) -> None:
        """
        ELB のノードの ENI（説明 `"ELB net/<名前>/<ID>"` / `"ELB app/<名前>/<ID>"`）について、
        どの ELB のものかを特定し、`ENIInfo.load_balancer_name` / `load_balancer_arn` に設定する。

        ALB -> NLB（NLB ノードの IP を ALB の ip TG に登録する構成）で下流の ELB を辿るために使う。
        同じ名前で作り直された ELB を取り違えないよう、名前に加えて ARN 末尾の ID も一致を確認する。

        :param enis: 解決対象の `ENIInfo` のイテラブル（インプレースで更新される）
        """
        parsed = [(eni, _parse_elb_eni_description(eni.description)) for eni in enis]
        targets = [(eni, p) for eni, p in parsed if p is not None]
        if not targets:
            return

        names = list(dict.fromkeys(name for _, (name, _id) in targets))
        raw_lbs: list[dict[str, Any]] = []
        try:
            raw_lbs = self._fetch_raw_load_balancers(names=names)
        except ClientError:
            # 1件でも存在しない名前があると Names 一括照会は失敗するため、1件ずつ照会し直す
            for name in names:
                try:
                    raw_lbs += self._fetch_raw_load_balancers(names=[name])
                except ClientError as exc:
                    # 消えた ELB の ENI が残っているのはふつうのことなので、記録しない
                    if error_code(exc) not in _NOT_FOUND_CODES:
                        note_skipped("log.what.load_balancers", exc, name)

        # (ELB名, ARN末尾のID) をキーにすることで、削除済みの同名ELBの古いENI設定と誤一致させない
        arn_by_name_and_id = {
            (lb.get("LoadBalancerName", ""), lb["LoadBalancerArn"].split("/")[-1]): lb[
                "LoadBalancerArn"
            ]
            for lb in raw_lbs
        }
        for eni, (name, lb_id) in targets:
            arn = arn_by_name_and_id.get((name, lb_id), "")
            if arn:
                eni.load_balancer_name = name
                eni.load_balancer_arn = arn

    # =========================================================================
    # ECS クラスター / サービス / タスク / コンテナインスタンス解決
    # =========================================================================

    def _fetch_ecs_maps(
        self,
        relevant_ips: set[str],
        relevant_instance_ids: set[str],
        relevant_tg_arns: set[str],
    ) -> ECSMaps:
        """
        ECSクラスター・サービス・タスクを走査し、ターゲットから ECS タスクを引くための対応表を作る
        （各対応表の意味は `ECSMaps` を参照）。

        不要なAPIコールを避けるため、対象外のコンテナインスタンスやタスクの走査はスキップする。

        :param relevant_ips: 照合対象のIPターゲット集合
        :param relevant_instance_ids: 照合対象のEC2インスタンスID集合
        :param relevant_tg_arns: 照合対象のターゲットグループARN集合
        :return: `ECSMaps`
        """
        maps = ECSMaps()
        if not self.ecs or not (relevant_ips or relevant_instance_ids or relevant_tg_arns):
            return maps

        def scan(c_arn: str) -> _ECSClusterScan:
            return self._scan_ecs_cluster(c_arn, relevant_ips, relevant_instance_ids)

        # EC2 インスタンス ID -> そのホストのタスクと「hostPort が分かっているか」
        tasks_per_instance: dict[str, list[tuple[ECSTaskInfo, bool]]] = {}
        list_clusters = self.ecs.list_clusters
        with tolerate_aws_errors("log.what.ecs"):
            cluster_arns = self._once(
                "ecs-clusters", lambda: _collect_pages(list_clusters, "clusterArns", "nextToken")
            )
            for c_arn, (services, ci_to_ec2, tasks) in zip(
                cluster_arns, self._parallel_map(scan, cluster_arns), strict=True
            ):
                for svc in services.values():
                    for lb_cfg in svc.get("loadBalancers", []):
                        if lb_cfg.get("targetGroupArn"):
                            maps.by_tg_arn[lb_cfg["targetGroupArn"]] = _ecs_service_info(
                                c_arn, svc, lb_cfg
                            )

                for task in tasks:
                    task_info, task_ips, host_ports = _parse_ecs_task(c_arn, task, services)
                    eni_id = _ecs_task_eni_id(task)
                    for ip in task_ips:
                        # デュアルスタック等で複数IP（IPv4/IPv6）を持つタスクでも、
                        # 照合されたIP自身が private_ip に入るようにして登録する
                        ip_task_info = (
                            task_info
                            if ip == task_info.private_ip
                            else dataclasses.replace(task_info, private_ip=ip)
                        )
                        maps.by_ip.setdefault(ip, {})[eni_id] = ip_task_info
                        canon = canonical_ip(ip)
                        if canon and canon != ip:
                            maps.by_ip.setdefault(canon, {})[eni_id] = ip_task_info

                    ec2_id = ci_to_ec2.get(task.get("containerInstanceArn", ""))
                    if ec2_id:
                        tasks_per_instance.setdefault(ec2_id, []).append(
                            (task_info, bool(host_ports))
                        )
                        for h_port in host_ports:
                            maps.by_instance_port[(ec2_id, h_port)] = task_info

        # 1台のホストで複数タスクが動く場合、インスタンスIDだけでは特定できないため登録しない。
        # hostPort が分かっているタスクも登録しない（ポートの違うターゲットは、同じホストの別の
        # プロセスなので、(ホスト, ポート) で一致したときだけそのタスクにする）
        for ec2_id, task_list in tasks_per_instance.items():
            if len(task_list) == 1 and not task_list[0][1]:
                maps.by_instance[ec2_id] = task_list[0][0]
        return maps

    def _scan_ecs_cluster(
        self, c_arn: str, relevant_ips: set[str], relevant_instance_ids: set[str]
    ) -> _ECSClusterScan:
        """
        1 つのクラスターのサービスと、照合に必要なときだけコンテナインスタンスとタスクを取得する。

        :param c_arn: ECSクラスターのARN
        :param relevant_ips: 照合対象のIPターゲット集合
        :param relevant_instance_ids: 照合対象のEC2インスタンスID集合
        :return: `(サービス, コンテナインスタンス -> EC2 の対応, タスク)`
        """
        # 同じクラスターは、1 回の実行の中で 1 回だけ読む（下流の ELB をたどるときに読み直さない）。
        # サービスは、コンテナインスタンスとタスクを待たずに取り始める
        services = self._start(
            lambda: self._once(("ecs-services", c_arn), lambda: self._fetch_ecs_services(c_arn))
        )
        # コンテナインスタンスは instance ターゲットがある場合だけ照会する
        ci_to_ec2 = (
            self._once(
                ("ecs-container-instances", c_arn),
                lambda: self._fetch_ecs_container_instances(c_arn),
            )
            if relevant_instance_ids
            else {}
        )
        # タスクは IP ターゲットか、調べている EC2 がこのクラスターにあるときだけ照会する
        # （無関係なクラスターのタスクまで読まない）
        needs_tasks = relevant_ips or not relevant_instance_ids.isdisjoint(ci_to_ec2.values())
        tasks = (
            self._once(("ecs-tasks", c_arn), lambda: self._fetch_ecs_tasks(c_arn))
            if needs_tasks
            else []
        )
        return services.result(), ci_to_ec2, tasks

    def _fetch_ecs_services(self, c_arn: str) -> dict[str, dict[str, Any]]:
        """
        クラスター内の全サービスの詳細を取得する（describe_services は1回最大10件）。

        :param c_arn: ECSクラスターのARN
        :return: `{サービス名: describe_services のサービス辞書}`（取得失敗時は取得できた分まで）
        """
        services: dict[str, dict[str, Any]] = {}
        ecs = self.ecs
        if ecs is None:
            return services
        with tolerate_aws_errors("log.what.ecs", _short_arn_name(c_arn)):
            s_arns = _collect_pages(ecs.list_services, "serviceArns", "nextToken", cluster=c_arn)
            for s_chunk in _chunked(s_arns, 10):
                resp = ecs.describe_services(cluster=c_arn, services=s_chunk)
                for svc in resp.get("services", []):
                    services[svc.get("serviceName", "")] = svc
        return services

    def _fetch_ecs_container_instances(self, c_arn: str) -> dict[str, str]:
        """
        クラスター内のコンテナインスタンスと、それが動いている EC2 インスタンスの対応を取得する。

        :param c_arn: ECSクラスターのARN
        :return: `{コンテナインスタンスARN: EC2インスタンスID}`（取得失敗時は取得できた分まで）
        """
        ci_to_ec2: dict[str, str] = {}
        ecs = self.ecs
        if ecs is None:
            return ci_to_ec2
        with tolerate_aws_errors("log.what.ecs", _short_arn_name(c_arn)):
            ci_arns = _collect_pages(
                ecs.list_container_instances,
                "containerInstanceArns",
                "nextToken",
                cluster=c_arn,
            )
            for ci_chunk in _chunked(ci_arns, 100):
                resp = ecs.describe_container_instances(cluster=c_arn, containerInstances=ci_chunk)
                for ci in resp.get("containerInstances", []):
                    if ci.get("containerInstanceArn") and ci.get("ec2InstanceId"):
                        ci_to_ec2[ci["containerInstanceArn"]] = ci["ec2InstanceId"]
        return ci_to_ec2

    def _fetch_ecs_tasks(self, c_arn: str) -> list[dict[str, Any]]:
        """
        クラスター内の実行中タスクの詳細を取得する（describe_tasks は1回最大100件）。

        :param c_arn: ECSクラスターのARN
        :return: describe_tasks のタスク辞書のリスト（取得失敗時は取得できた分まで）
        """
        tasks: list[dict[str, Any]] = []
        ecs = self.ecs
        if ecs is None:
            return tasks
        with tolerate_aws_errors("log.what.ecs", _short_arn_name(c_arn)):
            t_arns = _collect_pages(ecs.list_tasks, "taskArns", "nextToken", cluster=c_arn)
            for t_chunk in _chunked(t_arns, 100):
                tasks += ecs.describe_tasks(cluster=c_arn, tasks=t_chunk).get("tasks", [])
        return tasks

    # =========================================================================
    # AWS Lambda / VPC サブネット / セキュリティグループ / ACM / Route53 解決
    # =========================================================================

    def _fetch_single_lambda_function(self, fn_arn: str) -> tuple[str, LambdaFunctionInfo]:
        """
        単一Lambda関数の詳細（ランタイム、ハンドラー、VPC設定等）を取得する。
        get_function はコードの署名付き URL も返し、参照に必要な以上の権限を求めるので使わない。

        :param fn_arn: 対象Lambda関数のARN
        :return: `(fn_arn, LambdaFunctionInfo)` のタプル（取得失敗時はARN末尾の名前のみの最小情報）
        """
        name_from_arn = _lambda_name_from_arn(fn_arn)
        fallback = (fn_arn, LambdaFunctionInfo(function_name=name_from_arn, function_arn=fn_arn))
        if self.lambda_client is None:
            return fallback
        try:
            cfg = self.lambda_client.get_function_configuration(FunctionName=fn_arn)
            vpc_cfg = cfg.get("VpcConfig", {})
            return fn_arn, LambdaFunctionInfo(
                function_name=cfg.get("FunctionName", name_from_arn),
                function_arn=cfg.get("FunctionArn", fn_arn),
                runtime=cfg.get("Runtime", ""),
                handler=cfg.get("Handler", ""),
                state=cfg.get("State", "Active"),
                last_modified=cfg.get("LastModified", ""),
                description=cfg.get("Description", ""),
                vpc_id=vpc_cfg.get("VpcId", ""),
                subnet_ids=list(vpc_cfg.get("SubnetIds", [])),
                security_group_ids=list(vpc_cfg.get("SecurityGroupIds", [])),
            )
        except AWS_ERRORS as exc:
            note_skipped("log.what.lambda", exc, fn_arn)
            return fallback

    def _fetch_lambda_functions_map(self, lambda_arns: list[str]) -> dict[str, LambdaFunctionInfo]:
        """
        Lambda ARNのリストから `LambdaFunctionInfo` のマップを並列一括取得する。

        :param lambda_arns: 取得対象のLambda関数ARNリスト
        :return: `{lambda_arn: LambdaFunctionInfo}` の辞書
        """
        if not lambda_arns or not self.lambda_client:
            return {}
        unique_arns = list(dict.fromkeys(lambda_arns))
        pairs = self._parallel_map(self._fetch_single_lambda_function, unique_arns)
        return dict(pairs)

    @staticmethod
    def _parse_subnet_dict(sub: dict[str, Any]) -> dict[str, Any]:
        """
        Boto3のSubnet辞書からNameタグ、IPv4/IPv6 CIDR、AZを抽出する。

        :param sub: Boto3のSubnet辞書
        :return: `{"name", "cidr_block", "ipv6_cidr_blocks", "availability_zone", "vpc_id"}`
            を持つ辞書
        """
        ipv6_cidrs = [
            assoc.get("Ipv6CidrBlock", "")
            for assoc in sub.get("Ipv6CidrBlockAssociationSet", [])
            if assoc.get("Ipv6CidrBlock")
        ]
        cidr_block = sub.get("CidrBlock", "") or (ipv6_cidrs[0] if ipv6_cidrs else "")
        return {
            "name": _extract_name_tag(sub.get("Tags")),
            "cidr_block": cidr_block,
            "ipv6_cidr_blocks": ipv6_cidrs,
            "availability_zone": sub.get("AvailabilityZone", ""),
            "vpc_id": sub.get("VpcId", ""),
        }

    def _fetch_subnets_map(
        self,
        subnet_ids: list[str] | None = None,
        vpc_ids: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """
        サブネットIDまたはVPC IDのリストから、各サブネットのNameタグ・CIDR・AZを一括取得する。

        `vpc_ids` を併せて渡すとVPC内の全サブネットを取得できるため、IPターゲットのプライベートIPが
        どのサブネットのCIDRに含まれるかを逆引きする用途（`_match_subnet_for_ip`）にも利用される。

        :param subnet_ids: 取得対象のサブネットIDリスト
        :param vpc_ids: 全サブネットを取得する対象のVPC IDリスト
        :return: `{subnet_id: {"name", "cidr_block", "ipv6_cidr_blocks", "availability_zone",
            "vpc_id"}}` 辞書
        """
        subnet_map: dict[str, dict[str, Any]] = {}
        cache = self._run_cache
        known = cache.subnets if cache is not None else {}

        # VPC の全サブネットを先に取得する（ID で求めるサブネットは、たいていその中にある）
        if vpc_ids:
            unique_vpcs = [v for v in dict.fromkeys(vpc_ids) if v]
            if unique_vpcs:
                subnet_map.update(
                    self._once(
                        ("vpc-subnets", tuple(sorted(unique_vpcs))),
                        lambda: self._fetch_subnets_of_vpcs(unique_vpcs),
                    )
                )
                known.update(subnet_map)

        # SubnetIds= は存在しない ID が1件でも混ざるとチャンク全体が失敗するため、フィルタで照会する
        if subnet_ids:
            unique_ids = [s for s in dict.fromkeys(subnet_ids) if s and s not in subnet_map]
            # 同じ実行の中でもう取得したサブネットは読み直さない
            subnet_map.update({s: known[s] for s in unique_ids if s in known})
            unique_ids = [s for s in unique_ids if s not in known]
            for chunk in _chunked(unique_ids, 100):
                try:
                    subnets = _collect_pages(
                        self.ec2.describe_subnets,
                        "Subnets",
                        Filters=[{"Name": "subnet-id", "Values": chunk}],
                    )
                except AWS_ERRORS as exc:
                    note_skipped("log.what.subnets", exc)
                    continue
                for sub in subnets:
                    s_id = sub.get("SubnetId", "")
                    if s_id:
                        subnet_map[s_id] = known[s_id] = self._parse_subnet_dict(sub)

        return subnet_map

    def _fetch_subnets_of_vpcs(self, vpc_ids: list[str]) -> dict[str, dict[str, Any]]:
        """
        VPC の全サブネットを取得する。

        :param vpc_ids: VPC ID のリスト（重複なし）
        :return: `{subnet_id: _parse_subnet_dict の辞書}`（取得できなければ空）
        """
        subnet_map: dict[str, dict[str, Any]] = {}
        with tolerate_aws_errors("log.what.subnets"):
            vpc_subnets = _collect_pages(
                self.ec2.describe_subnets,
                "Subnets",
                Filters=[{"Name": "vpc-id", "Values": vpc_ids}],
            )
            for sub in vpc_subnets:
                s_id = sub.get("SubnetId", "")
                if s_id:
                    subnet_map[s_id] = self._parse_subnet_dict(sub)
        return subnet_map

    @staticmethod
    def _sg_port_range(perm: dict[str, Any]) -> str:
        """
        SG のルールのポート範囲の表示を作る。

        ICMP の FromPort / ToPort はポートではなくタイプとコード（-1 は全部）なので、範囲の形に
        しない（`8--1` や、範囲に見える `3-4` にならないように）。

        :param perm: Boto3が返すIpPermissionsの要素
        :return: `"443"`、`"80-8080"`、`"ALL"`、ICMP は `"8"`、`"3/4"`
        """
        from_port = perm.get("FromPort")
        to_port = perm.get("ToPort")
        if perm.get("IpProtocol", "-1") == "-1" or from_port is None or from_port == -1:
            return "ALL"
        if perm.get("IpProtocol", "").lower() in _ICMP_PROTOCOLS:
            return str(from_port) if to_port in (None, -1) else f"{from_port}/{to_port}"
        return str(from_port) if from_port == to_port else f"{from_port}-{to_port}"

    @staticmethod
    def _parse_sg_permissions(perms: list[dict[str, Any]]) -> list[SecurityGroupRuleSummary]:
        """
        EC2 セキュリティグループの `IpPermissions` / `IpPermissionsEgress` を
        `SecurityGroupRuleSummary` のリストに変換する。

        IPv4 CIDR・IPv6 CIDR・SG参照（UserIdGroupPairs）・プレフィックスリスト（PrefixListIds）の
        すべてをフラットなルール行として展開する。

        :param perms: Boto3が返すIpPermissions形式の辞書リスト
        :return: `SecurityGroupRuleSummary` のリスト
        """
        rules: list[SecurityGroupRuleSummary] = []
        for perm in perms:
            proto = perm.get("IpProtocol", "-1")
            # IpProtocol == "-1" は全プロトコル・全ポート許可を意味する
            proto_str = "ALL" if proto == "-1" else proto.upper()
            port_str = AWSFetcherMixin._sg_port_range(perm)

            peers: list[tuple[str, str]] = []
            peers += [
                (r.get("CidrIp", ""), r.get("Description", "")) for r in perm.get("IpRanges", [])
            ]
            peers += [
                (r.get("CidrIpv6", ""), r.get("Description", ""))
                for r in perm.get("Ipv6Ranges", [])
            ]
            peers += [
                (r.get("GroupId", ""), r.get("Description", ""))
                for r in perm.get("UserIdGroupPairs", [])
            ]
            # プレフィックスリスト（CloudFront のマネージドプレフィックスリスト等）
            peers += [
                (r.get("PrefixListId", ""), r.get("Description", ""))
                for r in perm.get("PrefixListIds", [])
            ]
            for peer, desc in peers:
                rules.append(
                    SecurityGroupRuleSummary(
                        protocol=proto_str,
                        port_range=port_str,
                        source_or_dest=peer,
                        description=desc,
                    )
                )
        return rules

    def _fetch_security_groups_map(self, sg_ids: list[str]) -> dict[str, SecurityGroupInfo]:
        """
        セキュリティグループIDのリストからインバウンド/アウトバウンドルールを含む情報を一括取得する。

        :param sg_ids: 取得対象のセキュリティグループIDリスト
        :return: `{group_id: SecurityGroupInfo}` の辞書
        """
        if not sg_ids:
            return {}

        unique_ids = [s for s in dict.fromkeys(sg_ids) if s]
        # 同じ実行の中でもう取得した SG は読み直さない
        cache = self._run_cache
        known = cache.security_groups if cache is not None else {}
        sg_map: dict[str, SecurityGroupInfo] = {s: known[s] for s in unique_ids if s in known}
        unique_ids = [s for s in unique_ids if s not in known]
        # GroupIds= は削除済みの ID が1件でも混ざるとチャンク全体が失敗するため、フィルタで照会する
        for chunk in _chunked(unique_ids, 100):
            try:
                groups = _collect_pages(
                    self.ec2.describe_security_groups,
                    "SecurityGroups",
                    Filters=[{"Name": "group-id", "Values": chunk}],
                )
            except AWS_ERRORS as exc:
                note_skipped("log.what.security_groups", exc)
                continue

            for sg in groups:
                g_id = sg.get("GroupId", "")
                if not g_id:
                    continue
                sg_map[g_id] = known[g_id] = SecurityGroupInfo(
                    group_id=g_id,
                    group_name=sg.get("GroupName", ""),
                    description=sg.get("Description", ""),
                    inbound_rules=self._parse_sg_permissions(sg.get("IpPermissions", [])),
                    outbound_rules=self._parse_sg_permissions(sg.get("IpPermissionsEgress", [])),
                )
        return sg_map

    def _fetch_single_certificate(self, c_arn: str) -> tuple[str, CertificateInfo]:
        """
        単一ACM証明書の詳細（ドメイン名、SAN、ステータス、有効期限等）を取得する。

        :param c_arn: 対象ACM証明書のARN
        :return: `(c_arn, CertificateInfo)` のタプル
        """
        # IAM のサーバー証明書は ACM では引けない。失敗ではないので、呼ばずに ARN だけを返す
        if not is_arn_of(c_arn, "acm"):
            return c_arn, CertificateInfo(certificate_arn=c_arn)
        try:
            resp = self.acm.describe_certificate(CertificateArn=c_arn)
            c = resp.get("Certificate", {})
            return c_arn, CertificateInfo(
                certificate_arn=c_arn,
                domain_name=c.get("DomainName", ""),
                subject_alternative_names=c.get("SubjectAlternativeNames", []),
                status=c.get("Status", ""),
                not_after=_format_datetime(c.get("NotAfter")),
                in_use=bool(c.get("InUseBy")),
            )
        except AWS_ERRORS as exc:
            note_skipped("log.what.certificate", exc, c_arn)
            return c_arn, CertificateInfo(certificate_arn=c_arn)

    def _fetch_certificates_map(self, cert_arns: list[str]) -> dict[str, CertificateInfo]:
        """
        ACM証明書ARNのリストからドメイン名・有効期限等の詳細情報を並列一括取得する。

        :param cert_arns: 取得対象のACM証明書ARNリスト
        :return: `{certificate_arn: CertificateInfo}` の辞書
        """
        if not cert_arns:
            return {}
        unique_arns = list(dict.fromkeys(cert_arns))
        pairs = self._parallel_map(self._fetch_single_certificate, unique_arns)
        return dict(pairs)

    def _fetch_zone_records(
        self, hz: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """
        単一Route53ホストゾーンのレコードセット一覧をページネーションしながら取得する。

        :param hz: `list_hosted_zones` が返すHostedZone辞書
        :return: `(hz, ResourceRecordSetsリスト)` のタプル
        """
        zone_id = hz.get("Id", "").split("/")[-1]
        records: list[dict[str, Any]] = []
        if self.route53 is None:
            return hz, records
        try:
            paginator = self.route53.get_paginator("list_resource_record_sets")
            for page in paginator.paginate(HostedZoneId=zone_id):
                records.extend(page.get("ResourceRecordSets", []))
        except AWS_ERRORS as exc:
            note_skipped("log.what.route53", exc, zone_id)
            # ホスト名から ELB を探す側が、「レコードがない」と「読めなかった」を
            # 見分けられるように残す
            failures = self._route53_zone_failures
            if failures is not None:
                failures[zone_id] = exc
        return hz, records

    def _get_route53_zone_records_cached(
        self,
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """
        全ホストゾーンとレコードセットを取得し、短時間（`ROUTE53_CACHE_TTL_SECONDS`）キャッシュする。
        NLB -> ALB のような再帰ハイドレーション時に全ゾーンを何度も走査しないための最適化。

        :return: `(HostedZone辞書, ResourceRecordSetsリスト)` のタプルリスト
        """
        with self._route53_lock:
            now = time.monotonic()
            cache = self._route53_zone_cache
            if cache is not None and now - cache[0] < ROUTE53_CACHE_TTL_SECONDS:
                return cache[1]

            if self.route53 is None:
                return []
            zones = self._fetch_hosted_zones()
            self._route53_zone_failures = {}
            zone_results = self._parallel_map(self._fetch_zone_records, zones)
            self._route53_zone_cache = (now, zone_results)
            return zone_results

    def _fetch_hosted_zones(self) -> list[dict[str, Any]]:
        """
        Route 53 のホストゾーンの一覧を取得する（レコードは読まない）。1 回の実行の中では 1 回
        だけ読む（`_prefetch_hosted_zones` で先に読み始めたものも、ここで受け取る）。

        :return: `list_hosted_zones` が返すHostedZone辞書のリスト
        """
        return self._once("route53-zones", self._list_hosted_zones)

    def _list_hosted_zones(self) -> list[dict[str, Any]]:
        """`list_hosted_zones` をページネーションで最後まで読む。"""
        zones: list[dict[str, Any]] = []
        if self.route53 is None:
            return zones
        paginator = self.route53.get_paginator("list_hosted_zones")
        for page in paginator.paginate():
            zones.extend(page.get("HostedZones", []))
        return zones

    def _prefetch_hosted_zones(self) -> None:
        """
        Route 53 のゾーン一覧を、ほかの取得を待たずに読み始める。

        Route 53 のエンドポイントは 1 か所（米国）にしかなく、最初の呼び出しに時間がかかる。
        ELB の一覧を待ってから読み始めると、その時間がそのまま全体の待ち時間に足されるので、
        Route 53 を使うことが分かっているときは、ELB の取得と並行して始めておく。読むのは
        ゾーンの一覧だけで、レコードは ELB が決まってから読む。失敗は、あとで
        `_fetch_hosted_zones` を呼んだところに同じ例外で伝わる。
        """
        if self.route53 is not None and self._run_cache is not None:
            self._start(self._fetch_hosted_zones)

    def _fetch_route53_zones_for_host(
        self, host: str
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """
        `host` の名前のレコードを持ちうるゾーンだけについて、レコードセットを取得する。

        レコードの名前はゾーンの名前で終わるので、`host`（ワイルドカードのレコードも含む）に
        一致するレコードは、名前が `host` と同じか `host` の親のドメインのゾーンにしかない。
        ゾーンの多いアカウントで、ホスト名 1 つのために全ゾーンのレコードを読まないため。

        :param host: 探すホスト名（正規化済みの小文字）
        :return: `(HostedZone辞書, ResourceRecordSetsリスト)` のタプルリスト
        """
        zones = []
        for hz in self._fetch_hosted_zones():
            zone_name = _decode_route53_name(hz.get("Name", "")).lower()
            if host == zone_name or host.endswith(f".{zone_name}"):
                zones.append(hz)
        self._route53_zone_failures = {}
        return self._parallel_map(self._fetch_zone_records, zones)

    def _fetch_route53_records_by_dns(
        self,
        lb_dns_names: list[str],
        zone_results: list[tuple[dict[str, Any], list[dict[str, Any]]]] | None = None,
    ) -> dict[str, list[Route53RecordInfo]]:
        """
        ELBのDNS名リストに対し、Route 53のAliasレコードおよびCNAME/Aレコードを逆引きして
        `{lb_dns_name: list[Route53RecordInfo]}` のマップを返す。

        :param lb_dns_names: 逆引き対象のELB DNS名リスト
        :param zone_results: 取得済みのゾーンとレコード（省略時は全ゾーンを走査する）
        :return: `{lb_dns_name: Route53RecordInfoのリスト}` の辞書
        """
        if not lb_dns_names or not self.route53:
            return {}

        # 正規化後のDNS名から元のDNS名リストを引けるようにしておく
        norm_to_orig: dict[str, list[str]] = {}
        for orig_dns in dict.fromkeys(lb_dns_names):
            if orig_dns:
                norm = _normalize_dns_name(orig_dns)
                norm_to_orig.setdefault(norm, []).append(orig_dns)

        if not norm_to_orig:
            return {}

        result: dict[str, list[Route53RecordInfo]] = {dns: [] for dns in lb_dns_names if dns}
        with tolerate_aws_errors("log.what.route53"):
            if zone_results is None:
                zone_results = self._get_route53_zone_records_cached()

            for hz, rrs_list in zone_results:
                zone_id = hz.get("Id", "").split("/")[-1]
                zone_name = _decode_route53_name(hz.get("Name", ""))
                is_private = bool(hz.get("Config", {}).get("PrivateZone", False))

                for rrs in rrs_list:
                    r_type = rrs.get("Type", "")
                    if r_type not in ("A", "AAAA", "CNAME"):
                        continue
                    # Alias レコード（A / AAAA Alias）は向き先の DNS 名、
                    # 通常のレコード（CNAME 等）は値を、ELB の DNS 名と照合する
                    alias = rrs.get("AliasTarget")
                    targets: list[tuple[str, dict[str, Any] | None]] = (
                        [(alias["DNSName"], alias)] if alias and alias.get("DNSName") else []
                    )
                    targets += [
                        (rr["Value"], None)
                        for rr in rrs.get("ResourceRecords", [])
                        if rr.get("Value")
                    ]
                    for target_dns, via_alias in targets:
                        for orig_dns in norm_to_orig.get(_normalize_dns_name(target_dns), []):
                            result.setdefault(orig_dns, []).append(
                                Route53RecordInfo(
                                    zone_id=zone_id,
                                    zone_name=zone_name,
                                    record_name=_decode_route53_name(rrs.get("Name", "")),
                                    record_type=r_type,
                                    is_alias=via_alias is not None,
                                    private_zone=is_private,
                                    ttl=None if via_alias is not None else rrs.get("TTL"),
                                    evaluate_target_health=bool(
                                        via_alias and via_alias.get("EvaluateTargetHealth", False)
                                    ),
                                )
                            )

        return result
