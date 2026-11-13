"""
Text output: the load balancer tree, the target / lookup tables and the route simulation,
with optional ANSI colors. Standard library only.

テキストの出力。ELB のツリー、ターゲット・逆引きの表、route の結果を整形する（ANSI の色は任意）。
標準ライブラリだけで描く。
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
import secrets
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from typing import TypeVar

from .messages import t as tr
from .models import (
    EC2InstanceInfo,
    ECSTaskInfo,
    LambdaFunctionInfo,
    ListenerView,
    LoadBalancerView,
    RouteHop,
    RouteSimulationResult,
    RuleView,
    TargetGroupView,
    TargetLookupResult,
    TargetView,
)

T = TypeVar("T")


class _AnsiColor:
    """ANSIエスケープシーケンスによる色付けヘルパー。"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    def __init__(self, enabled: bool | None = None) -> None:
        """
        ANSIカラーヘルパーを初期化する。

        :param enabled: カラー出力の有効/無効（None の場合は環境から決める: `FORCE_COLOR` が
            あれば有効、`NO_COLOR` があれば無効、どちらもなければ標準出力が端末かどうか）
        """
        if enabled is None:
            if os.environ.get("FORCE_COLOR"):
                self.enabled = True
            elif os.environ.get("NO_COLOR"):
                self.enabled = False
            else:
                self.enabled = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        else:
            self.enabled = enabled
        if self.enabled and os.name == "nt":
            _enable_windows_ansi()
        # wrap() は ESC の代わりにこの目印を出し、render() が最後に ESC に戻す。AWS から来た文字列の
        # ESC [ … m と、elbx が付けた色を区別するため（値を推測できないので、文字列の中に書いても
        # 色にはならない）
        self._esc_marker = f"\ue000{secrets.token_hex(8)}\ue000"

    def wrap(self, text: str, *codes: str) -> str:
        """
        テキストを指定したANSIエスケープコードで囲む。カラー無効時はそのまま返す。

        :param text: 装飾対象の文字列
        :param codes: 適用するANSIコード（複数指定可）
        :return: ANSIコード付与済みの文字列（または元の文字列）
        """
        if not self.enabled or not codes:
            return text
        opening = "".join(codes).replace("\x1b", self._esc_marker)
        return opening + text + self.RESET.replace("\x1b", self._esc_marker)

    def render(self, lines: Iterable[str]) -> str:
        """
        行を 1 つの文字列にする。行の中の制御文字（改行も）は置き換え、wrap() で付けた色だけを残す。

        :param lines: 出力する行
        :return: 端末に出せる文字列
        """
        escaped = "\n".join(_escape_controls(line) for line in lines)
        return escaped.replace(self._esc_marker, "\x1b")

    def health_badge(self, state: str) -> str:
        """
        ヘルス状態に応じた色付きバッジ文字列を返す。

        :param state: ELBターゲットのヘルス状態（`"healthy"`, `"unhealthy"`, `"draining"` 等）
        :return: 記号と色が付いたバッジ文字列
        """
        st = state.lower()
        mark = _HEALTH_MARKS.get(st, "○")
        return self.wrap(f"[{mark} {st}]", *self.health_codes(st, default=(self.DIM,)))

    def health_codes(self, state: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        """
        ヘルス状態の表示に使う ANSI コードを返す。

        :param state: ELBターゲットのヘルス状態
        :param default: 色を決めていない状態（unused / unavailable 等）に使うコード
        :return: ANSI コードのタプル
        """
        colors = {
            "healthy": (self.GREEN, self.BOLD),
            "unhealthy": (self.RED, self.BOLD),
            "draining": (self.YELLOW, self.BOLD),
            "initial": (self.YELLOW, self.BOLD),
        }
        return colors.get(state.lower(), default)


# ヘルス状態ごとの記号（ここにない状態は ○）
_HEALTH_MARKS = {"healthy": "●", "unhealthy": "✖", "draining": "◐", "initial": "◐"}

# 制御文字（C0・DEL・C1）と、表示の向きを変える文字（Bidi の埋め込み・上書き・分離）。
# 後者は、名前やタグに入っていると、文字の並びを見た目だけ入れ替えて別の値に見せられる
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _escape_controls(text: str) -> str:
    """
    制御文字を `\\x1b` の形の文字に置き換える。
    EC2 の Name タグなど AWS から来た文字列に ESC や CR が入っていると、端末がそれを解釈して
    表示が書き換わり、出力が読めなくなる（隠される）ため。改行も置き換えるのは、値の中の改行で
    本物に見える行（healthy のターゲットなど）を作れてしまうため。

    :param text: 1 行ぶんの文字列
    :return: 制御文字を置き換えた文字列
    """
    return _CONTROL_CHARS.sub(_escaped_char, text)


def _escaped_char(match: re.Match[str]) -> str:
    """制御文字 1 つを、`\\x1b`（1 バイトに収まるもの）か `\\u202e` の形にする。"""
    code = ord(match.group())
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"


@functools.cache
def _enable_windows_ansi() -> None:
    """Windows コンソールで ANSI エスケープシーケンス（0x0004）を有効にする（1回だけ行う）。"""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def _tree_prefixes(prefix: str, is_last: bool) -> tuple[str, str]:
    """
    ツリーの1要素ぶんの罫線を返す。

    :param prefix: 親の階層までのインデント
    :param is_last: 兄弟の中で最後の要素か
    :return: `(この要素の行の先頭, この要素の子の行の先頭)`
    """
    if is_last:
        return prefix + "└── ", prefix + "    "
    return prefix + "├── ", prefix + "│   "


def _with_last(items: Sequence[T]) -> list[tuple[T, bool]]:
    """要素と「最後の要素か」の組を返す（ツリーで └── と ├── を描き分けるため）。"""
    return [(item, idx == len(items) - 1) for idx, item in enumerate(items)]


# =============================================================================
# 関連図（elbx tree の既定）: ELB -> リスナー -> ルール -> TG -> ターゲット名だけの短いツリー
# =============================================================================

# 関連図で1つの TG に名前を出すターゲットの既定の上限（healthy 以外は常に出す）
DEFAULT_MAX_TARGETS = 10


def format_lb_map(
    lb: LoadBalancerView,
    use_color: bool | None = None,
    max_targets: int = DEFAULT_MAX_TARGETS,
) -> str:
    """
    Render one load balancer as a short relationship map (the default of `elbx tree`):
    listeners -> rules (priority and conditions) -> target groups (health summary) -> one line
    per target with only its health mark and name. Downstream load balancers are expanded
    inline. Attributes, subnets, security groups and the details of each target are left out
    (see `format_lb_tree` / `elbx tree --full`), so the lines stay short in a terminal.

    1つの ELB を短い関連図にする（`elbx tree` の既定）。リスナー → ルール（優先度と条件）→ TG
    （ヘルス集計）→ ターゲット（ヘルスの記号と名前だけを1行ずつ）。下流の ELB はその場で展開する。
    属性・サブネット・SG・ターゲットの詳細は出さない（`format_lb_tree` / `elbx tree --full`）ので、
    端末で行が長くならない。

    :param lb: the load balancer / 対象の ELB
    :param use_color: ANSI colors on / off; None (default) decides from the environment:
        on with `FORCE_COLOR`, off with `NO_COLOR`, otherwise only when stdout is a terminal
        / ANSI の色を付けるか。None（既定）なら環境で決める: `FORCE_COLOR` があれば付け、
        `NO_COLOR` があれば付けず、どちらもなければ標準出力が端末のときだけ付ける
    :param max_targets: targets named per target group; the rest is folded into one
        "... and N more" line. Targets that are not healthy are always named. 0 names all
        / TG ごとに名前を出すターゲットの上限。残りは「ほか N 台」の1行にまとめる。healthy
        以外は常に出す。0 なら全部出す
    :return: the map as text / 関連図の文字列
    """
    c = _AnsiColor(use_color)
    lines: list[str] = []
    state_color = c.GREEN if lb.state == "active" else c.YELLOW
    ip_type_part = f", ip={lb.ip_address_type}" if lb.ip_address_type != "ipv4" else ""
    lines.append(
        f"{c.wrap(f'[{lb.type.upper()}]', c.CYAN, c.BOLD)} {c.wrap(lb.name, c.BOLD)} "
        f"({lb.scheme}{ip_type_part}, {c.wrap(lb.state, state_color)})"
    )
    # 人が使う名前（Route 53 のレコード名）を出す。なければ ELB の DNS 名
    names = _route53_names(lb)
    lines.append(f"│  {c.wrap(names, c.GREEN) if names else c.wrap(lb.dns_name, c.DIM)}")
    _append_lb_map_body(lines, lb, "", c, max_targets, expanded={lb.arn})
    return c.render(lines)


def _route53_names(lb: LoadBalancerView) -> str:
    """Route 53 のレコード名を重複なしで返す（デュアルスタックの A と AAAA は同じ名前になる）。"""
    return ", ".join(dict.fromkeys(rec.record_name for rec in lb.route53_records))


def _append_lb_map_body(
    lines: list[str],
    lb: LoadBalancerView,
    prefix: str,
    c: _AnsiColor,
    max_targets: int,
    expanded: set[str],
) -> None:
    """
    関連図の本体（リスナーと、どのルールからも参照されない TG）を `lines` に追加する。

    :param expanded: この関連図の中ですでに展開した ELB の ARN（下流の ELB を2回展開しないため）
    """
    referenced = {
        ref.target_group_arn
        for lst in lb.listeners
        for r in lst.rules
        for ref in r.target_group_refs
    }
    unreferenced = [tg for tg in lb.target_groups if tg.arn not in referenced]

    for listener, is_last in _with_last(lb.listeners):
        branch, child = _tree_prefixes(prefix, is_last and not unreferenced)
        cert_part = ""
        if listener.certificates:
            first = listener.certificates[0]
            domain = first.domain_name or first.certificate_arn.split("/")[-1]
            more = f" +{len(listener.certificates) - 1}" if len(listener.certificates) > 1 else ""
            cert_part = f"  {c.wrap(f'cert={domain}{more}', c.DIM)}"
        lines.append(f"{branch}{c.wrap(f'{listener.protocol}:{listener.port}', c.BOLD)}{cert_part}")
        for rule, r_last in _with_last(sorted(listener.rules, key=lambda r: r.priority_order)):
            _append_rule_map_lines(lines, rule, child, r_last, c, max_targets, expanded)

    if unreferenced:
        branch, child = _tree_prefixes(prefix, True)
        lines.append(f"{branch}{c.wrap('Target Groups (unreferenced)', c.CYAN)}")
        for tg, t_last in _with_last(unreferenced):
            _append_tg_map_lines(lines, tg, "", child, t_last, c, max_targets, expanded)


def _append_rule_map_lines(
    lines: list[str],
    rule: RuleView,
    prefix: str,
    is_last: bool,
    c: _AnsiColor,
    max_targets: int,
    expanded: set[str],
) -> None:
    """関連図のルール1件（条件の要約と、転送先 TG またはアクション）を `lines` に追加する。"""
    branch, child = _tree_prefixes(prefix, is_last)
    if rule.is_default:
        label = c.wrap("[default]", c.DIM)
    else:
        # "Host in [a, b]" -> "Host a, b" のように、条件の種類と値だけにする
        conds = " AND ".join(
            cond.replace(" in [", " ", 1).removesuffix("]") for cond in rule.conditions
        )
        label = f"{c.wrap(f'[{rule.priority}]', c.YELLOW)} {conds}"
    lines.append(f"{branch}{label}")
    # 転送以外のアクション（転送の前の認証、リダイレクト、固定レスポンス）を先に、転送先を
    # あとに出す。認証つきの転送を、認証のない転送に見せないため
    refs = rule.target_group_refs
    other_actions = rule.non_forward_actions
    for index, action in enumerate(other_actions):
        b, _ = _tree_prefixes(child, not refs and index == len(other_actions) - 1)
        lines.append(f"{b}{c.wrap(action, c.CYAN)}")
    for ref, ref_last in _with_last(refs):
        weight = f" (weight={ref.weight})" if ref.weight is not None and len(refs) > 1 else ""
        if ref.target_group is not None:
            _append_tg_map_lines(
                lines, ref.target_group, weight, child, ref_last, c, max_targets, expanded
            )
        else:
            b, _ = _tree_prefixes(child, ref_last)
            lines.append(f"{b}{ref.target_group_name}{weight}")


def _health_summary_badge(tg: TargetGroupView, c: _AnsiColor) -> str:
    """
    TG のヘルス集計のバッジ（`[2/3 healthy, 1 unhealthy]`）を、状態に応じた色で返す。

    すべて healthy なら緑、unhealthy があれば赤、ほかに調べるべきターゲットがあるか
    ターゲットがなければ黄。設定どおりの状態（ヘルスチェックが無効な Lambda、どの ELB にも
    使われていない TG の unused）しかなければ、目立たせない。
    """
    if tg.total_count > 0 and tg.healthy_count == tg.total_count:
        codes: tuple[str, ...] = (c.GREEN, c.BOLD)
    elif tg.unhealthy_count > 0:
        codes = (c.RED, c.BOLD)
    elif tg.total_count == 0 or tg.attention_count > 0:
        codes = (c.YELLOW, c.BOLD)
    else:
        codes = (c.DIM,)
    return c.wrap(f"[{tg.health_summary}]", *codes)


def _append_tg_map_lines(
    lines: list[str],
    tg: TargetGroupView,
    extra_label: str,
    prefix: str,
    is_last: bool,
    c: _AnsiColor,
    max_targets: int,
    expanded: set[str],
) -> None:
    """関連図の TG 1件（名前・ヘルス集計と、ターゲット名の一覧）を `lines` に追加する。"""
    branch, child = _tree_prefixes(prefix, is_last)
    summary = _health_summary_badge(tg, c)
    lines.append(f"{branch}{c.wrap(tg.name, c.BOLD)}{extra_label} {summary}")
    if not tg.targets:
        lines.append(f"{child}└── {c.wrap(tr('fmt.no_targets_registered'), c.DIM)}")
        return

    # healthy 以外を先に出し、上限を超えた healthy は「ほか N 台」にまとめる
    bad = [t for t in tg.targets if t.health_state != "healthy"]
    good = [t for t in tg.targets if t.health_state == "healthy"]
    shown = bad + (good if max_targets <= 0 else good[: max(0, max_targets - len(bad))])
    omitted = len(tg.targets) - len(shown)
    rows: list[tuple[TargetView | None, int]] = [(t, 0) for t in shown]
    if omitted:
        rows.append((None, omitted))
    for (target, n_omitted), t_last in _with_last(rows):
        b, t_child = _tree_prefixes(child, t_last)
        if target is None:
            lines.append(f"{b}{c.wrap(tr('fmt.more_healthy', count=n_omitted), c.DIM)}")
            continue
        mark = c.wrap(
            _HEALTH_MARKS.get(target.health_state.lower(), "○"),
            *c.health_codes(target.health_state),
        )
        state = (
            ""
            if target.health_state == "healthy"
            else c.wrap(f" ({target.health_state})", c.RED if tg.needs_attention(target) else c.DIM)
        )
        # 下流の ELB は、そのリスナー以下をこの場で展開する（同じ ELB は最初の1回だけ。
        # NLB の 80 と 443 が同じ ALB を向くときなどに、同じ図を2回出さない）
        if target.downstream_lb is not None and target.downstream_lb.arn in expanded:
            expanded_note = c.wrap(tr("fmt.expanded_above"), c.DIM)
            lines.append(f"{b}{mark} {_map_target_name(target)}{state} {expanded_note}")
            continue
        lines.append(f"{b}{mark} {_map_target_name(target)}{state}")
        if target.downstream_lb is not None:
            expanded.add(target.downstream_lb.arn)
            _append_lb_map_body(lines, target.downstream_lb, t_child, c, max_targets, expanded)


def _map_target_name(t: TargetView) -> str:
    """関連図に出すターゲットの短い名前（種別が分かる接頭辞つき）。"""
    if t.downstream_lb is not None:
        # ノードの IP で登録された ELB は AZ の数だけ行が並ぶので、どのノードかを IP で分ける
        via_ip = f" ({t.target_id})" if t.eni is not None and t.eni.load_balancer_arn else ""
        kind = "NLB" if t.downstream_lb.type == "network" else "ALB"
        return f"{kind} {t.downstream_lb.name}{via_ip}"
    if t.lambda_function is not None:
        return f"Lambda {t.lambda_function.function_name}"
    if t.ecs is not None:
        svc = f"/{t.ecs.service_name}" if t.ecs.service_name else ""
        task = f" ({t.ecs.task_id})" if t.ecs.task_id else ""
        return f"ECS {t.ecs.cluster_name}{svc}{task}"
    if t.ec2 is not None:
        # IP で登録された EC2 は、instance 登録と区別できるように (ip) を添える
        via_ip = " (ip)" if t.target_kind == "ec2" and t.ip_address == t.target_id else ""
        return f"{t.ec2.name or t.ec2.instance_id}{via_ip}"
    if t.eni is not None:
        return f"{t.target_id} ({t.eni.network_interface_id})"
    return t.target_id


def format_lb_tree(
    lb: LoadBalancerView,
    use_color: bool | None = None,
    show_network: bool = True,
) -> str:
    """
    Render one load balancer as a tree (`elbx tree --full`): Route 53 records, attributes,
    subnets and security groups, then listeners -> rules -> target groups -> targets, with the
    resolved resource behind every target. Downstream load balancers are expanded inline, each
    one only the first time it appears.

    1つの ELB をツリーにする（`elbx tree --full`）。Route 53 のレコード・属性・サブネット・SG、
    続いてリスナー → ルール → TG → ターゲット（実体つき）。下流の ELB はその場で展開する
    （同じ ELB は最初の1回だけ）。

    :param lb: the load balancer / 対象の ELB
    :param use_color: ANSI colors on / off; None (default) decides from the environment:
        on with `FORCE_COLOR`, off with `NO_COLOR`, otherwise only when stdout is a terminal
        / ANSI の色を付けるか。None（既定）なら環境で決める: `FORCE_COLOR` があれば付け、
        `NO_COLOR` があれば付けず、どちらもなければ標準出力が端末のときだけ付ける
    :param show_network: include subnets and security groups (of the load balancer and the
        targets) / サブネットと SG（ELB とターゲットの）を含める
    :return: the tree as text / ツリーの文字列
    """
    c = _AnsiColor(use_color)
    lines: list[str] = []

    lb_type_badge = c.wrap(f"[{lb.type.upper()}]", c.CYAN, c.BOLD)
    state_color = c.GREEN if lb.state == "active" else c.YELLOW
    state_str = c.wrap(lb.state, state_color, c.BOLD)
    # ipv4 以外（dualstack / dualstack-without-public-ipv4）のときだけ IP アドレスタイプを出す
    ip_type_part = f", ip={lb.ip_address_type}" if lb.ip_address_type != "ipv4" else ""
    lines.append(
        f"{lb_type_badge} {c.wrap(lb.name, c.BOLD)} "
        f"({lb.scheme}{ip_type_part}, state={state_str}, vpc={lb.vpc_id})"
    )
    lines.append(f"│  DNS: {c.wrap(lb.dns_name, c.DIM)}")
    if lb.route53_records:
        r53_parts: list[str] = []
        for rec in lb.route53_records:
            rec_kind = f"{rec.record_type} Alias" if rec.is_alias else rec.record_type
            zone_scope = "Private" if rec.private_zone else "Public"
            r53_parts.append(
                f"{c.wrap(rec.record_name, c.GREEN, c.BOLD)} "
                f"{c.wrap(f'({rec_kind}, zone={rec.zone_name} [{zone_scope}])', c.DIM)}"
            )
        lines.append(f"│  Route53: {', '.join(r53_parts)}")
    lb_attr_text = _lb_attributes_summary(lb)
    if lb_attr_text:
        lines.append(f"│  Attributes: {c.wrap(lb_attr_text, c.DIM)}")

    _append_lb_body_lines(
        lines=lines,
        lb=lb,
        base_prefix="",
        c=c,
        show_network=show_network,
        expanded={lb.arn},
    )
    return c.render(lines)


def _attr_bool(value: str) -> str:
    """
    属性値の `'true'` / `'false'` を `'on'` / `'off'` 表記に変換する。

    :param value: 属性値の文字列
    :return: `'on'`、`'off'`、または元の文字列
    """
    return {"true": "on", "false": "off"}.get(value.lower(), value)


def _lb_attributes_summary(lb: LoadBalancerView) -> str:
    """
    ロードバランサー属性のうち運用・障害調査でよく確認する主要項目を1行にまとめる。

    :param lb: 対象の `LoadBalancerView`
    :return: カンマ区切りの属性サマリー文字列（該当属性がない場合は空文字列）
    """
    a = lb.attributes
    parts: list[str] = []
    if "idle_timeout.timeout_seconds" in a:
        parts.append(f"idle_timeout={a['idle_timeout.timeout_seconds']}s")
    if "load_balancing.cross_zone.enabled" in a:
        parts.append(f"cross_zone={_attr_bool(a['load_balancing.cross_zone.enabled'])}")
    if "deletion_protection.enabled" in a:
        parts.append(f"deletion_protection={_attr_bool(a['deletion_protection.enabled'])}")
    if "access_logs.s3.enabled" in a:
        bucket = a.get("access_logs.s3.bucket", "")
        enabled = a["access_logs.s3.enabled"].lower() == "true"
        parts.append(
            f"access_logs=on (s3://{bucket})"
            if enabled and bucket
            else f"access_logs={_attr_bool(a['access_logs.s3.enabled'])}"
        )
    if "routing.http.drop_invalid_header_fields.enabled" in a:
        parts.append(
            "drop_invalid_headers="
            + _attr_bool(a["routing.http.drop_invalid_header_fields.enabled"])
        )
    return ", ".join(parts)


def _tg_attributes_summary(tg: TargetGroupView) -> str:
    """
    ターゲットグループ属性を1行にまとめる。
    ツリーが読みにくくならないよう、登録解除の遅延（`dereg_delay`）以外はデフォルトから変更されている項目だけを出す。

    :param tg: 対象の `TargetGroupView`
    :return: カンマ区切りの属性サマリー文字列（表示対象がない場合は空文字列）
    """
    a = tg.attributes
    parts: list[str] = []
    # Lambda TG には登録解除の遅延がない（API が値を返しても意味を持たない）
    if "deregistration_delay.timeout_seconds" in a and tg.target_type != "lambda":
        parts.append(f"dereg_delay={a['deregistration_delay.timeout_seconds']}s")
    if a.get("stickiness.enabled", "").lower() == "true":
        parts.append(f"stickiness=on ({a.get('stickiness.type', '-')})")
    slow_start = a.get("slow_start.duration_seconds", "0")
    if slow_start not in ("", "0"):
        parts.append(f"slow_start={slow_start}s")
    algorithm = a.get("load_balancing.algorithm.type", "")
    if algorithm and algorithm != "round_robin":
        parts.append(f"algorithm={algorithm}")
    cross_zone = a.get("load_balancing.cross_zone.enabled", "")
    if cross_zone and cross_zone != "use_load_balancer_configuration":
        parts.append(f"cross_zone={_attr_bool(cross_zone)}")
    if "preserve_client_ip.enabled" in a:
        parts.append(f"preserve_client_ip={_attr_bool(a['preserve_client_ip.enabled'])}")
    if a.get("proxy_protocol_v2.enabled", "").lower() == "true":
        parts.append("proxy_protocol_v2=on")
    return ", ".join(parts)


def _append_lb_body_lines(
    lines: list[str],
    lb: LoadBalancerView,
    base_prefix: str,
    c: _AnsiColor,
    show_network: bool,
    expanded: set[str],
) -> None:
    """
    `LoadBalancerView` の配下セクション（Network / Listeners / TargetGroups）を
    再帰的に `lines` へ追加する。

    :param lines: 出力行を蓄積するリスト
    :param lb: 描画対象の `LoadBalancerView`
    :param base_prefix: 現在の階層のインデント接頭辞
    :param c: `_AnsiColor` インスタンス
    :param show_network: サブネット・セキュリティグループ情報を表示するか
    :param expanded: このツリーの中ですでに展開した ELB の ARN（下流の ELB を2回展開しないため）
    """
    # 末尾セクションのみ "└── "、それ以外は "├── " にするため、有効なセクション一覧を先に確定する
    sections: list[str] = []
    if show_network and (lb.subnets or lb.security_groups):
        sections.append("network")
    if lb.listeners:
        sections.append("listeners")

    referenced_tg_arns = {
        ref.target_group_arn
        for listener in lb.listeners
        for r in listener.rules
        for ref in r.target_group_refs
    }
    unreferenced_tgs = [tg for tg in lb.target_groups if tg.arn not in referenced_tg_arns]
    if unreferenced_tgs or (not lb.listeners and lb.target_groups):
        sections.append("unreferenced_tgs")

    # 1. ネットワーク・セキュリティグループセクション
    if "network" in sections:
        sec_prefix, sec_child = _tree_prefixes(base_prefix, sections[-1] == "network")
        lines.append(f"{sec_prefix}{c.wrap('Network & Security', c.BLUE, c.BOLD)}")

        if lb.subnets:
            p_sub, c_sub = _tree_prefixes(sec_child, not lb.security_groups)
            lines.append(f"{p_sub}Subnets ({len(lb.subnets)} AZs)")
            for sub, s_last in _with_last(lb.subnets):
                s_branch, _ = _tree_prefixes(c_sub, s_last)
                name_part = f" ({sub.name})" if sub.name else ""
                cidr_part = f" [{', '.join(sub.cidr_blocks)}]" if sub.cidr_blocks else ""
                lines.append(
                    f"{s_branch}{sub.availability_zone}: {sub.subnet_id}{name_part}{cidr_part}"
                )

        if lb.security_groups:
            p_sg, c_sg = _tree_prefixes(sec_child, True)
            lines.append(f"{p_sg}Security Groups ({len(lb.security_groups)})")
            for sg, sg_last in _with_last(lb.security_groups):
                sg_branch, sg_child = _tree_prefixes(c_sg, sg_last)
                sg_name = f" ({sg.group_name})" if sg.group_name else ""
                lines.append(f"{sg_branch}{sg.group_id}{sg_name}")
                sg_rules = [("in ", "from", r) for r in sg.inbound_rules] + [
                    ("out", "to", r) for r in sg.outbound_rules
                ]
                for (direction, prep, rule), r_last in _with_last(sg_rules):
                    r_branch, _ = _tree_prefixes(sg_child, r_last)
                    desc = f" # {rule.description}" if rule.description else ""
                    lines.append(
                        f"{r_branch}{direction} allow {rule.protocol}:{rule.port_range} "
                        f"{prep} {rule.source_or_dest}{c.wrap(desc, c.DIM)}"
                    )

    # 2. リスナー → ルール → ターゲットグループ → 実体ターゲット階層
    if "listeners" in sections:
        sec_prefix, sec_child = _tree_prefixes(base_prefix, sections[-1] == "listeners")
        lines.append(f"{sec_prefix}{c.wrap(f'Listeners ({len(lb.listeners)})', c.MAGENTA, c.BOLD)}")
        for listener, l_last in _with_last(lb.listeners):
            l_branch, l_child = _tree_prefixes(sec_child, l_last)
            _append_listener_lines(
                lines, listener, l_branch, l_child, c, show_network=show_network, expanded=expanded
            )

    # 3. 未参照または直接紐づくターゲットグループ
    if "unreferenced_tgs" in sections:
        sec_prefix, sec_child = _tree_prefixes(base_prefix, True)
        lines.append(
            f"{sec_prefix}{c.wrap('Target Groups (Direct / Unreferenced)', c.CYAN, c.BOLD)}"
        )
        tgs_to_show = unreferenced_tgs if lb.listeners else lb.target_groups
        for tg, t_last in _with_last(tgs_to_show):
            t_branch, t_child = _tree_prefixes(sec_child, t_last)
            _append_target_group_lines(
                lines=lines,
                tg=tg,
                branch_prefix=t_branch,
                child_prefix=t_child,
                c=c,
                show_network=show_network,
                expanded=expanded,
            )


def _append_listener_lines(
    lines: list[str],
    listener: ListenerView,
    l_branch: str,
    l_child: str,
    c: _AnsiColor,
    show_network: bool,
    expanded: set[str],
) -> None:
    """
    単一リスナーとその配下のACM証明書・ルール一覧をツリー行として `lines` に追加する。

    :param lines: 出力行を蓄積するリスト
    :param listener: 描画対象の `ListenerView`
    :param l_branch: リスナー行自体の罫線プレフィックス
    :param l_child: リスナー配下要素のインデントプレフィックス
    :param c: `_AnsiColor` インスタンス
    :param show_network: ターゲット配下のサブネット・SG補助行を表示するか
    :param expanded: このツリーの中ですでに展開した ELB の ARN
    """
    proto_port = c.wrap(f"{listener.protocol}:{listener.port}", c.BOLD)
    # moto 等が HTTP にもデフォルトの SslPolicy を返すことがあるため HTTPS/TLS のみ表示する
    ssl_part = (
        f" [SSLPolicy: {listener.ssl_policy}]"
        if listener.ssl_policy and listener.protocol in ("HTTPS", "TLS")
        else ""
    )
    lines.append(f"{l_branch}Listener {proto_port}{ssl_part}")

    for cert, cert_last in _with_last(listener.certificates):
        cert_branch, _ = _tree_prefixes(l_child, cert_last and not listener.rules)
        domain_str = cert.domain_name or cert.certificate_arn.split("/")[-1]
        exp_str = f", expires={cert.not_after[:10]}" if cert.not_after else ""
        status_str = f", status={cert.status}" if cert.status else ""
        cert_id = cert.certificate_arn.split("/")[-1]
        lines.append(
            f"{cert_branch}{c.wrap('ACM Cert:', c.CYAN)} {domain_str} "
            f"({cert_id}{status_str}{exp_str})"
        )

    for rule, r_last in _with_last(listener.rules):
        r_branch, r_child = _tree_prefixes(l_child, r_last)
        prio_label = "[default]" if rule.is_default else f"[priority:{rule.priority}]"
        prio_colored = c.wrap(prio_label, c.YELLOW if not rule.is_default else c.DIM)
        lines.append(f"{r_branch}Rule {prio_colored} IF {rule.condition_text}")

        # 転送以外のアクション（転送の前の認証、リダイレクト、固定レスポンス）を先に、転送先を
        # あとに出す。認証つきの転送を、認証のない転送に見せないため
        refs = rule.target_group_refs
        other_actions = rule.non_forward_actions
        for index, act_text in enumerate(other_actions):
            is_last = not refs and index == len(other_actions) - 1
            a_branch, _ = _tree_prefixes(r_child, is_last)
            lines.append(f"{a_branch}{c.wrap(act_text, c.CYAN)}")
        for ref, ref_last in _with_last(refs):
            ref_branch, ref_child = _tree_prefixes(r_child, ref_last)
            weight_str = (
                f" (weight={ref.weight})" if ref.weight is not None and len(refs) > 1 else ""
            )
            if ref.target_group:
                _append_target_group_lines(
                    lines=lines,
                    tg=ref.target_group,
                    branch_prefix=ref_branch,
                    child_prefix=ref_child,
                    c=c,
                    expanded=expanded,
                    extra_label=weight_str,
                    show_network=show_network,
                )
            else:
                lines.append(f"{ref_branch}TargetGroup: {ref.target_group_name}{weight_str}")


def _append_target_group_lines(
    lines: list[str],
    tg: TargetGroupView,
    branch_prefix: str,
    child_prefix: str,
    c: _AnsiColor,
    expanded: set[str],
    extra_label: str = "",
    show_network: bool = True,
) -> None:
    """
    ターゲットグループと配下の実体ターゲット一覧（EC2/ECS/Lambda/ALB/NLB/ENI）をツリー行として追加する。

    :param lines: 出力行を蓄積するリスト
    :param tg: 描画対象の `TargetGroupView`
    :param branch_prefix: TG行自体の罫線プレフィックス
    :param child_prefix: TG配下要素のインデントプレフィックス
    :param c: `_AnsiColor` インスタンス
    :param expanded: このツリーの中ですでに展開した ELB の ARN
    :param extra_label: 重み表記（`" (weight=90)"` 等）の追加ラベル
    :param show_network: ターゲット配下のサブネット・SG補助行を表示するか
    """
    summary_badge = _health_summary_badge(tg, c)
    if tg.target_type == "lambda":
        # Lambda TG にはプロトコル・ポートがなく、ヘルスチェックも任意のため種別だけを出す
        tg_detail = "type=lambda"
    else:
        hc_info = f"HC={tg.health_check_protocol}:{tg.health_check_port}{tg.health_check_path}"
        ip_type_part = f", ip={tg.ip_address_type}" if tg.ip_address_type == "ipv6" else ""
        tg_detail = (
            f"{tg.protocol}:{tg.port or '-'}, type={tg.target_type}{ip_type_part}, {hc_info}"
        )
    lines.append(
        f"{branch_prefix}TG: {c.wrap(tg.name, c.BOLD)}{extra_label} ({tg_detail}) {summary_badge}"
    )

    tg_attr_text = _tg_attributes_summary(tg)
    if tg_attr_text:
        # 後ろに必ずターゲット行（または未登録行）が続くため常に ├──
        lines.append(f"{child_prefix}├── {c.wrap('attrs:', c.BLUE)} {c.wrap(tg_attr_text, c.DIM)}")

    if not tg.targets:
        lines.append(f"{child_prefix}└── {c.wrap(tr('fmt.no_targets_registered'), c.DIM)}")
        return

    for t, t_last in _with_last(tg.targets):
        t_branch, t_child = _tree_prefixes(child_prefix, t_last)
        _append_single_target_line(
            lines=lines,
            t=t,
            tg_type=tg.target_type,
            t_branch=t_branch,
            t_child=t_child,
            c=c,
            expanded=expanded,
            show_network=show_network,
            needs_attention=tg.needs_attention(t),
        )


def _format_target_network_summary(t: TargetView) -> str:
    """
    ターゲットに紐づくサブネット・セキュリティグループ情報を1行テキストに整形する。

    :param t: 対象の `TargetView`
    :return: `"subnet=... | sg=..."` 形式の文字列（情報がない場合は空文字列）
    """
    parts: list[str] = []
    if t.subnets:
        sub_strs: list[str] = []
        for s in t.subnets:
            cidrs = ", ".join(s.cidr_blocks)
            meta = ", ".join(x for x in (s.subnet_id, cidrs) if x)
            if s.name:
                sub_strs.append(f"{s.name} ({meta})" if meta else s.name)
            else:
                sub_strs.append(f"{s.subnet_id} ({cidrs})" if cidrs else s.subnet_id)
        label = "subnet" if len(sub_strs) == 1 else "subnets"
        parts.append(f"{label}={', '.join(sub_strs)}")

    if t.security_groups:
        sg_strs: list[str] = []
        for sg in t.security_groups:
            if sg.group_name:
                sg_strs.append(f"{sg.group_name} ({sg.group_id})")
            else:
                sg_strs.append(sg.group_id)
        parts.append(f"sg={', '.join(sg_strs)}")

    return " | ".join(parts)


def _append_target_network_subline(
    lines: list[str],
    t: TargetView,
    t_child: str,
    c: _AnsiColor,
    show_network: bool,
) -> None:
    """
    `show_network=True` の場合にターゲット行の直下へサブネット・SG情報の補助行（`net:`）を追加する。

    :param lines: 出力行を蓄積するリスト
    :param t: 対象の `TargetView`
    :param t_child: ターゲット配下要素のインデントプレフィックス
    :param c: `_AnsiColor` インスタンス
    :param show_network: ネットワーク補助行を表示するか
    """
    if not show_network:
        return
    net_summary = _format_target_network_summary(t)
    if net_summary:
        lines.append(f"{t_child}└── {c.wrap('net:', c.BLUE)} {c.wrap(net_summary, c.DIM)}")


def _append_single_target_line(
    lines: list[str],
    t: TargetView,
    tg_type: str,
    t_branch: str,
    t_child: str,
    c: _AnsiColor,
    expanded: set[str],
    show_network: bool = True,
    needs_attention: bool = True,
) -> None:
    """
    個別 `TargetView` の実体種別（ALB/NLB/Lambda/ECS/EC2/ENI/IP）に応じた行を `lines` に追加する。

    :param lines: 出力行を蓄積するリスト
    :param t: 描画対象の `TargetView`
    :param tg_type: 親TGの `target_type`（`"instance"`, `"ip"`, `"lambda"`, `"alb"`）
    :param t_branch: ターゲット行自体の罫線プレフィックス
    :param t_child: ターゲット配下要素のインデントプレフィックス
    :param c: `_AnsiColor` インスタンス
    :param expanded: このツリーの中ですでに展開した ELB の ARN
    :param show_network: サブネット・SG補助行を表示するか
    :param needs_attention: 調べるべき状態か（`TargetGroupView.needs_attention`）。偽なら理由を
        赤にしない（ヘルスチェックが無効な Lambda など、設定どおりの状態）
    """
    badge = c.health_badge(t.health_state)
    reason_color = c.RED if needs_attention else c.DIM
    reason_part = (
        f" {c.wrap(f'({t.health_reason}: {t.health_description})', reason_color)}"
        if t.health_reason and t.health_state != "healthy"
        else ""
    )

    # 下流LB: 下流LBのツリーも入れ子で展開する（同じ ELB は最初の1回だけ。複数 AZ のノードの IP や、
    # NLB の 80 と 443 が同じ ELB を向くときに、同じツリーを何度も出さない）
    if t.downstream_lb is not None:
        label = f"{t_branch}{badge} {_downstream_lb_label(t, t.downstream_lb, c)}{reason_part}"
        if t.downstream_lb.arn in expanded:
            lines.append(f"{label} {c.wrap(tr('fmt.expanded_above'), c.DIM)}")
            return
        lines.append(label)
        expanded.add(t.downstream_lb.arn)
        _append_lb_body_lines(
            lines=lines,
            lb=t.downstream_lb,
            base_prefix=t_child,
            c=c,
            show_network=False,
            expanded=expanded,
        )
        return

    lines.append(f"{t_branch}{badge} {_target_label(t, tg_type, c)}{reason_part}")
    _append_target_network_subline(lines, t, t_child, c, show_network)


def _downstream_lb_label(t: TargetView, lb: LoadBalancerView, c: _AnsiColor) -> str:
    """
    下流 ELB のターゲットの行（バッジの後ろ）。

    - NLB の alb タイプTGに登録された ALB → [ALB]
    - ip タイプTGに登録された ELB のノードの IP（ENI から特定）→ [IP->NLB] / [IP->ALB]

    :param t: 対象の `TargetView`
    :param lb: その下流 ELB（`t.downstream_lb`）
    :param c: `_AnsiColor` インスタンス
    :return: `[ALB] 名前 [詳細]` の形の文字列
    """
    lb_kind = "NLB" if lb.type == "network" else "ALB"
    via_ip = t.eni is not None and bool(t.eni.load_balancer_arn)
    tag = c.wrap(f"[IP->{lb_kind}]" if via_ip else f"[{lb_kind}]", c.CYAN, c.BOLD)
    ip_part = (
        f"ip={t.ip_address}, az={t.availability_zone or '-'}, eni={t.eni.network_interface_id}, "
        if via_ip and t.eni
        else ""
    )
    detail = f"port={t.port}, {ip_part}scheme={lb.scheme}, state={lb.state}, dns={lb.dns_name}"
    return f"{tag} {c.wrap(lb.name, c.BOLD)} [{detail}]"


def _target_label(t: TargetView, tg_type: str, c: _AnsiColor) -> str:
    """
    下流 ELB 以外のターゲットの行（バッジの後ろ）。実体の種別ごとに見出しと詳細を変える。

    :param t: 対象の `TargetView`
    :param tg_type: 親TGの `target_type`
    :param c: `_AnsiColor` インスタンス
    :return: `[種別] 名前 [詳細]` の形の文字列
    """
    if t.lambda_function is not None:
        return _lambda_label(t.lambda_function, c)
    if t.ecs is not None:
        return _ecs_label(t, t.ecs, c)
    if t.ec2 is not None:
        # instance登録 または IP登録でEC2に逆引きできたケース
        return _ec2_label(t, t.ec2, tg_type, c)
    if t.eni is not None:
        eni = t.eni
        desc_part = f", desc={eni.description}" if eni.description else ""
        az_part = f", az={t.availability_zone or eni.availability_zone}"
        return (
            f"{c.wrap('[IP/ENI]', c.CYAN)} {c.wrap(t.target_id, c.BOLD)} "
            f"[eni={eni.network_interface_id}, port={t.port}{az_part}{desc_part}]"
        )
    # その他のターゲット（未解決IPやARN）
    az_part = f", az={t.availability_zone}" if t.availability_zone else ""
    return f"{c.wrap(t.target_id, c.BOLD)} [port={t.port}{az_part}]"


def _lambda_label(fn: LambdaFunctionInfo, c: _AnsiColor) -> str:
    """Lambda 関数のターゲットの `[Lambda] 名前 [詳細]`。"""
    detail = (
        f"runtime={fn.runtime or '-'}, handler={fn.handler or '-'}, state={fn.state or 'Active'}"
    )
    return f"{c.wrap('[Lambda]', c.MAGENTA, c.BOLD)} {c.wrap(fn.function_name, c.BOLD)} [{detail}]"


def _ecs_label(t: TargetView, ecs: ECSTaskInfo, c: _AnsiColor) -> str:
    """ECS タスク / サービス（Fargate または EC2 起動タイプ）のターゲットの `[ECS] 名前 [詳細]`。"""
    lt = f":{ecs.launch_type}" if ecs.launch_type else ""
    title = f"{ecs.cluster_name}/{ecs.service_name}" if ecs.service_name else ecs.cluster_name
    parts = [f"port={t.port}"]
    if ecs.task_id:
        parts.append(f"task={ecs.task_id}")
    if t.ip_address:
        parts.append(f"ip={t.ip_address}")
    if ecs.container_name:
        parts.append(f"container={ecs.container_name}")
    if ecs.task_definition:
        parts.append(f"def={ecs.task_definition}")
    if t.ec2:
        parts.append(f"host={t.ec2.name or t.ec2.instance_id}")
    az_val = t.availability_zone or ecs.availability_zone
    if az_val:
        parts.append(f"az={az_val}")
    return f"{c.wrap(f'[ECS{lt}]', c.BLUE, c.BOLD)} {c.wrap(title, c.BOLD)} [{', '.join(parts)}]"


def _ec2_label(t: TargetView, ec2: EC2InstanceInfo, tg_type: str, c: _AnsiColor) -> str:
    """EC2 のターゲットの `名前 [詳細]`（IP で登録されていれば先頭に `[IP->EC2]`）。"""
    prefix_tag = f"{c.wrap('[IP->EC2]', c.CYAN, c.BOLD)} " if tg_type == "ip" else ""
    name = c.wrap(ec2.name or tr("fmt.no_name_tag"), c.BOLD)
    detail = (
        f"id={ec2.instance_id}, port={t.port}, ip={t.ip_address or ec2.private_ip or '-'}, "
        f"az={t.availability_zone or ec2.availability_zone or '-'}, "
        f"type={ec2.instance_type or '-'}, ec2={ec2.state or '-'}"
    )
    return f"{prefix_tag}{name} [{detail}]"


def format_lb_summary_table(lbs: Sequence[LoadBalancerView], use_color: bool | None = None) -> str:
    """
    Render load balancers as a one-line-per-load-balancer table (`elbx ls`): type, scheme,
    state, listeners, healthy / total targets (with the number that need attention, when
    there are any; see `TargetGroupView.needs_attention`) and the Route 53 names.

    ELB を1行ずつの表にする（`elbx ls`）。種別・スキーム・状態・リスナー・ターゲットの
    healthy / 総数（調べるべきターゲットがあればその数も。`TargetGroupView.needs_attention`
    を参照）・Route 53 の名前。

    :param lbs: the load balancers / 対象の ELB
    :param use_color: ANSI colors on / off; None (default) decides from the environment:
        on with `FORCE_COLOR`, off with `NO_COLOR`, otherwise only when stdout is a terminal
        / ANSI の色を付けるか。None（既定）なら環境で決める: `FORCE_COLOR` があれば付け、
        `NO_COLOR` があれば付けず、どちらもなければ標準出力が端末のときだけ付ける
    :return: the table as text / 表の文字列
    """
    if not lbs:
        return tr("fmt.no_load_balancers")

    headers = [
        "NAME",
        "TYPE",
        "SCHEME",
        "STATE",
        "VPC",
        "LISTENERS",
        "TARGETS (HEALTHY/TOTAL)",
        "ROUTE53 / DNS NAME",
    ]
    rows: list[list[str]] = []
    alert_cells: set[tuple[int, int]] = set()
    for row_index, lb in enumerate(lbs):
        ports = ", ".join(f"{lst.protocol}:{lst.port}" for lst in lb.listeners) or "-"
        # ipv4 以外（dualstack 等）のときだけ IP アドレスタイプを SCHEME に添える
        scheme = (
            lb.scheme if lb.ip_address_type == "ipv4" else f"{lb.scheme} ({lb.ip_address_type})"
        )
        total_t = sum(tg.total_count for tg in lb.target_groups)
        healthy_t = sum(tg.healthy_count for tg in lb.target_groups)
        # healthy の数だけでは、ヘルスチェックが無効な Lambda を含む ELB がいつも欠けて見えるので、
        # 調べるべきものがあるときだけ、その数を添えて目立たせる
        attention = sum(tg.attention_count for tg in lb.target_groups)
        attention_part = f", {tr('fmt.ls_attention', count=attention)}" if attention else ""
        if attention:
            alert_cells.add((row_index, headers.index("TARGETS (HEALTHY/TOTAL)")))
        r53_names = _route53_names(lb)
        dns_display = f"{r53_names} -> {lb.dns_name}" if r53_names else lb.dns_name
        rows.append(
            [
                lb.name,
                lb.type,
                scheme,
                lb.state,
                lb.vpc_id,
                ports,
                f"{healthy_t}/{total_t} ({len(lb.target_groups)} TGs{attention_part})",
                dns_display,
            ]
        )
    return _render_table(headers, rows, use_color=use_color, alert_cells=alert_cells)


def _target_resource_label(t: TargetView) -> str:
    """
    テーブル表示用のリソース名・詳細ラベル（`RESOURCE / NAME` 列）を生成する。

    :param t: 対象の `TargetView`
    :return: リソース名ラベル文字列（該当しない場合は `"-"`)
    """
    if t.downstream_lb:
        lb_prefix = "NLB" if t.downstream_lb.type == "network" else "ALB"
        return f"{lb_prefix}: {t.downstream_lb.name}"
    if t.lambda_function:
        return f"Lambda: {t.lambda_function.function_name} ({t.lambda_function.runtime})"
    if t.ecs:
        svc = f"/{t.ecs.service_name}" if t.ecs.service_name else ""
        tid = f" ({t.ecs.task_id})" if t.ecs.task_id else ""
        return f"ECS: {t.ecs.cluster_name}{svc}{tid}"
    if t.ec2 and t.ec2.name:
        return t.ec2.name
    if t.eni and t.eni.network_interface_id:
        return f"ENI: {t.eni.network_interface_id}"
    return "-"


def _target_subnet_cell(t: TargetView) -> str:
    """
    テーブル表示用のターゲットサブネット列（`SUBNET` 列）文字列を返す。

    :param t: 対象の `TargetView`
    :return: `"サブネット名 (subnet-xxx)"` または `"subnet-xxx"`（不明時は `"-"`)
    """
    if t.subnet:
        return f"{t.subnet.name} ({t.subnet.subnet_id})" if t.subnet.name else t.subnet.subnet_id
    return "-"


def _target_sg_cell(t: TargetView) -> str:
    """
    テーブル表示用のターゲットセキュリティグループ列（`SECURITY GROUPS` 列）文字列を返す。

    :param t: 対象の `TargetView`
    :return: カンマ区切りのSG表記（不明時は `"-"`)
    """
    if not t.security_groups:
        return "-"
    return ", ".join(
        f"{sg.group_name} ({sg.group_id})" if sg.group_name else sg.group_id
        for sg in t.security_groups
    )


def _target_runtime_state(t: TargetView) -> str:
    """
    テーブル表示用の実体リソース稼働状態（`STATE` 列）を返す。

    :param t: 対象の `TargetView`
    :return: 稼働状態文字列（例: `"running"`, `"RUNNING"`, `"Active"`、不明時は `"-"`)
    """
    if t.downstream_lb:
        return t.downstream_lb.state
    if t.lambda_function:
        return t.lambda_function.state or "Active"
    if t.ecs:
        return t.ecs.last_status or "RUNNING"
    if t.ec2 and t.ec2.state:
        return t.ec2.state
    return "-"


def _health_check_cell(tg: TargetGroupView) -> str:
    """
    テーブル表示用のヘルスチェック設定列（`HEALTH CHECK` 列）を返す。

    :param tg: 対象の `TargetGroupView`
    :return: ヘルスチェック設定の概要文字列
    """
    if tg.target_type == "lambda" and not tg.health_check_protocol:
        return "-"
    path = tg.health_check_path or ""
    matcher = f" {tg.matcher}" if tg.matcher else ""
    return (
        f"{tg.health_check_protocol}:{tg.health_check_port}{path}{matcher} "
        f"every {tg.health_check_interval_seconds}s "
        f"(timeout {tg.health_check_timeout_seconds}s, "
        f"unhealthy x{tg.unhealthy_threshold_count}, healthy x{tg.healthy_threshold_count})"
    )


def format_targets_table(
    target_groups: Sequence[TargetGroupView],
    use_color: bool | None = None,
    show_health_detail: bool = False,
) -> str:
    """
    Render targets as a one-line-per-target table (`elbx targets`): target group, kind,
    resource name, IP, port, AZ, subnet, security groups, health and state.

    ターゲットを1行ずつの表にする（`elbx targets`）。TG・種別・リソース名・IP・ポート・AZ・
    サブネット・
    SG・ヘルス状態・稼働状態。

    :param target_groups: the target groups / 対象の TG
    :param use_color: ANSI colors on / off; None (default) decides from the environment:
        on with `FORCE_COLOR`, off with `NO_COLOR`, otherwise only when stdout is a terminal
        / ANSI の色を付けるか。None（既定）なら環境で決める: `FORCE_COLOR` があれば付け、
        `NO_COLOR` があれば付けず、どちらもなければ標準出力が端末のときだけ付ける
    :param show_health_detail: add the `DETAIL` (health description) and `HEALTH CHECK`
        (health check settings) columns, as `elbx targets --unhealthy` does
        / `DETAIL`（ヘルス状態の詳細）と `HEALTH CHECK`（ヘルスチェックの設定）の列を足す
        （`elbx targets --unhealthy` と同じ）
    :return: the table as text / 表の文字列
    """
    if not target_groups:
        return tr("fmt.no_target_groups")

    headers = [
        "TARGET GROUP",
        "KIND",
        "TARGET ID",
        "RESOURCE / NAME",
        "PRIVATE IP",
        "PORT",
        "AZ",
        "SUBNET",
        "SECURITY GROUPS",
        "ELB HEALTH",
        "STATE",
        "REASON",
    ]
    base_columns = len(headers)
    if show_health_detail:
        headers += ["DETAIL", "HEALTH CHECK"]
    rows: list[list[str]] = []
    for tg in target_groups:
        if not tg.targets:
            row = [tg.name, tg.target_type, "(none)"] + ["-"] * (base_columns - 3)
            if show_health_detail:
                row += ["-", _health_check_cell(tg)]
            rows.append(row)
            continue
        for t in tg.targets:
            row = [
                tg.name,
                t.target_kind,
                t.target_id,
                _target_resource_label(t),
                t.ip_address or "-",
                str(t.port or "-"),
                t.availability_zone or "-",
                _target_subnet_cell(t),
                _target_sg_cell(t),
                t.health_state,
                _target_runtime_state(t),
                t.health_reason or "-",
            ]
            if show_health_detail:
                row += [t.health_description or "-", _health_check_cell(tg)]
            rows.append(row)
    return _render_table(headers, rows, use_color=use_color)


def format_lookup_results_table(
    results: Sequence[TargetLookupResult], use_color: bool | None = None
) -> str:
    """
    Render the matches of `ELBClient.find_target` as a table (`elbx find`).

    `ELBClient.find_target` の結果を表にする（`elbx find`）。

    :param results: the matches / 逆引きの結果
    :param use_color: ANSI colors on / off; None (default) decides from the environment:
        on with `FORCE_COLOR`, off with `NO_COLOR`, otherwise only when stdout is a terminal
        / ANSI の色を付けるか。None（既定）なら環境で決める: `FORCE_COLOR` があれば付け、
        `NO_COLOR` があれば付けず、どちらもなければ標準出力が端末のときだけ付ける
    :return: the table as text / 表の文字列
    """
    if not results:
        return tr("fmt.no_matching_targets")

    headers = [
        "LOAD BALANCER",
        "TARGET GROUP",
        "KIND",
        "TARGET ID",
        "RESOURCE / NAME",
        "PRIVATE IP",
        "PORT",
        "ELB HEALTH",
        "STATE",
    ]
    rows: list[list[str]] = []
    for r in results:
        t = r.target
        rows.append(
            [
                r.load_balancer_name
                or tr("fmt.unknown_lb" if r.load_balancer_arn else "fmt.no_lb"),
                r.target_group_name,
                t.target_kind,
                t.target_id,
                _target_resource_label(t),
                t.ip_address or "-",
                str(t.port or "-"),
                t.health_state,
                _target_runtime_state(t),
            ]
        )
    return _render_table(headers, rows, use_color=use_color)


def format_route_simulation(result: RouteSimulationResult, use_color: bool | None = None) -> str:
    """
    Render a route simulation (`elbx route`): every rule evaluated with ✓ / ✗ and the
    reasons, then the matched action, its target groups and their targets' health, and the
    downstream load balancers indented below.

    route シミュレーションの結果を整形する（`elbx route`）。評価したルールを ✓ / ✗ と理由つきで
    並べ、一致したアクション・転送先 TG・ターゲットのヘルス状態、下流の ELB を字下げして出す。

    :param result: the simulation result / シミュレーションの結果
    :param use_color: ANSI colors on / off; None (default) decides from the environment:
        on with `FORCE_COLOR`, off with `NO_COLOR`, otherwise only when stdout is a terminal
        / ANSI の色を付けるか。None（既定）なら環境で決める: `FORCE_COLOR` があれば付け、
        `NO_COLOR` があれば付けず、どちらもなければ標準出力が端末のときだけ付ける
    :return: the text / 文字列
    """
    c = _AnsiColor(use_color)
    lines = [c.wrap(f"{result.method} {result.url}", c.BOLD)]
    for entry in result.entries:
        lines.append("")
        _append_route_hop_lines(lines, entry, "", c, tr("fmt.route_entry"))
    return c.render(lines)


def _append_route_hop_lines(
    lines: list[str], hop: RouteHop, indent: str, c: _AnsiColor, label: str
) -> None:
    """
    1台のロードバランサーでのルール評価経過を `lines` に追加し、下流のロードバランサーへ再帰する。

    :param lines: 出力行を蓄積するリスト
    :param hop: 描画対象の `RouteHop`
    :param indent: 現在のインデント文字列
    :param c: `_AnsiColor` インスタンス
    :param label: ホップ先頭に付与する見出し（`"入口: "` または `"↓ 転送先: "`）
    """
    lb = hop.load_balancer
    badge = c.wrap(f"[{lb.type.upper()}]", c.CYAN, c.BOLD)
    listener = (
        f"Listener {hop.listener.protocol}:{hop.listener.port}"
        if hop.listener
        else f"port {hop.port}"
    )
    lines.append(f"{indent}{label}{badge} {c.wrap(lb.name, c.BOLD)} ({listener})")

    for ev in hop.evaluations:
        mark = c.wrap("✓", c.GREEN, c.BOLD) if ev.matched else c.wrap("✗", c.DIM)
        prio = "[default]" if ev.rule.is_default else f"[priority:{ev.rule.priority}]"
        lines.append(f"{indent}  {mark} Rule {prio} IF {ev.rule.condition_text}")
        lines.extend(f"{indent}      {c.wrap(reason, c.DIM)}" for reason in ev.reasons)

    rule = hop.matched_rule
    if rule is not None:
        actions = ", ".join(rule.actions_summary) or "-"
        lines.append(f"{indent}  => {c.wrap(actions, c.MAGENTA, c.BOLD)}")
        for ref in rule.target_group_refs:
            tg = ref.target_group
            # 実際の AWS は TG が 1 つの forward にも weight=1 を付けて返すので、tree と同じく
            # 複数の TG のときだけ重みを出す。重み 0 は届かない理由なので、1 つでも出す
            weighted = len(rule.target_group_refs) > 1 or ref.weight == 0
            weight = f" (weight={ref.weight})" if ref.weight is not None and weighted else ""
            summary = f" [{tg.health_summary}]" if tg else ""
            # 重み 0 の TG にはリクエストが届かないので、simulate_hop と同じくターゲットをたどらない
            if ref.weight == 0:
                no_traffic = c.wrap(tr("fmt.route_no_traffic"), c.DIM)
                lines.append(
                    f"{indent}     TG: {ref.target_group_name}{weight}{summary} {no_traffic}"
                )
                continue
            lines.append(f"{indent}     TG: {ref.target_group_name}{weight}{summary}")
            for t in tg.targets if tg else []:
                if t.downstream_lb is not None:
                    continue  # 下流LBは以下の「転送先」で展開する
                reason = f" ({t.health_reason})" if t.health_reason else ""
                lines.append(
                    f"{indent}       - {c.health_badge(t.health_state)} "
                    f"{t.display_name} port={t.port or '-'}{reason}"
                )

    if hop.note:
        lines.append(f"{indent}  {c.wrap('! ' + hop.note, c.YELLOW)}")

    for down in hop.downstream:
        _append_route_hop_lines(lines, down, indent + "    ", c, tr("fmt.route_downstream"))


def _display_width(text: str) -> int:
    """
    端末に表示したときの幅（全角文字は 2、結合文字と幅のない文字は 0）を返す。
    `len()` で数えると、日本語の Name タグや「(ELB未アタッチ)」の行だけ表の列がずれるため。

    :param text: 表示する文字列
    :return: 表示幅（カラム数）
    """
    # Mn / Me は結合文字（異体字セレクタを含む）、Cf はゼロ幅スペースなどの幅のない文字
    return sum(
        0
        if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf")
        else 2
        if unicodedata.east_asian_width(ch) in "WF"
        else 1
        for ch in text
    )


def _pad(text: str, width: int) -> str:
    """
    `str.ljust` の表示幅版（全角文字を 2 と数えて、右を空白で埋める）。

    :param text: 埋める文字列
    :param width: 埋めたあとの表示幅
    :return: 右を空白で埋めた文字列
    """
    return text + " " * (width - _display_width(text))


def _render_table(
    headers: list[str],
    rows: list[list[str]],
    use_color: bool | None = None,
    alert_cells: set[tuple[int, int]] | None = None,
) -> str:
    """
    2次元配列を列幅揃えのテキストテーブルに変換する（列幅は端末での表示幅で数える）。

    :param headers: カラム見出しの文字列リスト
    :param rows: 各行のセル文字列リストのリスト
    :param use_color: ANSIカラー出力の有無（Noneの場合は自動判定）
    :param alert_cells: 赤で目立たせるセルの `(行, 列)`（どちらも 0 から）
    :return: 整形されたテーブル文字列
    """
    c = _AnsiColor(use_color)
    rows = [[_escape_controls(cell) for cell in row] for row in rows]
    widths = [_display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _display_width(cell))

    header_line = "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers))
    sep_line = "  ".join("-" * widths[i] for i in range(len(headers)))

    out_lines = [c.wrap(header_line, c.BOLD), c.wrap(sep_line, c.DIM)]
    for row_index, row in enumerate(rows):
        formatted_cells: list[str] = []
        for i, cell in enumerate(row):
            # ANSIコードを付ける前に埋めないと、エスケープ文字分だけ列幅計算がずれる
            padded = _pad(cell, widths[i])
            if headers[i] == "ELB HEALTH":
                padded = c.wrap(padded, *c.health_codes(cell))
            elif alert_cells and (row_index, i) in alert_cells:
                padded = c.wrap(padded, c.RED, c.BOLD)
            formatted_cells.append(padded)
        out_lines.append("  ".join(formatted_cells))
    return c.render(out_lines)


def print_lb_tree(
    lb: LoadBalancerView,
    use_color: bool | None = None,
    show_network: bool = True,
) -> None:
    """
    Print `format_lb_tree(lb, ...)` to stdout.

    `format_lb_tree(lb, ...)` を標準出力に出す。

    :param lb: the load balancer / 対象の ELB
    :param use_color: ANSI colors on / off; None (default) decides from the environment:
        on with `FORCE_COLOR`, off with `NO_COLOR`, otherwise only when stdout is a terminal
        / ANSI の色を付けるか。None（既定）なら環境で決める: `FORCE_COLOR` があれば付け、
        `NO_COLOR` があれば付けず、どちらもなければ標準出力が端末のときだけ付ける
    :param show_network: include subnets and security groups / サブネットと SG を含める
    """
    print(format_lb_tree(lb, use_color=use_color, show_network=show_network))
