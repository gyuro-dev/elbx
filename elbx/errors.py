"""
The exceptions raised by elbx.

elbx が送出する例外。
"""

from __future__ import annotations


class ELBError(Exception):
    """
    Base class of the exceptions raised by elbx.

    elbx が送出する例外の基底クラス。
    """


class AWSAPIError(ELBError):
    """
    An AWS API call failed (no credentials or region, access denied, throttling, ...).
    The botocore exception is kept as `__cause__`.

    AWS API の呼び出しに失敗したときの例外（認証情報やリージョンがない、権限がない、
    スロットリングなど）。botocore の例外は `__cause__` に残す。
    """


class ResourceNotFoundError(ELBError):
    """
    The load balancer or target group given by name / ARN does not exist, or no load
    balancer serves the requested host name.

    名前・ARN で指定した ELB やターゲットグループが存在しない、またはホスト名を向いている ELB が
    見つからないときの例外。
    """
