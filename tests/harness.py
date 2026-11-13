"""
テストと playground で共通に使うハーネス。

- tests/data/infra.yaml を読み、moto（インメモリの擬似AWS）上に構成を作る
- YAML（tests/casebook_elbx_ja.yaml / _en.yaml）の states（ヘルス状態）を
  describe_target_health に反映する
- ランダムな ID や日時を読みやすい名前に置き換える対応表を持つ
  （ゴールデンテストでは pytest-casebook に渡し、playground では自分で置き換える）
"""

from __future__ import annotations

import contextlib
import copy
import io
import ipaddress
import os
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
import yaml
from moto import mock_aws

from elbx import ELBClient

TESTS_DIR = Path(__file__).resolve().parent
DATA_DIR = TESTS_DIR / "data"


# =============================================================================
# YAML の読み込み
# =============================================================================


def load_yaml(file_name: str) -> dict[str, Any]:
    """
    `tests/data` 配下の YAML ファイルを読み込んで辞書として返す。

    :param file_name: `tests/data` ディレクトリ内のファイル名
    :return: パースされた YAML の辞書
    """
    with (DATA_DIR / file_name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_infra() -> dict[str, Any]:
    """
    AWS 構成定義（`tests/data/infra.yaml`）を読み込んで返す。

    :return: infra.yaml のデータ辞書
    """
    return load_yaml("infra.yaml")


def for_real_aws(infra: dict[str, Any]) -> dict[str, Any]:
    """
    実際の AWS で作る構成を返す（`moto_only: true` と書いたリソースを除く）。

    今は moto 専用のリソースはない。IPv6 の構成も実際の AWS で作る（VPC の IPv6 CIDR は Amazon が
    割り当てるので、infra.yaml の文書用アドレスは「割り当てられた /56 の中の同じ位置」に読み替える。
    `ipv6_offset` / `TestEnv.command_arg`）。実際の AWS で作れない構成を足すときのために残してある。

    :param infra: infra.yaml のデータ辞書
    :return: moto 専用の部分を除いた infra のコピー
    """
    result = copy.deepcopy(infra)
    for key, value in result.items():
        if isinstance(value, list):
            result[key] = [v for v in value if not (isinstance(v, dict) and v.get("moto_only"))]
    for zone in result.get("route53_zones", []):
        zone["records"] = [r for r in zone.get("records", []) if not r.get("moto_only")]
    return result


def ipv6_offset(infra: dict[str, Any], value: str) -> int:
    """
    infra.yaml の IPv6 のアドレスか CIDR が、VPC の IPv6 CIDR（`vpc_ipv6_cidr`）の先頭から
    どれだけ離れているかを返す。実際の AWS では、割り当てられた /56 の同じ位置に読み替える。

    :param infra: infra.yaml のデータ辞書
    :param value: IPv6 のアドレス（`2001:db8:1:1::31`）か CIDR（`2001:db8:1:1::/64`）
    :return: VPC の IPv6 CIDR の先頭からの差
    :raises ValueError: VPC の IPv6 CIDR の外のアドレスの場合
    """
    vpc = ipaddress.IPv6Network(infra["vpc_ipv6_cidr"])
    address = ipaddress.IPv6Interface(value).ip
    if address not in vpc:
        raise ValueError(f"infra.yaml: {value} は vpc_ipv6_cidr（{vpc}）の外です")
    return int(address) - int(vpc.network_address)


def instance_sg_keys(infra: dict[str, Any], inst: dict[str, Any]) -> list[str]:
    """
    EC2 インスタンスに付ける SG の key を返す（`security_groups` がなければ先頭の SG）。

    :param infra: infra.yaml のデータ辞書
    :param inst: infra.yaml の EC2 インスタンスの定義
    :return: SG の key のリスト
    """
    return list(inst.get("security_groups") or [infra["security_groups"][0]["key"]])


# =============================================================================
# 外部公開の方針チェック（実際の AWS で作る前に必ず確認する）
# =============================================================================


def check_exposure_policy(infra: dict[str, Any]) -> list[str]:
    """
    `infra.yaml` が外部公開のセキュリティ方針を守っているかを確認し、違反の一覧を返す。

    - `internet-facing` またはデュアルスタックの ELB には必ずセキュリティグループが付いていること
    - グローバル IP を持つリソース（`internet-facing` またはデュアルスタックの ELB、
      パブリック IP 付きの ECS タスク、IPv6 アドレスを持つ EC2）の SG は、
      VPC の CIDR（IPv4 は `vpc_cidr`、IPv6 は `vpc_ipv6_cidr`）以外からの
      インバウンドを許可しないこと
    EC2 にパブリック IPv4 を付けないことは、テンプレート側で常に強制している。

    :param infra: infra.yaml のデータ辞書
    :return: 違反メッセージのリスト（問題なければ空リスト）
    """
    vpc = ipaddress.ip_network(infra["vpc_cidr"])
    vpc_v6 = ipaddress.ip_network(infra["vpc_ipv6_cidr"]) if infra.get("vpc_ipv6_cidr") else None
    sgs = {sg["key"]: sg for sg in infra["security_groups"]}
    violations: list[str] = []

    public_sg_keys: dict[str, str] = {}
    for lb in infra["load_balancers"]:
        # IPv6 アドレスはグローバルアドレスのため、デュアルスタックの ELB も外部公開として扱う
        if lb.get("scheme") != "internet-facing" and lb.get("ip_address_type") != "dualstack":
            continue
        if not lb.get("security_groups"):
            violations.append(
                f"internet-facing またはデュアルスタックの ELB '{lb['name']}' に SG がありません"
            )
        for key in lb.get("security_groups", []):
            public_sg_keys[key] = f"ELB '{lb['name']}'"
    # ECS タスク（イメージ取得のためパブリック IP を付ける）と EC2 の SG は security_groups の先頭
    first_sg = infra["security_groups"][0]["key"]
    if infra.get("ecs_clusters"):
        public_sg_keys.setdefault(first_sg, "ECS タスク")
    for inst in infra["ec2_instances"]:
        if inst.get("ipv6_ips"):
            for key in instance_sg_keys(infra, inst):
                public_sg_keys.setdefault(key, f"IPv6 アドレスを持つ EC2 '{inst['name']}'")

    for key, owner in public_sg_keys.items():
        for rule in sgs[key].get("ingress", []):
            if "ipv6" in rule and not (
                vpc_v6 and ipaddress.ip_network(rule["ipv6"]).subnet_of(vpc_v6)
            ):
                violations.append(
                    f"SG '{sgs[key]['name']}'（{owner}）が VPC 外の IPv6 {rule['ipv6']} "
                    "を許可しています"
                )
            if "cidr" in rule and not ipaddress.ip_network(rule["cidr"]).subnet_of(vpc):
                violations.append(
                    f"SG '{sgs[key]['name']}'（{owner}）が VPC 外の {rule['cidr']} を許可しています"
                )
    return violations


# =============================================================================
# moto 環境の構築
# =============================================================================


@dataclass
class TestEnv:
    """構築した環境（moto または実際の AWS）と、論理 key -> 実際の AWS リソース ID の対応表。"""

    __test__ = False  # pytest にテストクラスとして収集させない

    session: Any
    region: str
    infra: dict[str, Any]
    vpc_id: str = ""
    subnet_ids: dict[str, str] = field(default_factory=dict)
    sg_ids: dict[str, str] = field(default_factory=dict)
    cert_arns: dict[str, str] = field(default_factory=dict)
    instance_ids: dict[str, str] = field(default_factory=dict)
    eni_ids: dict[str, str] = field(default_factory=dict)
    lambda_arns: dict[str, str] = field(default_factory=dict)
    tg_arns: dict[str, str] = field(default_factory=dict)
    lb_arns: dict[str, str] = field(default_factory=dict)
    zone_ids: dict[str, str] = field(default_factory=dict)
    # 実際の AWS 用の追加の置き換え
    # （アカウント ID、DNS 名、ECS タスク、名前の prefix / suffix など）
    extra_placeholders: list[tuple[str, str]] = field(default_factory=list)
    extra_patterns: list[tuple[re.Pattern[str], str]] = field(default_factory=list)
    # ケースのコマンドに書いた元の名前 -> 実際の名前（実際の AWS で prefix / suffix を付けたとき）
    command_names: dict[str, str] = field(default_factory=dict)
    # 実際の AWS で Amazon が割り当てた VPC の IPv6 CIDR（moto では空。infra.yaml のアドレスのまま）
    real_vpc_ipv6_cidr: str = ""

    def real_ipv6(self, value: str) -> str:
        """
        infra.yaml の IPv6 のアドレスか CIDR を、実際の AWS のもの（割り当てられた /56 の同じ
        位置）に読み替える。moto では、そのまま返す。

        :param value: infra.yaml の IPv6 のアドレスか CIDR
        :return: この環境でのアドレスか CIDR（省略した小文字の表記）
        """
        if not self.real_vpc_ipv6_cidr:
            return value
        base = int(ipaddress.IPv6Network(self.real_vpc_ipv6_cidr).network_address)
        address = ipaddress.IPv6Address(base + ipv6_offset(self.infra, value))
        _, slash, prefix = value.partition("/")
        return f"{address}{slash}{prefix}"

    def command_arg(self, arg: str) -> str:
        """
        ケースのコマンドの引数 1 つを、この環境で実行する形にする。実際の AWS では、リソースの名前に
        prefix / suffix を付け、infra.yaml の IPv6 アドレスを実際のアドレスに読み替える（省略しない
        大文字の書き方は、その書き方のまま）。moto では、そのまま返す。

        :param arg: ケースのコマンドに書いた引数
        :return: 実行するときの引数
        """
        if arg in self.command_names:
            return self.command_names[arg]
        if not self.real_vpc_ipv6_cidr:
            return arg
        try:
            written = ipaddress.IPv6Address(arg)
            real = ipaddress.IPv6Address(self.real_ipv6(str(written)))
        except ValueError:  # IPv6 アドレスではない、または VPC の外のアドレス
            return arg
        return str(real) if arg == str(written) else real.exploded.upper()

    def _ipv6_placeholders(self) -> list[tuple[str, str]]:
        """
        実際の AWS の IPv6 を、`<ipv6:public_1a>::31`・`<vpc-ipv6-cidr>` の表記にする置き換えを返す
        （Amazon が割り当てるプレフィックスは、環境を作るたびに変わるため）。
        """
        if not self.real_vpc_ipv6_cidr:
            return []
        pairs = [(self.real_vpc_ipv6_cidr, "<vpc-ipv6-cidr>")]
        for sub in self.infra["subnets"]:
            if sub.get("ipv6_cidr"):
                network = ipaddress.IPv6Network(self.real_ipv6(sub["ipv6_cidr"]))
                # "2406:da14:e5a:6f01::" の末尾の "::" を除いた部分が、その /64 の中の
                # 全アドレスの書き出しになる
                prefix = str(network.network_address).removesuffix("::")
                pairs.append((f"{prefix}:", f"<ipv6:{sub['key']}>:"))
        return pairs

    def client(self) -> ELBClient:
        """
        このテスト環境に接続する新しい ELBClient インスタンスを生成して返す。

        :return: 新しい ELBClient インスタンス
        """
        return ELBClient(region_name=self.region, session=self.session)

    def placeholders(self) -> list[tuple[str, str]]:
        """
        出力中の動的リソース ID を論理名プレースホルダに置き換えるための
        `(実ID, 置換後文字列)` の一覧を返す。

        :return: 実IDの文字列長の降順でソートされた `(実ID, 置換後文字列)` タプルのリスト
        """
        pairs: list[tuple[str, str]] = []
        if self.vpc_id:
            pairs.append((self.vpc_id, "<vpc>"))
        for prefix, mapping in (
            ("subnet", self.subnet_ids),
            ("sg", self.sg_ids),
            ("i", self.instance_ids),
            ("eni", self.eni_ids),
            ("tg-arn", self.tg_arns),
            ("lb-arn", self.lb_arns),
            ("zone", self.zone_ids),
        ):
            pairs += [(real, f"<{prefix}:{key}>") for key, real in mapping.items()]
        for key, arn in self.cert_arns.items():
            pairs.append((arn, f"<cert-arn:{key}>"))
            pairs.append((arn.split("/")[-1], f"<cert:{key}>"))
        # ELB の ID（ARN の末尾）。ELB の ENI の説明 "ELB net/<名前>/<ID>" などに現れる
        pairs += [(arn.split("/")[-1], f"<lb-id:{key}>") for key, arn in self.lb_arns.items()]
        pairs += [(real, repl) for real, repl in self.extra_placeholders if real and real != repl]
        pairs += self._ipv6_placeholders()
        # 部分一致で短い ID が先に置き換わらないよう、長いものから置き換える
        return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def _create_dummy_lambda_zip() -> bytes:
    """
    moto の Lambda 関数作成に渡す最小限のインメモリ ZIP アーカイブを生成する。

    :return: `index.py` を含む ZIP ファイルのバイト列
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.py", "def handler(event, context):\n    return {'statusCode': 200}\n")
    return buf.getvalue()


def _ip_permission(rule: dict[str, Any]) -> dict[str, Any]:
    """
    `infra.yaml` のセキュリティグループ ingress 定義1件を EC2 API の `IpPermission` 形式に変換する。

    :param rule: infra.yaml の ingress ルール辞書
    :return: EC2 `authorize_security_group_ingress` 用の IpPermission 辞書
    """
    proto = str(rule["protocol"]).lower()
    perm: dict[str, Any] = {"IpProtocol": "-1" if proto == "all" else proto}
    if proto != "all":
        low, _, high = str(rule["port"]).partition("-")
        perm["FromPort"] = int(low)
        perm["ToPort"] = int(high or low)
    desc = rule.get("description", "")
    if "cidr" in rule:
        perm["IpRanges"] = [{"CidrIp": rule["cidr"], "Description": desc}]
    if "ipv6" in rule:
        perm["Ipv6Ranges"] = [{"CidrIpv6": rule["ipv6"], "Description": desc}]
    return perm


_VALUE_CONDITIONS = {
    "host-header": "HostHeaderConfig",
    "path-pattern": "PathPatternConfig",
    "http-request-method": "HttpRequestMethodConfig",
    "source-ip": "SourceIpConfig",
}


def _rule_conditions(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """
    `infra.yaml` の簡略なルール条件定義を ELBv2 API の `Conditions` リスト形式に変換する。

    :param spec: infra.yaml のルール条件辞書
    :return: ELBv2 `create_rule` 用の Conditions リスト
    :raises ValueError: 未対応のルール条件フィールドが指定された場合
    """
    conditions: list[dict[str, Any]] = []
    for field_name, value in spec.items():
        if field_name in _VALUE_CONDITIONS:
            conditions.append(
                {"Field": field_name, _VALUE_CONDITIONS[field_name]: {"Values": list(value)}}
            )
        elif field_name == "http-header":
            conditions.append(
                {
                    "Field": field_name,
                    "HttpHeaderConfig": {
                        "HttpHeaderName": value["name"],
                        "Values": list(value["values"]),
                    },
                }
            )
        elif field_name == "query-string":
            pairs = [
                {"Value": q["value"], **({"Key": q["key"]} if "key" in q else {})} for q in value
            ]
            conditions.append({"Field": field_name, "QueryStringConfig": {"Values": pairs}})
        else:
            raise ValueError(f"infra.yaml: 未対応のルール条件です: {field_name}")
    return conditions


def _pascal_keys(spec: dict[str, Any]) -> dict[str, Any]:
    """
    `status_code` -> `StatusCode` のように snake_case キーを ELBv2 API 用の PascalCase に変換する。

    :param spec: 変換元の辞書
    :return: キーを PascalCase、値を文字列に変換した辞書
    """
    return {"".join(p.capitalize() for p in k.split("_")): str(v) for k, v in spec.items()}


def _rule_action(spec: dict[str, Any], tg_arns: dict[str, Any]) -> dict[str, Any]:
    """
    `infra.yaml` のアクション定義を ELBv2 API / CloudFormation の `Action` 形式に変換する。

    :param spec: infra.yaml のアクション定義辞書
        （`forward`, `forward_weighted`, `redirect`, `fixed_response`）
    :param tg_arns: TG の論理 key -> TG ARN（または CloudFormation の `{"Ref": ...}`）のマップ
    :return: ELBv2 Action 辞書
    :raises ValueError: 未対応のアクション種別が指定された場合
    """
    ((kind, body),) = spec.items()
    if kind == "forward":
        return {"Type": "forward", "TargetGroupArn": tg_arns[body]}
    if kind == "forward_weighted":
        return {
            "Type": "forward",
            "ForwardConfig": {
                "TargetGroups": [
                    {"TargetGroupArn": tg_arns[tg_key], "Weight": weight}
                    for tg_key, weight in body.items()
                ]
            },
        }
    if kind == "redirect":
        return {"Type": "redirect", "RedirectConfig": _pascal_keys(body)}
    if kind == "fixed_response":
        return {"Type": "fixed-response", "FixedResponseConfig": _pascal_keys(body)}
    if kind == "authenticate_oidc":
        return {"Type": "authenticate-oidc", "AuthenticateOidcConfig": _pascal_keys(body)}
    raise ValueError(f"infra.yaml: 未対応のアクションです: {kind}")


def action_specs(spec: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    `infra.yaml` の `action` / `default_action`（1 つの辞書か、順に行うアクションのリスト）を、
    アクション定義のリストにそろえる。

    :param spec: infra.yaml のアクション定義
    :return: アクション定義のリスト（書いた順）
    """
    return list(spec) if isinstance(spec, list) else [spec]


def _rule_actions(
    spec: dict[str, Any] | list[dict[str, Any]], tg_arns: dict[str, Any]
) -> list[dict[str, Any]]:
    """
    `infra.yaml` のアクション定義を、ELBv2 API / CloudFormation の `Actions` にする。
    複数のアクションには、書いた順に `Order` を付ける（認証してから転送する、など）。

    :param spec: infra.yaml のアクション定義（1 つの辞書か、リスト）
    :param tg_arns: TG の論理 key -> TG ARN（または CloudFormation の `{"Ref": ...}`）のマップ
    :return: ELBv2 Action 辞書のリスト
    """
    actions = [_rule_action(one, tg_arns) for one in action_specs(spec)]
    if len(actions) > 1:
        for order, action in enumerate(actions, start=1):
            action["Order"] = order
    return actions


def _build_environment(session: Any, infra: dict[str, Any]) -> TestEnv:
    """
    `infra.yaml` の定義に従って moto 上に全 AWS リソースを構築し、`TestEnv` を返す。

    :param session: moto コンテキスト内の boto3.Session
    :param infra: infra.yaml のデータ辞書
    :return: 構築されたリソース ID 対応表を持つ TestEnv インスタンス
    """
    region = infra["region"]
    env = TestEnv(session=session, region=region, infra=infra)
    ec2 = session.client("ec2", region_name=region)
    elbv2 = session.client("elbv2", region_name=region)

    # 1. VPC / サブネット / セキュリティグループ
    env.vpc_id = ec2.create_vpc(CidrBlock=infra["vpc_cidr"])["Vpc"]["VpcId"]
    # VPC 作成時に自動で作られる default SG（SG を指定しない ENI に付く）も ID の置き換え対象にする
    for sg in ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [env.vpc_id]},
            {"Name": "group-name", "Values": ["default"]},
        ]
    )["SecurityGroups"]:
        env.sg_ids["default"] = sg["GroupId"]
    for sub in infra["subnets"]:
        sub_kwargs: dict[str, Any] = {
            "VpcId": env.vpc_id,
            "AvailabilityZone": sub["az"],
            "TagSpecifications": [
                {"ResourceType": "subnet", "Tags": [{"Key": "Name", "Value": sub["name"]}]}
            ],
        }
        if sub.get("cidr"):
            sub_kwargs["CidrBlock"] = sub["cidr"]
        if sub.get("ipv6_cidr"):
            sub_kwargs["Ipv6CidrBlock"] = sub["ipv6_cidr"]
            # IPv4 の cidr がなければ IPv6 専用のサブネットとして作る
            sub_kwargs["Ipv6Native"] = not sub.get("cidr")
        resp = ec2.create_subnet(**sub_kwargs)
        env.subnet_ids[sub["key"]] = resp["Subnet"]["SubnetId"]

    for sg in infra["security_groups"]:
        sg_id = ec2.create_security_group(
            GroupName=sg["name"], Description=sg["description"], VpcId=env.vpc_id
        )["GroupId"]
        env.sg_ids[sg["key"]] = sg_id
        if sg.get("ingress"):
            ec2.authorize_security_group_ingress(
                GroupId=sg_id, IpPermissions=[_ip_permission(r) for r in sg["ingress"]]
            )

    # 2. ACM 証明書
    acm = session.client("acm", region_name=region)
    for cert in infra["certificates"]:
        env.cert_arns[cert["key"]] = acm.request_certificate(
            DomainName=cert["domain_name"],
            SubjectAlternativeNames=cert["san"],
            ValidationMethod="DNS",
        )["CertificateArn"]

    # 3. EC2 インスタンス（security_groups を書かなければ、先頭の SG を付ける）
    default_sg = env.sg_ids[infra["security_groups"][0]["key"]]
    for inst in infra["ec2_instances"]:
        resp = ec2.run_instances(
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            InstanceType=inst["instance_type"],
            SubnetId=env.subnet_ids[inst["subnet"]],
            PrivateIpAddress=inst["private_ip"],
            SecurityGroupIds=[env.sg_ids[k] for k in instance_sg_keys(infra, inst)],
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": inst["name"]}]}
            ],
        )
        instance = resp["Instances"][0]
        env.instance_ids[inst["key"]] = instance["InstanceId"]
        for ni in instance.get("NetworkInterfaces", []):
            env.eni_ids[inst["key"]] = ni["NetworkInterfaceId"]
        # moto の run_instances は IPv6 アドレスを指定できないため、起動後に ENI へ割り当てる
        if inst.get("ipv6_ips"):
            ec2.assign_ipv6_addresses(
                NetworkInterfaceId=env.eni_ids[inst["key"]], Ipv6Addresses=list(inst["ipv6_ips"])
            )

    # 4. Lambda 関数
    iam = session.client("iam", region_name=region)
    role_arn = iam.create_role(
        RoleName="lambda-role",
        AssumeRolePolicyDocument=(
            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
            '"Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
        ),
    )["Role"]["Arn"]
    lambda_client = session.client("lambda", region_name=region)
    zip_bytes = _create_dummy_lambda_zip()
    for fn in infra.get("lambda_functions", []):
        fn_kwargs: dict[str, Any] = {
            "FunctionName": fn["function_name"],
            "Runtime": fn["runtime"],
            "Role": role_arn,
            "Handler": fn["handler"],
            "Description": fn.get("description", ""),
            "Code": {"ZipFile": zip_bytes},
            "Publish": True,
        }
        if fn.get("subnets") or fn.get("security_groups"):
            fn_kwargs["VpcConfig"] = {
                "SubnetIds": [env.subnet_ids[k] for k in fn.get("subnets", [])],
                "SecurityGroupIds": [env.sg_ids[k] for k in fn.get("security_groups", [])],
            }
        env.lambda_arns[fn["key"]] = lambda_client.create_function(**fn_kwargs)["FunctionArn"]

    # 5. ターゲットグループ（枠のみ）
    for tg in infra["target_groups"]:
        tg_kwargs: dict[str, Any] = {"Name": tg["name"], "TargetType": tg["target_type"]}
        if tg["target_type"] != "lambda":
            tg_kwargs.update(
                Protocol=tg["protocol"],
                Port=tg["port"],
                VpcId=env.vpc_id,
                HealthCheckProtocol=tg.get("health_check_protocol", tg["protocol"]),
            )
            if tg.get("health_check_path"):
                tg_kwargs["HealthCheckPath"] = tg["health_check_path"]
            if tg.get("ip_address_type"):
                tg_kwargs["IpAddressType"] = tg["ip_address_type"]
        resp = elbv2.create_target_group(**tg_kwargs)
        env.tg_arns[tg["key"]] = resp["TargetGroups"][0]["TargetGroupArn"]

    # 6. ロードバランサー / リスナー / ルール
    lb_dns: dict[str, str] = {}
    lb_zone: dict[str, str] = {}
    for lb in infra["load_balancers"]:
        lb_kwargs: dict[str, Any] = {
            "Name": lb["name"],
            "Subnets": [env.subnet_ids[k] for k in lb["subnets"]],
            "Scheme": lb["scheme"],
            "Type": lb["type"],
            "IpAddressType": lb.get("ip_address_type", "ipv4"),
        }
        if lb.get("security_groups"):
            lb_kwargs["SecurityGroups"] = [env.sg_ids[k] for k in lb["security_groups"]]
        lb_obj = elbv2.create_load_balancer(**lb_kwargs)["LoadBalancers"][0]
        env.lb_arns[lb["key"]] = lb_obj["LoadBalancerArn"]
        lb_dns[lb["key"]] = lb_obj["DNSName"]
        lb_zone[lb["key"]] = lb_obj.get("CanonicalHostedZoneId", "Z14GRHDCWA56QT")

        for listener in lb.get("listeners", []):
            l_kwargs: dict[str, Any] = {
                "LoadBalancerArn": lb_obj["LoadBalancerArn"],
                "Protocol": listener["protocol"],
                "Port": listener["port"],
                "DefaultActions": _rule_actions(listener["default_action"], env.tg_arns),
            }
            if listener.get("ssl_policy"):
                l_kwargs["SslPolicy"] = listener["ssl_policy"]
            if listener.get("certificate"):
                l_kwargs["Certificates"] = [
                    {"CertificateArn": env.cert_arns[listener["certificate"]]}
                ]
            listener_arn = elbv2.create_listener(**l_kwargs)["Listeners"][0]["ListenerArn"]
            for rule in listener.get("rules", []):
                elbv2.create_rule(
                    ListenerArn=listener_arn,
                    Priority=rule["priority"],
                    Conditions=_rule_conditions(rule["conditions"]),
                    Actions=_rule_actions(rule["action"], env.tg_arns),
                )

    # 7. ELB のノードの ENI（実際の AWS では ELB が自動で作るが、moto は作らないため再現する）
    lb_specs = {lb["key"]: lb for lb in infra["load_balancers"]}
    for eni in infra.get("load_balancer_enis", []):
        lb_spec = lb_specs[eni["load_balancer"]]
        lb_type = "net" if lb_spec["type"] == "network" else "app"
        lb_name_and_id = "/".join(env.lb_arns[lb_spec["key"]].split("/")[-2:])
        eni_kwargs: dict[str, Any] = {
            "SubnetId": env.subnet_ids[eni["subnet"]],
            "PrivateIpAddress": eni["private_ip"],
            "Description": f"ELB {lb_type}/{lb_name_and_id}",
        }
        # ELB に付けた SG は、そのノードの ENI にも付く
        if lb_spec.get("security_groups"):
            eni_kwargs["Groups"] = [env.sg_ids[k] for k in lb_spec["security_groups"]]
        env.eni_ids[eni["key"]] = ec2.create_network_interface(**eni_kwargs)["NetworkInterface"][
            "NetworkInterfaceId"
        ]

    # 8. ターゲット登録（下流 ELB を含むため ELB 作成後に行う）
    for tg in infra["target_groups"]:
        port = tg.get("port")
        ids = (
            [env.instance_ids[k] for k in tg.get("instances", [])]
            + list(tg.get("ips", []))
            + [env.lambda_arns[k] for k in tg.get("lambdas", [])]
            + [env.lb_arns[k] for k in tg.get("load_balancers", [])]
        )
        if ids:
            elbv2.register_targets(
                TargetGroupArn=env.tg_arns[tg["key"]],
                Targets=[{"Id": t, "Port": port} if port else {"Id": t} for t in ids],
            )

    # 9. ECS クラスター / タスク定義 / Fargate サービス
    ecs = session.client("ecs", region_name=region)
    for spec in infra.get("ecs_clusters", []):
        ecs.create_cluster(clusterName=spec["cluster_name"])
        td_arn = ecs.register_task_definition(
            family=spec["task_family"],
            networkMode="awsvpc",
            requiresCompatibilities=["FARGATE"],
            cpu="256",
            memory="512",
            containerDefinitions=[
                {
                    "name": spec["container_name"],
                    "image": "nginx:latest",
                    "memory": 512,
                    "cpu": 256,
                    "portMappings": [{"containerPort": spec["container_port"], "protocol": "tcp"}],
                }
            ],
        )["taskDefinition"]["taskDefinitionArn"]
        ecs.create_service(
            cluster=spec["cluster_name"],
            serviceName=spec["service_name"],
            taskDefinition=td_arn,
            desiredCount=1,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": [env.subnet_ids[spec["subnet"]]],
                    "securityGroups": [default_sg],
                }
            },
            loadBalancers=[
                {
                    "targetGroupArn": env.tg_arns[spec["target_group"]],
                    "containerName": spec["container_name"],
                    "containerPort": spec["container_port"],
                }
            ],
        )

    # 10. Route 53 ホストゾーンとレコード
    route53 = session.client("route53", region_name=region)
    for idx, zone in enumerate(infra.get("route53_zones", [])):
        zone_kwargs: dict[str, Any] = {
            "Name": f"{zone['name']}.",
            "CallerReference": f"elbx-ref-{idx}",
            "HostedZoneConfig": {"Comment": "elbx mock", "PrivateZone": bool(zone.get("private"))},
        }
        if zone.get("private"):
            # 非公開のホストゾーンは VPC に関連付けて作る
            zone_kwargs["VPC"] = {"VPCRegion": region, "VPCId": env.vpc_id}
        zone_id = route53.create_hosted_zone(**zone_kwargs)["HostedZone"]["Id"].split("/")[-1]
        env.zone_ids[zone["name"]] = zone_id
        changes: list[dict[str, Any]] = []
        for rec in zone.get("records", []):
            rrs: dict[str, Any] = {"Name": f"{rec['name']}.", "Type": rec["type"]}
            if rec.get("alias_to"):
                rrs["AliasTarget"] = {
                    "HostedZoneId": lb_zone[rec["alias_to"]],
                    "DNSName": f"dualstack.{lb_dns[rec['alias_to']]}.",
                    "EvaluateTargetHealth": True,
                }
            else:
                rrs["TTL"] = rec.get("ttl", 300)
                rrs["ResourceRecords"] = [{"Value": lb_dns[rec["cname_to"]]}]
            changes.append({"Action": "UPSERT", "ResourceRecordSet": rrs})
        if changes:
            route53.change_resource_record_sets(
                HostedZoneId=zone_id, ChangeBatch={"Changes": changes}
            )

    return env


@contextlib.contextmanager
def _moto_ipv6_filters() -> Iterator[None]:
    """
    moto が未実装の、IPv6 アドレスで絞り込むフィルタを補う（実際の AWS にはあるフィルタ）。

    - describe_network_interfaces の `ipv6-addresses.ipv6-address`
    - describe_instances の `ipv6-address`

    moto はこれらを FilterNotImplementedError / NotImplementedError にするため、
    補わないと IPv6 の ip ターゲットから ENI・EC2 を辿る処理を moto で確認できない。
    """
    from unittest import mock

    from moto.ec2 import utils as ec2_utils
    from moto.ec2.models.elastic_network_interfaces import NetworkInterface

    original_eni_filter = NetworkInterface.get_filter_value
    original_passes = ec2_utils.passes_filter_dict

    def eni_filter(self: Any, filter_name: str, method_name: str | None = None) -> Any:
        if filter_name == "ipv6-addresses.ipv6-address":
            return list(self.ipv6_addresses)
        return original_eni_filter(self, filter_name, method_name)

    def passes_filter_dict(instance: Any, filter_dict: dict[str, Any]) -> bool:
        rest = dict(filter_dict)
        wanted = rest.pop("ipv6-address", None)
        if wanted is not None:
            addrs = {ip for nic in instance.nics.values() for ip in nic.ipv6_addresses}
            if not addrs.intersection(wanted):
                return False
        return original_passes(instance, rest)

    with (
        mock.patch.object(NetworkInterface, "get_filter_value", eni_filter),
        mock.patch.object(ec2_utils, "passes_filter_dict", passes_filter_dict),
    ):
        yield


@contextlib.contextmanager
def _moto_target_health() -> Iterator[None]:
    """
    moto の describe_target_health を、実際の AWS が返す状態に合わせる。

    moto は登録したターゲットを常に healthy にするが、実際の AWS では次のようになる。

    - どの ELB にも使われていない TG のターゲット: `unused`（`Target.NotInUse`）
    - ヘルスチェックが無効な TG（Lambda の TG の既定）のターゲット: `unavailable`
      （`Target.HealthCheckDisabled`）
    """
    from unittest import mock

    from moto.elbv2.models import FakeHealthStatus, FakeTargetGroup

    original = FakeTargetGroup.health_for

    def health_for(self: Any, target: dict[str, Any], ec2_backend: Any) -> Any:
        status = original(self, target, ec2_backend)
        if status.status != "healthy":
            return status
        if not self.load_balancer_arns:
            state, reason, description = (
                "unused",
                "Target.NotInUse",
                "Target group is not configured to receive traffic from the load balancer",
            )
        elif not self.health_check_enabled:
            state, reason, description = (
                "unavailable",
                "Target.HealthCheckDisabled",
                "Health checks are not enabled for this target",
            )
        else:
            return status
        return FakeHealthStatus(
            status.instance_id,
            status.port,
            status.health_check_port,
            state,
            reason,
            description,
        )

    with mock.patch.object(FakeTargetGroup, "health_for", health_for):
        yield


@contextlib.contextmanager
def moto_environment(infra: dict[str, Any] | None = None) -> Iterator[TestEnv]:
    """
    moto を起動して `infra.yaml` の構成をインメモリで構築し、
    終了時に破棄するコンテキストマネージャ。

    :param infra: 構築するインフラ定義辞書（省略時は `load_infra()` で読み込む）
    :return: 構築された TestEnv インスタンスを yield するイテレータ
    """
    infra = infra or load_infra()
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        os.environ[name] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = infra["region"]
    with mock_aws(), _moto_ipv6_filters(), _moto_target_health():
        session = boto3.Session(region_name=infra["region"])
        yield _build_environment(session, infra)


# =============================================================================
# ヘルス状態シナリオ
# =============================================================================


def apply_health_overrides(
    client: ELBClient, env: TestEnv, overrides: dict[str, dict[str, str]]
) -> None:
    """
    `client.elbv2.describe_target_health` をフックし、指定ターゲットのヘルス状態を上書きする。
    `overrides` のキーには EC2 の Name / IP / Lambda 関数名 / ELB 名、
    または `"*"`（全ターゲット）を指定できる。

    :param client: パッチ適用対象の ELBClient インスタンス
    :param env: リソース名と実IDの対応を持つ TestEnv
    :param overrides: ターゲット識別子 -> `{"state", "reason", "description"}` のマップ
    """
    if not overrides:
        return

    name_to_ids: dict[str, set[str]] = {}
    for inst in env.infra["ec2_instances"]:
        ids = {env.instance_ids[inst["key"]], inst["private_ip"]}
        name_to_ids.setdefault(inst["name"], set()).update(ids)
        name_to_ids.setdefault(inst["private_ip"], set()).update(ids)
    for fn in env.infra.get("lambda_functions", []):
        name_to_ids.setdefault(fn["function_name"], set()).add(env.lambda_arns[fn["key"]])
    for lb in env.infra["load_balancers"]:
        name_to_ids.setdefault(lb["name"], set()).add(env.lb_arns[lb["key"]])

    wildcard = overrides.get("*")
    by_id: dict[str, dict[str, str]] = {}
    for key, cfg in overrides.items():
        if key == "*":
            continue
        for target_id in name_to_ids.get(key, {key}):
            by_id[target_id] = cfg

    original = client.elbv2.describe_target_health

    def _patched(**kwargs: Any) -> dict[str, Any]:
        resp = copy.deepcopy(original(**kwargs))
        for desc in resp.get("TargetHealthDescriptions", []):
            cfg = by_id.get(desc.get("Target", {}).get("Id", "")) or wildcard
            if cfg:
                desc["TargetHealth"] = {
                    "State": cfg.get("state", "unhealthy"),
                    "Reason": cfg.get("reason", ""),
                    "Description": cfg.get("description", ""),
                }
        return resp

    client.elbv2.describe_target_health = _patched


# =============================================================================
# 出力の正規化（ランダムな ID を infra.yaml の key を使った表記に置き換える）
#   置き換えは pytest-casebook が、conftest.py の hook（placeholders / patterns）から受け取った
#   対応表で行う（pytest でも playground.py の run_case() でも同じ）。
# =============================================================================

# 置き換え漏れの保険（対応表にない ID が出ても実行ごとに変わらない表記にする）
_FALLBACK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "<datetime>",
    ),
    (re.compile(r"expires=\d{4}-\d{2}-\d{2}"), "expires=<date>"),
    (
        # ELB の ID は先に <lb-id:...> に置き換わっているため、その部分は任意の文字列で受ける
        re.compile(r"(listener(?:-rule)?/(?:app|net)/[\w-]+)/[^/\s\"]+/[0-9a-f]+(?:/[0-9a-f]+)?"),
        r"\1/<id>",
    ),
    (re.compile(r"\b(i|subnet|sg|vpc|eni)-[0-9a-f]{8,17}\b"), r"<\1:?>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
]
