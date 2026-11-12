"""
入力の受け付け方と、失敗の伝え方のテスト。

- 引数は読み込み（argparse）の時点で弾く（0 や負の数を黙って別の値に直さない）
- 空文字・空のリストを「省略」と取り違えない（`elbx tree ""` が全 ELB の表示にならない）
- botocore の例外は `AWSAPIError` にして、何をしようとして失敗したかを添えて出す
- 取得できなかった詳細は、ログと標準エラー出力の注意で分かるようにする
- AWS から来た文字列の制御文字は、端末に解釈させずに `\\x1b` の形で見せる
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import re
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    NoRegionError,
    TokenRetrievalError,
)

from elbx import (
    AWSAPIError,
    ELBClient,
    ELBError,
    ENIInfo,
    LoadBalancerView,
    ResourceNotFoundError,
    cli,
    format_lb_map,
    format_lb_summary_table,
    format_lb_tree,
    format_route_simulation,
)
from elbx.cli import build_parser
from elbx.client import _is_known_region
from elbx.hydrator import _condition_text
from elbx.simulator import SimulatedRequest, evaluate_condition, wildcard_match

from .harness import load_yaml

# =============================================================================
# 引数の読み込み
# =============================================================================


@pytest.mark.parametrize(
    "argv",
    [
        ["--workers", "0", "ls"],
        ["ls", "-j", "-1"],
        ["ls", "-j", "x"],
        ["tree", "--max-targets", "-1"],
        ["route", "http://a", "-H", ": value"],
        ["route", "http://a", "-H", "no-colon"],
        ["targets", "my-alb", "--tg", "my-tg"],
    ],
    ids=[
        "workers_0",
        "workers_negative",
        "workers_not_int",
        "max_targets_negative",
        "header_empty_name",
        "header_no_colon",
        "targets_lb_and_tg",
    ],
)
def test_invalid_arguments_are_rejected_when_parsing(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """不正な引数は、実行を始める前に argparse のエラー（終了コード 2）にする。"""
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(argv)
    assert exc_info.value.code == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "options",
    [
        ["--full", "--no-network", "--no-dns"],
        ["--max-targets", "1", "--search", "prod-web"],
        ["--search", "ls"],
        ["--search=prod-web", "-j", "2"],
        ["--sea", "prod-web"],
        ["-vj", "2"],
        ["-vj2", "-sprod-web"],
    ],
    ids=[
        "full",
        "values",
        "value_is_a_subcommand_name",
        "equals_form",
        "abbreviated_long_option",
        "short_options_in_one_argument",
        "value_attached_to_short_option",
    ],
)
def test_tree_options_work_without_subcommand(
    elb_client: ELBClient, options: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """サブコマンドを省略すると tree で、tree のオプションもそのまま書ける（値が ls でも）。"""
    assert cli.main([*options, "--no-color"], client=elb_client) == 0
    without = capsys.readouterr().out
    assert cli.main(["tree", *options, "--no-color"], client=elb_client) == 0
    assert without == capsys.readouterr().out
    assert without


@pytest.mark.parametrize(
    ("argv", "code", "expected"),
    [
        (["tre"], 2, "'tre' はコマンドではありません"),
        (["prod-web-alb", "--full"], 2, "elbx tree prod-web-alb"),
        (["--nope"], 2, "elbx: エラー: 知らない引数です: --nope"),
        (["ls", "--full"], 2, "知らない引数です: --full"),
        (["route", "--lb", "web"], 2, "elbx route: エラー: 次の引数が必要です: URL"),
        (["targets", "web", "--tg", "tg"], 2, "引数 --tg: 引数 LB とは一緒に使えません"),
        (["ls", "--region"], 2, "引数 -r/--region: 値を 1 つ指定してください"),
        (["route", "http://a.example", "--source-ip", ""], 2, "IP アドレスを指定してください: "),
        (["route", "http://a.example", "--source-ip", "999.1.1.1"], 2, "引数 --source-ip: IP"),
        (["--lang", "en", "route"], 2, "elbx route: error: the following arguments are required"),
        (["--version"], 0, "elbx "),
        (["--help"], 0, "<command>"),
        (["--lang"], 2, "--lang"),
        (["ls", "-s", "--lang"], 2, "-s/--search"),
        (["tree", "prod-web-alb", "-s", "web"], 2, "1 つに決める LB とは一緒に使えません"),
        (["targets", "--tg", "web-tg", "-s", "web"], 2, "1 つに決める --tg とは一緒に使えません"),
        (["-vp", "prod", "nope"], 2, "'nope' はコマンドではありません"),
    ],
    ids=[
        "typo",
        "lb_name_without_tree",
        "unknown_option",
        "tree_option_on_ls",
        "missing_url",
        "lb_with_tg",
        "option_without_value",
        "empty_source_ip",
        "invalid_source_ip",
        "argparse_errors_stay_english_in_english",
        "version",
        "help",
        "lang_without_value",
        "lang_as_search_value",
        "search_with_one_load_balancer",
        "search_with_one_target_group",
        "value_of_a_short_option_is_not_the_command",
    ],
)
def test_main_parser_keeps_its_own_arguments(
    argv: list[str], code: int, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """打ち間違いのサブコマンドは tree にせずエラー。--help / --version はメインのもの。"""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv)
    assert exc_info.value.code == code
    captured = capsys.readouterr()
    assert expected in captured.out + captured.err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--lang", "en", "ls"], "en"),
        (["ls", "--lang=ja"], "ja"),
        (["--lang", "en", "--lang", "ja"], "ja"),
        (["--lang"], None),
        (["find", "--", "--lang", "en"], None),
        (["ls"], None),
    ],
    ids=["separate", "equals_form", "last_wins", "no_value", "after_double_dash", "absent"],
)
def test_lang_option_is_read_before_the_parser(argv: list[str], expected: str | None) -> None:
    """ヘルプの言語を決めるため、--lang の値だけを先に拾う（誤りはここでは出さない）。"""
    assert cli._lang_option(argv) == expected


def test_same_header_given_twice_is_joined(capsys: pytest.CaptureFixture[str]) -> None:
    """同じ名前の -H（大文字小文字は区別しない）は上書きせず、HTTP と同じく ", " でつなぐ。"""
    received: dict[str, Any] = {}

    class _Recorder:
        def simulate_request(self, url: str, **kwargs: Any) -> None:
            received.update(kwargs)
            raise ELBError("stop")

    argv = ["route", "http://a", "-H", "Accept: text/html", "-H", "accept: application/json"]
    assert cli.main([*argv, "-H", "X-Env: test"], client=_Recorder()) == 1  # type: ignore[arg-type]
    assert received["headers"] == {"accept": "text/html, application/json", "x-env": "test"}
    assert "stop" in capsys.readouterr().err


def test_valid_boundary_arguments_are_accepted() -> None:
    """境目の値（--max-targets 0 は「全部出す」、-j 1）は受け付ける。"""
    args = build_parser().parse_args(["tree", "--max-targets", "0", "-j", "1"])
    assert (args.max_targets, args.workers) == (0, 1)
    args = build_parser().parse_args(["route", "http://a", "-H", "X: "])
    assert args.header == [("X", "")]
    args = build_parser().parse_args(["route", "http://a", "-H", " X-Env : a:b "])
    assert args.header == [("X-Env", "a:b")]


# =============================================================================
# 空文字・空のリストを「省略」と取り違えない
# =============================================================================


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["tree", ""], "ロードバランサーが見つかりません"),
        (["targets", ""], "ロードバランサーが見つかりません"),
        (["targets", "--tg", ""], "ターゲットグループが見つかりません"),
        (["find", ""], "検索キーワードが空です"),
        (["find", "   "], "検索キーワードが空です"),
        (["route", "http://a.example", "--lb", ""], "ロードバランサーが見つかりません"),
    ],
    ids=["tree", "targets", "targets_tg", "find", "find_spaces", "route_lb"],
)
def test_empty_string_argument_is_an_error_not_omitted(
    elb_client: ELBClient,
    argv: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """空文字の引数はエラーにする（省略と同じ「全部」の表示にしない）。"""
    assert cli.main([*argv, "--no-color"], client=elb_client) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("エラー: ")
    assert message in captured.err


def test_empty_name_lists_return_nothing(elb_client: ELBClient) -> None:
    """`names=[]` は「0件を指定した」なので空を返す（全件を返さない）。"""
    assert elb_client.list_load_balancers(names=[]) == []
    assert elb_client.list_target_groups(names=[]) == []
    # 比べるために、省略（None）なら全件
    assert elb_client.list_load_balancers(include_relations=False)


# =============================================================================
# botocore の例外を AWSAPIError にする
# =============================================================================


def _access_denied(**_: Any) -> dict[str, Any]:
    raise ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized"}}, "DescribeLoadBalancers"
    )


def test_aws_error_becomes_awsapierror_with_the_cause(elb_client: ELBClient) -> None:
    """ClientError は、何をしようとしたかを添えた AWSAPIError（ELBError）になる。"""
    elb_client.elbv2.describe_load_balancers = _access_denied
    with pytest.raises(AWSAPIError, match="ロードバランサーの一覧の取得で") as exc_info:
        elb_client.list_load_balancers()
    assert isinstance(exc_info.value, ELBError)
    assert isinstance(exc_info.value.__cause__, ClientError)


def test_cli_reports_aws_error_in_one_line(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI は AWS の失敗を「予期しないエラー」ではなく、elbx のエラーとして 1 行で出す。"""
    elb_client.elbv2.describe_load_balancers = _access_denied
    assert cli.main(["find", "web"], client=elb_client) == 1
    err = capsys.readouterr().err
    assert err.startswith("エラー: 逆引き検索で AWS API の呼び出しに失敗しました: ")
    assert "AccessDenied" in err
    assert len(err.splitlines()) == 1


def test_clients_retry_throttling_more_than_the_botocore_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    並列に呼ぶので、スロットリングは既定より多くやり直す。利用者が環境変数で決めた項目は変えない。
    """
    events = SimpleNamespace(register=lambda *_args: None)
    configs: list[Any] = []

    class _Session:
        def client(self, _service: str, config: Any = None) -> Any:
            configs.append(config)
            return SimpleNamespace(meta=SimpleNamespace(events=events))

    monkeypatch.delenv("AWS_RETRY_MODE", raising=False)
    monkeypatch.delenv("AWS_MAX_ATTEMPTS", raising=False)
    ELBClient(session=_Session())
    assert len(configs) == 6
    assert all(c.retries == {"mode": "standard", "total_max_attempts": 10} for c in configs)

    configs.clear()
    monkeypatch.setenv("AWS_RETRY_MODE", "adaptive")
    ELBClient(session=_Session())
    assert all(c.retries == {"total_max_attempts": 10} for c in configs)

    configs.clear()
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "3")
    ELBClient(session=_Session())
    assert all(c.retries is None for c in configs)


def test_missing_region_becomes_awsapierror() -> None:
    """リージョンがないなど、クライアントを作る時点の失敗も AWSAPIError になる。"""

    class _NoRegionSession:
        def client(self, _service: str, **_kwargs: Any) -> Any:
            raise NoRegionError()

    with pytest.raises(AWSAPIError, match="AWS への接続の準備で"):
        ELBClient(session=_NoRegionSession())


# =============================================================================
# 制御文字を端末に解釈させない
# =============================================================================

_EVIL = "web\x1b]0;owned\x07\r\x1b[2J\x9b"
_ESCAPED = "web\\x1b]0;owned\\x07\\x0d\\x1b[2J\\x9b"


@pytest.mark.parametrize("use_color", [False, True], ids=["no_color", "color"])
def test_control_characters_from_aws_are_shown_escaped(
    elb_client: ELBClient, use_color: bool
) -> None:
    """名前に入った ESC・BEL・CR などは `\\x1b` の形で出す。色を付けるときも elbx の色だけ残す。"""
    lb = elb_client.get_load_balancer("prod-web-alb", include_network=False, include_dns=False)
    lb.name = _EVIL
    for text in (
        format_lb_map(lb, use_color=use_color),
        format_lb_tree(lb, use_color=use_color, show_network=False),
        format_lb_summary_table([lb], use_color=use_color),
    ):
        assert _ESCAPED in text
        assert "\r" not in text
        assert "\x07" not in text
        assert "\x1b]" not in text
        assert "\x1b[2J" not in text
        assert ("\x1b[" in text) is use_color


# 表示を隠す SGR（ESC [ 8 m は文字を見えなくする）。色の指定の形でも、AWS から来たものは残さない
_EVIL_SGR = "web\x1b[8mhidden\x1b[30;40m"
_ESCAPED_SGR = "web\\x1b[8mhidden\\x1b[30;40m"


def _all_formats(lb: LoadBalancerView, use_color: bool) -> list[str]:
    """ELB の名前が出るすべての表示を返す。"""
    return [
        format_lb_map(lb, use_color=use_color),
        format_lb_tree(lb, use_color=use_color, show_network=False),
        format_lb_summary_table([lb], use_color=use_color),
    ]


@pytest.mark.parametrize("use_color", [False, True], ids=["no_color", "color"])
def test_sgr_in_a_name_from_aws_is_not_a_color(elb_client: ELBClient, use_color: bool) -> None:
    """名前の中の色の指定は、色を付けて出すときも文字として出す（文字を隠させない）。"""
    lb = elb_client.get_load_balancer("prod-web-alb", include_network=False, include_dns=False)
    lb.name = _EVIL_SGR
    for text in _all_formats(lb, use_color):
        assert _ESCAPED_SGR in text
        assert "\x1b[8m" not in text
        assert "\x1b[30;40m" not in text
        assert ("\x1b[" in text) is use_color


def test_sgr_in_a_name_is_escaped_in_the_route_result(elb_client: ELBClient) -> None:
    """route の結果でも、名前の中の色の指定は文字として出す。"""
    result = elb_client.simulate_request("http://x.example.com/", lb_name_or_arn="prod-web-alb")
    result.entries[0].load_balancer.name = _EVIL_SGR
    text = format_route_simulation(result, use_color=True)
    assert _ESCAPED_SGR in text
    assert "\x1b[8m" not in text


def test_color_marker_cannot_be_written_in_a_name(elb_client: ELBClient) -> None:
    """色の目印に似せた文字列を名前に入れても、色にはならない（目印の値は実行ごとに変わる）。"""
    lb = elb_client.get_load_balancer("prod-web-alb", include_network=False, include_dns=False)
    lb.name = "web\ue000[8m"
    assert "\x1b[8m" not in format_lb_map(lb, use_color=True)


@pytest.mark.parametrize("use_color", [False, True], ids=["no_color", "color"])
def test_newline_in_a_name_does_not_make_a_new_line(elb_client: ELBClient, use_color: bool) -> None:
    """名前の中の改行で、本物に見える行を作らせない。"""
    lb = elb_client.get_load_balancer("prod-web-alb", include_network=False, include_dns=False)
    before = [text.count("\n") for text in _all_formats(lb, use_color)]
    lb.name = "web\n└── [● healthy] fake"
    after = _all_formats(lb, use_color)
    assert [text.count("\n") for text in after] == before
    for text in after:
        assert "web\\x0a" in text


def test_print_json_escapes_del_and_c1(capsys: pytest.CaptureFixture[str]) -> None:
    """`--json` でも DEL・C1（U+009B など）は `\\uXXXX` にする。日本語と値そのものは変えない。"""
    data = {"name": "あ\x7f\x9b2J\x1b[0m"}
    cli._print_json(data)
    out = capsys.readouterr().out
    assert not re.search(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", out)
    assert "\\u009b" in out
    assert "あ" in out
    assert json.loads(out) == data


def test_to_json_escapes_del_and_c1(elb_client: ELBClient) -> None:
    """ライブラリの `to_json()` も同じ。"""
    lb = elb_client.get_load_balancer("prod-web-alb", include_network=False, include_dns=False)
    lb.name = "web\x9b2J"
    text = lb.to_json()
    assert "\x9b" not in text
    assert json.loads(text)["name"] == lb.name


def test_cli_error_message_stays_on_one_line(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """エラーの文に入った改行や ESC も、端末に解釈させず 1 行で出す。"""

    def _fail(_query: str, **_kwargs: Any) -> None:
        raise ELBError("line1\nline2\x1b[8m")

    monkeypatch.setattr(elb_client, "find_target", _fail)
    assert cli.main(["find", "x"], client=elb_client) == 1
    err = capsys.readouterr().err
    assert "line1\\x0aline2\\x1b[8m" in err
    assert len(err.splitlines()) == 1


# =============================================================================
# 出力先が閉じた・中断した
# =============================================================================


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (BrokenPipeError(), 0),
        (KeyboardInterrupt(), 130),
        # Windows では閉じたパイプへの書き込みが EINVAL になる
        (OSError(errno.EINVAL, "Invalid argument"), 0 if os.name == "nt" else None),
    ],
    ids=["broken_pipe", "ctrl_c", "einval"],
)
def test_cli_handles_closed_output_and_interrupt(
    elb_client: ELBClient,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected: int | None,
) -> None:
    """`elbx ls | head` はエラーにせず、Ctrl-C は 130 で終わる。トレースバックは出さない。"""

    def _raise(*_args: Any) -> None:
        raise error

    monkeypatch.setitem(cli._COMMANDS, "ls", _raise)
    if expected is None:
        assert cli.main(["ls"], client=elb_client) == 1
        assert len(capsys.readouterr().err.splitlines()) == 1
        return
    assert cli.main(["ls"], client=elb_client) == expected
    assert capsys.readouterr().err == ""


def test_other_os_errors_are_still_reported(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """閉じたパイプ以外の OSError は、これまでどおりエラーとして 1 行で出す。"""

    def _raise(*_args: Any) -> None:
        raise OSError(errno.ENOENT, "No such file")

    monkeypatch.setitem(cli._COMMANDS, "ls", _raise)
    assert cli.main(["ls"], client=elb_client) == 1
    assert "No such file" in capsys.readouterr().err


def test_closing_the_pipe_ends_quietly() -> None:
    """本物のパイプを途中で閉じても（`elbx ls | head -1`）、終了コード 0 で stderr は空。"""
    script = (
        "import sys\n"
        "from elbx import cli\n"
        "cli._COMMANDS['ls'] = lambda *_: [print('line', i) for i in range(200000)]\n"
        "sys.exit(cli.main(['ls'], client=object()))\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    assert proc.stdout.readline().startswith("line 0")
    proc.stdout.close()
    err = proc.stderr.read()
    proc.stderr.close()
    assert proc.wait(timeout=60) == 0
    assert err == ""


# =============================================================================
# 「見つからない」と AWS の失敗を区別する
# =============================================================================


def _client_error(code: str) -> Any:
    def _raise(**_: Any) -> dict[str, Any]:
        raise ClientError({"Error": {"Code": code, "Message": "boom"}}, "Describe")

    return _raise


_NAME_LOOKUPS: list[tuple[str, Any]] = [
    ("describe_load_balancers", lambda c: c.get_load_balancer("prod-web-alb")),
    ("describe_load_balancers", lambda c: c.list_load_balancers(names=["prod-web-alb"])),
    ("describe_load_balancers", lambda c: c.list_target_groups(lb_name_or_arn="prod-web-alb")),
    ("describe_target_groups", lambda c: c.get_target_group("prod-web-tg")),
    ("describe_target_groups", lambda c: c.list_target_groups(names=["prod-web-tg"])),
]


@pytest.mark.parametrize("code", ["AccessDenied", "ExpiredToken", "Throttling"])
@pytest.mark.parametrize(("api", "call"), _NAME_LOOKUPS)
def test_aws_failure_is_not_reported_as_not_found(
    elb_client: ELBClient, api: str, call: Any, code: str
) -> None:
    """権限不足や期限切れの認証情報を「見つからない」と伝えない。"""
    setattr(elb_client.elbv2, api, _client_error(code))
    with pytest.raises(AWSAPIError, match=code) as exc_info:
        call(elb_client)
    assert not isinstance(exc_info.value, ResourceNotFoundError)


@pytest.mark.parametrize("code", ["LoadBalancerNotFound", "TargetGroupNotFound", "ValidationError"])
@pytest.mark.parametrize(("api", "call"), _NAME_LOOKUPS)
def test_missing_or_malformed_name_is_not_found(
    elb_client: ELBClient, api: str, call: Any, code: str
) -> None:
    """存在しない名前と、形が正しくない名前は ResourceNotFoundError。"""
    setattr(elb_client.elbv2, api, _client_error(code))
    with pytest.raises(ResourceNotFoundError, match=code):
        call(elb_client)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_load_balancer("no-such-lb"),
        lambda c: c.get_target_group("no-such-tg"),
        lambda c: c.list_load_balancers(names=["no-such-lb"]),
        lambda c: c.list_target_groups(names=["no-such-tg"]),
    ],
    ids=["get_lb", "get_tg", "list_lb", "list_tg"],
)
def test_not_found_with_the_real_error_codes(elb_client: ELBClient, call: Any) -> None:
    """moto が返す本物のエラーコードでも ResourceNotFoundError になる。"""
    with pytest.raises(ResourceNotFoundError):
        call(elb_client)


def test_list_target_groups_applies_both_the_lb_and_the_names(elb_client: ELBClient) -> None:
    """`lb_name_or_arn` と `names` を両方渡すと、両方の条件で絞る（片方を黙って無視しない）。"""
    lookup = elb_client.list_target_groups
    attached = lookup(lb_name_or_arn="prod-web-alb", names=["prod-web-tg"])
    assert [tg.name for tg in attached] == ["prod-web-tg"]
    assert lookup(lb_name_or_arn="edge-ingress-nlb", names=["prod-web-tg"]) == []


def test_find_ignores_spaces_around_the_query(elb_client: ELBClient) -> None:
    """検索語の前後の空白は無視する（空白だけの検索語を弾くのと同じ見方）。"""
    assert elb_client.find_target("  canary-prod-01 ") == elb_client.find_target("canary-prod-01")


# =============================================================================
# route の入力
# =============================================================================


@pytest.mark.parametrize(
    ("url", "scheme", "host", "port", "query"),
    [
        (
            "api.example.com/login?next=https://x.example.com",
            "http",
            "api.example.com",
            80,
            [("next", "https://x.example.com")],
        ),
        ("HTTPS://api.example.com/?a=1", "https", "api.example.com", 443, [("a", "1")]),
        ("http://api.example.com:0/", "http", "api.example.com", 0, []),
    ],
    ids=["scheme_in_query", "uppercase_scheme", "port_zero"],
)
def test_url_is_parsed(
    url: str, scheme: str, host: str, port: int, query: list[tuple[str, str]]
) -> None:
    """スキームは URL の先頭だけを見る（クエリの `://` と取り違えない）。ポート 0 も省略しない。"""
    req = SimulatedRequest.from_url(url)
    assert (req.scheme, req.host, req.port, req.query) == (scheme, host, port, query)


def test_other_schemes_are_still_rejected() -> None:
    """http / https 以外のスキームは、これまでどおり弾く。"""
    with pytest.raises(ValueError, match="ftp"):
        SimulatedRequest.from_url("ftp://api.example.com/")


def test_query_string_condition_reads_escaped_wildcards_as_the_characters() -> None:
    """query-string 条件の `\\*`・`\\?` は、ワイルドカードではなくその文字そのものと照合する。"""
    assert wildcard_match(r"a\*b", "a*b", ignore_case=True, escape=True)
    assert not wildcard_match(r"a\*b", "axb", ignore_case=True, escape=True)
    assert wildcard_match(r"a\?b*", "a?bcd", ignore_case=True, escape=True)
    # ほかの文字の前の `\` と、末尾の `\` は、その文字のまま
    assert wildcard_match("a\\b\\", "a\\b\\", ignore_case=True, escape=True)
    # ほかの条件（ホスト・パス・ヘッダー）は、これまでどおり `\` を特別に扱わない
    assert not wildcard_match(r"a\*b", "a*b", ignore_case=True)

    cond = {
        "Field": "query-string",
        "QueryStringConfig": {"Values": [{"Key": "q", "Value": r"1\*2"}]},
    }
    matched, _ = evaluate_condition(cond, SimulatedRequest.from_url("https://x.example.com/?q=1*2"))
    assert matched
    matched, _ = evaluate_condition(cond, SimulatedRequest.from_url("https://x.example.com/?q=1x2"))
    assert not matched


def _alb_matching() -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """実際の ALB で確かめた結果（alb_matching.yaml）を、ルールの条件とリクエストにする。"""
    data = load_yaml("alb_matching.yaml")
    rules = []
    for rule in data["rules"]:
        if "path" in rule:
            cond = {"Field": "path-pattern", "PathPatternConfig": {"Values": [rule["path"]]}}
        else:
            pair = {name.capitalize(): value for name, value in rule["query"].items()}
            cond = {"Field": "query-string", "QueryStringConfig": {"Values": [pair]}}
        rules.append({"name": f"R{rule['priority']}", "priority": rule["priority"], "cond": cond})
    rules.sort(key=lambda rule: rule["priority"])
    return rules, [(target, matched) for target, matched in data["requests"]]


@pytest.mark.parametrize(("target", "expected"), _alb_matching()[1])
def test_route_matches_requests_as_a_real_alb_did(target: str, expected: str) -> None:
    """クエリとパスの照合が、実際の ALB で確かめた結果（どのルールに一致したか）と同じになる。"""
    rules, _ = _alb_matching()
    req = SimulatedRequest.from_url(f"http://qtest.elbx-test.internal{target}")
    matched = next(
        (rule["name"] for rule in rules if evaluate_condition(rule["cond"], req)[0]), "default"
    )
    assert matched == expected


def test_query_condition_without_a_key_is_shown_as_the_value_alone() -> None:
    """キーのない query-string 条件は、クエリ文字列の全体と照合するので、値だけを表示する。"""
    cond = {
        "Field": "query-string",
        "QueryStringConfig": {"Values": [{"Value": "*wild*"}, {"Key": "k", "Value": "v"}]},
    }
    assert _condition_text(cond) == "Query in [*wild*, k=v]"
    # 理由には、照合したデコード前のクエリ文字列を出す
    req = SimulatedRequest.from_url("http://x.example.com/?a=%77ild")
    assert evaluate_condition(cond, req) == (
        False,
        "Query 'a=%77ild' は [*wild*, k=v] のいずれにも一致しません",
    )


def test_path_condition_shows_the_path_it_was_matched_against() -> None:
    """パスの条件の理由には、デコードして `.` と `..` を取り除いたあとのパスを出す。"""
    cond = {"Field": "path-pattern", "PathPatternConfig": {"Values": ["/a/b"]}}
    req = SimulatedRequest.from_url("http://x.example.com/a/x/../%62")
    assert evaluate_condition(cond, req) == (True, "Path '/a/b' は [/a/b] に一致")
    assert req.path == "/a/x/../%62"


def test_many_wildcards_do_not_make_matching_slow() -> None:
    """`*` が多いパターンでも時間がかからない（正規表現のときは 1 回で数秒かかった）。"""
    started = time.perf_counter()
    assert wildcard_match("*a*a*a*ab", "a" * 400, ignore_case=False) is False
    assert wildcard_match("*a" * 12 + "b", "a" * 2000, ignore_case=False) is False
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("cond", "label"),
    [
        (
            {
                "Field": "host-header",
                "HostHeaderConfig": {"Values": [], "RegexValues": ["^api[.]"]},
            },
            "Host",
        ),
        (
            {"Field": "path-pattern", "PathPatternConfig": {"RegexValues": ["^/v[0-9]+/"]}},
            "Path",
        ),
        (
            {
                "Field": "http-header",
                "HttpHeaderConfig": {"HttpHeaderName": "X-Env", "RegexValues": ["^prod$"]},
            },
            "Header(X-Env)",
        ),
    ],
    ids=["host", "path", "header"],
)
def test_regex_condition_is_shown_and_not_evaluated(cond: dict[str, Any], label: str) -> None:
    """正規表現の条件は「値が空の条件」にせず、表示し、評価できないことを理由に出す。"""
    regex = next(iter(next(v for v in cond.values() if isinstance(v, dict))["RegexValues"]))
    assert _condition_text(cond) == f"{label} in [regex:{regex}]"
    req = SimulatedRequest.from_url("http://api.example.com/v1/", headers={"X-Env": "prod"})
    ok, reason = evaluate_condition(cond, req)
    assert ok is False
    assert regex in reason
    assert "評価できない" in reason


# =============================================================================
# ログ
# =============================================================================

_TREE = ["tree", "prod-web-alb", "--full", "--no-dns"]


def _deny(*_args: Any, **_kwargs: Any) -> Any:
    raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "Describe")


@pytest.fixture
def elbx_warnings(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """`elbx` ロガーの WARNING を受け取る。"""
    caplog.set_level(logging.WARNING, logger="elbx")
    return caplog


def test_cli_says_nothing_extra_when_everything_is_fetched(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """失敗がなければ、標準エラー出力には何も出さない。"""
    assert cli.main(_TREE, client=elb_client) == 0
    assert capsys.readouterr().err == ""


def test_cli_notes_missing_details_in_one_line(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-v` なしでも、情報が欠けたことは 1 行で知らせる。終了コードは 0 で、出力は続く。"""
    elb_client.ec2.describe_security_groups = _deny
    assert cli.main(_TREE, client=elb_client) == 0
    out, err = capsys.readouterr()
    assert "prod-web-alb" in out
    assert err.startswith("注意: ")
    assert "-v" in err
    assert len(err.splitlines()) == 1


@pytest.mark.parametrize(
    "argv",
    [["-v", *_TREE], [*_TREE, "-v"], [*_TREE, "--verbose"]],
    ids=["before_command", "after_command", "long_option"],
)
def test_verbose_shows_what_could_not_be_fetched(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """`-v` は取得できなかったものと理由を出す。理由が出ているので注意は繰り返さない。"""
    elb_client.ec2.describe_security_groups = _deny
    assert cli.main(argv, client=elb_client) == 0
    err = capsys.readouterr().err
    assert "WARNING: SG を取得できなかったため、なしで続けます: " in err
    assert "AccessDenied" in err
    assert "注意: " not in err
    assert "DEBUG: " not in err


def test_very_verbose_shows_api_calls_without_secrets(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-vv` は API の呼び出しも出す。引数やヘッダーは出さず、`--json` の標準出力は壊さない。"""
    assert cli.main(["-vv", *_TREE, "--json"], client=elb_client) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)[0]["name"] == "prod-web-alb"
    assert re.search(r"DEBUG: elbv2\.DescribeLoadBalancers: HTTP 200, \d+ ms", err)
    assert "DEBUG: elbv2.DescribeTargetHealth: " in err
    for secret in ("Authorization", "AWS4-HMAC", "x-amz-", "prod-web-alb"):
        assert secret not in err


def test_log_messages_follow_the_language(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """注意とログは選んだ言語で出す。"""
    elb_client.ec2.describe_security_groups = _deny
    assert cli.main(["--lang", "en", *_TREE], client=elb_client) == 0
    assert capsys.readouterr().err.startswith("Note: ")
    assert cli.main(["--lang", "en", "-v", *_TREE], client=elb_client) == 0
    assert "WARNING: could not fetch security groups" in capsys.readouterr().err


def test_log_lines_do_not_pass_control_characters(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """ログに入る AWS の文にも、端末に解釈させる文字や、行を増やす改行を残さない。"""

    def _evil(**_: Any) -> Any:
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "a\nWARNING: fake\x1b[8m"}}, "Describe"
        )

    elb_client.ec2.describe_security_groups = _evil
    assert cli.main(["-v", *_TREE], client=elb_client) == 0
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert "a\\x0aWARNING: fake\\x1b[8m" in err
    assert not any(line.startswith("WARNING: fake") for line in err.splitlines())


def test_cli_leaves_no_logging_settings_behind(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main()` のあとに、ロガーの設定を残さない（続けて `-v` なしで呼べば、詳細は出ない）。"""
    elbx_logger = logging.getLogger("elbx")
    handlers, level = list(elbx_logger.handlers), elbx_logger.level
    assert cli.main(["-vv", *_TREE], client=elb_client) == 0
    assert elbx_logger.handlers == handlers
    assert elbx_logger.level == level
    capsys.readouterr()
    assert cli.main(_TREE, client=elb_client) == 0
    assert capsys.readouterr().err == ""


def test_cli_error_does_not_add_the_note(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """読めなかった詳細がなければ、コマンドが失敗したときはエラーの 1 行だけを出す。"""
    elb_client.elbv2.describe_load_balancers = _deny
    assert cli.main(["find", "web"], client=elb_client) == 1
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_library_logs_nothing_unless_configured() -> None:
    """ライブラリは NullHandler だけを持つ（設定しなければ何も出さない）。"""
    elbx_logger = logging.getLogger("elbx")
    assert any(isinstance(handler, logging.NullHandler) for handler in elbx_logger.handlers)
    assert elbx_logger.level == logging.NOTSET


_ECS_CLUSTER = "arn:aws:ecs:ap-northeast-1:123456789012:cluster/c1"

_SKIPPED_FETCHES: list[tuple[str, str, Any, str]] = [
    (
        "elbv2",
        "describe_load_balancer_attributes",
        lambda c: c._fetch_lb_attributes("lb-arn-1"),
        "ELB の属性 (lb-arn-1)",
    ),
    (
        "elbv2",
        "describe_target_group_attributes",
        lambda c: c._fetch_tg_attributes("tg-arn-1"),
        "TG の属性 (tg-arn-1)",
    ),
    (
        "ec2",
        "describe_instances",
        lambda c: c._fetch_ec2_instances_map(["i-1"]),
        "EC2 インスタンス",
    ),
    (
        "ec2",
        "describe_instances",
        lambda c: c._fetch_ec2_instances_by_ips(["10.0.0.1"]),
        "EC2 インスタンス",
    ),
    ("ec2", "describe_network_interfaces", lambda c: c._fetch_eni_map_by_ips(["10.0.0.1"]), "ENI"),
    ("ec2", "describe_subnets", lambda c: c._fetch_subnets_map(["subnet-1"]), "サブネット"),
    (
        "ec2",
        "describe_subnets",
        lambda c: c._fetch_subnets_map(vpc_ids=["vpc-1"]),
        "サブネット",
    ),
    ("ec2", "describe_security_groups", lambda c: c._fetch_security_groups_map(["sg-1"]), "SG"),
    (
        "ecs",
        "list_clusters",
        lambda c: c._fetch_ecs_maps({"10.0.0.1"}, set(), set()),
        "ECS のクラスター・サービス・タスク",
    ),
    (
        "ecs",
        "list_services",
        lambda c: c._fetch_ecs_services(_ECS_CLUSTER),
        "ECS のクラスター・サービス・タスク (c1)",
    ),
    (
        "ecs",
        "list_container_instances",
        lambda c: c._fetch_ecs_container_instances(_ECS_CLUSTER),
        "ECS のクラスター・サービス・タスク (c1)",
    ),
    (
        "ecs",
        "list_tasks",
        lambda c: c._fetch_ecs_tasks(_ECS_CLUSTER),
        "ECS のクラスター・サービス・タスク (c1)",
    ),
    (
        "lambda_client",
        "get_function_configuration",
        lambda c: c._fetch_single_lambda_function("fn-arn"),
        "Lambda 関数の詳細 (fn-arn)",
    ),
    (
        "acm",
        "describe_certificate",
        lambda c: c._fetch_single_certificate("arn:aws:acm:ap-northeast-1:1:certificate/x"),
        "ACM 証明書の詳細 (arn:aws:acm:ap-northeast-1:1:certificate/x)",
    ),
    (
        "route53",
        "get_paginator",
        lambda c: c._fetch_zone_records({"Id": "/hostedzone/Z1"}),
        "Route 53 のレコード (Z1)",
    ),
    (
        "route53",
        "get_paginator",
        lambda c: c._fetch_route53_records_by_dns(["a.example.com"]),
        "Route 53 のレコード",
    ),
]


@pytest.mark.parametrize(("client_attr", "api", "call", "expected"), _SKIPPED_FETCHES)
def test_every_skipped_fetch_is_recorded(
    elb_client: ELBClient,
    elbx_warnings: pytest.LogCaptureFixture,
    client_attr: str,
    api: str,
    call: Any,
    expected: str,
) -> None:
    """AWS の失敗で詳細を諦めた箇所は、何を諦めたかと理由を WARNING で記録する。"""
    setattr(getattr(elb_client, client_attr), api, _deny)
    call(elb_client)
    messages = [record.getMessage() for record in elbx_warnings.records]
    assert any(expected in m and "AccessDenied" in m for m in messages), messages


def test_failed_optional_client_is_recorded(elbx_warnings: pytest.LogCaptureFixture) -> None:
    """ECS などのクライアントを作れなくても続け、そのことを記録する。"""

    events = SimpleNamespace(register=lambda *_args: None)

    class _Session:
        def client(self, service: str, **_kwargs: Any) -> Any:
            if service == "ecs":
                raise NoRegionError()
            return SimpleNamespace(meta=SimpleNamespace(events=events))

    client = ELBClient(session=_Session())
    assert client.ecs is None
    assert "AWS のクライアント (ecs)" in elbx_warnings.text


def test_lost_downstream_load_balancer_is_recorded(
    elb_client: ELBClient, elbx_warnings: pytest.LogCaptureFixture
) -> None:
    """下流の ELB を取得できないときは、展開されない理由を記録する。"""
    real = elb_client.elbv2.describe_load_balancers

    def _deny_by_arn(**kwargs: Any) -> Any:
        return _deny() if "LoadBalancerArns" in kwargs else real(**kwargs)

    elb_client.elbv2.describe_load_balancers = _deny_by_arn
    lb = elb_client.get_load_balancer("edge-ingress-nlb", include_network=False, include_dns=False)
    assert not any(target.downstream_lb for target in lb.all_targets)
    assert "下流の ELB" in elbx_warnings.text


def _elb_eni() -> ENIInfo:
    return ENIInfo(
        network_interface_id="eni-1", private_ip="10.0.0.1", description="ELB net/gone-nlb/abc123"
    )


def test_eni_of_a_deleted_load_balancer_is_not_a_warning(
    elb_client: ELBClient, elbx_warnings: pytest.LogCaptureFixture
) -> None:
    """消えた ELB の ENI が残っているのはふつうのことなので、記録しない。"""

    def _not_found(**_: Any) -> Any:
        raise ClientError({"Error": {"Code": "LoadBalancerNotFound"}}, "DescribeLoadBalancers")

    elb_client.elbv2.describe_load_balancers = _not_found
    elb_client._resolve_elb_enis([_elb_eni()])
    assert elbx_warnings.records == []


def test_denied_eni_load_balancer_lookup_is_a_warning(
    elb_client: ELBClient, elbx_warnings: pytest.LogCaptureFixture
) -> None:
    """権限がなくて ENI の ELB を調べられないときは、記録する。"""
    elb_client.elbv2.describe_load_balancers = _deny
    elb_client._resolve_elb_enis([_elb_eni()])
    assert "ENI の ELB (gone-nlb)" in elbx_warnings.text


def test_iam_server_certificate_is_not_looked_up_in_acm(
    elb_client: ELBClient, elbx_warnings: pytest.LogCaptureFixture
) -> None:
    """IAM のサーバー証明書は ACM では引けないので、呼ばない（失敗として記録もしない）。"""
    elb_client.acm.describe_certificate = _deny
    arn = "arn:aws:iam::123456789012:server-certificate/legacy"
    assert elb_client._fetch_single_certificate(arn)[1].certificate_arn == arn
    assert elbx_warnings.records == []


# =============================================================================
# 失敗したときに、次にすることを案内する
# =============================================================================


def _raises(exc: Exception) -> Any:
    """呼ばれると `exc` を送出する関数を返す。"""

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return _raise


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (NoRegionError(), "--region"),
        (NoCredentialsError(), "--profile"),
        (TokenRetrievalError(provider="sso", error_msg="expired"), "aws sso login --profile prod"),
        (
            ClientError({"Error": {"Code": "ExpiredToken", "Message": "x"}}, "Describe"),
            "aws sts get-caller-identity --profile prod",
        ),
    ],
    ids=["no_region", "no_credentials", "sso", "expired"],
)
def test_error_says_what_to_do_next(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str], exc: Exception, expected: str
) -> None:
    """次にすることが決まっている失敗には、エラーのあとにヒントを 1 行添える。"""
    elb_client.elbv2.describe_load_balancers = _raises(exc)
    assert cli.main(["find", "web", "--profile", "prod"], client=elb_client) == 1
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("エラー: ")
    assert lines[1].startswith("ヒント: ")
    assert expected in lines[1]


def test_hint_is_in_the_chosen_language(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """ヒントも `--lang` の言語で出す。プロファイルの指定がなければコマンドにも付けない。"""
    elb_client.elbv2.describe_load_balancers = _raises(
        TokenRetrievalError(provider="sso", error_msg="expired")
    )
    assert cli.main(["find", "web", "--lang", "en"], client=elb_client) == 1
    assert capsys.readouterr().err.splitlines()[1] == ("Hint: sign in again with: aws sso login")


def test_access_denied_has_no_hint(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """権限不足は AWS のメッセージで分かるので、ヒントを足さない。"""
    elb_client.elbv2.describe_load_balancers = _deny
    assert cli.main(["find", "web"], client=elb_client) == 1
    assert len(capsys.readouterr().err.splitlines()) == 1


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["targets", "prod-web-tg"], "elbx targets --tg prod-web-tg"),
        (["tree", "prod-web-tg"], "'prod-web-tg' はターゲットグループの名前です"),
        (["route", "http://a.example/", "--lb", "prod-web-tg"], "elbx targets --tg prod-web-tg"),
        (["targets", "--tg", "prod-web-alb"], "elbx targets prod-web-alb"),
        (["route", "prod-web-tg"], "route にはリクエストの URL を指定します"),
    ],
    ids=["targets", "tree", "route_lb", "targets_tg", "route_bare_word"],
)
def test_not_found_says_so_when_the_name_is_of_the_other_kind(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str], argv: list[str], expected: str
) -> None:
    """TG の名前を ELB として（またはその逆に）渡したときと、route に名前を渡したときは案内する。"""
    assert cli.main(argv, client=elb_client) == 1
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("エラー: ")
    assert lines[1].startswith("ヒント: ")
    assert expected in lines[1]


@pytest.mark.parametrize(
    "argv",
    [
        ["tree", "no-such-name"],
        ["targets", "--tg", "no-such-name"],
        ["route", "https://nothing.example.com/"],
    ],
    ids=["tree", "targets_tg", "route_url"],
)
def test_not_found_has_no_hint_when_nothing_has_that_name(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """どちらの種類にもない名前と、URL の形をした route の引数には、案内を足さない。"""
    assert cli.main(argv, client=elb_client) == 1
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_unreachable_endpoint_hints_at_the_region_name(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """エンドポイントにつながらないときは、リージョン名を確かめるように案内する。"""
    elb_client.elbv2.describe_load_balancers = _raises(
        EndpointConnectionError(endpoint_url="https://elasticloadbalancing.xx.amazonaws.com/")
    )
    assert cli.main(["ls"], client=elb_client) == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines[1].startswith("ヒント: リージョン名（ap-northeast-1）")


def test_unknown_region_name_is_not_retried_for_long(monkeypatch: pytest.MonkeyPatch) -> None:
    """botocore の知らないリージョン名（打ち間違い）では、接続のやり直しを 2 回までにする。"""
    monkeypatch.delenv("AWS_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("AWS_RETRY_MODE", raising=False)
    session = boto3.Session(
        aws_access_key_id="x", aws_secret_access_key="x", region_name="ap-northeast-1"
    )
    assert _is_known_region(session, "ap-northeast-1")
    assert _is_known_region(session, "us-gov-west-1")
    assert _is_known_region(session, None)
    assert not _is_known_region(session, "ap-northeast1")
    typo = ELBClient(session=session, region_name="ap-northeast1")
    assert typo.elbv2.meta.config.retries["total_max_attempts"] == 2
    known = ELBClient(session=session)
    assert known.elbv2.meta.config.retries["total_max_attempts"] == 10


def test_argparse_messages_are_restored_after_main() -> None:
    """argparse の文の差し替えは、main() の外に残さない。"""
    before = argparse._  # type: ignore[attr-defined]
    with pytest.raises(SystemExit):
        cli.main(["route"])
    assert argparse._ is before  # type: ignore[attr-defined]


def test_route_says_so_when_route53_cannot_be_read(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Route 53 を読めないときに「向いている ELB がない」と答えず、原因と --lb を案内する。"""
    elb_client._fetch_hosted_zones = _raises(  # type: ignore[method-assign]
        ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "ListHostedZones")
    )
    assert cli.main(["route", "https://nothing.example.com/"], client=elb_client) == 1
    err = capsys.readouterr().err
    assert "AccessDenied" in err
    assert "--lb" in err
    assert "確認しました" not in err


def test_route_says_so_when_the_records_of_a_zone_cannot_be_read(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """ゾーンのレコードを読めないときも「確認しました」と言わず、原因と --lb を案内する。"""
    real_paginator = elb_client.route53.get_paginator

    def get_paginator(name: str) -> Any:
        if name == "list_resource_record_sets":
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "ListResourceRecordSets"
            )
        return real_paginator(name)

    elb_client.route53.get_paginator = get_paginator
    assert cli.main(["route", "https://nothing.elbx-test.internal/"], client=elb_client) == 1
    err = capsys.readouterr().err
    assert "AccessDenied" in err
    assert "--lb" in err
    assert "確認しました" not in err


def test_route_without_route53_access_still_works_for_the_dns_name_of_the_load_balancer(
    elb_client: ELBClient,
) -> None:
    """ELB 自身の DNS 名なら Route 53 を読まないので、権限がなくても評価できる。"""
    elb_client._fetch_hosted_zones = _deny  # type: ignore[method-assign]
    raw_lb = elb_client._fetch_raw_load_balancers()[0]
    assert elb_client._find_raw_lbs_for_host(raw_lb["DNSName"]) == [raw_lb]


def test_failed_command_also_notes_missing_details(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """読めなかったものがあると「見つからない」の原因がそこにあるかもしれないので、失敗でも知らせる。"""
    real_paginator = elb_client.route53.get_paginator

    def get_paginator(name: str) -> Any:
        if name == "list_resource_record_sets":
            _deny()
        return real_paginator(name)

    elb_client.route53.get_paginator = get_paginator
    assert cli.main(["route", "https://nothing.elbx-test.internal/"], client=elb_client) == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("エラー: ")
    assert lines[-1].startswith("注意: ")


# =============================================================================
# ELBClient: session と region
# =============================================================================


def _session_stub(calls: list[dict[str, Any]], region_name: str | None = None) -> Any:
    """`client()` の引数を記録するだけの boto3.Session のスタブ。"""
    events = SimpleNamespace(register=lambda *_args: None)

    def client(_service: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(meta=SimpleNamespace(events=events))

    return SimpleNamespace(client=client, region_name=region_name)


def test_region_name_applies_to_a_given_session() -> None:
    """session を渡したときも、region_name はそのセッションから作るクライアントに効く。"""
    calls: list[dict[str, Any]] = []
    ELBClient(session=_session_stub(calls, "ap-northeast-1"), region_name="eu-west-1")
    assert calls
    assert all(c["region_name"] == "eu-west-1" for c in calls)


def test_session_without_region_name_keeps_its_region() -> None:
    """region_name を省けば、セッションのリージョンのまま（client() に region_name を渡さない）。"""
    calls: list[dict[str, Any]] = []
    ELBClient(session=_session_stub(calls, "ap-northeast-1"))
    assert calls
    assert all("region_name" not in c for c in calls)


def test_session_and_profile_together_is_a_value_error() -> None:
    """既存のセッションにプロファイルは適用できないので、黙って無視せずにエラーにする。"""
    with pytest.raises(ValueError, match="session と profile_name"):
        ELBClient(session=_session_stub([], "ap-northeast-1"), profile_name="prod")


def test_empty_region_is_reported_as_no_region() -> None:
    """空のリージョン（AWS_REGION= のような設定）は、リージョンなしと同じ失敗にする。"""
    with pytest.raises(AWSAPIError) as exc_info:
        ELBClient(session=_session_stub([], ""))
    assert isinstance(exc_info.value.__cause__, NoRegionError)


# =============================================================================
# 色: --color / --no-color / FORCE_COLOR / NO_COLOR
# =============================================================================


def test_color_option_forces_colors(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """--color は端末でなくても色を付け、--no-color と一緒には書けない。"""
    assert cli.main(["ls", "--color"], client=elb_client) == 0
    assert "\x1b[" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["ls", "--color", "--no-color"], client=elb_client)
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    ("environ", "colored"),
    [
        ({"FORCE_COLOR": "1"}, True),
        ({"NO_COLOR": "1"}, False),
        ({"FORCE_COLOR": "1", "NO_COLOR": "1"}, True),
        ({}, False),
    ],
    ids=["force", "no", "both", "neither"],
)
def test_color_environment_variables(
    elb_client: ELBClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
    colored: bool,
) -> None:
    """FORCE_COLOR は端末でなくても色を付け、NO_COLOR は付けない。両方あれば FORCE_COLOR。"""
    for name in ("FORCE_COLOR", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    assert cli.main(["ls"], client=elb_client) == 0
    assert ("\x1b[" in capsys.readouterr().out) is colored


# =============================================================================
# find: IP アドレスの一部へのヒント
# =============================================================================


@pytest.mark.parametrize(
    ("query", "hinted"),
    [
        ("10.0.9", True),
        ("10.0.9.", True),
        ("2001:db8:9", True),
        ("2001:db8:1:1::3", False),
        ("no-such-name", False),
        ("192.0.2.1", False),
    ],
)
def test_find_hints_when_the_text_looks_like_part_of_an_ip(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str], query: str, hinted: bool
) -> None:
    """IP アドレスの一部に見える文字列で 0 件のときだけ、全体を書くようにヒントを出す。"""
    assert cli.main(["find", query], client=elb_client) == 0
    captured = capsys.readouterr()
    assert captured.out == "該当するターゲットは見つかりませんでした。\n"
    assert ("IP アドレスは全体でしか" in captured.err) is hinted


def test_find_hint_is_not_shown_in_json(
    elb_client: ELBClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json のときはヒントを出さない（出力をそのまま読む側のため）。"""
    assert cli.main(["find", "10.0.9", "--json"], client=elb_client) == 0
    captured = capsys.readouterr()
    assert captured.out == "[]\n"
    assert captured.err == ""


# =============================================================================
# ヘルプの例の行は折り返さない
# =============================================================================


def test_help_examples_are_not_wrapped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """例の行は狭い端末でも折り返さない（# のあとの説明が次の行に落ちないように）。"""
    monkeypatch.setenv("COLUMNS", "60")
    with pytest.raises(SystemExit):
        cli.main(["--lang", "en", "--help"])
    out = capsys.readouterr().out
    assert (
        "  elbx targets --unhealthy                     # only the targets that need attention\n"
        in out
    )
