"""
The language of the text elbx shows (CLI help, errors, the labels and reasons in the output)
and the English / Japanese messages.

The language is, in this order: `set_language()` (the CLI's `--lang`), the `ELBX_LANG`
environment variable (`en` or `ja`), the locale environment variables (`LC_ALL`,
`LC_MESSAGES`, `LANG`), and on Windows the display language. Japanese gives `ja`;
anything else gives `en`.

elbx が出す文（CLI のヘルプ・エラー・出力のラベルや理由）の言語と、英語・日本語のメッセージ。

言語は次の順で決める: `set_language()`（CLI の `--lang`）、環境変数 `ELBX_LANG`（`en` か
`ja`）、ロケールの環境変数（`LC_ALL`・`LC_MESSAGES`・`LANG`）、Windows なら表示言語。
日本語なら `ja`、それ以外は `en`。
"""

from __future__ import annotations

import contextlib
import functools
import os
import sys
from collections.abc import Iterator, Mapping

from .errors import ELBError

LANGUAGES = ("en", "ja")
ENV_VAR = "ELBX_LANG"
# POSIX と同じ優先順（LC_ALL が最優先、次にメッセージの分類、最後に LANG）
_LOCALE_VARS = ("LC_ALL", "LC_MESSAGES", "LANG")

# set_language() で選んだ言語（None なら環境から決める）
_selected: str | None = None


def set_language(lang: str | None) -> None:
    """
    Choose the language of elbx's text: `"en"`, `"ja"`, or None to decide from the
    environment again (the default).

    elbx が出す文の言語を選ぶ（`"en"`・`"ja"`、None なら環境から決め直す。既定は None）。

    :param lang: `"en"`, `"ja"` or None / `"en"`・`"ja"`・None
    :raises ELBError: `lang` is not one of them / それ以外の値
    """
    global _selected
    if lang is not None and lang not in LANGUAGES:
        raise ELBError(f"unsupported language: {lang!r} (choose from: {', '.join(LANGUAGES)})")
    _selected = lang


@contextlib.contextmanager
def using_language(lang: str | None) -> Iterator[None]:
    """
    `with` の中だけ言語を `lang` にし、出るときに元に戻す（None なら変えない）。
    CLI の `--lang` が、同じプロセスで続けて使うライブラリの言語に残らないようにするため。

    :param lang: `"en"`・`"ja"`・None
    """
    global _selected
    previous = _selected
    if lang is not None:
        set_language(lang)
    try:
        yield
    finally:
        _selected = previous


def get_language() -> str:
    """
    Return the language in use: the one given to `set_language()`, otherwise the one
    decided from the environment.

    使っている言語を返す（`set_language()` で選んだもの、なければ環境から決めたもの）。

    :return: `"en"` or `"ja"`
    :raises ELBError: `ELBX_LANG` is set to something other than `en` / `ja`
        / `ELBX_LANG` が `en`・`ja` 以外
    """
    if _selected is not None:
        return _selected
    return detect_language(os.environ)


def detect_language(environ: Mapping[str, str]) -> str:
    """
    Decide the language from environment variables (`ELBX_LANG`, then `LC_ALL`,
    `LC_MESSAGES`, `LANG`), then the Windows display language. Japanese gives `"ja"`,
    anything else `"en"`.

    環境変数（`ELBX_LANG`、次に `LC_ALL`・`LC_MESSAGES`・`LANG`）、それもなければ Windows の
    表示言語から言語を決める。日本語なら `"ja"`、それ以外は `"en"`。

    :param environ: the environment variables / 環境変数
    :return: `"en"` or `"ja"`
    :raises ELBError: `ELBX_LANG` is set to something other than `en` / `ja`
        / `ELBX_LANG` が `en`・`ja` 以外
    """
    # 空の値は POSIX と同じく「設定なし」として扱う（`LANG=` で打ち消せるように）
    forced = environ.get(ENV_VAR, "")
    if forced:
        if forced not in LANGUAGES:
            # どの言語で出すか決められないので、この1文だけは英語で出す
            raise ELBError(f"{ENV_VAR} must be one of: {', '.join(LANGUAGES)} (got {forced!r})")
        return forced
    for name in _LOCALE_VARS:
        value = environ.get(name, "")
        if value:
            # ja_JP.UTF-8・ja・ja_JP など。C・POSIX・C.UTF-8 やほかの言語は英語
            return "ja" if value.lower().startswith("ja") else "en"
    return _windows_ui_language() or "en"


@functools.cache
def _windows_ui_language() -> str | None:
    """
    Windows の表示言語が日本語なら `"ja"`、それ以外の Windows なら `"en"`、Windows でなければ
    None を返す（Windows ではロケールの環境変数がふつうは設定されないため）。

    :return: `"ja"`・`"en"`・None
    """
    if sys.platform != "win32":
        return None
    with contextlib.suppress(Exception):
        import ctypes

        # LANGID の下位 10 ビットが主言語。0x11 が日本語
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "ja" if lang_id & 0x3FF == 0x11 else "en"
    return "en"


def t(key: str, /, **values: object) -> str:
    """
    今の言語のメッセージを返す。`{…}` の穴は `values` で埋める。

    :param key: `MESSAGES` のキー
    :param values: 穴に入れる値
    :return: メッセージ
    """
    return MESSAGES[get_language()][key].format(**values)


# 利用者に見える文はすべてここに書く。キーと {…} の穴は en と ja でそろえる
# （tests/test_messages.py で確かめる）
MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # ---- CLI のヘルプ
        "cli.description": (
            "Read-only CLI for AWS ALB / NLB: shows a load balancer with everything behind it "
            "(Route 53 / EC2 / ECS / Lambda / SG / subnet / ACM), finds which load balancer a "
            "target is behind, and works out where a request goes"
        ),
        "cli.usage": "usage: ",
        "cli.group.commands": "commands",
        "cli.group.command": "{command} options",
        "cli.group.aws": "AWS connection",
        "cli.group.output": "output and help",
        "cli.help.help": "show this help and exit",
        "cli.help.version": "show the version and exit",
        "cli.examples.main": (
            "Without a command, elbx runs tree: 'elbx' shows every load balancer, and "
            "'elbx --full' is 'elbx tree --full'. To show one load balancer, write tree: "
            "'elbx tree my-alb' ('elbx my-alb' is an error).\n"
            "\n"
            "examples:\n"
            "  elbx ls                                      # list the load balancers\n"
            "  elbx tree my-alb                             # one load balancer as a tree\n"
            "  elbx route https://api.example.org/v1/users  # where does this request go?\n"
            "  elbx targets --unhealthy                     # only the targets that need "
            "attention\n"
            "  elbx find web-01                             # which load balancer is it behind?\n"
            "\n"
            "'elbx <command> --help' shows the options of each command."
        ),
        "cli.examples.ls": (
            "examples:\n"
            "  elbx ls          # every load balancer\n"
            "  elbx ls -s prod  # load balancers with prod in the name"
        ),
        "cli.examples.tree": (
            "examples:\n"
            "  elbx tree my-alb         # relationship map of one load balancer\n"
            "  elbx tree my-alb --full  # everything: attributes, SGs, subnets, targets\n"
            "  elbx tree -s prod        # load balancers with prod in the name\n"
            "  elbx tree my-alb --json  # everything as JSON"
        ),
        "cli.examples.targets": (
            "examples:\n"
            "  elbx targets --unhealthy         # only the targets that need attention\n"
            "  elbx targets my-alb              # the target groups of one load balancer\n"
            "  elbx targets --tg my-tg          # one target group\n"
            "  elbx targets -s api --unhealthy  # target groups with api in the name"
        ),
        "cli.examples.find": (
            "examples:\n"
            "  elbx find web-01      # an EC2 Name tag or instance ID\n"
            "  elbx find 10.0.1.11   # an IP address (the whole address)\n"
            "  elbx find my-service  # an ECS service, a Lambda function, an ELB"
        ),
        "cli.examples.route": (
            "examples:\n"
            "  elbx route https://api.example.org/v1/users\n"
            "  elbx route https://api.example.org/x -X POST -H 'X-Env: test' "
            "--source-ip 203.0.113.10\n"
            "  elbx route https://new.example.org/ --lb my-alb   # a host not in Route 53 yet"
        ),
        "cli.help.profile": "AWS CLI profile to use",
        "cli.help.region": "AWS region (e.g. ap-northeast-1)",
        "cli.help.color": (
            "color the output even when it is not a terminal (a pipe to less -R, a CI log); "
            "same as the FORCE_COLOR environment variable"
        ),
        "cli.help.no_color": "turn off colors (same as the NO_COLOR environment variable)",
        "cli.help.verbose": (
            "show details on stderr: -v what could not be fetched, -vv also every AWS API call"
        ),
        "cli.help.workers": "parallel AWS API calls (default: 10)",
        "cli.help.lang": (
            "language of the output: en or ja (default: ELBX_LANG, then the OS language)"
        ),
        "cli.help.json": "print JSON",
        "cli.help.no_dns": "do not scan the Route 53 zones (faster with many zones)",
        "cli.help.ls": "list the load balancers with their target health",
        "cli.help.search": "only load balancers whose name or DNS name contains this text",
        "cli.help.tree": (
            "load balancer -> listeners -> rules -> target groups -> targets as a tree"
        ),
        "cli.help.tree.name_or_arn": "load balancer name or ARN (default: all load balancers)",
        "cli.help.tree.full": (
            "everything: attributes, subnets, SGs, certificates, target details (default: the "
            "relationship map, listener -> rule -> TG -> target name)"
        ),
        "cli.help.tree.max_targets": (
            "targets named per TG in the relationship map (default: {default}, 0 = all); "
            "targets that are not healthy are always named"
        ),
        "cli.help.tree.no_network": "with --full, skip subnets and security groups",
        "cli.help.targets": ("list target groups and their targets with health, AZ, subnet and SG"),
        "cli.help.targets.lb": "load balancer name or ARN (default: every target group)",
        "cli.help.targets.tg": "only this target group (name or ARN)",
        "cli.help.targets.search": "only target groups whose name contains this text",
        "cli.help.targets.unhealthy": (
            "only the targets that need attention (not healthy, except a Lambda with health "
            "checks disabled and a target group no load balancer uses), with the health "
            "details and the health check settings"
        ),
        "cli.help.find": (
            "find the load balancers and TGs of an EC2 name / ID / IP, ECS service / cluster, "
            "Lambda function or ALB / NLB name"
        ),
        "cli.help.find.query": "text to look for (substring; an IP address must match as a whole)",
        "cli.help.route": (
            "which listener rule a request to the URL matches, and which TG and targets it reaches"
        ),
        "cli.help.route.url": "URL to evaluate (e.g. https://api.example.com/v1/users?id=1)",
        "cli.help.route.lb": (
            "entry load balancer name or ARN (default: found from the host name via Route 53 / "
            "DNS names)"
        ),
        "cli.help.route.method": "HTTP method (default: GET)",
        "cli.help.route.header": "request header (repeatable)",
        "cli.help.route.source_ip": "client IP (for source-ip conditions)",
        # ---- CLI の引数の誤り
        "cli.arg.positive_int": "give an integer of 1 or more: {value}",
        "cli.arg.integer": "give an integer: {value}",
        "cli.arg.non_negative_int": "give an integer of 0 or more: {value}",
        "cli.arg.header": "give a header as 'Name: value': {value}",
        "cli.arg.unknown_command": (
            "'{command}' is not a command (choose from {choices}). "
            "To show one load balancer: elbx tree {command}"
        ),
        "cli.arg.search_with_single": (
            "-s/--search filters a list and cannot be used with {option}, which already selects one"
        ),
        "cli.arg.ip_address": "give an IP address: {value}",
        # ---- argparse 自身の文（%s と %(name)s は argparse が埋めるので、そのまま残す）
        "cli.argparse.error": "%(prog)s: error: %(message)s\n",
        "cli.argparse.argument": "argument %(argument_name)s: %(message)s",
        "cli.argparse.required": "the following arguments are required: %s",
        "cli.argparse.not_allowed_with": "not allowed with argument %s",
        "cli.argparse.expected_one": "expected one argument",
        "cli.argparse.unrecognized": "unrecognized arguments: %s",
        "cli.argparse.invalid_choice": "invalid choice: %(value)r (choose from %(choices)s)",
        "cli.argparse.ambiguous": "ambiguous option: %(option)s could match %(matches)s",
        "cli.argparse.explicit_value": "ignored explicit argument %r",
        # ---- ログ
        "cli.partial_result": (
            "Note: {count} request(s) for details failed, so some information may be missing "
            "(-v shows which)."
        ),
        "log.skipped": "could not fetch {what}{target}, continuing without it: {error}",
        "log.api_call": "{service}.{operation}: HTTP {status}, {ms} ms",
        "log.what.lb_attributes": "load balancer attributes",
        "log.what.tg_attributes": "target group attributes",
        "log.what.ec2_instances": "EC2 instances",
        "log.what.network_interfaces": "network interfaces",
        "log.what.load_balancers": "the load balancer of a network interface",
        "log.what.downstream_lbs": "downstream load balancers",
        "log.what.target_groups": "target groups referenced by rules",
        "log.what.ecs": "ECS clusters, services and tasks",
        "log.what.lambda": "Lambda function details",
        "log.what.subnets": "subnets",
        "log.what.security_groups": "security groups",
        "log.what.certificate": "ACM certificate details",
        "log.what.route53": "Route 53 records",
        "log.what.client": "an AWS client",
        # ---- CLI の結果とエラー
        "cli.no_unhealthy": "No targets need attention.",
        "cli.error": "Error: {message}",
        "cli.hint": "Hint: {hint}",
        "cli.hint.region": "set the region with --region or the AWS_REGION environment variable.",
        "cli.hint.partial_ip": (
            "an IP address only matches as a whole: give the whole address (e.g. 10.0.1.11) "
            "or search by name."
        ),
        "cli.hint.credentials": (
            "set up credentials (aws configure, environment variables) or choose a profile "
            "with --profile."
        ),
        "cli.hint.sso": "sign in again with: aws sso login{profile}",
        "cli.hint.expired": (
            "the credentials have expired or are not valid. Sign in again, then check with: "
            "aws sts get-caller-identity{profile}"
        ),
        "cli.hint.route53": (
            "name the entry load balancer with --lb; then elbx does not need to read Route 53."
        ),
        "cli.hint.endpoint": (
            "check the region name ({region}) and the network connection. "
            "The region is set with --region or AWS_REGION."
        ),
        "cli.hint.is_target_group": (
            "'{name}' is the name of a target group. To show it: elbx targets --tg {name}"
        ),
        "cli.hint.is_load_balancer": (
            "'{name}' is the name of a load balancer. To show its target groups: "
            "elbx targets {name}"
        ),
        "cli.hint.route_needs_url": (
            "route takes the URL of a request, e.g. elbx route https://api.example.com/v1/users"
        ),
        "cli.unexpected_error": "Unexpected error: {message}",
        # ---- ELBClient の例外
        "client.aws_error": "AWS API call failed while {action}: {error}",
        "client.action.connect": "preparing the AWS clients",
        "client.action.list_load_balancers": "listing load balancers",
        "client.action.get_load_balancer": "getting the load balancer",
        "client.action.list_target_groups": "listing target groups",
        "client.action.get_target_group": "getting the target group",
        "client.action.find_target": "looking up targets",
        "client.action.simulate_request": "simulating the route",
        "client.lb_not_found": "load balancer not found: {name}",
        "client.lb_not_found_detail": "load balancer not found: {name} ({error})",
        "client.tg_not_found": "target group not found: {name}",
        "client.tg_not_found_detail": "target group not found: {name} ({error})",
        "client.session_and_profile": (
            "give session or profile_name, not both (a profile cannot be applied to an "
            "existing boto3.Session; create the session from the profile instead)"
        ),
        "client.empty_query": "the search text is empty",
        "client.bad_request": "cannot read the request: {url} ({error})",
        "client.no_lb_for_host": (
            "no load balancer serves the host name '{host}' (checked the Route 53 records and "
            "the load balancers' DNS names). Give the entry load balancer with --lb."
        ),
        # ---- 出力のラベル
        "fmt.no_load_balancers": "No load balancers found.",
        "fmt.no_target_groups": "No target groups found.",
        "fmt.no_matching_targets": "No matching targets found.",
        "fmt.no_targets_registered": "(no targets registered)",
        "fmt.more_healthy": "… {count} more (healthy)",
        "fmt.expanded_above": "(expanded above)",
        "fmt.unknown_lb": "(unknown load balancer)",
        "fmt.no_lb": "(not attached to a load balancer)",
        "fmt.ls_attention": "attention: {count}",
        "fmt.no_name_tag": "(no Name tag)",
        "fmt.route_entry": "Entry: ",
        "fmt.route_downstream": "↓ Forwarded to: ",
        "fmt.route_no_traffic": "(receives no requests)",
        "model.default_rule": "DEFAULT (every request no other rule matched)",
        # ---- route の評価
        "sim.bad_scheme": "only http and https are supported: {scheme}",
        "sim.no_host": "the host name is missing",
        "sim.bad_source_ip": "the source IP is not an IP address: {value}",
        "sim.matched": "{label} '{actual}' matches [{values}]",
        "sim.not_matched": "{label} '{actual}' matches none of [{values}]",
        "sim.header_missing": "Header({name}) is not in the request (give it with -H)",
        "sim.no_query": "(none)",
        "sim.no_source_ip": (
            "no source IP given, so taken as not matching (give it with --source-ip)"
        ),
        "sim.regex_condition": (
            "{field} regular expressions [{values}] cannot be evaluated, so taken as not matching"
        ),
        "sim.unsupported_condition": "unsupported condition '{field}', so taken as not matching",
        "sim.default_applied": "no rule matched, so the default rule applies",
        "sim.no_listeners": "none",
        "sim.no_listener_on_port": "no listener on port {port} (listeners: {ports})",
        "sim.no_matching_rule": "no rule matched (the default rule could not be read)",
        "sim.too_deep": "stopped after {depth} levels of downstream load balancers",
    },
    "ja": {
        # ---- CLI のヘルプ
        "cli.description": (
            "AWS の ALB / NLB を調べる参照専用の CLI。ELB の配下と関連リソース"
            "（Route 53 / EC2 / ECS / Lambda / SG / サブネット / ACM）を表示し、ターゲットが"
            "どの ELB の裏にいるかを逆引きし、リクエストがどこへ届くかを確かめる"
        ),
        "cli.usage": "使い方: ",
        "cli.group.commands": "コマンド",
        "cli.group.command": "{command} のオプション",
        "cli.group.aws": "AWS への接続",
        "cli.group.output": "出力とヘルプ",
        "cli.help.help": "このヘルプを表示して終了する",
        "cli.help.version": "バージョンを表示して終了する",
        "cli.examples.main": (
            "コマンドを省略すると tree になる: 'elbx' だけで全 ELB を表示し、"
            "'elbx --full' は 'elbx tree --full' と同じ。1 つの ELB を表示するときは tree を書く: "
            "'elbx tree my-alb'（'elbx my-alb' はエラー）。\n"
            "\n"
            "例:\n"
            "  elbx ls                                      # ロードバランサーの一覧\n"
            "  elbx tree my-alb                             # 1 つの ELB をツリーで\n"
            "  elbx route https://api.example.org/v1/users  # このリクエストはどこへ行く？\n"
            "  elbx targets --unhealthy                     # 調べるべきターゲットだけ\n"
            "  elbx find web-01                             # どの ELB の裏にいる？\n"
            "\n"
            "各コマンドのオプション: 'elbx <コマンド> --help'"
        ),
        "cli.examples.ls": (
            "例:\n"
            "  elbx ls          # すべてのロードバランサー\n"
            "  elbx ls -s prod  # 名前に prod を含むロードバランサー"
        ),
        "cli.examples.tree": (
            "例:\n"
            "  elbx tree my-alb         # 1 つの ELB の関連図\n"
            "  elbx tree my-alb --full  # すべて: 属性・SG・サブネット・ターゲットの詳細\n"
            "  elbx tree -s prod        # 名前に prod を含むロードバランサー\n"
            "  elbx tree my-alb --json  # すべてを JSON で"
        ),
        "cli.examples.targets": (
            "例:\n"
            "  elbx targets --unhealthy         # 調べるべきターゲットだけ\n"
            "  elbx targets my-alb              # 1 つの ELB のターゲットグループ\n"
            "  elbx targets --tg my-tg          # 1 つのターゲットグループ\n"
            "  elbx targets -s api --unhealthy  # 名前に api を含むターゲットグループ"
        ),
        "cli.examples.find": (
            "例:\n"
            "  elbx find web-01      # EC2 の Name タグかインスタンス ID\n"
            "  elbx find 10.0.1.11   # IP アドレス（全体を書く）\n"
            "  elbx find my-service  # ECS サービス・Lambda 関数・ELB の名前"
        ),
        "cli.examples.route": (
            "例:\n"
            "  elbx route https://api.example.org/v1/users\n"
            "  elbx route https://api.example.org/x -X POST -H 'X-Env: test' "
            "--source-ip 203.0.113.10\n"
            "  elbx route https://new.example.org/ --lb my-alb   # まだ Route 53 にないホスト名"
        ),
        "cli.help.profile": "AWS CLI のプロファイル",
        "cli.help.region": "AWS リージョン（例: ap-northeast-1）",
        "cli.help.color": (
            "端末でなくても色を付ける（less -R へのパイプ、CI のログなど）。"
            "環境変数 FORCE_COLOR でも同じ"
        ),
        "cli.help.no_color": "色を付けない（環境変数 NO_COLOR でも同じ）",
        "cli.help.verbose": (
            "詳細を標準エラー出力に出す: -v は取得できなかったもの、-vv は AWS API の呼び出しも"
        ),
        "cli.help.workers": "AWS API を同時に呼ぶ数（デフォルト: 10）",
        "cli.help.lang": "出力の言語: en か ja（デフォルト: ELBX_LANG、なければ OS の言語）",
        "cli.help.json": "JSON で出力する",
        "cli.help.no_dns": "Route 53 のゾーンを走査しない（ゾーンが多いと速くなる）",
        "cli.help.ls": "ロードバランサーの一覧をヘルスの集計つきで表示する",
        "cli.help.search": "名前か DNS 名にこの文字列を含むロードバランサーだけ",
        "cli.help.tree": "ELB -> リスナー -> ルール -> TG -> ターゲットをツリーで表示する",
        "cli.help.tree.name_or_arn": "ロードバランサーの名前か ARN（省略時は全 ELB）",
        "cli.help.tree.full": (
            "すべて出す: 属性・サブネット・SG・証明書・ターゲットの詳細"
            "（省略時は関連図: リスナー -> ルール -> TG -> ターゲット名）"
        ),
        "cli.help.tree.max_targets": (
            "関連図で TG ごとに名前を出すターゲットの数（デフォルト: {default}、0 で全部）。"
            "healthy 以外は常に出す"
        ),
        "cli.help.tree.no_network": "--full のとき、サブネットと SG を取得しない",
        "cli.help.targets": "TG とターゲットを、ヘルス・AZ・サブネット・SG つきで一覧にする",
        "cli.help.targets.lb": "このロードバランサー（名前か ARN）の TG だけ（省略時は全 TG）",
        "cli.help.targets.tg": "この TG（名前か ARN）だけ",
        "cli.help.targets.search": "名前にこの文字列を含む TG だけ",
        "cli.help.targets.unhealthy": (
            "調べるべきターゲットだけ（healthy 以外。ヘルスチェックが無効な Lambda と、"
            "どの ELB にも使われていない TG は除く）。状態の詳細とヘルスチェック設定も出す"
        ),
        "cli.help.find": (
            "EC2 の名前・ID・IP、ECS のサービス / クラスター、Lambda 関数、ALB / NLB の名前から、"
            "登録先の ELB・TG を逆引きする"
        ),
        "cli.help.find.query": "探す文字列（部分一致。IP アドレスは全体で一致）",
        "cli.help.route": (
            "URL へのリクエストがどのリスナールールに一致し、どの TG・ターゲットへ届くかを評価する"
        ),
        "cli.help.route.url": "評価する URL（例: https://api.example.com/v1/users?id=1）",
        "cli.help.route.lb": (
            "入口のロードバランサーの名前か ARN（省略時はホスト名から Route 53 / DNS 名で探す）"
        ),
        "cli.help.route.method": "HTTP メソッド（デフォルト: GET）",
        "cli.help.route.header": "リクエストヘッダー（複数可）",
        "cli.help.route.source_ip": "送信元 IP（source-ip 条件に使う）",
        # ---- CLI の引数の誤り
        "cli.arg.positive_int": "1 以上の整数を指定してください: {value}",
        "cli.arg.integer": "整数を指定してください: {value}",
        "cli.arg.non_negative_int": "0 以上の整数を指定してください: {value}",
        "cli.arg.header": "ヘッダーは 'Name: value' 形式で指定してください: {value}",
        "cli.arg.unknown_command": (
            "'{command}' はコマンドではありません（{choices} のどれか）。"
            "1 つの ELB を表示するには: elbx tree {command}"
        ),
        "cli.arg.search_with_single": (
            "-s/--search は一覧を絞るオプションなので、1 つに決める {option} とは一緒に使えません"
        ),
        "cli.arg.ip_address": "IP アドレスを指定してください: {value}",
        # ---- argparse 自身の文（%s と %(name)s は argparse が埋めるので、そのまま残す）
        "cli.argparse.error": "%(prog)s: エラー: %(message)s\n",
        "cli.argparse.argument": "引数 %(argument_name)s: %(message)s",
        "cli.argparse.required": "次の引数が必要です: %s",
        "cli.argparse.not_allowed_with": "引数 %s とは一緒に使えません",
        "cli.argparse.expected_one": "値を 1 つ指定してください",
        "cli.argparse.unrecognized": "知らない引数です: %s",
        "cli.argparse.invalid_choice": "%(value)r は選べません（%(choices)s のどれか）",
        "cli.argparse.ambiguous": "%(option)s は %(matches)s のどれにも読めます",
        "cli.argparse.explicit_value": "値 %r は付けられません",
        # ---- ログ
        "cli.partial_result": (
            "注意: 詳細の取得に {count} 回失敗したため、一部の情報が欠けている場合があります"
            "（-v で確認できます）。"
        ),
        "log.skipped": "{what}{target} を取得できなかったため、なしで続けます: {error}",
        "log.api_call": "{service}.{operation}: HTTP {status}, {ms} ms",
        "log.what.lb_attributes": "ELB の属性",
        "log.what.tg_attributes": "TG の属性",
        "log.what.ec2_instances": "EC2 インスタンス",
        "log.what.network_interfaces": "ENI",
        "log.what.load_balancers": "ENI の ELB",
        "log.what.downstream_lbs": "下流の ELB",
        "log.what.target_groups": "ルールが参照する TG",
        "log.what.ecs": "ECS のクラスター・サービス・タスク",
        "log.what.lambda": "Lambda 関数の詳細",
        "log.what.subnets": "サブネット",
        "log.what.security_groups": "SG",
        "log.what.certificate": "ACM 証明書の詳細",
        "log.what.route53": "Route 53 のレコード",
        "log.what.client": "AWS のクライアント",
        # ---- CLI の結果とエラー
        "cli.no_unhealthy": "調べるべきターゲットはありません。",
        "cli.error": "エラー: {message}",
        "cli.hint": "ヒント: {hint}",
        "cli.hint.region": "--region か環境変数 AWS_REGION でリージョンを指定してください。",
        "cli.hint.partial_ip": (
            "IP アドレスは全体でしか一致しません。アドレス全体（例: 10.0.1.11）を書くか、"
            "名前で検索してください。"
        ),
        "cli.hint.credentials": (
            "認証情報を設定する（aws configure、環境変数）か、--profile でプロファイルを"
            "指定してください。"
        ),
        "cli.hint.sso": "次のコマンドでサインインし直してください: aws sso login{profile}",
        "cli.hint.expired": (
            "認証情報の期限が切れているか、無効です。サインインし直してから、次のコマンドで"
            "確認してください: aws sts get-caller-identity{profile}"
        ),
        "cli.hint.route53": (
            "--lb で入口の ELB を指定してください。指定すれば Route 53 を読む必要はありません。"
        ),
        "cli.hint.endpoint": (
            "リージョン名（{region}）とネットワークの接続を確かめてください。"
            "リージョンは --region か AWS_REGION で指定します。"
        ),
        "cli.hint.is_target_group": (
            "'{name}' はターゲットグループの名前です。中身を見るには: elbx targets --tg {name}"
        ),
        "cli.hint.is_load_balancer": (
            "'{name}' はロードバランサーの名前です。その TG を見るには: elbx targets {name}"
        ),
        "cli.hint.route_needs_url": (
            "route にはリクエストの URL を指定します。例: elbx route https://api.example.com/v1/users"
        ),
        "cli.unexpected_error": "予期しないエラー: {message}",
        # ---- ELBClient の例外
        "client.aws_error": "{action}で AWS API の呼び出しに失敗しました: {error}",
        "client.action.connect": "AWS への接続の準備",
        "client.action.list_load_balancers": "ロードバランサーの一覧の取得",
        "client.action.get_load_balancer": "ロードバランサーの取得",
        "client.action.list_target_groups": "ターゲットグループの一覧の取得",
        "client.action.get_target_group": "ターゲットグループの取得",
        "client.action.find_target": "逆引き検索",
        "client.action.simulate_request": "ルーティング先のシミュレーション",
        "client.lb_not_found": "ロードバランサーが見つかりません: {name}",
        "client.lb_not_found_detail": "ロードバランサーが見つかりません: {name} ({error})",
        "client.tg_not_found": "ターゲットグループが見つかりません: {name}",
        "client.tg_not_found_detail": "ターゲットグループが見つかりません: {name} ({error})",
        "client.session_and_profile": (
            "session と profile_name はどちらか一方だけ指定してください（既存の boto3.Session に"
            "プロファイルは適用できません。プロファイルからセッションを作って渡してください）"
        ),
        "client.empty_query": "検索キーワードが空です",
        "client.bad_request": "リクエストを解釈できません: {url} ({error})",
        "client.no_lb_for_host": (
            "ホスト名 '{host}' を向いている ELB が見つかりません"
            "（Route 53 のレコードと ELB の DNS 名を確認しました）。"
            "--lb で入口の ELB を指定してください。"
        ),
        # ---- 出力のラベル
        "fmt.no_load_balancers": "ロードバランサーが見つかりませんでした。",
        "fmt.no_target_groups": "ターゲットグループが見つかりませんでした。",
        "fmt.no_matching_targets": "該当するターゲットは見つかりませんでした。",
        "fmt.no_targets_registered": "(ターゲット未登録)",
        "fmt.more_healthy": "… ほか {count} 台 (healthy)",
        "fmt.expanded_above": "(上に展開済み)",
        "fmt.unknown_lb": "(不明なELB)",
        "fmt.no_lb": "(ELB未アタッチ)",
        "fmt.ls_attention": "要確認: {count}",
        "fmt.no_name_tag": "(Name未設定)",
        "fmt.route_entry": "入口: ",
        "fmt.route_downstream": "↓ 転送先: ",
        "fmt.route_no_traffic": "(リクエストは届きません)",
        "model.default_rule": "DEFAULT (すべての未一致リクエスト)",
        # ---- route の評価
        "sim.bad_scheme": "http / https 以外のスキームには対応していません: {scheme}",
        "sim.no_host": "ホスト名がありません",
        "sim.bad_source_ip": "送信元 IP が IP アドレスではありません: {value}",
        "sim.matched": "{label} '{actual}' は [{values}] に一致",
        "sim.not_matched": "{label} '{actual}' は [{values}] のいずれにも一致しません",
        "sim.header_missing": "Header({name}) がリクエストにありません（-H で指定できます）",
        "sim.no_query": "(なし)",
        "sim.no_source_ip": (
            "送信元IPが未指定のため不一致として扱いました（--source-ip で指定できます）"
        ),
        "sim.regex_condition": (
            "{field} の正規表現の条件 [{values}] は評価できないため不一致として扱いました"
        ),
        "sim.unsupported_condition": "未対応の条件 '{field}' のため不一致として扱いました",
        "sim.default_applied": "どのルールにも一致しなかったため default を適用",
        "sim.no_listeners": "なし",
        "sim.no_listener_on_port": (
            "ポート {port} のリスナーがありません（既存のリスナー: {ports}）"
        ),
        "sim.no_matching_rule": (
            "一致するルールがありません（default ルールを取得できませんでした）"
        ),
        "sim.too_deep": "下流のロードバランサーが {depth} 段を超えたため打ち切りました",
    },
}
