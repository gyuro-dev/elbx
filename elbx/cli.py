"""
The `elbx` command (argparse): `ls`, `tree`, `targets`, `find` and `route`. Read-only.

`elbx` コマンド（argparse）。`ls`・`tree`・`targets`・`find`・`route`。参照専用。
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import errno
import logging
import os
import re
import sys
import textwrap
from collections.abc import Callable, Iterator, Sequence

from botocore.exceptions import ClientError

from . import __version__
from ._ids import canonical_ip
from .client import ELBClient
from .errors import ELBError, ResourceNotFoundError
from .fetchers import error_code, logger
from .formatter import (
    DEFAULT_MAX_TARGETS,
    _escape_controls,
    format_lb_map,
    format_lb_summary_table,
    format_lb_tree,
    format_lookup_results_table,
    format_route_simulation,
    format_targets_table,
)
from .messages import LANGUAGES, MESSAGES, get_language, t, using_language
from .models import TargetGroupView, _dumps_json

# =============================================================================
# 引数の型
# =============================================================================


def _positive_int(value: str) -> int:
    """
    1 以上の整数を受け取る argparse の型（0 や負の数を、黙って別の値に直さずに弾くため）。

    :param value: コマンドラインで指定された文字列
    :return: 整数
    :raises argparse.ArgumentTypeError: 整数でない、または 1 未満の場合
    """
    number = _non_negative_int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(t("cli.arg.positive_int", value=value))
    return number


def _non_negative_int(value: str) -> int:
    """
    0 以上の整数を受け取る argparse の型。

    :param value: コマンドラインで指定された文字列
    :return: 整数
    :raises argparse.ArgumentTypeError: 整数でない、または負の数の場合
    """
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(t("cli.arg.integer", value=value)) from None
    if number < 0:
        raise argparse.ArgumentTypeError(t("cli.arg.non_negative_int", value=value))
    return number


def _parse_header(value: str) -> tuple[str, str]:
    """
    `'Name: value'` 形式のHTTPヘッダー指定文字列を `(ヘッダー名, 値)` のタプルに分解する。

    :param value: コマンドラインで指定された `'Name: value'` 形式の文字列
    :return: 前後の空白を除去した `(ヘッダー名, 値)` のタプル
    :raises argparse.ArgumentTypeError: `:` がない、またはヘッダー名が空の場合
    """
    name, sep, val = value.partition(":")
    # 名前が空のヘッダー（': x'）はどの http-header 条件にも一致しないので、指定の誤りとして弾く
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(t("cli.arg.header", value=value))
    return name.strip(), val.strip()


def _ip_address(value: str) -> str:
    """
    IP アドレス（IPv4 / IPv6）を受け取る argparse の型。

    :param value: コマンドラインで指定された文字列
    :return: 指定された文字列のまま
    :raises argparse.ArgumentTypeError: IP アドレスでない場合（空文字を含む）
    """
    if canonical_ip(value) is None:
        raise argparse.ArgumentTypeError(t("cli.arg.ip_address", value=value))
    return value


# argparse 自身の文（英語）-> messages.py のキー。argparse は文を `argparse._`（gettext）で
# 引くので、引数を読むあいだだけ差し替えて、選んだ言語にする。ここにない文（Python の版で文面が
# 違うもの）は英語のまま出る
_ARGPARSE_MESSAGES = {
    "%(prog)s: error: %(message)s\n": "cli.argparse.error",
    "argument %(argument_name)s: %(message)s": "cli.argparse.argument",
    "the following arguments are required: %s": "cli.argparse.required",
    "not allowed with argument %s": "cli.argparse.not_allowed_with",
    "expected one argument": "cli.argparse.expected_one",
    "unrecognized arguments: %s": "cli.argparse.unrecognized",
    "invalid choice: %(value)r (choose from %(choices)s)": "cli.argparse.invalid_choice",
    "ambiguous option: %(option)s could match %(matches)s": "cli.argparse.ambiguous",
    "ignored explicit argument %r": "cli.argparse.explicit_value",
}


@contextlib.contextmanager
def _argparse_messages() -> Iterator[None]:
    """`with` の中だけ、argparse 自身のエラーの文を選んだ言語にする。"""
    original = getattr(argparse, "_", None)
    if original is None:  # argparse の作りが変わっていたら、英語のままにする
        yield
        return

    def translate(message: str) -> str:
        key = _ARGPARSE_MESSAGES.get(message)
        return MESSAGES[get_language()][key] if key else str(original(message))

    argparse._ = translate  # type: ignore[attr-defined]
    try:
        yield
    finally:
        argparse._ = original  # type: ignore[attr-defined]


# =============================================================================
# パーサー（ヘルプは「そのコマンドのオプション」「AWS への接続」「出力とヘルプ」の見出しに分ける）
# =============================================================================


# ヘルプの例の行（折り返さない）
_EXAMPLE_LINE = re.compile(r"^\s+elbx\b")
# `route` の URL が、スキームもドットもスラッシュもない 1 語か（名前を渡した取り違えを見分ける）
_BARE_WORD = re.compile(r"^[A-Za-z0-9_-]+$")

# `find` に IP アドレスの一部らしいものが渡されたか（IPv4 の数字とドット、IPv6 の 16 進とコロン）
_PARTIAL_IP = re.compile(r"^(?:[0-9]+(?:\.[0-9]*)+|[0-9a-fA-F:.]*:[0-9a-fA-F:.]*)$")


class _HelpFormatter(argparse.HelpFormatter):
    """説明と例の改行を保ったまま折り返し、`usage:` も選んだ言語で出すヘルプの書式。"""

    def _format_action(self, action: argparse.Action) -> str:
        """コマンドの一覧に `<command>` の行を出さず、コマンドだけを並べる。"""
        if isinstance(action, argparse._SubParsersAction):
            return "".join(self._format_action(sub) for sub in action._get_subactions())
        return super()._format_action(action)

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        """
        行ごとに折り返す（例の一覧を 1 段落にまとめない）。コマンドの例の行（`  elbx ...`）は
        折り返さない。折り返すと `#` のあとの説明が字下げなしで次の行に落ち、コマンドの一部に
        見えるため。
        """
        return "\n".join(
            indent + line
            if _EXAMPLE_LINE.match(line)
            else textwrap.fill(line, width, initial_indent=indent, subsequent_indent=indent)
            if line
            else ""
            for line in text.splitlines()
        )

    def add_usage(
        self,
        usage: str | None,
        actions: object,
        groups: object,
        prefix: str | None = None,
    ) -> None:
        """`usage:` を選んだ言語にする。"""
        if prefix is None:
            prefix = t("cli.usage")
        super().add_usage(usage, actions, groups, prefix)  # type: ignore[arg-type]


@dataclasses.dataclass
class _HelpGroups:
    """1 つのパーサーのヘルプの見出し（オプションはどれかに入れる）。"""

    own: argparse._ArgumentGroup
    aws: argparse._ArgumentGroup
    output: argparse._ArgumentGroup


def _add_common_arguments(groups: _HelpGroups, is_subcommand: bool = False) -> None:
    """
    全サブコマンド共通のAWS接続・表示オプションを追加する。

    共通オプションはサブコマンドの前（`elbx --profile x ls`）にも後（`elbx ls --profile x`）にも
    書けるよう、メインとサブコマンドの両方に追加する。サブコマンド側で既定値を持つと、
    サブコマンドの前に書いた値が既定値で上書きされてしまうため、サブコマンド側は既定値を
    持たせない（argparse.SUPPRESS）。既定値はメインのパーサーだけが持つ。

    :param groups: オプションを追加するパーサーの見出し
    :param is_subcommand: サブコマンド用パーサーかどうか
        （True の場合は default=argparse.SUPPRESS にする）
    """

    def default(value: object) -> object:
        """メインパーサーなら value、サブコマンドパーサーなら argparse.SUPPRESS を返す。"""
        return argparse.SUPPRESS if is_subcommand else value

    groups.aws.add_argument(
        "-p", "--profile", metavar="NAME", default=default(None), help=t("cli.help.profile")
    )
    groups.aws.add_argument(
        "-r", "--region", metavar="REGION", default=default(None), help=t("cli.help.region")
    )
    groups.aws.add_argument(
        "-j",
        "--workers",
        type=_positive_int,
        metavar="N",
        default=default(10),
        help=t("cli.help.workers"),
    )
    color = groups.output.add_mutually_exclusive_group()
    color.add_argument(
        "--color", action="store_true", default=default(False), help=t("cli.help.color")
    )
    color.add_argument(
        "--no-color", action="store_true", default=default(False), help=t("cli.help.no_color")
    )
    groups.output.add_argument(
        "-v", "--verbose", action="count", default=default(0), help=t("cli.help.verbose")
    )
    # 言語は main() がパーサーを作る前に決める（ヘルプの文もその言語で作るため）。
    # ここではヘルプに出すことと、値の誤りを argparse のエラーにすることだけを受け持つ
    groups.output.add_argument(
        "--lang", choices=LANGUAGES, default=default(None), help=t("cli.help.lang")
    )
    groups.output.add_argument("-h", "--help", action="help", help=t("cli.help.help"))


def _help_groups(parser: argparse.ArgumentParser, own_title: str | None) -> _HelpGroups:
    """
    パーサーにヘルプの見出しを作る（`own_title` がなければ、そのコマンドの見出しは作らない）。

    :param parser: 見出しを作るパーサー
    :param own_title: そのコマンドのオプションの見出し
    :return: 作った見出し
    """
    own = parser.add_argument_group(own_title) if own_title else parser._optionals
    return _HelpGroups(
        own=own,
        aws=parser.add_argument_group(t("cli.group.aws")),
        output=parser.add_argument_group(t("cli.group.output")),
    )


def _add_subcommand(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    *,
    usage: str,
    summary: str,
    examples: str,
) -> _HelpGroups:
    """
    サブコマンドのパーサーを作り、共通オプションを入れた見出しを返す。

    :param subparsers: `add_subparsers()` の戻り値
    :param name: サブコマンドの名前
    :param usage: ヘルプの `usage:` の行
    :param summary: 1 行の説明（メインのヘルプの一覧と、サブコマンドのヘルプの先頭に出す）
    :param examples: ヘルプの末尾に出す例
    :return: そのサブコマンドの見出し
    """
    parser = subparsers.add_parser(
        name,
        prog=f"elbx {name}",
        usage=usage,
        help=summary,
        description=summary,
        epilog=examples,
        formatter_class=_HelpFormatter,
        add_help=False,
    )
    groups = _help_groups(parser, t("cli.group.command", command=name))
    groups.output.add_argument("--json", action="store_true", help=t("cli.help.json"))
    _add_common_arguments(groups, is_subcommand=True)
    return groups


def build_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser of the `elbx` command with its subcommands `ls`, `tree`,
    `targets`, `find` and `route` (also used to generate the help).

    `elbx` コマンドの引数パーサー（サブコマンド `ls`・`tree`・`targets`・`find`・`route`）を作る
    （ヘルプの生成にも使う）。
    """
    parser = argparse.ArgumentParser(
        prog="elbx",
        usage="elbx [<command>] [options]",
        description=t("cli.description"),
        epilog=t("cli.examples.main"),
        formatter_class=_HelpFormatter,
        add_help=False,
    )
    subparsers = parser.add_subparsers(
        dest="command", metavar="<command>", title=t("cli.group.commands"), required=True
    )
    main_groups = _help_groups(parser, None)
    _add_common_arguments(main_groups)
    main_groups.output.add_argument(
        "--version",
        action="version",
        version=f"elbx {__version__}",
        help=t("cli.help.version"),
    )

    # 1. ls: ロードバランサー一覧
    ls = _add_subcommand(
        subparsers,
        "ls",
        usage="elbx ls [options]",
        summary=t("cli.help.ls"),
        examples=t("cli.examples.ls"),
    )
    ls.own.add_argument("-s", "--search", metavar="TEXT", help=t("cli.help.search"))
    ls.own.add_argument("--no-dns", action="store_true", help=t("cli.help.no_dns"))

    # 2. tree: 階層ツリー表示（メイン機能）
    tree = _add_subcommand(
        subparsers,
        "tree",
        usage="elbx tree [LB] [options]",
        summary=t("cli.help.tree"),
        examples=t("cli.examples.tree"),
    )
    tree.own.add_argument(
        "name_or_arn", nargs="?", default=None, metavar="LB", help=t("cli.help.tree.name_or_arn")
    )
    tree.own.add_argument("-s", "--search", metavar="TEXT", help=t("cli.help.search"))
    tree.own.add_argument("-f", "--full", action="store_true", help=t("cli.help.tree.full"))
    tree.own.add_argument(
        "--max-targets",
        type=_non_negative_int,
        default=DEFAULT_MAX_TARGETS,
        metavar="N",
        help=t("cli.help.tree.max_targets", default=DEFAULT_MAX_TARGETS),
    )
    tree.own.add_argument("--no-network", action="store_true", help=t("cli.help.tree.no_network"))
    tree.own.add_argument("--no-dns", action="store_true", help=t("cli.help.no_dns"))

    # 3. targets: ターゲット一覧テーブル表示（LB と --tg はどちらか一方）
    targets = _add_subcommand(
        subparsers,
        "targets",
        usage="elbx targets [LB | --tg TG] [options]",
        summary=t("cli.help.targets"),
        examples=t("cli.examples.targets"),
    )
    which = targets.own.add_mutually_exclusive_group()
    which.add_argument("lb", nargs="?", default=None, metavar="LB", help=t("cli.help.targets.lb"))
    which.add_argument("--tg", default=None, metavar="TG", help=t("cli.help.targets.tg"))
    targets.own.add_argument("-s", "--search", metavar="TEXT", help=t("cli.help.targets.search"))
    targets.own.add_argument(
        "--unhealthy", action="store_true", help=t("cli.help.targets.unhealthy")
    )

    # 4. find: 逆引き検索
    find = _add_subcommand(
        subparsers,
        "find",
        usage="elbx find TEXT [options]",
        summary=t("cli.help.find"),
        examples=t("cli.examples.find"),
    )
    find.own.add_argument("query", metavar="TEXT", help=t("cli.help.find.query"))

    # 5. route: ルーティング先シミュレーション
    route = _add_subcommand(
        subparsers,
        "route",
        usage="elbx route URL [options]",
        summary=t("cli.help.route"),
        examples=t("cli.examples.route"),
    )
    route.own.add_argument("url", metavar="URL", help=t("cli.help.route.url"))
    route.own.add_argument("--lb", default=None, metavar="LB", help=t("cli.help.route.lb"))
    route.own.add_argument(
        "-X", "--method", default="GET", metavar="METHOD", help=t("cli.help.route.method")
    )
    route.own.add_argument(
        "-H",
        "--header",
        action="append",
        type=_parse_header,
        default=[],
        metavar="'NAME: VALUE'",
        help=t("cli.help.route.header"),
    )
    route.own.add_argument(
        "--source-ip",
        default=None,
        type=_ip_address,
        metavar="IP",
        help=t("cli.help.route.source_ip"),
    )

    return parser


# =============================================================================
# サブコマンドの省略（`elbx` だけなら `elbx tree`）
# =============================================================================


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """
    パーサーのサブコマンド（名前 → そのパーサー）を返す。

    :param parser: `build_parser()` のパーサー
    :return: サブコマンドの名前とパーサーの辞書
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _first_positional(parser: argparse.ArgumentParser, argv: Sequence[str]) -> str | None:
    """
    最初の位置引数（オプションとその値を除いた最初の引数）を返す。サブコマンドが書かれているかを
    知るため。値を取るオプションは、メインと `tree` のパーサーの定義から調べる
    （`--search ls` の `ls` をサブコマンドと取り違えないように）。

    :param parser: `build_parser()` のパーサー
    :param argv: コマンドライン引数
    :return: 最初の位置引数（なければ None）
    """
    # オプションの書き方 -> 値を取るか
    takes_value = {
        option: action.nargs != 0
        for p in (parser, _subcommands(parser)["tree"])
        for action in p._actions
        for option in action.option_strings
    }
    tokens = iter(argv)
    for token in tokens:
        if token == "--":
            return next(tokens, None)
        if not token.startswith("-") or token == "-":
            return token
        if _value_is_next_argument(token, takes_value):
            next(tokens, None)
    return None


def _value_is_next_argument(token: str, takes_value: dict[str, bool]) -> bool:
    """
    オプションの値が次の引数に書かれているかを返す。argparse と同じく、長いオプションの省略形
    （`--sea web`）と、まとめた短いオプション（`-vp prod`）も読む。

    :param token: `-` で始まる引数
    :param takes_value: オプションの書き方 -> 値を取るか
    :return: 次の引数がこのオプションの値なら True
    """
    if token.startswith("--"):
        # `--search=prod-web` の形は値を含むので、次の引数を飛ばさない
        if "=" in token:
            return False
        matches = (
            [takes_value[token]]
            if token in takes_value
            else [
                needs_value
                for option, needs_value in takes_value.items()
                if option.startswith(token)
            ]
        )
        # どのオプションの省略か決まらないものは、本物のパーサーにエラーを出させる
        return bool(matches) and all(matches)
    # `-vp prod` は -v と `-p prod`。値を取るオプションのあとに文字が続けば、それが値（`-pprod`）
    for index, letter in enumerate(token[1:], start=1):
        if takes_value.get(f"-{letter}", False):
            return index == len(token) - 1
    return False


def _with_default_command(parser: argparse.ArgumentParser, argv: list[str]) -> list[str]:
    """
    サブコマンドを省略したら `tree`（全 ELB の階層ツリー）を先頭に補う。

    `elbx --full` のように tree のオプションも書けるよう、先頭に `tree` を補ってから読む
    （共通オプションはサブコマンドの後ろにも書けるので、補っても意味は変わらない）。
    位置引数があるのにサブコマンドでないとき（`elbx tre` の打ち間違いや、`elbx my-alb` と
    ELB の名前だけを書いたとき）は補わず、`elbx tree my-alb` を案内するエラーにする。
    `elbx --help` / `elbx --version` はメインのパーサーのものなので、補わない。

    :param parser: `build_parser()` のパーサー
    :param argv: コマンドライン引数
    :return: 読む引数
    """
    options = argv[: argv.index("--")] if "--" in argv else argv
    if {"-h", "--help", "--version"} & set(options):
        return argv
    first = _first_positional(parser, argv)
    if first is None:
        return ["tree", *argv]
    commands = _subcommands(parser)
    if first not in commands:
        parser.error(
            t(
                "cli.arg.unknown_command",
                command=_escape_controls(first),
                choices=", ".join(commands),
            )
        )
    return argv


# =============================================================================
# サブコマンドの実行（1 つのコマンドにつき 1 つの関数）
# =============================================================================


def _print_json(data: object) -> None:
    """
    オブジェクトを日本語がエスケープされない整形済みJSONとして標準出力へ表示する。

    :param data: JSONシリアライズ可能なデータオブジェクト
    """
    print(_dumps_json(data))


def _only_unhealthy(tgs: list[TargetGroupView]) -> list[TargetGroupView]:
    """
    調べるべきターゲット（`TargetGroupView.needs_attention`）を持つターゲットグループだけを抽出し、
    配下のターゲットリストもそれに絞り込んだ新しいリストを返す。
    ヘルスチェックが無効な Lambda の `unavailable` や、どの ELB にも使われていない TG の
    `unused` は、設定どおりの状態なので含めない。

    :param tgs: フィルタ対象の TargetGroupView のリスト
    :return: 調べるべきターゲットのみを含む TargetGroupView のリスト
    """
    filtered: list[TargetGroupView] = []
    for tg in tgs:
        bad = [target for target in tg.targets if tg.needs_attention(target)]
        if bad:
            # 元の TargetGroupView を変更せず、targets フィールドのみ差し替えたコピーを作る
            filtered.append(dataclasses.replace(tg, targets=bad))
    return filtered


def _run_ls(elb: ELBClient, args: argparse.Namespace, use_color: bool | None) -> None:
    """`elbx ls`: ロードバランサーの一覧。"""
    # 表に出すのは ELB とリスナー、ヘルスの件数、Route 53 の名前だけなので、ネットワーク・証明書・
    # 属性・ターゲットの実体は --json のときだけ取得する
    lbs = elb.list_load_balancers(
        search=args.search,
        include_network=args.json,
        include_dns=not args.no_dns,
        include_certificates=args.json,
        include_attributes=args.json,
        resolve_targets=args.json,
    )
    if args.json:
        _print_json([lb.to_dict() for lb in lbs])
    else:
        print(format_lb_summary_table(lbs, use_color=use_color))


def _run_tree(elb: ELBClient, args: argparse.Namespace, use_color: bool | None) -> None:
    """`elbx tree`: 関連図（既定）か、`--full` ですべての情報のツリー。"""
    full = args.full or args.json
    # 関連図（既定）はサブネット・SG・属性を出さないので取得もしない（速くなる）
    include_network = full and not args.no_network
    include_dns = not args.no_dns
    if args.name_or_arn is not None:
        lbs = [
            elb.get_load_balancer(
                args.name_or_arn,
                include_network=include_network,
                include_dns=include_dns,
                include_attributes=full,
            )
        ]
    else:
        lbs = elb.list_load_balancers(
            search=args.search,
            include_network=include_network,
            include_dns=include_dns,
            include_attributes=full,
        )

    if args.json:
        _print_json([lb.to_dict() for lb in lbs])
    elif not lbs:
        print(t("fmt.no_load_balancers"))
    elif full:
        trees = [
            format_lb_tree(lb, use_color=use_color, show_network=include_network) for lb in lbs
        ]
        print("\n\n".join(trees))
    else:
        maps = [format_lb_map(lb, use_color=use_color, max_targets=args.max_targets) for lb in lbs]
        print("\n\n".join(maps))


def _run_targets(elb: ELBClient, args: argparse.Namespace, use_color: bool | None) -> None:
    """`elbx targets`: ターゲットの一覧（`--unhealthy` で調べるべきものだけ）。"""
    # TG の属性と下流の ELB の配下は表に出さないので、--json のときだけ取得する
    if args.tg is not None:
        tgs = [
            elb.get_target_group(args.tg, include_attributes=args.json, expand_downstream=args.json)
        ]
    else:
        tgs = elb.list_target_groups(
            lb_name_or_arn=args.lb,
            search=args.search,
            include_attributes=args.json,
            expand_downstream=args.json,
        )
    all_tgs = {tg.arn: tg for tg in tgs}
    if args.unhealthy:
        tgs = _only_unhealthy(tgs)

    if args.json:
        # --unhealthy で絞るのは targets だけ。summary は TG 全体の件数のままにする（絞ったあとの
        # 件数にすると、9 台が healthy の TG でも healthy: 0 になり、全滅に見える）
        _print_json(
            [{**tg.to_dict(), "summary": all_tgs[tg.arn].to_dict()["summary"]} for tg in tgs]
        )
    elif args.unhealthy and not tgs:
        print(t("cli.no_unhealthy"))
    else:
        print(format_targets_table(tgs, use_color=use_color, show_health_detail=args.unhealthy))


def _run_find(elb: ELBClient, args: argparse.Namespace, use_color: bool | None) -> None:
    """`elbx find`: リソースの名前・ID・IP から、登録先の ELB と TG を逆引きする。"""
    # 表に出さないサブネット・SG と下流の ELB の配下は、--json のときだけ取得する
    results = elb.find_target(args.query, include_network=args.json, expand_downstream=args.json)
    if args.json:
        _print_json([r.to_dict() for r in results])
    else:
        print(format_lookup_results_table(results, use_color=use_color))
        # IP アドレスは全体でしか一致しないので、一部だけ書いて 0 件のときはそう伝える
        if not results and _looks_like_partial_ip(args.query):
            print(t("cli.hint", hint=t("cli.hint.partial_ip")), file=sys.stderr)


def _looks_like_partial_ip(query: str) -> bool:
    """
    `find` の検索文字列が IP アドレスの一部に見えるか（数字とドットだけ、または 16 進とコロン
    だけで、IP アドレスそのものではない）を返す。

    :param query: 検索文字列
    :return: IP アドレスの一部に見えるなら True
    """
    query = query.strip()
    return bool(_PARTIAL_IP.match(query)) and canonical_ip(query) is None


def _run_route(elb: ELBClient, args: argparse.Namespace, use_color: bool | None) -> None:
    """`elbx route`: URL へのリクエストが一致するルールと、届く先を評価する。"""
    # 同じ名前のヘッダー（大文字小文字は区別しない）は、HTTP と同じく ", " でつないで 1 つにする
    headers: dict[str, str] = {}
    for name, value in args.header:
        key = name.lower()
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    result = elb.simulate_request(
        args.url,
        method=args.method,
        headers=headers,
        source_ip=args.source_ip,
        lb_name_or_arn=args.lb,
    )
    if args.json:
        _print_json(result.to_dict())
    else:
        print(format_route_simulation(result, use_color=use_color))


_COMMANDS: dict[str, Callable[[ELBClient, argparse.Namespace, bool | None], None]] = {
    "ls": _run_ls,
    "tree": _run_tree,
    "targets": _run_targets,
    "find": _run_find,
    "route": _run_route,
}


# =============================================================================
# エントリポイント
# =============================================================================


def main(
    argv: Sequence[str] | None = None,
    client: ELBClient | None = None,
) -> int:
    """
    Entry point of the `elbx` command. Errors are printed to stderr as one line.

    `elbx` コマンドのエントリポイント。エラーは標準エラー出力に1行で出す。

    :param argv: command line arguments (default `sys.argv[1:]`)
        / コマンドライン引数（省略時は `sys.argv[1:]`）
    :param client: an `ELBClient` to use instead of creating one from the arguments
        (tests, playground) / 引数から作る代わりに使う `ELBClient`（テストや playground 用）
    :return: exit code: 0 on success (including "nothing found"), 1 on error, 130 when
        interrupted with Ctrl-C
        / 終了コード（0 = 正常（該当なしを含む）、1 = エラー、130 = Ctrl-C で中断）
    """
    # Windows のコンソール等でも日本語・罫線を出せるよう、標準出力と標準エラー出力を UTF-8 にする
    # （エラーのメッセージも日本語になるため、標準エラー出力もそろえる）
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8")

    # ヘルプの文も選んだ言語で作るため、パーサーを作る前に --lang だけを先に読む。
    # 値の誤り（--lang fr）は、ここでは無視して本物のパーサーに argparse のエラーとして出させる
    lang = _lang_option(sys.argv[1:] if argv is None else argv)
    with using_language(lang if lang in LANGUAGES else None):
        try:
            get_language()
        except ELBError as exc:
            # ELBX_LANG の誤りは、どの言語で出すか決められないので英語で出す
            print(MESSAGES["en"]["cli.error"].format(message=exc), file=sys.stderr)
            return 1
        return _run(argv, client)


def _lang_option(argv: Sequence[str]) -> str | None:
    """
    `--lang X` / `--lang=X` の値を、パーサーを作る前に引数から拾う（複数あれば最後のもの）。

    argparse で先に読むと、`--lang` に値がないときなどに、本物のパーサーより先に
    英語のエラーで終了してしまう。ここでは拾うだけで、誤りは本物のパーサーに出させる。

    :param argv: コマンドライン引数
    :return: `--lang` の値（なければ None）
    """
    lang: str | None = None
    tokens = iter(argv)
    for token in tokens:
        if token == "--":
            break
        if token == "--lang":
            lang = next(tokens, None)
        elif token.startswith("--lang="):
            lang = token.partition("=")[2]
    return lang


# 次にすることが決まっている AWS の失敗（botocore の例外の名前。版によっては存在しない例外もある
# ので、名前で見分ける）
_NO_CREDENTIALS_ERRORS = frozenset({"NoCredentialsError", "PartialCredentialsError"})
_SSO_ERRORS = frozenset(
    {"SSOError", "SSOTokenLoadError", "TokenRetrievalError", "UnauthorizedSSOTokenError"}
)
_EXPIRED_CREDENTIALS_CODES = frozenset(
    {
        "AuthFailure",
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidClientTokenId",
        "RequestExpired",
        "UnrecognizedClientException",
    }
)
_ROUTE53_LOOKUP_OPERATIONS = frozenset({"ListHostedZones", "ListResourceRecordSets"})


def _is_closed_pipe(exc: Exception) -> bool:
    """
    出力先のパイプが閉じたことによる例外かを返す（Windows では EINVAL の OSError になる）。

    :param exc: 受け止めた例外
    :return: 閉じたパイプによるものなら True
    """
    if isinstance(exc, BrokenPipeError):
        return True
    return isinstance(exc, OSError) and os.name == "nt" and exc.errno == errno.EINVAL


class _WarningCounter(logging.Handler):
    """WARNING の数を数える（`-v` なしでも、情報が欠けたことだけは知らせるため）。"""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        """1 件数える。"""
        self.count += 1


class _LogFormatter(logging.Formatter):
    """ログの 1 行。AWS から来た文字列が混ざるので、制御文字を置き換える。"""

    def format(self, record: logging.LogRecord) -> str:
        """`WARNING: …` の形にする。"""
        return _escape_controls(f"{record.levelname}: {record.getMessage()}")


@contextlib.contextmanager
def _cli_logging(verbosity: int) -> Iterator[_WarningCounter]:
    """
    `with` の中だけ elbx のログを受け取る（`main()` を同じプロセスで呼ぶ側に設定を残さないため）。
    `-v` なら WARNING、`-vv` なら DEBUG も標準エラー出力に出す。標準出力には混ぜない
    （`--json` をパイプに渡せるように）。botocore のログは、署名ヘッダーなどが入るので出さない。

    :param verbosity: `-v` の数
    :return: WARNING の数を数えるハンドラー
    """
    counter = _WarningCounter()
    handlers: list[logging.Handler] = [counter]
    if verbosity:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(_LogFormatter())
        handlers.append(stream)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG if verbosity >= 2 else logging.WARNING)
    for handler in handlers:
        logger.addHandler(handler)
    try:
        yield counter
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _run(argv: Sequence[str] | None, client: ELBClient | None) -> int:
    """
    `main()` の本体（言語を決めたあとに、引数を読んでサブコマンドを実行する）。

    :param argv: コマンドライン引数
    :param client: 使う `ELBClient`（None なら引数から作る）
    :return: 終了コード
    """
    with _argparse_messages():
        parser = build_parser()
        args = parser.parse_args(
            _with_default_command(parser, list(sys.argv[1:] if argv is None else argv))
        )
        _reject_search_with_single_target(parser, args)
    with _cli_logging(args.verbose) as warnings:
        return _execute(args, client, warnings)


def _reject_search_with_single_target(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """
    `--search` を、1 つに決める指定（`tree LB`・`targets --tg TG`）と一緒に書いたらエラーにする。
    一覧を絞るオプションなので、黙って無視すると、絞り込めたように見えてしまう。

    :param parser: `build_parser()` のパーサー
    :param args: 読んだ引数
    """
    single = {
        "tree": ("LB", getattr(args, "name_or_arn", None)),
        "targets": ("--tg", getattr(args, "tg", None)),
    }
    option, value = single.get(args.command, ("", None))
    if value is not None and getattr(args, "search", None) is not None:
        _subcommands(parser)[args.command].error(t("cli.arg.search_with_single", option=option))


def _next_step_hint(cause: BaseException | None, profile: str | None) -> str | None:
    """
    AWS の失敗に、利用者が次にすることが決まっているものなら、その案内を返す。

    :param cause: `AWSAPIError` の元の例外
    :param profile: `--profile` の値
    :return: 案内の文（決まっていなければ None）
    """
    profile_part = f" --profile {profile}" if profile else ""
    name = type(cause).__name__
    if name == "NoRegionError":
        return t("cli.hint.region")
    if name in _NO_CREDENTIALS_ERRORS:
        return t("cli.hint.credentials")
    if name in _SSO_ERRORS:
        return t("cli.hint.sso", profile=profile_part)
    if isinstance(cause, ClientError):
        if error_code(cause) in _EXPIRED_CREDENTIALS_CODES:
            return t("cli.hint.expired", profile=profile_part)
        if cause.operation_name in _ROUTE53_LOOKUP_OPERATIONS:
            return t("cli.hint.route53")
    return None


def _endpoint_hint(cause: BaseException | None, elb: ELBClient | None) -> str | None:
    """
    AWS のエンドポイントにつながらなかったときの案内を返す（リージョン名の打ち間違いが多い）。

    :param cause: `AWSAPIError` の元の例外
    :param elb: 使ったクライアント（作れなかったときは None）
    :return: 案内の文（つながらなかったのでなければ None）
    """
    if type(cause).__name__ != "EndpointConnectionError" or elb is None:
        return None
    return t("cli.hint.endpoint", region=elb._resolved_region() or "?")


def _not_found_hint(elb: ELBClient | None, args: argparse.Namespace) -> str | None:
    """
    ELB・TG・ホスト名が見つからなかったときに、指定する場所を取り違えていないかを調べて案内する
    （TG の名前を ELB として渡した、`route` に URL ではなく名前を渡した、など）。
    失敗したときだけ、AWS を 1 回余分に呼ぶ。

    :param elb: 使ったクライアント（作れなかったときは None）
    :param args: 読んだ引数
    :return: 案内の文（取り違えが見つからなければ None）
    """
    if elb is None:
        return None
    as_lb = {"tree": "name_or_arn", "targets": "lb", "route": "lb"}.get(args.command, "")
    lb_name = getattr(args, as_lb, None)
    tg_name = getattr(args, "tg", None) if args.command == "targets" else None
    if lb_name is not None and not lb_name.startswith("arn:"):
        if elb._has_target_group(lb_name):
            return t("cli.hint.is_target_group", name=lb_name)
    elif tg_name is not None and not tg_name.startswith("arn:"):
        if elb._has_load_balancer(tg_name):
            return t("cli.hint.is_load_balancer", name=tg_name)
    elif args.command == "route" and lb_name is None and _BARE_WORD.match(args.url):
        # `elbx route nlb-to-alb-tg` のように、URL ではなく名前を渡している
        return t("cli.hint.route_needs_url")
    return None


def _print_partial_note(args: argparse.Namespace, warnings: _WarningCounter) -> None:
    """
    取得できなかった詳細があれば、そのことを標準エラー出力に 1 行出す
    （-v なら理由がもう出ているので、繰り返さない）。

    :param args: 読んだ引数
    :param warnings: 実行中の WARNING の数を数えるハンドラー
    """
    if warnings.count and not args.verbose:
        print(t("cli.partial_result", count=warnings.count), file=sys.stderr)


def _execute(args: argparse.Namespace, client: ELBClient | None, warnings: _WarningCounter) -> int:
    """
    サブコマンドを実行し、失敗を終了コードと 1 行のエラーにする。

    :param args: 読んだ引数
    :param client: 使う `ELBClient`（None なら引数から作る）
    :param warnings: 実行中の WARNING の数を数えるハンドラー
    :return: 終了コード
    """
    use_color = True if args.color else (False if args.no_color else None)
    elb = client
    try:
        if elb is None:
            elb = ELBClient(
                profile_name=args.profile, region_name=args.region, max_workers=args.workers
            )
        _COMMANDS[args.command](elb, args, use_color)
        # 出力先が閉じていれば、終了時ではなくここで分かるように書き出す
        sys.stdout.flush()
        _print_partial_note(args, warnings)
    except KeyboardInterrupt:
        return 130  # シェルの慣例（128 + SIGINT）。トレースバックは見せない
    except ELBError as exc:
        print(t("cli.error", message=_escape_controls(str(exc))), file=sys.stderr)
        hint = _next_step_hint(exc.__cause__, args.profile) or _endpoint_hint(exc.__cause__, elb)
        if hint is None and isinstance(exc, ResourceNotFoundError):
            hint = _not_found_hint(elb, args)
        if hint:
            print(t("cli.hint", hint=_escape_controls(hint)), file=sys.stderr)
        # 取得できなかったものがあると、エラーの原因がそこにあることもあるので、失敗のときも知らせる
        _print_partial_note(args, warnings)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI の最後の受け止め。スタックトレースではなく1行で知らせる
        if _is_closed_pipe(exc):
            # `elbx ls | head` など。読み手が満足して閉じただけなのでエラーにしない。
            # 終了時の flush でまた失敗しないよう、標準出力を捨て先に向ける
            with contextlib.suppress(OSError, ValueError):
                devnull = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(devnull, sys.stdout.fileno())
                finally:
                    os.close(devnull)
            return 0
        print(t("cli.unexpected_error", message=_escape_controls(str(exc))), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
