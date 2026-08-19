# ppk2lab

[English](https://github.com/tipoLi5890/ppk2lab/blob/main/README.md) · [繁體中文](https://github.com/tipoLi5890/ppk2lab/blob/main/.github/README.zh-Hant.md) · [简体中文](https://github.com/tipoLi5890/ppk2lab/blob/main/.github/README.zh-Hans.md) · **日本語**

> 本書は [README.md](https://github.com/tipoLi5890/ppk2lab/blob/main/README.md) の翻訳です。内容に差異がある場合は英語版を正とします。

**デバイスの電力がどこで使われているかを正確に把握できます。** `ppk2lab` は、Nordic Power Profiler Kit II をスクリプトで操作できる測定ラボに変えます。電流と 8 本のデジタル信号を単一のタイムライン上で記録し、低速 UART と SPI をデコードし、個々のプロトコルイベントにエネルギーを対応付けます。すべての機能は Python・コマンドライン・AI エージェントから利用できるため、電力回帰を、テストの失敗と同じように CI ビルドの失敗として扱えます。

[![CI](https://github.com/tipoLi5890/ppk2lab/actions/workflows/ci.yml/badge.svg)](https://github.com/tipoLi5890/ppk2lab/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ppk2lab)](https://pypi.org/project/ppk2lab/)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-experimental-orange)

> [!IMPORTANT]
> 本プロジェクトは実験段階であり、Nordic Semiconductor ASA との提携・承認関係は一切ありません。

## ハイライト

`ppk2lab` は、人間とエージェントの双方が次のことを行えるようにします：

- 1 台以上の PPK2 デバイスを検出・設定する；
- 校正済み電流とすべての D0-D7 状態を単一の同期タイムライン上でキャプチャする；
- 低速 UART / SPI トラフィックをデコードする（9,600 ボー／10 kHz で検証済み）；
- ウィンドウまたはデコード済みイベントの電荷・エネルギー・ピーク電流・レイテンシ・分布（p50/p90/p99）を、レンジごとの代表的な誤差範囲つきで測定する；
- 時間単位のキャプチャを扱う：サンプルを読み込まずに manifest だけを読む、単一ウィンドウだけを読む、間引き（デシメーション）サマリをエクスポートする；
- 電流・デジタル状態・UART 内容・SPI トランザクションからキャプチャをトリガする；
- ローカル自動化と CI で再現可能な電力アサーションを実行する；
- サンプルが答えを支えられないとき——サンプル欠落、ADC 飽和、データのないウィンドウ——確信ありげな数値を返す代わりに、そう明示する；
- `--simulate` でハードウェアなしにすべてを試せる。

## インストール

コアパッケージには Python 3.11 以上が必要です。ランタイム依存関係は `pyserial` のみです。

```bash
pipx install ppk2lab        # CLI 利用にはこちらを推奨
# または仮想環境内で：
pip install ppk2lab

ppk2lab --version
ppk2lab doctor --json
```

現在 PyPI にあるビルドは `0.1.0.dev0` だけで、バージョンを明示したピン留めが必要です：`pip install ppk2lab==0.1.0.dev0`。単なる `pip install ppk2lab` では何もインストールされません。プレリリースは既定で解決対象から外れ、安定版がまだ出ていないためです——最初の安定版は、下記のハードウェアゲートを通過したあとにリリースされる `0.2.0` です。本リポジトリはすでに `0.2.0.dev0` で、公開済みプレビューにはない変更を含みます。追随するにはソースチェックアウトからインストールしてください。

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

`skills/` にあるのは素の `SKILL.md` ファイルで、Codex から直接読み込めます。Codex プラグイン manifest はまだ公開していないため、下記のスタンドアロン skill の手順をご利用ください。

リポジトリ単位のスタンドアロン skills は `.agents/skills/` に配置します。ユーザー単位でインストールする場合：

```bash
mkdir -p ~/.agents/skills
cp -R skills/* ~/.agents/skills/
```

Claude Code は同じ skills を `.claude/skills/` から、または同梱プラグイン経由で利用できます。セットアップとトラブルシューティングの詳細は [INSTALL.md](https://github.com/tipoLi5890/ppk2lab/blob/main/INSTALL.md) を参照してください。

## クイックスタート

どのコマンドも `--simulate` を付けることでハードウェアなしに実行でき、内蔵のシミュレート PPK2 を使ってツールチェーン全体を試せます。シミュレート実行は測定ではありません。

ハードウェア状態を変更せずに環境と接続デバイスを確認します：

```bash
ppk2lab doctor --json
ppk2lab discover --json
ppk2lab info --device <serial> --json
```

電流と D0-D7 をキャプチャします。`capture` が DUT 電源を有効化することはありません：

```bash
ppk2lab capture --device <serial> --duration 5s --digital D0-D7 --output capture.ppk2a
```

サンプルを 1 つも読み込まずに、その artifact が実際に何を保持しているか——タイムライン、欠落、警告、校正情報——を確認します：

```bash
ppk2lab inspect capture.ppk2a --json
```

サポート対象の低速 UART 信号をデコードし、フレームごとにエネルギーを測定します：

```bash
ppk2lab decode capture.ppk2a --uart D0 --baud 9600 --output uart.jsonl
ppk2lab measure capture.ppk2a --annotations uart.jsonl --group-by frame --json
```

再現可能な電力アサーションを実行します：

```bash
ppk2lab assert capture.ppk2a \
  --rule 'p99_current < 15mA' \
  --rule 'after uart("TX_DONE"), within 20ms, avg_current < 10uA' \
  --format json
```

CI のしきい値には `max_current` よりパーセンタイルが適しています。キャプチャが長くなるほどレンジ切り替えの回数は積み上がるため、最大値はキャプチャ長とともに上振れしていきますが、`p99_current` はそうなりません。

これらのコマンドは現在すでに実装されています。すべての JSON コントラクトは `schema_version` 1 を持ち、`0.2.0` リリースまでは変更される可能性があります。

## できること

| コマンド | 目的 | ハードウェア状態 |
|---|---|---|
| `discover`・`info` | デバイスを検出し、ファームウェア・metadata・校正・現在状態を確認 | 読み取り専用 |
| `capabilities`・`schema` | 機械可読なコマンド・制限・デコーダ・警告／欠落理由／中断理由のカタログ・JSON schema を返す | 読み取り専用 |
| `doctor` | USB 権限・ポート占有・metadata・校正・ストリームレートを診断；チェックが失敗すると非ゼロで終了 | 既定で読み取り専用 |
| `configure` | モード・出力電圧・DUT 電源を選択し、前後の状態を報告 | 状態変更；`--apply` なしでは dry run |
| `capture` | 生電流・レンジ・シーケンスカウンタ・D0-D7 をトリガ付きで記録 | 測定；DUT 電源を有効化することはない |
| `inspect` | キャプチャの manifest——タイムライン・欠落・警告・校正——をサンプルを読み込まずに読む | オフライン |
| `decode` | UART・SPI・エッジ・パルス・トランザクションの注釈を生成 | オフライン |
| `measure` | 時間範囲または注釈に対して電流・電荷・エネルギー・ピーク・パーセンタイル・任意のデューティ比分割を計算 | オフライン |
| `assert` | 再現可能な電力・プロトコル回帰ルールを適用 | オフライン |
| `export` | 生の証拠を置き換えずに CSV・VCD・JSONL・ウィンドウ・間引きなどの派生ビューを生成 | オフライン |

## 適用範囲と物理的制約

PPK2 のデジタル入力は 100 kS/s でサンプリングされるため、プロトコルデコードは低速信号を対象とします。初期の検証目標：

- UART は最大 9,600 ボー；
- SPI クロックは最大 10 kHz；
- 8 本すべてのデジタル入力 D0-D7；
- それ以上のレートは、conditional または experimental と明示された場合のみ。

本プロジェクトは MHz 級ロジックアナライザの代替を意図していません。サンプル欠落区間をまたぐフレームは、常に不完全または無効として報告され、確信あるデコード結果として扱われることはありません。

> [!IMPORTANT]
> **本プロジェクトが出すどの数値も、基準器と突き合わせた実績がありません。** ここにある精度の数字はいずれも Nordic が公開しているレンジごとの「代表値」仕様を言い換えたものであり、テストの波形はすべて内蔵シミュレータ由来です。不確かさが常に `guaranteed: false` なのは、まさにそのためです。実機で検証することが、[ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md) に残っているリリースゲートの目的です。

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

- `configure` は `--apply` を付けない限り dry run です。
- `capture` は DUT 電源を有効化せず、有効化できるオプションも存在しません。DUT にメーター経由で給電するのは、常に独立した明示的な `configure --dut-power on --apply` です。
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
ppk2lab inspect
ppk2lab decode
ppk2lab measure
ppk2lab assert
ppk2lab export
ppk2lab doctor
```

DUT 電源の有効化・出力電圧の変更・ハードウェアリセットを行うコマンドは、state-changing として明示されます。電圧引数は `voltage_mv` のように API 名に単位を含みます。

オプションのエージェントアダプタは別パッケージとして提供される可能性があります。コアドライバとファイル形式が特定のエージェントフレームワークに依存することはありません。

すべての JSON 結果には、schema バージョン・安定エラーコード・修復ヒント（remediation）・完了状態・サンプル欠落情報が含まれます。`ppk2lab capabilities --json` は実際のコマンドとデコーダの対応範囲を記述し、警告・欠落理由・中断理由のコードカタログも公開するため、エージェントが非対応の挙動を推測する必要も、`W_*` コードの意味を文章から読み取る必要もありません。

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

本プロジェクトが使用する Nordic 公式リファレンスと記録済みの PPK2 挙動は、[docs/sources.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/sources.md) と [docs/protocol-spec.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/protocol-spec.md) を参照してください。

## ロードマップ

最初の安定版は `0.2.0` です。そこまでに残っているのは実装ではなく検証で、その多くは実機の PPK2 を必要とします：

- Windows・macOS・Linux を、ファームウェア 1.1.0・1.2.0・1.2.4 に対して検証する OS／ファームウェア互換性マトリクス。すべてのキャプチャが記録するファームウェアフィンガープリントをキーとします；
- 既知負荷を用いた、公式 Power Profiler アプリとの校正クロスチェック；
- 定義されたエラー率しきい値に対する、実信号での UART / SPI デコーダ検証；
- 複数デバイスセッション、活線抜去からの復旧、8-24 時間のソークテスト；
- Claude Code と Codex の skill インストールを現行リリースに対して検証すること；
- クリーンな環境からドキュメントとサンプルを再現すること；
- リリースリハーサル、タグ時点での依存ライセンス再スキャン、そして PyPI への公開そのもの。

ハードウェアを要するゲートに CI ワークフローはなく、用意する予定もありません — PPK2 と治具 MCU の接続が必要なためで、メンテナが実機で実行します。完了基準・互換性マトリクス・意図的に作らないものは [ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md) を参照してください。

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [INSTALL.md](https://github.com/tipoLi5890/ppk2lab/blob/main/INSTALL.md) | Python、USB 権限、Claude Code、Codex、CI、トラブルシューティング |
| [ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md) | マイルストーン、サポートレベル、リリース完了基準 |
| [docs/SPEC.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/SPEC.md) | 公開データモデル、状態、API、schema コントラクト |
| [docs/api-baseline.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/api-baseline.md) | 凍結された公開 API サーフェスと安定性ポリシー |
| [docs/cli-reference.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/cli-reference.md) | CLI コマンド、フラグ、JSON 出力、終了コード |
| [docs/faq.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/faq.md) | サンプルレート、エネルギー、モード semantics、その他のよくある質問 |
| [docs/protocol-spec.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/protocol-spec.md) | 記録済みの PPK2 プロトコル、コマンド、フィールド、デバイス挙動 |
| [docs/capture-format.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/capture-format.md) | 欠落を認識する正規キャプチャ artifact |
| [docs/decimation.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/decimation.md) | 間引きエクスポートのバケット：各バケットが何を含み、何を含まないと表明するか |
| [docs/calibration.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/calibration.md) | 生サンプルから電流への変換式と単位 |
| [docs/bandwidth.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/bandwidth.md) | 電流チャネルのサンプリングモデル、エイリアシング、どの統計量が生き残るか |
| [docs/logic-port.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/logic-port.md) | D0-D7 の配線、Logic VCC、電圧レベル、実用帯域幅 |
| [docs/decoders.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/decoders.md) | UART/SPI コントラクト、信頼度、欠落、物理的制約 |
| [docs/triggers.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/triggers.md) | トリガの種類とプリ/ポストトリガのキャプチャウィンドウ |
| [docs/energy-analysis.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/energy-analysis.md) | 電荷・エネルギー・ピーク電流・レイテンシの定義 |
| [docs/agent-interface.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/agent-interface.md) | JSON、ツール、状態変更操作、コンテキスト予算 |
| [docs/sources.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/sources.md) | Nordic 公式ドキュメントとリポジトリのリファレンス |
| [SECURITY.md](https://github.com/tipoLi5890/ppk2lab/blob/main/SECURITY.md) | ハードウェア、USB、ファイル、信頼できない入力のセキュリティモデル |

## 連絡先

質問・バグ・互換性レポート・機能リクエストは [GitHub Issues](https://github.com/tipoLi5890/ppk2lab/issues) からお願いします。

## ライセンス

本プロジェクトは [MIT License](https://github.com/tipoLi5890/ppk2lab/blob/main/LICENSE) の下で公開されています。対象は本リポジトリのオリジナル素材のみであり——PPK2 ハードウェア、Nordic ファームウェア、Nordic ドキュメント、商標は、それぞれの権利者に帰属します。

## 開発

本プロジェクトは Claude Code と OpenAI Codex の支援を受けて開発されています。
