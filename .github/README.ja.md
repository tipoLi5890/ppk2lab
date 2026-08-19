# ppk2lab

[English](../README.md) · [繁體中文](README.zh-Hant.md) · [简体中文](README.zh-Hans.md) · **日本語**

> 本書は [README.md](../README.md) の翻訳です。内容に差異がある場合は英語版を正とします。

**デバイスの電力がどこで使われているかを正確に把握できます。** `ppk2lab` は、Nordic Power Profiler Kit II をスクリプトで操作できる測定ラボに変えます。電流と 8 本のデジタル信号を単一のタイムライン上で記録し、低速 UART と SPI をデコードし、個々のプロトコルイベントにエネルギーを対応付けます。すべての機能は Python・コマンドライン・AI エージェントから利用できるため、電力回帰を、テストの失敗と同じように CI ビルドの失敗として扱えます。

[![CI](https://github.com/tipoLi5890/ppk2lab/actions/workflows/ci.yml/badge.svg)](https://github.com/tipoLi5890/ppk2lab/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-experimental-orange)

> [!IMPORTANT]
> 本プロジェクトは実験段階であり、Nordic Semiconductor ASA との提携・承認関係は一切ありません。

## ハイライト

`ppk2lab` は、人間とエージェントの双方が次のことを行えるようにします：

- 1 台以上の PPK2 デバイスを検出・設定する；
- 校正済み電流とすべての D0-D7 状態を単一の同期タイムライン上でキャプチャする；
- 時間を黙って圧縮せず、サンプル欠落を検出する；
- 低速 UART / SPI トラフィックをデコードする（9,600 ボー／10 kHz で検証済み）；
- デコードした各イベントの電荷・エネルギー・ピーク電流・レイテンシを測定する；
- 電流・デジタル状態・UART 内容・SPI トランザクションからキャプチャをトリガする；
- ローカル自動化と CI で再現可能な電力アサーションを実行する；
- `--simulate` でハードウェアなしにすべてを試せる。

## インストール

コアパッケージには Python 3.11 以上が必要です。ランタイム依存関係の最終セットは、最初の PyPI リリース前に凍結されます。

```bash
pipx install ppk2lab        # CLI 利用にはこちらを推奨
# または仮想環境内で：
pip install ppk2lab

ppk2lab --version
ppk2lab doctor --json
```

本パッケージはまだ PyPI に公開されていません。`0.1.0` リリースまでは、以下のようにソースチェックアウトからインストールしてください。現在のバージョンは `0.1.0.dev0` です。

開発用チェックアウトから実行する場合：

```bash
git clone https://github.com/tipoLi5890/ppk2lab
cd ppk2lab
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ppk2lab doctor --json
```

### Claude Code プラグイン

`ppk2lab` CLI をインストールした後、本リポジトリを Claude Code の marketplace に追加して skills をインストールします：

```text
/plugin marketplace add tipoLi5890/ppk2lab
/plugin install ppk2lab@ppk2lab
```

プラグイン開発中は `claude --plugin-dir ./` でチェックアウトを直接読み込めます。

### Codex プラグインとスタンドアロン skills

`/plugins` で Codex プラグインブラウザを開き、本リポジトリの marketplace を追加して `ppk2lab` をインストールします。正確な非対話コマンドは、公開前に最新の Codex リリースで再検証します。

リポジトリ単位のスタンドアロン skills は `.agents/skills/` に配置します。ユーザー単位でインストールする場合：

```bash
mkdir -p ~/.agents/skills
cp -R skills/* ~/.agents/skills/
```

Claude Code は同じ skills を `.claude/skills/` から、または同梱プラグイン経由で利用できます。セットアップとトラブルシューティングの詳細は [INSTALL.md](../INSTALL.md) を参照してください。

## クイックスタート

どのコマンドも `--simulate` を付けることでハードウェアなしに実行でき、内蔵のシミュレート PPK2 を使ってツールチェーン全体を試せます。シミュレート実行は測定ではありません。

ハードウェア状態を変更せずに環境と接続デバイスを確認します：

```bash
ppk2lab doctor --json
ppk2lab discover --json
ppk2lab info --device <serial> --json
```

DUT 電源を自動的に有効化することなく、電流と D0-D7 をキャプチャします：

```bash
ppk2lab capture --device <serial> --duration 5s --digital D0-D7 --output capture.ppk2a
```

サポート対象の低速 UART 信号をデコードし、フレームごとにエネルギーを測定します：

```bash
ppk2lab decode capture.ppk2a --uart D0 --baud 9600 --output uart.jsonl
ppk2lab measure capture.ppk2a --annotations uart.jsonl --group-by frame --json
```

再現可能な電力アサーションを実行します：

```bash
ppk2lab assert capture.ppk2a \
  --rule 'after uart("TX_DONE"), within 20ms, avg_current < 10uA' \
  --format json
```

これらのコマンドは現在すでに実装されています。すべての JSON コントラクトは `schema_version` 1 を持ち、`0.1.0` リリースまでは変更される可能性があります。

## できること

| コマンド | 目的 | ハードウェア状態 |
|---|---|---|
| `discover`・`info` | デバイスを検出し、ファームウェア・metadata・校正・現在状態を確認 | 読み取り専用 |
| `capabilities`・`schema` | 機械可読なコマンド・制限・デコーダ・JSON schema を返す | 読み取り専用 |
| `doctor` | USB 権限・ポート占有・ストリームレート・metadata・サンプル欠落を診断 | 既定で読み取り専用 |
| `configure` | モード・出力電圧・DUT 電源を選択し、前後の状態を報告 | 状態変更 |
| `capture` | 生電流・レンジ・シーケンスカウンタ・D0-D7 をトリガ付きで記録 | 測定；電源変更はオプトイン |
| `decode` | UART・SPI・エッジ・パルス・トランザクションの注釈を生成 | 既定でオフライン |
| `measure` | 時間範囲または注釈に対して電流・電荷・エネルギー・ピーク・レイテンシを計算 | オフライン |
| `assert` | 再現可能な電力・プロトコル回帰ルールを適用 | 既定でオフライン |
| `export` | 生の証拠を置き換えずに CSV・VCD・JSONL などの派生ビューを生成 | オフライン |

## 適用範囲と物理的制約

PPK2 のデジタル入力は 100 kS/s でサンプリングされるため、プロトコルデコードは低速信号を対象とします。初期の検証目標：

- UART は最大 9,600 ボー；
- SPI クロックは最大 10 kHz；
- 8 本すべてのデジタル入力 D0-D7；
- それ以上のレートは、conditional または experimental と明示された場合のみ。

本プロジェクトは MHz 級ロジックアナライザの代替を意図していません。サンプル欠落区間をまたぐフレームは、常に不完全または無効として報告され、確信あるデコード結果として扱われることはありません。

| プロトコル | 初期サポートレベル |
|---|---|
| UART 1,200-9,600 ボー | 検証対象 |
| UART 19,200 ボー | 条件付き |
| UART 38,400 ボー | 実験的 |
| UART 57,600/115,200 ボー | デコード可能とは主張しない |
| SPI クロック 10 kHz 以下 | 検証対象 |
| SPI 10-20 kHz | 条件付き |
| SPI 20 kHz 超 | 実験的または非対応 |

## ハードウェア安全

- `configure` は、ユーザーが明示的に適用しない限り dry run です。
- ユーザーが承認したプロファイルまたは明示的なオプションが要求しない限り、capture は DUT 電源を有効化しません。
- 出力電圧は常に `voltage_mv` として表現され、デバイス能力に対して検証されます。DUT 名から推測することはありません。
- モード変更・DUT 電源・出力電圧・リセット操作は、変更前後の状態を報告します。
- 完了時または失敗時、ライブラリはセッション開始時の電源状態の復元を試み、復元の成否を記録します。
- いかなるプラグインフックや skill も、DUT 電源の自動有効化・電圧変更・デバイスリセットを行ってはなりません。

## AI エージェントでの利用

すべての操作は、型付き Python API と、安定した JSON 出力を持つ CLI コマンドの両方で利用できます。読み取り専用の検査と、ハードウェア状態を変更する操作は明確に分離されています。

コマンド一覧：

```text
ppk2lab discover
ppk2lab info
ppk2lab capabilities
ppk2lab schema
ppk2lab configure
ppk2lab capture
ppk2lab decode
ppk2lab measure
ppk2lab assert
ppk2lab export
ppk2lab doctor
```

DUT 電源の有効化・出力電圧の変更・ハードウェアリセットを行うコマンドは、state-changing として明示されます。電圧引数は `voltage_mv` のように API 名に単位を含みます。

オプションのエージェントアダプタは別パッケージとして提供される可能性があります。コアドライバとファイル形式が特定のエージェントフレームワークに依存することはありません。

すべての JSON 結果には、schema バージョン・安定エラーコード・修復ヒント（remediation）・完了状態・サンプル欠落情報が含まれます。`ppk2lab capabilities --json` が実際のコマンドとデコーダの対応範囲を記述するため、エージェントが非対応の挙動を推測する必要はありません。

### 同梱エージェント skills

プラグインには、1 つの巨大なプロンプトではなく、段階的開示（progressive disclosure）に基づく 2 つの skills が含まれます。各 `SKILL.md` は薄いルーターであり、安全コントラクトを本文に保持しつつ、トピックごとの参照ノートはエージェントが必要なときにだけ読み込みます。ライブな知識——コマンド・オプション・デコーダのレート階層・schema・エラーコード——をプロンプトに複製することはなく、エージェントは自己記述的な CLI（`capabilities --json`・`schema`・`doctor --json`）へ問い合わせます。

| Skill | 対象範囲 | オンデマンド references |
|---|---|---|
| `ppk2lab-operate` | PPK2 での測定：セットアップとトラブルシュート、安全な設定とキャプチャ、長時間記録、トリガ、UART/SPI/ロジックデコード、エネルギー解析、CI 電力回帰、ハードウェア診断 | `setup`・`capture`・`triggers`・`decode`・`analysis`・`regression`・`diagnostics` |
| `ppk2lab-maintain` | 本リポジトリの保守：観察のみのプロトコル調査、リリースゲート監査 | `protocol-research`・`release-gating` |

Skills はワークフローの指針を提供し、決定論的な実行・検証レイヤーはあくまで `ppk2lab` CLI です。インストールが DUT 電源を有効化したり、自動的にキャプチャを開始したりすることはありません。

## アーキテクチャ

```text
USB CDC transport
  -> device discovery and capability query
  -> streaming frame assembler
  -> raw sample parser and sequence-gap detector
  -> calibration and synchronized D0-D7 transitions
  -> trigger engine
  -> UART/SPI decoder plugins
  -> event-level charge and energy analysis
  -> capture files, CSV, VCD, JSONL, Python, and CLI
```

正規のキャプチャ形式は、生サンプル・校正 metadata・デバイスとファームウェアの識別情報・キャプチャ設定・タイムスタンプ・データ欠落マーカーを保存します。加工済みのエクスポートが元の証拠を置き換えることはありません。

## 公式リファレンス

PPK2 の機能は、Nordic Semiconductor の公式 PPK2 ドキュメントと公式 [Power Profiler アプリのリポジトリ](https://github.com/NordicSemiconductor/pc-nrfconnect-ppk) に基づいています。これらの公式資料が、本プロジェクトの製品挙動および互換性のリファレンスを定義します。

本プロジェクトは Nordic のファームウェアバイナリを配布しません。別途再配布許諾が確立されない限り、ファームウェアのインストールと更新は Nordic の公式ツールで行ってください。

Nordic Semiconductor、Power Profiler Kit、PPK2 は Nordic Semiconductor ASA の商標または製品名である可能性があります。ここでの使用は互換ハードウェアの識別のみを目的としています。

本プロジェクトが使用する Nordic 公式リファレンスと記録済みの PPK2 挙動は、[docs/sources.md](../docs/sources.md) と [docs/protocol-spec.md](../docs/protocol-spec.md) を参照してください。

## ロードマップ

初回リリースまでに残っている作業は次のとおりです：

- Windows・macOS・Linux にまたがり、ファームウェアバージョン・複数デバイス・取り外しを網羅するハードウェア互換性マトリクス；
- 既知負荷を用いた、公式 Power Profiler アプリとの校正クロスチェック；
- 定義されたエラー率しきい値に対する、実信号での UART / SPI デコーダ検証；
- ストリーミング・メモリ使用量・リカバリのための長時間ソークテスト；
- リリースゲート：凍結された公開 API ベースライン、依存ライセンス監査、リリースリハーサル、そして PyPI への公開そのもの。

バージョン付きの完了基準とハードウェア互換性マトリクスは [ROADMAP.md](../ROADMAP.md) を参照してください。

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [INSTALL.md](../INSTALL.md) | Python、USB 権限、Claude Code、Codex、CI、トラブルシューティング |
| [ROADMAP.md](../ROADMAP.md) | マイルストーン、サポートレベル、リリース完了基準 |
| [docs/SPEC.md](../docs/SPEC.md) | 公開データモデル、状態、API、schema コントラクト |
| [docs/api-baseline.md](../docs/api-baseline.md) | 凍結された公開 API サーフェスと安定性ポリシー |
| [docs/cli-reference.md](../docs/cli-reference.md) | CLI コマンド、フラグ、JSON 出力、終了コード |
| [docs/protocol-spec.md](../docs/protocol-spec.md) | 記録済みの PPK2 プロトコル、コマンド、フィールド、デバイス挙動 |
| [docs/capture-format.md](../docs/capture-format.md) | 欠落を認識する正規キャプチャ artifact |
| [docs/calibration.md](../docs/calibration.md) | 生サンプルから電流への変換式と単位 |
| [docs/logic-port.md](../docs/logic-port.md) | D0-D7 の配線、Logic VCC、電圧レベル、実用帯域幅 |
| [docs/decoders.md](../docs/decoders.md) | UART/SPI コントラクト、信頼度、欠落、物理的制約 |
| [docs/triggers.md](../docs/triggers.md) | トリガの種類とプリ/ポストトリガのキャプチャウィンドウ |
| [docs/energy-analysis.md](../docs/energy-analysis.md) | 電荷・エネルギー・ピーク電流・レイテンシの定義 |
| [docs/agent-interface.md](../docs/agent-interface.md) | JSON、ツール、状態変更操作、コンテキスト予算 |
| [docs/sources.md](../docs/sources.md) | Nordic 公式ドキュメントとリポジトリのリファレンス |
| [SECURITY.md](../SECURITY.md) | ハードウェア、USB、ファイル、信頼できない入力のセキュリティモデル |

## 連絡先

質問・バグ・互換性レポート・機能リクエストは [GitHub Issues](https://github.com/tipoLi5890/ppk2lab/issues) からお願いします。

## ライセンス

本プロジェクトは [MIT License](../LICENSE) の下で公開されています。対象は本リポジトリのオリジナル素材のみであり——PPK2 ハードウェア、Nordic ファームウェア、Nordic ドキュメント、商標は、それぞれの権利者に帰属します。

## 開発

本プロジェクトは Claude Code と OpenAI Codex の支援を受けて開発されています。
