"""
Route simulation: evaluates which listener rule a request (URL, method, headers, source IP)
matches on an already fetched `LoadBalancerView`, with the ALB semantics. No AWS API is
called here; `ELBClient.simulate_request` calls `simulate_hop` and fetches each load balancer
just before it is evaluated.

ルーティング先のシミュレーション。取得済みの `LoadBalancerView` に対して、リクエスト（URL・
メソッド・ヘッダー・送信元 IP）がどのリスナールールに一致するかを ALB と同じ規則で評価する。
AWS API は呼ばない（`ELBClient.simulate_request` が `simulate_hop` を呼び、各 ELB を評価の直前に
取得する）。
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from .messages import t
from .models import ListenerView, LoadBalancerView, RouteHop, RuleEvaluation, RuleView

# NLB -> ALB / ALB -> NLB と下流のロードバランサーをたどる最大段数（循環構成の保険）
MAX_ROUTE_DEPTH = 5

# URL の先頭のスキーム（クエリの中の `next=https://…` をスキームと取り違えない）
_URL_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://")

# RegexValues を持てる条件の設定キー
_REGEX_CONFIG_KEYS = {
    "host-header": "HostHeaderConfig",
    "path-pattern": "PathPatternConfig",
    "http-header": "HttpHeaderConfig",
}


@dataclass
class SimulatedRequest:
    """The request to simulate. `headers` keys are lower-cased; `query` keeps the order and
    duplicates of the query string, percent-decoded the way an ALB reads it (`+` stays `+`);
    `raw_query` is the query string as written, which a condition without a key is matched
    against. `path` is as written; it is decoded and its `.` / `..` segments are removed
    when a path condition is evaluated.

    評価するリクエスト。`headers` のキーは小文字。`query` はクエリ文字列の順と重複をそのまま持ち、
    ALB と同じく `%XX` をデコードしてある（`+` は `+` のまま）。`raw_query` は書かれたままの
    クエリ文字列で、キーのない条件はこれと照合する。`path` は書かれたままで、パスの条件を評価する
    ときに、デコードして `.` と `..` の部分を取り除く。
    """

    url: str
    scheme: str
    host: str
    port: int
    path: str
    query: list[tuple[str, str]] = field(default_factory=list)
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)  # キーは小文字
    source_ip: str | None = None
    raw_query: str = ""

    @classmethod
    def from_url(
        cls,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        source_ip: str | None = None,
    ) -> SimulatedRequest:
        """
        Build the request from a URL (`http://` is assumed when the scheme is missing; the
        port defaults to 443 for https and 80 for http). `Host` is added to the headers from
        the URL unless given.

        URL からリクエストを作る（スキームがなければ `http://`、ポートは https なら 443、http なら
        80）。`Host` ヘッダーがなければ URL のホスト名を入れる。

        :param url: the request URL / リクエストの URL
        :param method: HTTP method (default `"GET"`) / HTTP メソッド（既定 `"GET"`）
        :param headers: request headers / リクエストヘッダー
        :param source_ip: client IP / 送信元 IP
        :raises ValueError: the scheme is not http / https, the host name is missing, or
            `source_ip` is not an IP address
            / スキームが http / https でない、ホスト名がない、`source_ip` が IP アドレスでない
        """
        raw = url if _URL_SCHEME.match(url) else f"http://{url}"
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(t("sim.bad_scheme", scheme=scheme))
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError(t("sim.no_host"))
        port = parts.port
        if port is None:  # ポート 0 を省略と取り違えない
            port = 443 if scheme == "https" else 80
        # 空文字は「省略」ではなく不正な値として ValueError にする（--source-ip "" の誤り）
        if source_ip is not None:
            try:
                ipaddress.ip_address(source_ip)
            except ValueError:
                raise ValueError(t("sim.bad_source_ip", value=source_ip)) from None

        # ヘッダー名はALBの評価に合わせて小文字に正規化し、Host未指定ならURLのホスト名を入れる
        hdrs = {k.strip().lower(): v.strip() for k, v in (headers or {}).items()}
        hdrs.setdefault("host", host)
        return cls(
            url=raw,
            scheme=scheme,
            host=host,
            port=port,
            path=parts.path or "/",
            query=_decoded_query(parts.query),
            method=method,
            headers=hdrs,
            source_ip=source_ip,
            raw_query=parts.query,
        )


def _decoded_query(raw: str) -> list[tuple[str, str]]:
    """
    クエリ文字列を、ALB がキーと値の条件を照合するときと同じ読み方で `(キー, 値)` の並びにする。

    実際の ALB で確かめた動き（2026-11-12）: 全体の `%XX` を 1 回だけデコードしてから `&` と
    最初の `=` で分ける（`z=1%26k=v` は `k=v` を含み、`k%3Dv` は `k=v` になる）。`+` は空白に
    せず `+` のまま。`;` では分けない。`parse_qsl` は分けてからデコードし、`+` を空白にするので
    使わない。

    :param raw: `?` より後ろのクエリ文字列（デコード前）
    :return: `(キー, 値)` の並び（`=` のない部分は値が空文字）
    """
    pairs: list[tuple[str, str]] = []
    for part in unquote(raw).split("&"):
        if part:
            key, _, value = part.partition("=")
            pairs.append((key, value))
    return pairs


def _normalized_path(path: str) -> str:
    """
    パスを、ALB が path-pattern と照合するときの形にする。

    実際の ALB で確かめた動き（2026-11-12）: `%XX` を 1 回だけデコードし（`%2F` も `/` になる）、
    そのあとで `.` と `..` の部分を RFC 3986 のとおりに取り除く（`/a/./b` と `/a/x/../b` は
    `/a/b`、`/a/b/.` は `/a/b/`）。`//` と末尾の `/` はそのまま。`+` は `+` のまま。

    :param path: リクエストのパス（デコード前）
    :return: 照合に使うパス
    """
    decoded = unquote(path)
    if not decoded.startswith("/"):
        return decoded
    segments = decoded.split("/")[1:]
    kept: list[str] = []
    for i, segment in enumerate(segments):
        if segment in (".", ".."):
            if segment == ".." and kept:
                kept.pop()
            if i == len(segments) - 1:
                kept.append("")  # 末尾の `.` / `..` は `/` で終わる
        else:
            kept.append(segment)
    return "/" + "/".join(kept)


# パターンの 1 文字ぶん: (文字, ワイルドカードとして働くか)
_ANY_RUN = ("*", True)
_ANY_ONE = ("?", True)


def _pattern_tokens(pattern: str, escape: bool) -> list[tuple[str, bool]]:
    """
    パターンを `(文字, ワイルドカードとして働くか)` の並びにする。

    :param pattern: パターン
    :param escape: True なら `\\*` と `\\?` を、ワイルドカードではない `*` と `?` として読む
    :return: 1 文字ぶんずつの並び
    """
    tokens: list[tuple[str, bool]] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        # ほかの文字の前の `\` は、AWS の文書に決まりがないので、その文字のままにする
        if escape and ch == "\\" and pattern[i + 1 : i + 2] in ("*", "?"):
            tokens.append((pattern[i + 1], False))
            i += 2
            continue
        tokens.append((ch, ch in "*?"))
        i += 1
    return tokens


def wildcard_match(pattern: str, value: str, ignore_case: bool, escape: bool = False) -> bool:
    """
    Match `value` against an ALB rule pattern: `*` matches any run of characters
    (including none) and `?` exactly one; the whole value must match.

    ALB のルールと同じ規則で照合する（`*` は 0 文字以上、`?` は 1 文字。値全体が一致すること）。

    :param pattern: the pattern / パターン
    :param value: the value / 照合する値
    :param ignore_case: ignore letter case (hosts, headers, query strings)
        / 大文字小文字を区別しない（ホスト・ヘッダー・クエリ）
    :param escape: read `\\*` and `\\?` in the pattern as the characters themselves, as a
        query-string condition does
        / パターンの `\\*` と `\\?` を、その文字そのものとして読む（query-string 条件の規則）
    """
    if ignore_case:
        pattern, value = pattern.lower(), value.lower()
    tokens = _pattern_tokens(pattern, escape)
    # `.*` の正規表現にすると `*a*a*a*…` で極端に遅くなる（ReDoS）ので、直前の `*` にだけ戻って
    # やり直す照合にする（手間は最悪でも len(pattern) * len(value)）
    p = v = 0
    star_p = -1  # 直前の `*` の位置
    star_v = 0  # その `*` が飲み込んだ文字の終わり
    while v < len(value):
        if p < len(tokens) and tokens[p] == _ANY_RUN:
            star_p, star_v = p, v
            p += 1
        elif p < len(tokens) and tokens[p] in (_ANY_ONE, (value[v], False)):
            p += 1
            v += 1
        elif star_p >= 0:
            star_v += 1
            p, v = star_p + 1, star_v
        else:
            return False
    return all(token == _ANY_RUN for token in tokens[p:])


def _host_without_port(host_header: str) -> str:
    """
    Host ヘッダーの値からポートを除いた小文字のホスト名を返す（`[2001:db8::1]:443` のような
    IPv6 の表記にも対応）。空や解釈できない値は空文字列。

    :param host_header: Host ヘッダーの値
    :return: ポートを除いたホスト名（小文字）
    """
    if not host_header:
        return ""
    with contextlib.suppress(ValueError):
        return urlsplit(f"//{host_header.strip()}").hostname or ""
    return ""


def query_pair_label(pair: dict[str, Any]) -> str:
    """
    query-string 条件の 1 つの値を表示用の文字列にする。

    :param pair: `QueryStringConfig.Values` の 1 要素
    :return: Key があれば `key=value`、なければ `value` だけ（クエリ文字列の全体と照合するため）
    """
    value = str(pair.get("Value", "*"))
    key = pair.get("Key")
    return value if key is None else f"{key}={value}"


def _values(cond: dict[str, Any], config_key: str) -> list[str]:
    """
    条件辞書から値リストを取り出す（新形式の `*Config` と旧形式の `Values` の両対応）。

    :param cond: `describe_rules` の Condition 辞書
    :param config_key: `HostHeaderConfig` などの設定キー名
    :return: 条件値の文字列リスト
    """
    return list(cond.get(config_key, {}).get("Values") or cond.get("Values") or [])


def _regex_values(cond: dict[str, Any]) -> list[str]:
    """
    条件の正規表現の値（`RegexValues`）を返す。

    :param cond: `describe_rules` の Condition 辞書
    :return: 正規表現のリスト（host-header / path-pattern / http-header 以外や、値がなければ空）
    """
    config_key = _REGEX_CONFIG_KEYS.get(cond.get("Field", ""), "")
    return list(cond.get(config_key, {}).get("RegexValues") or [])


def _result(label: str, actual: str, values: list[str], ok: bool) -> tuple[bool, str]:
    """
    条件評価の判定結果と理由文を組み立てるヘルパー。

    :param label: 条件種別の表示名（例: `"Host"`, `"Path"`）
    :param actual: リクエスト側の実際の値
    :param values: ルール側に設定された候補値リスト
    :param ok: 一致したかどうか
    :return: `(一致フラグ, 理由メッセージ)` のタプル
    """
    key = "sim.matched" if ok else "sim.not_matched"
    return ok, t(key, label=label, actual=actual, values=", ".join(values))


def evaluate_condition(cond: dict[str, Any], req: SimulatedRequest) -> tuple[bool, str]:
    """
    単一のルール条件を評価する（条件内の複数値は OR 判定）。
    ALB の仕様に合わせ、ホスト・ヘッダー・クエリは大文字小文字を区別せず、
    パスとHTTPメソッドは区別する。

    :param cond: `describe_rules` の Condition 辞書
    :param req: 評価対象の `SimulatedRequest`
    :return: `(一致フラグ, 判定理由メッセージ)` のタプル
    """
    field_name = cond.get("Field", "")

    # ALB の正規表現は Python の re と同じ規則とは限らないので、評価せず、そのことを理由に出す
    regexes = _regex_values(cond)
    if regexes:
        return False, t("sim.regex_condition", field=field_name, values=", ".join(regexes))

    if field_name == "host-header":
        vals = _values(cond, "HostHeaderConfig")
        # ALB は Host ヘッダーの値（ポートを除く）と照合する。Host がなければ URL のホスト名
        host = _host_without_port(req.headers.get("host", "")) or req.host
        ok = any(wildcard_match(v, host, ignore_case=True) for v in vals)
        return _result("Host", host, vals, ok)

    if field_name == "path-pattern":
        vals = _values(cond, "PathPatternConfig")
        path = _normalized_path(req.path)
        ok = any(wildcard_match(v, path, ignore_case=False) for v in vals)
        return _result("Path", path, vals, ok)

    if field_name == "http-request-method":
        vals = _values(cond, "HttpRequestMethodConfig")
        return _result("Method", req.method, vals, req.method in vals)

    if field_name == "http-header":
        cfg = cond.get("HttpHeaderConfig", {})
        h_name = cfg.get("HttpHeaderName", "")
        vals = list(cfg.get("Values", []))
        actual = req.headers.get(h_name.lower())
        if actual is None:
            return False, t("sim.header_missing", name=h_name)
        ok = any(wildcard_match(v, actual, ignore_case=True) for v in vals)
        return _result(f"Header({h_name})", actual, vals, ok)

    if field_name == "query-string":
        pairs = cond.get("QueryStringConfig", {}).get("Values", [])
        labels = [query_pair_label(p) for p in pairs]
        decoded = "&".join(f"{k}={v}" for k, v in req.query)
        raw = req.raw_query or decoded
        # 実際の ALB で確かめた動き（2026-11-12）: Key のある条件は、デコードしたキーと値の組の
        # どれかと照合する。Key のない条件は、どれかの値ではなく、デコード前のクエリ文字列の全体と
        # 照合する（`solo` は `?solo` にだけ、`*wild*` は `wild` を含むクエリに一致する）。
        # query-string 条件では、`*` や `?` そのものを `\*`・`\?` と書ける
        ok = any(
            wildcard_match(p.get("Value", "*"), raw, ignore_case=True, escape=True)
            if p.get("Key") is None
            else any(
                wildcard_match(p["Key"], k, ignore_case=True, escape=True)
                and wildcard_match(p.get("Value", "*"), v, ignore_case=True, escape=True)
                for k, v in req.query
            )
            for p in pairs
            if raw
        )
        keyless = any(p.get("Key") is None for p in pairs)
        actual = (raw if keyless else decoded) or t("sim.no_query")
        return _result("Query", actual, labels, ok)

    if field_name == "source-ip":
        vals = _values(cond, "SourceIpConfig")
        if req.source_ip is None:
            return False, t("sim.no_source_ip")
        ip = ipaddress.ip_address(req.source_ip)
        ok = False
        for cidr in vals:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                continue
            if ip.version == net.version and ip in net:
                ok = True
                break
        return _result("SourceIP", req.source_ip, vals, ok)

    return False, t("sim.unsupported_condition", field=field_name)


def evaluate_rule(rule: RuleView, req: SimulatedRequest) -> RuleEvaluation:
    """
    1つのルールの全条件（AND）を評価する。default ルールは常に一致する。

    :param rule: 評価対象の `RuleView`
    :param req: 評価対象の `SimulatedRequest`
    :return: ルールの評価結果 `RuleEvaluation`
    """
    if rule.is_default:
        return RuleEvaluation(rule=rule, matched=True, reasons=[t("sim.default_applied")])
    results = [evaluate_condition(cond, req) for cond in rule.condition_specs]
    matched = bool(results) and all(ok for ok, _ in results)
    # 一致した場合は全条件の一致内容、不一致の場合は不一致になった条件だけを理由として残す
    reasons = [msg for ok, msg in results if matched or not ok]
    return RuleEvaluation(rule=rule, matched=matched, reasons=reasons)


def evaluate_listener(
    listener: ListenerView, req: SimulatedRequest
) -> tuple[list[RuleEvaluation], RuleView | None]:
    """
    Evaluate the rules of one listener in priority order, stopping at the first match.

    1つのリスナーのルールを優先度順に評価し、最初に一致したところで止める。

    :param listener: the listener / 対象のリスナー
    :param req: the request / リクエスト
    :return: the evaluations made, and the matched rule (None when no rule matches)
        / 行った評価と、一致したルール（どれにも一致しなければ None）
    """
    evaluations: list[RuleEvaluation] = []
    # 優先度の数値が小さい順に評価し、最初に一致したルールで評価を確定する（default は最後）
    for rule in sorted(listener.rules, key=lambda r: r.priority_order):
        evaluation = evaluate_rule(rule, req)
        evaluations.append(evaluation)
        if evaluation.matched:
            return evaluations, rule
    return evaluations, None


def simulate_hop(
    lb: LoadBalancerView,
    req: SimulatedRequest,
    port: int,
    _visited: frozenset[str] = frozenset(),
    _depth: int = 0,
    _load: Callable[[LoadBalancerView, int, frozenset[str]], LoadBalancerView] | None = None,
) -> RouteHop:
    """
    Evaluate the rules of the listener on `port` of one load balancer in priority order,
    stopping at the first match, and continue into the downstream load balancers of the
    matched rule's target groups (at most `MAX_ROUTE_DEPTH` hops; a load balancer is not
    visited twice).

    1台の ELB で、`port` のリスナーのルールを優先度順に評価し、最初に一致したところで止める。
    一致したルールの転送先 TG に下流の ELB があれば、そこへも続ける（最大 `MAX_ROUTE_DEPTH` 段。
    同じ ELB は2回たどらない）。

    :param lb: the load balancer / 対象の ELB
    :param req: the request / リクエスト
    :param port: the listener port / リスナーのポート
    :param _visited: ARNs of the load balancers already visited (internal)
        / たどった ELB の ARN（内部用）
    :param _depth: current hop count (internal) / 現在の段数（内部用）
    :param _load: fills in a load balancer just before it is evaluated, given the port and the
        ARNs of the load balancers on the way including itself (internal;
        `ELBClient.simulate_request` uses it to fetch only what the request reaches)
        / 評価の直前に ELB の中身を入れる関数。ポートと、そこまでにたどった ELB（自分を含む）の
        ARN を渡す（内部用。`ELBClient.simulate_request` が、リクエストの届く範囲だけを取得する
        のに使う）
    :return: `RouteHop` of this load balancer and its downstream hops
        / この ELB と下流の `RouteHop`
    """
    visited = _visited | {lb.arn}
    if _load is not None:
        lb = _load(lb, port, visited)
    hop = RouteHop(load_balancer=lb, port=port)
    listener = next((lst for lst in lb.listeners if lst.port == port), None)
    if listener is None:
        ports = ", ".join(f"{lst.protocol}:{lst.port}" for lst in lb.listeners) or t(
            "sim.no_listeners"
        )
        hop.note = t("sim.no_listener_on_port", port=port, ports=ports)
        hop.note_code = "no_listener"
        return hop

    hop.listener = listener
    hop.evaluations, hop.matched_rule = evaluate_listener(listener, req)
    if hop.matched_rule is None:
        hop.note = t("sim.no_matching_rule")
        hop.note_code = "no_matching_rule"
        return hop

    # 転送先TGに下流ELB（NLB -> ALB や ALB -> NLB）があれば、ターゲットポートで次段を評価
    # 複数 AZ の ELB はノードの IP が AZ の数だけ登録されるので、同じ（ELB, ポート）は 1 回だけ評価
    followed: set[tuple[str, int]] = set()
    for ref in hop.matched_rule.target_group_refs:
        # 重み 0 の TG にはリクエストが届かない
        if ref.target_group is None or ref.weight == 0:
            continue
        for target in ref.target_group.targets:
            downstream = target.downstream_lb
            if downstream is None or downstream.arn in visited:
                continue
            if _depth + 1 >= MAX_ROUTE_DEPTH:
                hop.note = t("sim.too_deep", depth=MAX_ROUTE_DEPTH)
                hop.note_code = "too_many_hops"
                continue
            next_port = target.port or port
            if (downstream.arn, next_port) in followed:
                continue
            followed.add((downstream.arn, next_port))
            hop.downstream.append(
                simulate_hop(downstream, req, next_port, visited, _depth + 1, _load)
            )
    return hop
