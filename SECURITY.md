# Security Policy

English | [日本語](#セキュリティポリシー)

## Supported versions

Security fixes are made for the latest release only.

## Reporting a vulnerability

Please do not report security vulnerabilities in public issues, discussions or pull requests.

Report them privately through GitHub instead: open the repository's **Security** tab and choose **Report a vulnerability** ([direct link](https://github.com/gyuro-dev/elbx/security/advisories/new)). Include the version, the steps to reproduce, and the impact you expect.

This is a personal project maintained on a best-effort basis. We will acknowledge your report as soon as we can, keep you updated on the fix, and credit you in the advisory if you wish.

## What to keep in mind when using elbx

elbx is read-only: it calls only `Describe*` / `List*` / `Get*` APIs and never changes anything in your AWS account. It uses the AWS credentials of your environment (the same ones as the AWS CLI), so run it with credentials that have only the read permissions it needs (see the IAM policy in the README).

Its output contains details of your infrastructure (IP addresses, security group rules, DNS names, account IDs in ARNs). Treat saved output and `--json` files as you would any other internal document.

---

## セキュリティポリシー

[English](#security-policy) | 日本語

### 対象の版

セキュリティの修正は、最新の版にだけ行います。

### 脆弱性の報告

脆弱性は、公開の issue・discussion・pull request には書かないでください。

GitHub の非公開の報告を使ってください。リポジトリの **Security** タブから **Report a vulnerability** を選びます（[直接のリンク](https://github.com/gyuro-dev/elbx/security/advisories/new)）。\
版、再現の手順、想定される影響を書いてください。

個人のプロジェクトのため、できる範囲で対応します。\
報告を受け取ったらできるだけ早く返事をし、修正の状況をお知らせします。\
希望があれば、公開する advisory に報告者としてお名前を載せます。

### 使うときに気をつけること

elbx は参照専用です。\
`Describe*` / `List*` / `Get*` の API だけを呼び、AWS アカウントの中を変更することはありません。\
認証情報は実行した環境のもの（AWS CLI と同じ）を使うので、必要な読み取りの権限だけを持つ認証情報で実行してください（README の IAM ポリシーを参照）。

出力にはインフラの詳細（IP アドレス、SG のルール、DNS 名、ARN の中のアカウント ID）が含まれます。\
保存した出力や `--json` のファイルは、ほかの社内の文書と同じように扱ってください。
