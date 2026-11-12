"""
実際の AWS 上に tests/data/infra.yaml の構成を CloudFormation で作り、
ゴールデンテストを実行する仕組み。

- 1回作った環境で全ケースを実行し、最後に1回だけ削除する（ケースごとに作り直さない）
- 作成前に、接続先アカウント・SSO の残り時間・名前の長さ・外部公開の方針を確認し、
  問題があれば何も作らずに止める
"""

from __future__ import annotations
