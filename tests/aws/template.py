"""
tests/data/infra.yaml から、実際の AWS に作るための CloudFormation テンプレートを生成する。

moto 版（tests/harness.py）との主な違い:
- 費用を抑えるため、EC2 はすべて t4g.nano（Amazon Linux 2023）、ECS タスクは最小サイズ（ARM）
- EC2 は起動時に簡易 HTTP サーバーを立て、ヘルスチェックのパスに 200 を返す
  （インターネット接続は不要）
- ヘルスチェックの間隔を短くし、早く healthy になるようにする
- ECS の TG にはサービスがタスクの IP を自動で登録する（infra.yaml の ips は使わない）
- internal NLB のノードの IP を load_balancer_enis の private_ip に固定する
- EC2 には常にパブリック IP を付けない。サブネットもパブリック IP を自動で付けない
- 証明書は自己署名のものを ACM にインポートし、パラメータ CertificateArn で受け取る
- IPv6 の部分（moto_only: true のリソースと、サブネットの ipv6_cidr）は作らない
"""

from __future__ import annotations

import json
import re
from typing import Any

from tests.harness import _rule_action, _rule_conditions, for_real_aws

from .options import LAMBDA_ROLE_BASE_NAME, TAG_KEY, VPC_BASE_NAME, AwsOptions

EC2_INSTANCE_TYPE = "t4g.nano"
EC2_IMAGE = "{{resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64}}"
ECS_IMAGE = "public.ecr.aws/docker/library/python:3.12-alpine"
HEALTH_CHECK_INTERVAL_SECONDS = 10
HEALTH_CHECK_THRESHOLD = 2

LAMBDA_CODE = """\
def handler(event, context):
    return {
        "statusCode": 200,
        "statusDescription": "200 OK",
        "isBase64Encoded": False,
        "headers": {"Content-Type": "text/plain"},
        "body": "ok",
    }
"""


def logical_id(prefix: str, key: str) -> str:
    """
    `infra.yaml` の論理 `key` から CloudFormation 用の論理 ID（英数字のみの PascalCase）を生成する。

    :param prefix: リソース種別を表すプレフィックス（例: `"Subnet"`, `"Lb"`）
    :param key: infra.yaml 内のリソース識別キー
    :return: CloudFormation の論理リソース ID
    """
    return prefix + "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", key) if p)


def _ref(name: str) -> dict[str, str]:
    """
    CloudFormation の組み込み関数 `{"Ref": name}` を生成する。

    :param name: 参照する論理リソース ID またはパラメータ名
    :return: `{"Ref": name}` の辞書
    """
    return {"Ref": name}


def _get_att(name: str, attr: str) -> dict[str, list[str]]:
    """
    CloudFormation の組み込み関数 `{"Fn::GetAtt": [name, attr]}` を生成する。

    :param name: 参照する論理リソース ID
    :param attr: 取得する属性名
    :return: `{"Fn::GetAtt": [name, attr]}` の辞書
    """
    return {"Fn::GetAtt": [name, attr]}


def _health_paths(infra: dict[str, Any]) -> list[str]:
    """
    `infra.yaml` 内の全ターゲットグループで定義されているヘルスチェックパス
    （先頭・末尾の `/` を除いたもの）を収集する。

    :param infra: infra.yaml のデータ辞書
    :return: 重複を除きソートされたヘルスチェックパス文字列のリスト
    """
    paths = {tg.get("health_check_path", "/") for tg in infra["target_groups"]}
    return sorted(p.strip("/") for p in paths if p.strip("/"))


def _instance_ports(infra: dict[str, Any], instance: dict[str, Any]) -> list[int]:
    """
    指定した EC2 インスタンスがヘルスチェック応答のために待ち受けるべきポート番号一覧を返す
    （`instance` 登録された TG のポートと、自身のプライベート IP が `ip` 登録された TG のポート）。

    :param infra: infra.yaml のデータ辞書
    :param instance: 対象 EC2 インスタンスの定義辞書
    :return: 昇順ソートされたポート番号のリスト
    """
    ports: set[int] = set()
    for tg in infra["target_groups"]:
        if not tg.get("port"):
            continue
        if instance["key"] in tg.get("instances", []) or instance["private_ip"] in tg.get(
            "ips", []
        ):
            ports.add(int(tg["port"]))
    return sorted(ports)


def _user_data(infra: dict[str, Any], instance: dict[str, Any]) -> str:
    """
    EC2 インスタンス起動時にヘルスチェック用パスを作成し
    `python3 -m http.server` を立ち上げる UserData スクリプトを生成する。

    :param infra: infra.yaml のデータ辞書
    :param instance: 対象 EC2 インスタンスの定義辞書
    :return: bash シェルスクリプト文字列
    """
    files = " ".join(f"/srv/www/{p}" for p in _health_paths(infra))
    ports = " ".join(str(p) for p in _instance_ports(infra, instance))
    return (
        "#!/bin/bash\n"
        "mkdir -p /srv/www\n"
        f"touch {files}\n"
        f"for port in {ports}; do\n"
        '  nohup python3 -m http.server "$port" --directory /srv/www >/dev/null 2>&1 &\n'
        "done\n"
    )


def _ecs_command(infra: dict[str, Any], port: int) -> str:
    """
    ECS Fargate コンテナ起動時にヘルスチェック用ファイルを作成して
    HTTP サーバーを起動するシェルコマンドを生成する。

    :param infra: infra.yaml のデータ辞書
    :param port: コンテナが待ち受けるポート番号
    :return: `sh -c` に渡すコマンド文字列
    """
    files = " ".join(f"/srv/{p}" for p in _health_paths(infra))
    return f"mkdir -p /srv && touch {files} && exec python -m http.server {port} --directory /srv"


def build_template(infra: dict[str, Any], opts: AwsOptions) -> dict[str, Any]:
    """
    `infra.yaml` の内容から実AWS検証用の CloudFormation テンプレート辞書を生成する。

    :param infra: infra.yaml のデータ辞書
    :param opts: AWS テストオプション
    :return: CloudFormation テンプレート全体の辞書
    """
    infra = for_real_aws(infra)
    stack = opts.stack_name
    resources: dict[str, Any] = {}
    outputs: dict[str, Any] = {}

    def tags(name: str | None = None) -> list[dict[str, str]]:
        result = [{"Key": TAG_KEY, "Value": stack}]
        if name:
            result.append({"Key": "Name", "Value": name})
        return result

    def output(name: str, value: Any) -> None:
        outputs[name] = {"Value": value}

    # 1. VPC / インターネットゲートウェイ
    #    （internet-facing の ALB と、ECS タスクのイメージ取得に必要）
    resources["Vpc"] = {
        "Type": "AWS::EC2::VPC",
        "Properties": {
            "CidrBlock": infra["vpc_cidr"],
            "EnableDnsSupport": True,
            "EnableDnsHostnames": True,
            "Tags": tags(opts.name(VPC_BASE_NAME)),
        },
    }
    output("VpcId", _ref("Vpc"))
    resources["Igw"] = {"Type": "AWS::EC2::InternetGateway", "Properties": {"Tags": tags()}}
    resources["IgwAttachment"] = {
        "Type": "AWS::EC2::VPCGatewayAttachment",
        "Properties": {"VpcId": _ref("Vpc"), "InternetGatewayId": _ref("Igw")},
    }
    resources["PublicRouteTable"] = {
        "Type": "AWS::EC2::RouteTable",
        "Properties": {"VpcId": _ref("Vpc"), "Tags": tags()},
    }
    resources["PublicRoute"] = {
        "Type": "AWS::EC2::Route",
        "DependsOn": "IgwAttachment",
        "Properties": {
            "RouteTableId": _ref("PublicRouteTable"),
            "DestinationCidrBlock": "0.0.0.0/0",
            "GatewayId": _ref("Igw"),
        },
    }

    # 2. サブネット（パブリック IP は自動で付けない）
    same_region = opts.region == infra["region"]
    for idx, sub in enumerate(infra["subnets"]):
        lid = logical_id("Subnet", sub["key"])
        az = sub["az"] if same_region else {"Fn::Select": [idx, {"Fn::GetAZs": ""}]}
        resources[lid] = {
            "Type": "AWS::EC2::Subnet",
            "Properties": {
                "VpcId": _ref("Vpc"),
                "CidrBlock": sub["cidr"],
                "AvailabilityZone": az,
                "MapPublicIpOnLaunch": False,
                "Tags": tags(opts.name(sub["name"])),
            },
        }
        resources[f"{lid}RouteAssoc"] = {
            "Type": "AWS::EC2::SubnetRouteTableAssociation",
            "Properties": {"SubnetId": _ref(lid), "RouteTableId": _ref("PublicRouteTable")},
        }
        output(lid, _ref(lid))

    # 3. セキュリティグループ（送信元は infra.yaml のとおり。外部公開の方針は作成前に確認済み）
    for sg in infra["security_groups"]:
        lid = logical_id("Sg", sg["key"])
        ingress: list[dict[str, Any]] = []
        for rule in sg.get("ingress", []):
            proto = str(rule["protocol"]).lower()
            item: dict[str, Any] = {"IpProtocol": "-1" if proto == "all" else proto}
            if proto != "all":
                low, _, high = str(rule["port"]).partition("-")
                item["FromPort"] = int(low)
                item["ToPort"] = int(high or low)
            if "cidr" in rule:
                item["CidrIp"] = rule["cidr"]
            if "ipv6" in rule:
                item["CidrIpv6"] = rule["ipv6"]
            if rule.get("description"):
                item["Description"] = rule["description"]
            ingress.append(item)
        resources[lid] = {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupName": opts.name(sg["name"]),
                "GroupDescription": sg["description"],
                "VpcId": _ref("Vpc"),
                "SecurityGroupIngress": ingress,
                "Tags": tags(opts.name(sg["name"])),
            },
        }
        output(lid, _ref(lid))
    default_sg = logical_id("Sg", infra["security_groups"][0]["key"])

    # 4. EC2（パブリック IP は常に付けない）
    for inst in infra["ec2_instances"]:
        lid = logical_id("Instance", inst["key"])
        resources[lid] = {
            "Type": "AWS::EC2::Instance",
            "Properties": {
                "ImageId": EC2_IMAGE,
                "InstanceType": EC2_INSTANCE_TYPE,
                "NetworkInterfaces": [
                    {
                        "DeviceIndex": "0",
                        "SubnetId": _ref(logical_id("Subnet", inst["subnet"])),
                        "PrivateIpAddress": inst["private_ip"],
                        "GroupSet": [_ref(default_sg)],
                        "AssociatePublicIpAddress": False,
                    }
                ],
                "MetadataOptions": {"HttpTokens": "required"},
                "UserData": {"Fn::Base64": _user_data(infra, inst)},
                "Tags": tags(opts.name(inst["name"])),
            },
        }
        output(lid, _ref(lid))

    # 5. Lambda（実行ロールに Permissions Boundary / パスを付けられる）
    if infra.get("lambda_functions"):
        role_props: dict[str, Any] = {
            "RoleName": opts.name(LAMBDA_ROLE_BASE_NAME),
            "Path": opts.iam_path or "/",
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "lambda.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            },
            "ManagedPolicyArns": [
                "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
            ],
            "Tags": tags(),
        }
        if opts.permissions_boundary:
            role_props["PermissionsBoundary"] = opts.permissions_boundary
        resources["LambdaRole"] = {"Type": "AWS::IAM::Role", "Properties": role_props}
    for fn in infra.get("lambda_functions", []):
        lid = logical_id("Lambda", fn["key"])
        props: dict[str, Any] = {
            "FunctionName": opts.name(fn["function_name"]),
            "Runtime": fn["runtime"],
            "Handler": "index.handler",
            "Role": _get_att("LambdaRole", "Arn"),
            "Code": {"ZipFile": LAMBDA_CODE},
            "Description": fn.get("description", ""),
            "MemorySize": 128,
            "Timeout": 5,
            "Tags": tags(),
        }
        if fn.get("subnets") or fn.get("security_groups"):
            props["VpcConfig"] = {
                "SubnetIds": [_ref(logical_id("Subnet", k)) for k in fn.get("subnets", [])],
                "SecurityGroupIds": [
                    _ref(logical_id("Sg", k)) for k in fn.get("security_groups", [])
                ],
            }
        resources[lid] = {"Type": "AWS::Lambda::Function", "Properties": props}
        resources[f"{lid}ElbPermission"] = {
            "Type": "AWS::Lambda::Permission",
            "Properties": {
                "FunctionName": _get_att(lid, "Arn"),
                "Action": "lambda:InvokeFunction",
                "Principal": "elasticloadbalancing.amazonaws.com",
                "SourceAccount": _ref("AWS::AccountId"),
            },
        }
        output(f"{lid}Arn", _get_att(lid, "Arn"))

    # 6. ロードバランサー / リスナー / ルール
    lb_by_key = {lb["key"]: lb for lb in infra["load_balancers"]}
    fixed_ips: dict[str, dict[str, str]] = {}
    for eni in infra.get("load_balancer_enis", []):
        fixed_ips.setdefault(eni["load_balancer"], {})[eni["subnet"]] = eni["private_ip"]
    tg_refs = {tg["key"]: _ref(logical_id("Tg", tg["key"])) for tg in infra["target_groups"]}
    listener_ids: dict[tuple[str, int], str] = {}
    rule_id_by_tg: dict[str, str] = {}

    for lb in infra["load_balancers"]:
        lid = logical_id("Lb", lb["key"])
        props = {
            "Name": opts.name(lb["name"]),
            "Type": lb["type"],
            "Scheme": lb["scheme"],
            "IpAddressType": "ipv4",
            "Tags": tags(opts.name(lb["name"])),
        }
        if lb["key"] in fixed_ips:
            props["SubnetMappings"] = [
                {
                    "SubnetId": _ref(logical_id("Subnet", s)),
                    **(
                        {"PrivateIPv4Address": fixed_ips[lb["key"]][s]}
                        if s in fixed_ips[lb["key"]]
                        else {}
                    ),
                }
                for s in lb["subnets"]
            ]
        else:
            props["Subnets"] = [_ref(logical_id("Subnet", s)) for s in lb["subnets"]]
        if lb.get("security_groups"):
            props["SecurityGroups"] = [_ref(logical_id("Sg", k)) for k in lb["security_groups"]]
        resource: dict[str, Any] = {
            "Type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
            "Properties": props,
        }
        if lb["scheme"] == "internet-facing":
            resource["DependsOn"] = "PublicRoute"
        resources[lid] = resource
        output(f"{lid}Arn", _ref(lid))
        output(f"{lid}Dns", _get_att(lid, "DNSName"))

        for listener in lb.get("listeners", []):
            l_lid = f"{lid}Listener{listener['port']}"
            l_props: dict[str, Any] = {
                "LoadBalancerArn": _ref(lid),
                "Protocol": listener["protocol"],
                "Port": listener["port"],
                "DefaultActions": [_rule_action(listener["default_action"], tg_refs)],
            }
            if listener.get("ssl_policy"):
                l_props["SslPolicy"] = listener["ssl_policy"]
            if listener.get("certificate"):
                l_props["Certificates"] = [{"CertificateArn": _ref("CertificateArn")}]
            resources[l_lid] = {
                "Type": "AWS::ElasticLoadBalancingV2::Listener",
                "Properties": l_props,
            }
            listener_ids[(lb["key"], int(listener["port"]))] = l_lid
            for tg_key in _forward_targets(listener["default_action"]):
                rule_id_by_tg.setdefault(tg_key, l_lid)

            for rule in listener.get("rules", []):
                r_lid = f"{l_lid}Rule{rule['priority']}"
                resources[r_lid] = {
                    "Type": "AWS::ElasticLoadBalancingV2::ListenerRule",
                    "Properties": {
                        "ListenerArn": _ref(l_lid),
                        "Priority": rule["priority"],
                        "Conditions": _rule_conditions(rule["conditions"]),
                        "Actions": [_rule_action(rule["action"], tg_refs)],
                    },
                }
                for tg_key in _forward_targets(rule["action"]):
                    rule_id_by_tg.setdefault(tg_key, r_lid)

    # 7. ターゲットグループ（ヘルスチェックの間隔を短くする）
    ecs_tg_keys = {c["target_group"] for c in infra.get("ecs_clusters", [])}
    lambda_lids = {
        fn["key"]: logical_id("Lambda", fn["key"]) for fn in infra.get("lambda_functions", [])
    }
    for tg in infra["target_groups"]:
        lid = logical_id("Tg", tg["key"])
        depends_on: list[str] = []
        props = {"Name": opts.name(tg["name"]), "TargetType": tg["target_type"], "Tags": tags()}
        port = tg.get("port")
        if tg["target_type"] != "lambda":
            hc_protocol = tg.get("health_check_protocol", tg["protocol"])
            props.update(
                Protocol=tg["protocol"],
                Port=port,
                VpcId=_ref("Vpc"),
                HealthCheckProtocol=hc_protocol,
                HealthCheckIntervalSeconds=HEALTH_CHECK_INTERVAL_SECONDS,
                HealthyThresholdCount=HEALTH_CHECK_THRESHOLD,
                UnhealthyThresholdCount=HEALTH_CHECK_THRESHOLD,
            )
            if hc_protocol in ("HTTP", "HTTPS"):
                props["HealthCheckPath"] = tg.get("health_check_path", "/")
                props["HealthCheckTimeoutSeconds"] = 5

        targets: list[dict[str, Any]] = [
            {"Id": _ref(logical_id("Instance", key)), "Port": port}
            for key in tg.get("instances", [])
        ]
        if tg["key"] not in ecs_tg_keys:  # ECS の TG はサービスがタスクの IP を登録する
            targets += [{"Id": ip, "Port": port} for ip in tg.get("ips", [])]
        for key in tg.get("lambdas", []):
            targets.append({"Id": _get_att(lambda_lids[key], "Arn")})
            depends_on.append(f"{lambda_lids[key]}ElbPermission")
        for key in tg.get("load_balancers", []):
            targets.append({"Id": _ref(logical_id("Lb", key)), "Port": port})
            # alb タイプの TG に ALB を登録するには、ALB にそのポートのリスナーが必要
            if (key, int(port)) in listener_ids:
                depends_on.append(listener_ids[(key, int(port))])
        if targets:
            props["Targets"] = targets
        resource = {"Type": "AWS::ElasticLoadBalancingV2::TargetGroup", "Properties": props}
        if depends_on:
            resource["DependsOn"] = depends_on
        resources[lid] = resource
        output(f"{lid}Arn", _ref(lid))

    # 8. ECS（Fargate、最小サイズ。イメージ取得のためパブリック IP を付ける。SG は先頭の SG）
    for idx, spec in enumerate(infra.get("ecs_clusters", [])):
        c_lid, td_lid, s_lid = f"EcsCluster{idx}", f"EcsTaskDef{idx}", f"EcsService{idx}"
        resources[c_lid] = {
            "Type": "AWS::ECS::Cluster",
            "Properties": {"ClusterName": opts.name(spec["cluster_name"]), "Tags": tags()},
        }
        # タスク実行ロール（ExecutionRoleArn）は付けない。イメージは認証のいらない ECR Public で、
        # ログ（awslogs）も Secrets Manager も使わないため。これらを足すときは実行ロールも足す
        resources[td_lid] = {
            "Type": "AWS::ECS::TaskDefinition",
            "Properties": {
                "Family": opts.name(spec["task_family"]),
                "RequiresCompatibilities": ["FARGATE"],
                "NetworkMode": "awsvpc",
                "Cpu": "256",
                "Memory": "512",
                "RuntimePlatform": {"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"},
                "ContainerDefinitions": [
                    {
                        "Name": spec["container_name"],
                        "Image": ECS_IMAGE,
                        "Essential": True,
                        "EntryPoint": ["sh", "-c"],
                        "Command": [_ecs_command(infra, int(spec["container_port"]))],
                        "PortMappings": [
                            {"ContainerPort": spec["container_port"], "Protocol": "tcp"}
                        ],
                    }
                ],
                "Tags": tags(),
            },
        }
        resources[s_lid] = {
            "Type": "AWS::ECS::Service",
            # TG が ELB に紐づいてからでないとサービスを作れない
            "DependsOn": [rule_id_by_tg[spec["target_group"]], "PublicRoute"],
            "Properties": {
                "ServiceName": opts.name(spec["service_name"]),
                "Cluster": _ref(c_lid),
                "TaskDefinition": _ref(td_lid),
                "DesiredCount": 1,
                "LaunchType": "FARGATE",
                "HealthCheckGracePeriodSeconds": 30,
                "NetworkConfiguration": {
                    "AwsvpcConfiguration": {
                        "AssignPublicIp": "ENABLED",
                        "Subnets": [_ref(logical_id("Subnet", spec["subnet"]))],
                        "SecurityGroups": [_ref(default_sg)],
                    }
                },
                "LoadBalancers": [
                    {
                        "ContainerName": spec["container_name"],
                        "ContainerPort": spec["container_port"],
                        "TargetGroupArn": tg_refs[spec["target_group"]],
                    }
                ],
                "Tags": tags(),
            },
        }
        output(f"{c_lid}Name", _ref(c_lid))
        output(f"{s_lid}Name", _get_att(s_lid, "Name"))

    # 9. Route 53（作成から12時間以内に削除すればホストゾーンは課金されない）
    for idx, zone in enumerate(infra.get("route53_zones", [])):
        z_lid = f"Zone{idx}"
        z_props: dict[str, Any] = {
            "Name": zone["name"],
            "HostedZoneTags": tags(),
            "HostedZoneConfig": {"Comment": f"elbx test ({stack})"},
        }
        if zone.get("private"):
            z_props["VPCs"] = [{"VPCId": _ref("Vpc"), "VPCRegion": _ref("AWS::Region")}]
        resources[z_lid] = {"Type": "AWS::Route53::HostedZone", "Properties": z_props}
        output(f"{z_lid}Id", _ref(z_lid))
        for r_idx, rec in enumerate(zone.get("records", [])):
            rrs: dict[str, Any] = {
                "HostedZoneId": _ref(z_lid),
                "Name": f"{rec['name']}.",
                "Type": rec["type"],
            }
            if rec.get("alias_to"):
                lb_lid = logical_id("Lb", rec["alias_to"])
                dns = _get_att(lb_lid, "DNSName")
                if lb_by_key[rec["alias_to"]]["type"] == "application":
                    dns = {"Fn::Join": ["", ["dualstack.", dns]]}
                rrs["AliasTarget"] = {
                    "DNSName": dns,
                    "HostedZoneId": _get_att(lb_lid, "CanonicalHostedZoneID"),
                    "EvaluateTargetHealth": True,
                }
            else:
                rrs["TTL"] = str(rec.get("ttl", 300))
                rrs["ResourceRecords"] = [_get_att(logical_id("Lb", rec["cname_to"]), "DNSName")]
            resources[f"{z_lid}Record{r_idx}"] = {
                "Type": "AWS::Route53::RecordSet",
                "Properties": rrs,
            }

    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": f"elbx test environment ({stack}). Delete this stack after the test.",
        "Parameters": {
            "CertificateArn": {
                "Type": "String",
                "Description": "ACM にインポートした自己署名証明書の ARN",
            }
        },
        "Resources": resources,
        "Outputs": outputs,
    }


def _forward_targets(action: dict[str, Any]) -> list[str]:
    """
    `infra.yaml` のアクション定義が転送先とするターゲットグループの論理 `key` 一覧を返す。

    :param action: infra.yaml のアクション定義辞書
    :return: 転送先 TG の論理 key のリスト（`redirect` や `fixed_response` の場合は空リスト）
    """
    ((kind, body),) = action.items()
    if kind == "forward":
        return [body]
    if kind == "forward_weighted":
        return list(body)
    return []


def template_body(infra: dict[str, Any], opts: AwsOptions) -> str:
    """
    CloudFormation API の `TemplateBody` に渡す空白を詰めた JSON 文字列を生成する
    （51,200 バイト制限対策）。

    :param infra: infra.yaml のデータ辞書
    :param opts: AWS テストオプション
    :return: ミニファイされた CloudFormation テンプレート JSON 文字列
    """
    return json.dumps(build_template(infra, opts), ensure_ascii=False, separators=(",", ":"))
