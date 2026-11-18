"""
出力が、AWS が返す順に左右されないことを確かめる。

AWS は、同じ設定でも作り直すと、ELB・サブネット・SG・ルールの条件などを別の順で返すことが
ある（2026-11 に実際の AWS で確認）。ここでは、moto のすべての応答のすべてのリストを逆の順に
してから同じコマンドを実行し、出力（表示と --json）が変わらないことを見る。並べ忘れたリストが
あれば、ここで分かる。
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_casebook import run_in_process

from elbx import ELBClient, cli

from .harness import TestEnv

# 確かめるコマンド（表示と --json の両方。下流の ELB・ECS・Lambda・IPv6 を含む）
COMMANDS = [
    ["ls", "--json"],
    ["tree", "--full"],
    ["tree", "--json"],
    ["tree"],
    ["targets", "--json"],
    ["targets"],
    ["find", "web", "--json"],
    ["find", "10.0.1.90", "--json"],
    [
        "route",
        "https://api.elbx-test.internal/x?mode=fast",
        "-X",
        "POST",
        "-H",
        "X-Env: ip-direct",
        "--source-ip",
        "203.0.113.10",
        "--json",
    ],
    ["route", "https://edge.elbx-test.internal/auth/login", "--json"],
]


def _reverse_lists(value: Any) -> None:
    """辞書とリストをたどり、すべてのリストをその場で逆の順にする。"""
    if isinstance(value, dict):
        for item in value.values():
            _reverse_lists(item)
    elif isinstance(value, list):
        value.reverse()
        for item in value:
            _reverse_lists(item)


def _reversing_client(env: TestEnv) -> ELBClient:
    """AWS の応答のリストをすべて逆の順にして返すクライアント。"""
    client = env.client()

    def reverse(parsed: dict[str, Any], **_: Any) -> None:
        _reverse_lists(parsed)

    for service in (
        client.elbv2,
        client.ec2,
        client.acm,
        client.ecs,
        client.lambda_client,
        client.route53,
    ):
        service.meta.events.register("after-call", reverse)
    return client


def _run(client: ELBClient, argv: list[str]) -> str:
    result = run_in_process(lambda a: cli.main(a, client=client), [*argv, "--no-color"])
    assert result.exit_code == 0, result.stderr
    return str(result.stdout)


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a))
def test_output_does_not_depend_on_the_order_aws_returns(
    moto_env: TestEnv, argv: list[str]
) -> None:
    """AWS の応答のリストを逆の順にしても、出力は同じ。"""
    assert _run(_reversing_client(moto_env), argv) == _run(moto_env.client(), argv)
