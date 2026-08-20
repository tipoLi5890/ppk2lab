# ppk2lab

[English](https://github.com/tipoLi5890/ppk2lab/blob/main/README.md) · **繁體中文** · [简体中文](https://github.com/tipoLi5890/ppk2lab/blob/main/.github/README.zh-Hans.md) · [日本語](https://github.com/tipoLi5890/ppk2lab/blob/main/.github/README.ja.md)

> 本文件為 [README.md](https://github.com/tipoLi5890/ppk2lab/blob/main/README.md) 的翻譯；若內容有出入，以英文版為準。

**清楚看見裝置電力的去向。** `ppk2lab` 將 Nordic Power Profiler Kit II 變成可程式化的量測實驗室：在同一條時間軸上記錄電流與八路數位訊號、解碼低速 UART 與 SPI，並將能耗歸因到個別的協定事件。所有功能皆可透過 Python、命令列或 AI Agent 存取，讓功耗回歸能像測試失敗一樣使 CI build 失敗。

[![CI](https://github.com/tipoLi5890/ppk2lab/actions/workflows/ci.yml/badge.svg)](https://github.com/tipoLi5890/ppk2lab/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ppk2lab)](https://pypi.org/project/ppk2lab/)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-experimental-orange)

> [!IMPORTANT]
> 本專案為實驗性質，與 Nordic Semiconductor ASA 無任何隸屬或背書關係。

## 亮點

`ppk2lab` 讓人與 Agent 都能夠：

- 探索並設定一台或多台 PPK2 裝置；
- 在同一條同步時間軸上擷取校正後電流與全部 D0-D7 數位狀態；
- 解碼低速 UART 與 SPI 流量（9,600 baud 與 10 kHz SCLK 是最高的速率分級）；
- 為一段視窗或一個解碼事件量測電量、能量、峰值電流、延遲與分布（p50/p90/p99），並附上各 range 的典型誤差範圍；
- 處理小時等級的擷取檔：只讀 manifest 而不載入樣本、只讀單一視窗，或匯出降取樣摘要；
- 以電流、數位狀態、UART 內容或 SPI transaction 觸發擷取；
- 在本機自動化與 CI 中執行可重現的功耗 assertion；
- 當樣本不足以支撐結論時明說——樣本遺失、ADC 飽和、視窗內沒有資料——而不是給出一個看似肯定的數字；
- 透過 `--simulate` 在沒有硬體的情況下嘗試所有功能。

## 安裝

核心套件需要 Python 3.11 以上，執行期相依套件只有 `pyserial`。

```bash
pipx install ppk2lab        # 建議以此安裝 CLI
# 或在虛擬環境中：
pip install ppk2lab

ppk2lab --version
ppk2lab doctor --json
```

`0.2.0` 是第一個穩定版本，因此直接 `pip install ppk2lab` 就能安裝。它刻意標示為 experimental：工具鏈在無硬體的情況下經過大量測試，讓量測保持誠實的那些性質是被強制執行而非假設的——但它背後的**硬體驗證仍然是部分的**。一台機器、只有 macOS，而且還沒有任何解碼器讀過真實訊號。[路線圖](#roadmap)說明它涵蓋了什麼、又沒有涵蓋什麼；在你信任任何一個數字之前，請先讀它。

從開發用原始碼執行：

```bash
git clone https://github.com/tipoLi5890/ppk2lab
cd ppk2lab
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ppk2lab doctor --json
```

### Claude Code 外掛

安裝 `ppk2lab` CLI 後，將本儲存庫加入 Claude Code 的 marketplace 並安裝其 skills：

```text
/plugin marketplace add tipoLi5890/ppk2lab
/plugin install ppk2lab@ppk2lab
```

外掛開發期間可用 `claude --plugin-dir ./` 直接載入原始碼目錄。

### Codex 外掛與獨立 skills

`skills/` 內是純 `SKILL.md` 檔案，Codex 可直接載入；目前尚未發布 Codex plugin manifest，請改用下方的獨立 skill 路徑。

儲存庫層級的獨立 skills 放在 `.agents/skills/`；使用者層級的安裝方式：

```bash
mkdir -p ~/.agents/skills
cp -R skills/* ~/.agents/skills/
```

Claude Code 也能從 `.claude/skills/` 或透過隨附外掛使用同一份 skills。完整安裝與疑難排解請見 [INSTALL.md](https://github.com/tipoLi5890/ppk2lab/blob/main/INSTALL.md)。

## 快速上手

每個指令都可以加上 `--simulate`，在沒有硬體的情況下執行，這會使用內建的模擬 PPK2 來驗證整個工具鏈；模擬執行不是量測。

在不改變硬體狀態的前提下檢視環境與已連接裝置：

```bash
ppk2lab doctor --json
ppk2lab discover --json
ppk2lab info --device <serial> --json
```

擷取電流與 D0-D7。`capture` 絕不會開啟 DUT 電源：

```bash
ppk2lab capture --device <serial> --duration 5s --digital D0-D7 --output capture.ppk2a
```

不載入任何樣本，直接查看這個 artifact 實際包含什麼——時間軸、缺口、警告、校正資訊：

```bash
ppk2lab inspect capture.ppk2a --json
```

解碼支援的低速 UART 訊號並依 frame 量測能耗：

```bash
ppk2lab decode capture.ppk2a --uart D0 --baud 9600 --output uart.jsonl
ppk2lab measure capture.ppk2a --annotations uart.jsonl --group-by frame --json
```

執行可重現的功耗 assertion：

```bash
ppk2lab assert capture.ppk2a \
  --rule 'p99_current < 15mA' \
  --rule 'after uart("TX_DONE"), within 20ms, avg_current < 10uA' \
  --format json
```

在 CI 門檻上，百分位數比 `max_current` 更合適：擷取時間愈長，range 切換次數就愈多，最大值會隨擷取長度往上飄，`p99_current` 則不會。

以上指令目前皆已可用。每個 JSON 契約都帶有 `schema_version` 1，且在 `0.2.0` 發布前仍可能變動。

## 功能一覽

| 指令 | 用途 | 硬體狀態 |
|---|---|---|
| `discover`、`info` | 尋找裝置並檢視韌體、metadata、校正與目前狀態 | 唯讀 |
| `capabilities`、`schema` | 回傳機器可讀的指令、限制、解碼器、警告／缺口／中斷原因目錄與 JSON schema | 唯讀 |
| `doctor` | 診斷 USB 權限、埠占用、metadata、校正與串流速率；任一檢查失敗即以非零碼結束 | 預設唯讀 |
| `configure` | 選擇模式、輸出電壓與 DUT 電源，並回報前後狀態 | 狀態變更；未加 `--apply` 時為 dry run |
| `capture` | 記錄原始電流、range、序號計數器與 D0-D7，支援觸發 | 量測；絕不開啟 DUT 電源 |
| `inspect` | 讀取 capture 的 manifest——時間軸、缺口、警告、校正——而不載入樣本 | 離線 |
| `decode` | 產生 UART、SPI、edge、pulse 與 transaction 註記 | 離線 |
| `measure` | 對時間區間或註記計算電流、電量、能量、峰值、百分位數與選用的工作週期切分 | 離線 |
| `assert` | 套用可重現的功耗與協定回歸規則 | 離線 |
| `export` | 產生 CSV、VCD、JSONL、視窗與降取樣等衍生視圖，不取代原始證據 | 離線 |

## 範圍與物理限制

PPK2 的數位輸入以 100 kS/s 取樣，協定解碼因此僅適用低速訊號。分級的界線只取決於每個 bit 在這個固定取樣率下能拿到幾個樣本——10、5 與 2.5——除此之外沒有別的依據。最高一級涵蓋：

- UART 最高 9,600 baud；
- SPI 時脈最高 10 kHz；
- 全部八路數位輸入 D0-D7；
- 更高速率僅在明確標為 conditional 或 experimental 時提供。

本專案無意取代 MHz 等級的邏輯分析儀。跨越樣本缺口的 frame 一律回報為不完整或無效，絕不宣稱為可信的解碼結果。

在 100 kS/s 之下，樣本被 USB 主機丟掉是常態，而每一次擷取都會回報。在某一台 macOS 主機上，60 秒的擷取在機器閒置時遺失 0.43% 的樣本，在同時跑 12 個 CPU 燒機程序與持續磁碟寫入時遺失 1.08%——兩者都是五個整段 chunk 的缺口，而不是零散樣本。同一台主機稍後的另一次 60 秒擷取遺失了 5.1%，可見遺失率取決於機器當下還在做什麼，而不是擷取本身。

> [!IMPORTANT]
> **已經量過一個已知負載；但仍未與校準過的參考儀器比對。** 在某一台裝置既有的 3700 mV 設定點下，於 VOUT 與 GND 之間接上 680 kΩ ±5% 電阻，經過量測器自身 1000.625 Ω 分流電阻的串聯電路應為 5.4332 µA；十一次擷取量到 5.55 µA（5.5280–5.5614 µA），比標稱值高 +2.1%。但電阻本身的容差讓真值可能落在 5.175–5.719 µA 之間，因此這項檢查只能排除嚴重誤差，**無法**解析儀器自身的增益誤差——要解析它，需要精度高一個數量級的電阻，或一台校準過的參考儀器。這裡的每一個準確度數字仍然只是重述 Nordic 公布的各量程「典型值」規格，不確定度一律標記為 `guaranteed: false`，原因正在於此。那次量測只涵蓋一台裝置、一個韌體指紋、僅 macOS，且沒有 MCU 治具，因此下方的 UART 與 SPI 分級仍是「每 bit 幾個樣本」的預算，而不是量測出來的錯誤率。仍未量測的項目請見 [ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md)。

| 協定 | 速率分級 |
|---|---|
| UART 1,200-9,600 baud | validated |
| UART 19,200 baud | conditional |
| UART 38,400 baud | experimental（需 `--allow-experimental`） |
| UART 57,600 baud 以上 | unsupported——直接拒絕 |
| SPI 時脈 10 kHz 以下 | validated |
| SPI 10-20 kHz | conditional |
| SPI 20-40 kHz | experimental；40 kHz 以上直接拒絕 |

## 硬體安全

- 未加 `--apply` 時，`configure` 一律是 dry run。
- `capture` 絕不會開啟 DUT 電源，也沒有任何選項可以讓它這麼做——先執行 `configure --dut-power on --apply` 同樣沒有用，因為主機一關閉序列埠，PPK2 就會讓 VOUT 斷電。在一台裝置上量到的結果是：關閉 0 ms 時仍然通電，100-250 ms 時是擲硬幣，從 500 ms 起每次都已斷電。因此要讓 DUT 透過量測器供電，必須和擷取在同一個開啟中的 session 內完成（Python 中的 `ppk2lab.PPK2`）；`configure --dut-power on --apply` 會發出 `W_DUT_POWER_TRANSIENT` 警告，而不是做出它守不住的承諾。
- 輸出電壓一律以 `voltage_mv` 表示、對照裝置能力驗證，絕不從 DUT 名稱推測。
- 模式切換、DUT 電源、輸出電壓與 reset 操作都會回報前後狀態。
- 結束或失敗時，程式庫會嘗試還原 session 開始時的電源狀態，並記錄還原是否成功。
- 任何外掛 hook 或 skill 都不得自動開啟 DUT 電源、改變電壓或重置裝置。

## 搭配 AI Agent 使用

每個操作都同時提供型別化 Python API 與具穩定 JSON 輸出的 CLI 指令。唯讀檢視與會改變硬體狀態的操作被清楚分離。

指令表面：

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

會開啟 DUT 電源、改變輸出電壓或重置硬體的指令都被明確標示為 state-changing。電壓參數在 API 名稱中就帶有單位，例如 `voltage_mv`。

選用的 Agent adapter 可能以獨立套件提供；核心驅動與檔案格式不會綁定任何單一 Agent 框架。

每個 JSON 結果都帶有 schema 版本、穩定錯誤碼、修復提示（remediation）、完成狀態與樣本缺口資訊。`ppk2lab capabilities --json` 描述即時的指令與解碼器表面，並發布警告、缺口原因與中斷原因的代碼目錄，因此 Agent 既不需要猜測哪些行為不被支援，也不必靠解析文字才能知道某個 `W_*` 代碼的意義。

### 隨附的 Agent skills

外掛提供兩個以漸進揭露（progressive disclosure）為原則設計的 skills，而非一份巨大的 prompt：每個 `SKILL.md` 都是薄薄的路由器，將安全契約保留在正文中，並把各主題的參考筆記留給 Agent 在需要時才讀取。即時知識——指令、選項、解碼器速率分級、schema、錯誤碼——從不重複寫進 prompt；Agent 應查詢自我描述的 CLI（`capabilities --json`、`schema`、`doctor --json`）。

| Skill | 涵蓋範圍 | 按需讀取的 references |
|---|---|---|
| `ppk2lab-operate` | 使用 PPK2 量測：安裝與疑難排解、安全設定與擷取、長時間記錄、觸發、UART/SPI/邏輯解碼、能耗分析、CI 功耗回歸、硬體診斷 | `setup`、`capture`、`triggers`、`decode`、`analysis`、`regression`、`diagnostics` |
| `ppk2lab-maintain` | 維護本儲存庫：僅觀察的協定研究、發布關卡稽核 | `protocol-research`、`release-gating` |

Skills 提供工作流程指引；`ppk2lab` CLI 仍是確定性的執行與驗證層。安裝過程絕不會開啟 DUT 電源或自動開始擷取。

## 架構

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

正式的 capture 格式會保存原始樣本、校正 metadata、裝置與韌體識別、擷取設定、時間戳與資料遺失標記。處理後的匯出檔絕不取代原始證據。

## 官方參考資料

PPK2 的功能以 Nordic Semiconductor 官方 PPK2 文件與官方 [Power Profiler App 儲存庫](https://github.com/NordicSemiconductor/pc-nrfconnect-ppk) 為依據；這些官方資料定義了本專案的產品行為與相容性參考。

本專案不散布 Nordic 韌體二進位檔。除非另行取得再散布授權，韌體安裝與更新請使用 Nordic 官方工具。

Nordic Semiconductor、Power Profiler Kit 與 PPK2 可能是 Nordic Semiconductor ASA 的商標或產品名稱，此處僅用於識別相容硬體。

本專案使用的 Nordic 官方參考來源與已記錄的 PPK2 行為，請見 [docs/sources.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/sources.md) 與 [docs/protocol-spec.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/protocol-spec.md)。

## 路線圖

`0.2.0` 已發布。仍待完成的是驗證而非實作，而分清楚哪個是哪個很重要。2026-08-20 的一次硬體測試在單一裝置上把工具從頭跑到尾——韌體指紋 `HW=49625 IA=59.0 keys=40 ports=2`，Apple silicon 上的 macOS——涵蓋裝置探索、metadata、100 kS/s 校正後擷取、中斷復原，以及所有離線指令。以下這些**尚未**驗證，而且各自都需要那次測試沒有的硬體：

- 在 Windows 與 Linux 上各跑一次實機測試，那裡的作業系統會提供 macOS 不提供的 USB interface 編號；
- 第二台裝置與第二個韌體指紋，目前兩者都各只見過一個；
- 一個能解析增益誤差的交叉驗證：精度比 ±5% 高一個數量級的電阻，或一台校準過的參考儀器；
- 以 MCU 治具產生的真實訊號，對照既定的錯誤率門檻驗證 UART 與 SPI 解碼器；
- 拔除後復原、8-24 小時的長時間穩定性測試（soak test）與多裝置 session——另有頻寬掃描與一個會跨越電流量程邊界的負載，兩者仍未量測，但不作為 `0.2.0` 的發布條件；
- 對照目前版本驗證 Claude Code 與 Codex 的 skill 安裝流程、從乾淨環境重現文件與範例、發布演練、標記版本時重新執行相依套件授權掃描，以及 PyPI 發布本身。

解碼器的支援層級是以「設計時針對的速率」命名的，不是實測錯誤率——目前還沒有任何實測。需要硬體的驗證沒有、也不打算有 CI workflow，因此由維護者在實機上執行，並隨著涵蓋的機器與平台增加逐步填上相容性矩陣。該矩陣以及刻意不做的項目請見 [ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md)。

## 文件

| 文件 | 內容 |
|---|---|
| [INSTALL.md](https://github.com/tipoLi5890/ppk2lab/blob/main/INSTALL.md) | Python、USB 權限、Claude Code、Codex、CI 與疑難排解 |
| [ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md) | 里程碑、支援等級與發布完成準則 |
| [docs/SPEC.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/SPEC.md) | 對外資料模型、狀態、API 與 schema 契約 |
| [docs/api-baseline.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/api-baseline.md) | 凍結的公開 API 介面與穩定性政策 |
| [docs/cli-reference.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/cli-reference.md) | CLI 指令、旗標、JSON 輸出與結束代碼 |
| [docs/faq.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/faq.md) | 取樣率、能量、模式語意與其他常見問題 |
| [docs/protocol-spec.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/protocol-spec.md) | 已記錄的 PPK2 協定、指令、欄位與裝置行為 |
| [docs/capture-format.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/capture-format.md) | 具遺失感知的正式 capture artifact |
| [docs/decimation.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/decimation.md) | 降取樣匯出的 bucket：每個 bucket 包含什麼，以及它如何說明自己缺少什麼 |
| [docs/calibration.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/calibration.md) | 原始樣本轉換為電流的公式與單位 |
| [docs/bandwidth.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/bandwidth.md) | 電流通道的取樣模型、混疊（aliasing），以及哪些統計量能倖存 |
| [docs/logic-port.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/logic-port.md) | D0-D7 接線、Logic VCC、電壓準位與可用頻寬 |
| [docs/decoders.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/decoders.md) | UART/SPI 契約、信心值、缺口與物理限制 |
| [docs/triggers.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/triggers.md) | 觸發類型與 pre/post-trigger 擷取視窗 |
| [docs/energy-analysis.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/energy-analysis.md) | 電量、能量、峰值電流與延遲的定義 |
| [docs/agent-interface.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/agent-interface.md) | JSON、工具、狀態變更操作與 context 預算 |
| [docs/sources.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/sources.md) | Nordic 官方文件與儲存庫參考 |
| [SECURITY.md](https://github.com/tipoLi5890/ppk2lab/blob/main/SECURITY.md) | 硬體、USB、檔案與不可信輸入的安全模型 |

## 聯絡方式

問題、bug、相容性回報與功能請求，請透過 [GitHub Issues](https://github.com/tipoLi5890/ppk2lab/issues) 提出。

## 授權

以 [MIT License](https://github.com/tipoLi5890/ppk2lab/blob/main/LICENSE) 發布。授權範圍僅涵蓋本儲存庫內的原創內容——PPK2 硬體、Nordic 韌體、Nordic 文件與商標仍屬其原持有者所有。

## 開發

本專案在 Claude Code 與 OpenAI Codex 的協助下開發。
