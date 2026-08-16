# Reading Pack Bot

[English](README.md)

Reading Pack Botは、[Reading Pack](https://github.com/ktakahashi74/reading-pack)を
対話形式で公開するためのサーバーソフトウェアです。Reading Packの仕様、作成方法、公開前の
検査については、リンク先のプロジェクトを参照してください。

中核機能は特定の対話サービスに依存しません。現在のリリースにはSlack用
アダプターが含まれます。モデルとの接続にはAnthropic APIまたはOpenAI互換APIを利用できます。
Web取得を有効にすると、接続先が対応している場合に、提供者側の検索・取得機能を
利用できます。

現在はアルファ版です。1.0までは設定形式とReading Packの互換条件が変わる可能性があります。

## ローカルで試す

Python 3.11以上が必要です。付属のテスト用モデル接続と合成Reading Packを使うため、モデルAPIや
Slackには接続しません。

```sh
git clone https://github.com/ktakahashi74/reading-pack-bot.git
cd reading-pack-bot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp config.example.toml config.local.toml
reading-pack-bot verify --config config.local.toml
reading-pack-bot doctor --config config.local.toml
reading-pack-bot ask --config config.local.toml
```

`ask`は標準入力から質問を1つ読みます。質問を入力してCtrl-Dを押してください。

## Slackへ配備する

実際のReading PackをSlackで公開する手順は、[Slackへの配備手順](docs/deploy-slack.md)にまとめて
います。Reading Packの選択、Slackアプリの作成、モデルAPIの設定、コンテナの導入、事前検査、
更新と切り戻しまでを扱います。

## コマンド

| コマンド | 用途 |
| --- | --- |
| `verify` | 設定とReading Packを検査し、SHA-256を表示します。 |
| `doctor` | 依存関係、秘密情報、権限、保存先、接続経路、緊急停止を調べます。 |
| `ask` | 質問を1回だけ実行します。実APIには`--allow-live`が必要です。 |
| `run` | 指定したアダプターを起動します。`--once`を付けると事前検査だけを行います。 |
| `purge` | 保存期限を過ぎた会話、イベント、レート制限の状態を削除します。 |

引数は`reading-pack-bot <command> --help`で確認できます。

## データとセキュリティ

モデルAPIには、Reading Pack全体、現在の質問、保持中の会話を送ります。非公開資料を扱う前に
[セキュリティ方針](SECURITY.md)を確認してください。設計上の境界は
[Architecture and trust boundaries](docs/architecture.md)に記載しています。

## 開発

開発環境と公開データの規則は[CONTRIBUTING.md](CONTRIBUTING.md)を参照してください。

```sh
sh scripts/test-suite.sh
```

## ライセンス

コードはMIT、文書と設定例はCC BY 4.0です。詳しくは[ライセンス一覧](LICENSES/README.md)を
参照してください。これらのライセンスは、配備する書籍やReading Packには適用されません。
