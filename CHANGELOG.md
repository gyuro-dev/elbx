# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (while the major version is 0, an
incompatible change raises the minor version).

## [Unreleased]

## [0.1.0] - 2026-11-13

### Added

- `elbx tree`: one tree from the load balancer down to the targets, with the real target behind
  each entry: EC2 (by instance or IP), ECS tasks (Fargate / bridge / host), Lambda, ENIs, and
  downstream load balancers (NLB → ALB with `alb` target groups, and ALB → NLB through the NLB
  node IPs registered in an `ip` target group; a load balancer that appears again, like the
  nodes of a multi-AZ NLB, is expanded once and marked `(expanded above)`). By default it is a
  short relationship map
  (listeners → rules → target groups with their health → one line per target with its name;
  targets that are not healthy come first, `--max-targets` folds long lists). `--full` adds
  Route 53 records, security group rules, subnets, certificates, attributes and the details of
  every target.
- `elbx route`: simulates which listener rule a request matches (host, path, method, headers,
  query string, source IP) with the reason for every condition, and follows downstream load
  balancers (the same load balancer and port once, even with one node IP per AZ; a target group
  of weight 0 is marked as receiving no requests and is not followed). `-H` given
  twice with the same name joins the values with `, `, as HTTP does.
  It fetches only what the request reaches: the rules of the listener on the request's port
  and the target groups of the matched rule (not the other target groups of the load
  balancer, certificates or attributes), and, without `--lb`, only the Route 53 zones named
  after the host name or a parent domain of it. `RouteHop.load_balancer` holds what was
  fetched, not the whole tree. A condition with `RegexValues` is shown and treated as not
  matched, with the reason, because it cannot be evaluated the way the load balancer does.
- `elbx targets` (narrowed to one load balancer with `elbx targets LB`, or to one target group
  with `--tg TG`, or by name with `-s TEXT`; `--unhealthy` for the targets that need attention and their
  health-check settings; a Lambda with health checks disabled and the targets of a target group
  no load balancer uses are not counted, as real AWS always reports them as `unavailable` /
  `unused`), `elbx find` (reverse lookup from an EC2 name / IP, ECS service, Lambda function or
  load balancer) and `elbx ls` (its table adds how many targets need attention, in red, and
  does not fetch what it does not show: what the targets are, certificates, attributes,
  subnets and security groups).
- `--help` groups the options by role (the command's own, AWS connection, output and help) and
  ends with examples (the example lines are not wrapped, so the `# ...` explanation stays on its
  line in a narrow terminal).
- Without a subcommand, elbx runs `tree`, and the options of `tree` can be written as they
  are (`elbx --full`, `elbx --search prod`, also abbreviated or joined as argparse allows:
  `elbx --sea prod`, `elbx -vp my-profile`). A misspelled subcommand is still an error, and
  `elbx my-alb` (a load balancer name without `tree`) points to `elbx tree my-alb`.
- `--json` output for every command, and the same data as typed Python objects through
  `elbx.ELBClient`.
- Fetches that do not depend on each other run side by side (Route 53 records next to the
  listeners and targets, Lambda / ECS / EC2 / downstream load balancers next to each other),
  with `-j` / `max_workers` as the limit on AWS API calls at once for the whole command, and
  one call does not read the same ECS cluster, load balancer, subnet or security group
  twice.
- Errors that say what to do next: a `Hint:` line for missing or expired credentials, an
  unreachable endpoint (a mistyped region fails at once instead of being retried for half a
  minute), a target group name given where a load balancer is expected (and the reverse),
  and a name given to `route` instead of a URL. The messages of argparse itself follow the
  chosen language too.
- IPv4, dual-stack and IPv6-only load balancers, target groups, subnets and targets.
- `ip` targets are matched to ENIs, EC2 instances, ECS tasks and subnets in the VPC of their
  target group, so VPCs that share a CIDR do not mix up the same private IP.
- Golden tests on moto (no AWS account needed), run by
  [pytest-casebook](https://github.com/gyuro-dev/pytest-casebook) from one YAML file that also
  becomes an HTML test specification sheet with the expected outputs; a `playground.py` that
  shows the same output; and an optional test run against a real AWS account built with
  CloudFormation.
- English and Japanese output: the help, the messages, the labels and the reasons of `route`
  follow `--lang en|ja`, then the `ELBX_LANG` environment variable, then the locale
  (`LC_ALL`, `LC_MESSAGES`, `LANG`), then the Windows display language; anything else is
  English. The library has `elbx.set_language()` / `elbx.get_language()`. README.md is in
  English and README.ja.md in Japanese.
- Errors: a failed AWS API call (no credentials or region, access denied, throttling, ...)
  raises `elbx.AWSAPIError` (a subclass of `ELBError`) that says what elbx was doing, with the
  botocore exception as `__cause__`; the CLI prints it as one `Error: …` line. Only a name or
  ARN that does not exist or is malformed raises `ResourceNotFoundError`, so access denied or
  expired credentials are not reported as "not found". `elbx ... | head` is not an error, and
  Ctrl-C ends with exit code 130 and no traceback. When the next step is clear (no region or
  credentials, expired SSO or credentials), the CLI adds one `Hint: …` line. `elbx route` does
  not say that no load balancer points at a host when it could not read Route 53 (the list of
  zones, or the records of a zone): it reports the failure and points to `--lb`. An empty
  `AWS_REGION` is reported as a missing region, with the same hint. `ELBClient(session=...,
  profile_name=...)` is a `ValueError`, and `region_name` applies to a given `session` instead
  of being ignored.
- Logs: details that cannot be fetched are left empty, and one note on stderr says so, also
  when the command then fails. `-v` shows what could not be fetched and why, and `-vv` also
  every AWS API call and how long it took. Logs go to stderr, never to stdout. In Python they are on the `elbx` logger of the
  standard `logging` module, silent by default. Request parameters and headers are not logged, and
  the debug log of botocore stays off. IAM server certificates are not looked up in ACM.
- Input checks: `-j` / `--workers` must be 1 or more and `--max-targets` 0 or more, and
  `-H` needs a header name. An empty argument is an error instead of being taken as omitted
  (`elbx tree ""`, `elbx targets ""`, `elbx find ""`, `--lb ""`, `--source-ip ""`), and an
  empty `names=[]` returns nothing instead of everything. `-s` / `--search` together with
  one load balancer (`elbx tree LB`) or one target group (`elbx targets --tg TG`) is an error
  instead of being ignored.
- Control characters in names and tags from AWS (ESC, CR, BEL, newlines, color codes, ...) are
  shown as `\x1b` and the like in the text output and as `\uXXXX` in the JSON output, instead of
  being interpreted by the terminal or making a line that looks real.
- Wildcards are matched without regular expressions, so a rule with many `*` cannot make `route`
  take a long time. In a `query-string` condition, `\*` and `\?` are the characters themselves,
  as on the load balancer.
- `elbx route` finds the entry load balancer through a Route 53 wildcard record only when the
  zone has no record with the host name itself (with `api.example.com` pointing at CloudFront,
  a request does not reach the load balancer of `*.example.com`).
- `elbx find` matches an ARN by its name part (the function name, the load balancer name and
  ID), so a region name, an account ID or `app` does not match every target; a query that
  starts with `arn:` is matched against the whole ARN.
- An instance target is shown as an ECS task only when its port is a host port of the task,
  or when the host ports of the only task on that instance are unknown; another process on the
  same instance is not taken for the task.
- AWS API calls that are throttled are tried up to 10 times in the `standard` retry mode
  (the calls run in parallel, and one that keeps failing fails the whole command). The
  `AWS_RETRY_MODE` and `AWS_MAX_ATTEMPTS` environment variables are respected.
- A load balancer that points back at itself (directly or through others) is not expanded again,
  so `tree --full` and `--json` finish.
- Only the permissions a read needs: Lambda details come from
  `lambda:GetFunctionConfiguration`, not `lambda:GetFunction` (which also returns a URL to download
  the code).
- Route 53 names are shown as written (`*.example.com`, not `\052.example.com`).
- `ELBClient`: `include_certificates`, `include_attributes`, `resolve_targets` and
  `expand_downstream` choose what to fetch (the tables of `elbx targets` and `elbx find` do
  not read the target groups behind a downstream load balancer a second time), and
  `TargetGroupView.to_dict()` has `by_state` and `attention` in `summary`.
  `TargetLookupResult.load_balancer_name` is empty when no load balancer is known, in every
  language.
- ECS clusters are read in parallel, and the tasks of a cluster only when an IP target or a
  target instance can belong to them. An IP target whose network interface was found is not
  looked up as an EC2 instance by its IP again.
- Tables count full-width characters (Japanese Name tags, `(ELB未アタッチ)`) as two columns,
  so their rows stay aligned.
- `--color` colors the output when it is not a terminal (a pipe to `less -R`, a CI log); the
  `FORCE_COLOR` environment variable has the same effect and wins over `NO_COLOR`.
- `elbx find` with part of an IP address (`10.0.1`) finds nothing, and a hint says that an IP
  address is matched as a whole.
- Python: `TargetView.downstream_lb` holds the load balancer behind a target, an ALB or an NLB (also the `downstream_lb` key of `to_dict()` and `--json`); `RouteHop.note_code`
  (`no_listener` / `no_matching_rule` / `too_many_hops`, also in `--json`) tells why an
  evaluation stopped without parsing `note`; `TargetView.target_kind` is the `TargetKind`
  literal; `repr()` of a load balancer, a target group, a target or a hop is one short line. The
  README examples pass `mypy --strict` as written.

[Unreleased]: https://github.com/gyuro-dev/elbx/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/gyuro-dev/elbx/releases/tag/v0.1.0
