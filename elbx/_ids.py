"""
AWS のリソース ID・ARN・IP アドレスの判定と正規化（モジュール間で共有する小さなヘルパー）。
"""

from __future__ import annotations

import ipaddress


def is_arn_of(value: str, service: str) -> bool:
    """
    文字列が指定サービスの ARN かを判定する。
    `arn:aws:`（標準）だけでなく `arn:aws-cn:`（中国）や `arn:aws-us-gov:`（GovCloud）も対象にする。

    :param value: 判定する文字列
    :param service: サービス名（例: `"elasticloadbalancing"`, `"lambda"`）
    :return: `arn:<パーティション>:<service>:` で始まれば True
    """
    parts = value.split(":", 3)
    return len(parts) == 4 and parts[0] == "arn" and parts[2] == service


def canonical_ip(value: str) -> str | None:
    """
    文字列が有効な IPv4 または IPv6 アドレスであれば正規化表記（小文字・省略形）を返し、
    IP アドレスでない場合（ARN やインスタンス ID 等）は None を返す。

    :param value: 判定・正規化する文字列
    :return: 正規化されたIPアドレス文字列、またはIPでない場合は None
    """
    if not value or value.startswith("arn:"):
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None
