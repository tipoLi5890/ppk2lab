# ppk2lab

[English](https://github.com/tipoLi5890/ppk2lab/blob/main/README.md) · [繁體中文](https://github.com/tipoLi5890/ppk2lab/blob/main/.github/README.zh-Hant.md) · **简体中文** · [日本語](https://github.com/tipoLi5890/ppk2lab/blob/main/.github/README.ja.md)

> 本文件为 [README.md](https://github.com/tipoLi5890/ppk2lab/blob/main/README.md) 的翻译；若内容有出入，以英文版为准。

**精确看清设备的功耗流向。** `ppk2lab` 将 Nordic Power Profiler Kit II 变成一个可脚本化的测量实验室：在同一条时间轴上记录电流与八路数字信号，解码低速 UART 与 SPI，并将能耗归因到具体的协议事件。无论是 Python、命令行还是 AI Agent，都能直接调用这一切，让功耗回归也能像测试失败一样，使 CI 构建失败。

[![CI](https://github.com/tipoLi5890/ppk2lab/actions/workflows/ci.yml/badge.svg)](https://github.com/tipoLi5890/ppk2lab/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ppk2lab)](https://pypi.org/project/ppk2lab/)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-experimental-orange)

> [!IMPORTANT]
> 本项目为实验性质，与 Nordic Semiconductor ASA 无任何隶属或背书关系。

## 亮点

`ppk2lab` 让人与 Agent 都能：

- 发现并配置一台或多台 PPK2 设备；
- 在同一条同步时间轴上采集校准后电流与全部 D0-D7 数字状态；
- 解码低速 UART 与 SPI 流量（已验证 9,600 波特 / 10 kHz）；
- 为一段窗口或一个解码事件测量电荷、能量、峰值电流、延迟与分布（p50/p90/p99），并附上各量程的典型误差范围；
- 处理小时级的采集文件：只读 manifest 而不加载样本、只读单个窗口，或导出降采样摘要；
- 以电流、数字状态、UART 内容或 SPI 事务触发采集；
- 在本地自动化与 CI 中运行可复现的功耗断言；
- 当样本不足以支撑结论时如实说明——样本丢失、ADC 饱和、窗口内没有数据——而不是给出一个看似确定的数字；
- 通过 `--simulate` 在没有硬件的情况下体验全部功能。

## 安装

核心包需要 Python 3.11 及以上，运行时依赖只有 `pyserial`。

```bash
pipx install ppk2lab        # 推荐以此安装 CLI
# 或在虚拟环境中：
pip install ppk2lab

ppk2lab --version
ppk2lab doctor --json
```

目前 PyPI 上唯一的构建是 `0.1.0.dev0`，必须明确指定版本号安装：`pip install ppk2lab==0.1.0.dev0`。直接 `pip install ppk2lab` 不会安装任何版本，因为预览版默认被排除在解析之外，而稳定版尚未发布——第一个稳定版将是 `0.2.0`，待下方的硬件关卡通过后才会发布。本仓库当前为 `0.2.0.dev0`，包含尚未发布到 PyPI 的改动；若要跟进，请从源码检出安装。

从开发源码运行：

```bash
git clone https://github.com/tipoLi5890/ppk2lab
cd ppk2lab
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ppk2lab doctor --json
```

### Claude Code 插件

安装 `ppk2lab` CLI 后，将本仓库添加为 Claude Code 的 marketplace 并安装其 skills：

```text
/plugin marketplace add tipoLi5890/ppk2lab
/plugin install ppk2lab@ppk2lab
```

插件开发期间可用 `claude --plugin-dir ./` 直接加载源码目录。

### Codex 插件与独立 skills

`skills/` 内是纯 `SKILL.md` 文件，Codex 可直接加载；目前尚未发布 Codex plugin manifest，请改用下方的独立 skill 路径。

仓库级的独立 skills 放在 `.agents/skills/`；用户级的安装方式：

```bash
mkdir -p ~/.agents/skills
cp -R skills/* ~/.agents/skills/
```

Claude Code 也可以从 `.claude/skills/` 或通过随附插件使用同一份 skills。完整安装与故障排查见 [INSTALL.md](https://github.com/tipoLi5890/ppk2lab/blob/main/INSTALL.md)。

## 快速开始

每条命令都可以加上 `--simulate` 在没有硬件的情况下运行，此时会使用内置的模拟 PPK2 来演练整个工具链。模拟运行不是真实测量。

在不改变硬件状态的前提下检查环境与已连接设备：

```bash
ppk2lab doctor --json
ppk2lab discover --json
ppk2lab info --device <serial> --json
```

采集电流与 D0-D7。`capture` 绝不会打开 DUT 电源：

```bash
ppk2lab capture --device <serial> --duration 5s --digital D0-D7 --output capture.ppk2a
```

不加载任何样本，直接查看这个 artifact 实际包含什么——时间轴、缺口、警告、校准信息：

```bash
ppk2lab inspect capture.ppk2a --json
```

解码支持的低速 UART 信号并按 frame 测量能耗：

```bash
ppk2lab decode capture.ppk2a --uart D0 --baud 9600 --output uart.jsonl
ppk2lab measure capture.ppk2a --annotations uart.jsonl --group-by frame --json
```

运行可复现的功耗断言：

```bash
ppk2lab assert capture.ppk2a \
  --rule 'p99_current < 15mA' \
  --rule 'after uart("TX_DONE"), within 20ms, avg_current < 10uA' \
  --format json
```

作为 CI 阈值，百分位数比 `max_current` 更合适：采集时间越长，量程切换次数就越多，最大值会随采集长度向上漂移，而 `p99_current` 不会。

这些命令目前已经实现。每个 JSON 契约都携带 `schema_version` 1，在 `0.2.0` 发布前仍可能变更。

## 功能一览

| 命令 | 用途 | 硬件状态 |
|---|---|---|
| `discover`、`info` | 查找设备并检查固件、metadata、校准与当前状态 | 只读 |
| `capabilities`、`schema` | 返回机器可读的命令、限制、解码器、警告／缺口／中断原因目录与 JSON schema | 只读 |
| `doctor` | 诊断 USB 权限、端口占用、metadata、校准与流速率；任一检查失败即以非零码退出 | 默认只读 |
| `configure` | 选择模式、输出电压与 DUT 电源，并报告前后状态 | 状态变更；不加 `--apply` 时为 dry run |
| `capture` | 记录原始电流、量程、序号计数器与 D0-D7，支持触发 | 测量；绝不打开 DUT 电源 |
| `inspect` | 读取采集文件的 manifest——时间轴、缺口、警告、校准——而不加载样本 | 离线 |
| `decode` | 生成 UART、SPI、边沿、脉冲与事务注记 | 离线 |
| `measure` | 对时间区间或注记计算电流、电荷、能量、峰值、百分位数与可选的占空比拆分 | 离线 |
| `assert` | 应用可复现的功耗与协议回归规则 | 离线 |
| `export` | 生成 CSV、VCD、JSONL、窗口与降采样等派生视图，不替换原始证据 | 离线 |

## 范围与物理限制

PPK2 的数字输入以 100 kS/s 采样，协议解码因此仅适用于低速信号。初期验证目标：

- UART 最高 9,600 波特；
- SPI 时钟最高 10 kHz；
- 全部八路数字输入 D0-D7；
- 更高速率仅在明确标注为 conditional 或 experimental 时提供。

本项目无意取代 MHz 级逻辑分析仪。跨越样本缺口的 frame 一律报告为不完整或无效，绝不宣称为可信的解码结果。

> [!IMPORTANT]
> **本项目产出的任何数值，都尚未与参考仪器比对过。** 这里的每一个准确度数字都只是重述 Nordic 公布的各量程「典型值」规格，测试中的每一段波形都来自内置模拟器。不确定度一律标记为 `guaranteed: false`，原因正在于此。用实机验证它，正是 [ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md) 中剩下那些发布关卡的用途。

| 协议 | 初期支持级别 |
|---|---|
| UART 1,200-9,600 波特 | 验证目标 |
| UART 19,200 波特 | 条件支持 |
| UART 38,400 波特 | 实验性 |
| UART 57,600/115,200 波特 | 不宣称可解码 |
| SPI 时钟 10 kHz 以下 | 验证目标 |
| SPI 10-20 kHz | 条件支持 |
| SPI 20 kHz 以上 | 实验性或不支持 |

## 硬件安全

- 不加 `--apply` 时，`configure` 一律是 dry run。
- `capture` 绝不会打开 DUT 电源，也没有任何选项可以让它这么做；让 DUT 通过测量仪供电，永远是独立且显式的 `configure --dut-power on --apply`。
- 输出电压一律以 `voltage_mv` 表示、对照设备能力校验，绝不从 DUT 名称推测。
- 模式切换、DUT 电源、输出电压与 reset 操作都会报告前后状态。
- 结束或失败时，库会尝试恢复 session 开始时的电源状态，并记录恢复是否成功。
- 任何插件 hook 或 skill 都不得自动打开 DUT 电源、改变电压或复位设备。

## 配合 AI Agent 使用

每个操作都同时提供类型化 Python API 与具有稳定 JSON 输出的 CLI 命令。只读检查与会改变硬件状态的操作被清晰分离。

命令面：

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

会打开 DUT 电源、改变输出电压或复位硬件的命令都被明确标注为 state-changing。电压参数在 API 名称中就带有单位，例如 `voltage_mv`。

可选的 Agent adapter 可能以独立包提供；核心驱动与文件格式不会绑定任何单一 Agent 框架。

每个 JSON 结果都携带 schema 版本、稳定错误码、修复提示（remediation）、完成状态与样本缺口信息。`ppk2lab capabilities --json` 描述实时的命令与解码器面，并发布警告、缺口原因与中断原因的代码目录，因此 Agent 既不需要猜测哪些行为不被支持，也不必靠解析文字才能知道某个 `W_*` 代码的含义。

### 随附的 Agent skills

插件提供两个以渐进披露（progressive disclosure）为原则设计的 skills，而非一份巨大的 prompt：每个 `SKILL.md` 都是轻薄的路由器，把安全契约保留在正文中，并把各主题的参考笔记留给 Agent 在需要时才读取。实时知识——命令、选项、解码器速率分级、schema、错误码——从不重复写进 prompt；Agent 应查询自描述的 CLI（`capabilities --json`、`schema`、`doctor --json`）。

| Skill | 覆盖范围 | 按需读取的 references |
|---|---|---|
| `ppk2lab-operate` | 使用 PPK2 测量：安装与故障排查、安全配置与采集、长时间记录、触发、UART/SPI/逻辑解码、能耗分析、CI 功耗回归、硬件诊断 | `setup`、`capture`、`triggers`、`decode`、`analysis`、`regression`、`diagnostics` |
| `ppk2lab-maintain` | 维护本仓库：仅观察的协议研究、发布关卡审计 | `protocol-research`、`release-gating` |

Skills 提供工作流指引；`ppk2lab` CLI 仍是确定性的执行与验证层。安装过程绝不会打开 DUT 电源或自动开始采集。

## 架构

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

正式的采集格式会保存原始样本、校准 metadata、设备与固件标识、采集配置、时间戳与数据丢失标记。处理后的导出文件绝不替换原始证据。

## 官方参考资料

PPK2 的功能以 Nordic Semiconductor 官方 PPK2 文档与官方 [Power Profiler App 仓库](https://github.com/NordicSemiconductor/pc-nrfconnect-ppk) 为依据；这些官方资料定义了本项目的产品行为与兼容性参考。

本项目不分发 Nordic 固件二进制文件。除非另行取得再分发授权，固件安装与更新请使用 Nordic 官方工具。

Nordic Semiconductor、Power Profiler Kit 与 PPK2 可能是 Nordic Semiconductor ASA 的商标或产品名称，此处仅用于标识兼容硬件。

本项目使用的 Nordic 官方参考来源与已记录的 PPK2 行为，见 [docs/sources.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/sources.md) 与 [docs/protocol-spec.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/protocol-spec.md)。

## 路线图

第一个稳定版是 `0.2.0`。在那之前仍待完成的是验证而非实现，而且大多需要实体 PPK2：

- 覆盖 Windows、macOS 与 Linux，对照固件 1.1.0、1.2.0 与 1.2.4 的操作系统与固件兼容性矩阵，并以每次采集都会记录的固件指纹（fingerprint）作为索引；
- 使用已知负载，与官方 Power Profiler app 进行的校准交叉验证；
- 在真实信号上按既定错误率阈值验证 UART 与 SPI 解码器；
- 多设备 session、拔除后恢复，以及 8-24 小时的长时间稳定性测试（soak tests）；
- 对照当前版本验证 Claude Code 与 Codex 的 skill 安装流程；
- 从干净环境复现文档与示例；
- 发布演练、打标签时重新执行依赖许可证扫描，以及 PyPI 发布本身。

需要硬件的关卡没有、也不打算有 CI workflow——它们需要接上 PPK2 与治具 MCU——因此由维护者在实机上执行。完成标准、兼容性矩阵，以及刻意不做的事项见 [ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [INSTALL.md](https://github.com/tipoLi5890/ppk2lab/blob/main/INSTALL.md) | Python、USB 权限、Claude Code、Codex、CI 与故障排查 |
| [ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md) | 里程碑、支持级别与发布完成标准 |
| [docs/SPEC.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/SPEC.md) | 对外数据模型、状态、API 与 schema 契约 |
| [docs/api-baseline.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/api-baseline.md) | 冻结的公开 API 范围与稳定性策略 |
| [docs/cli-reference.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/cli-reference.md) | CLI 命令、参数（flags）、JSON 输出与退出码 |
| [docs/faq.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/faq.md) | 采样率、能量、模式语义与其他常见问题 |
| [docs/protocol-spec.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/protocol-spec.md) | 已记录的 PPK2 协议、命令、字段与设备行为 |
| [docs/capture-format.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/capture-format.md) | 具丢失感知的正式采集 artifact |
| [docs/decimation.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/decimation.md) | 降采样导出的 bucket：每个 bucket 包含什么，以及它如何声明自己缺少什么 |
| [docs/calibration.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/calibration.md) | 原始样本到电流的换算公式与单位 |
| [docs/bandwidth.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/bandwidth.md) | 电流通道的采样模型、混叠（aliasing），以及哪些统计量能幸存 |
| [docs/logic-port.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/logic-port.md) | D0-D7 接线、Logic VCC、电平与可用带宽 |
| [docs/decoders.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/decoders.md) | UART/SPI 契约、置信度、缺口与物理限制 |
| [docs/triggers.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/triggers.md) | 触发类型与 pre/post-trigger 采集窗口 |
| [docs/energy-analysis.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/energy-analysis.md) | 电荷、能量、峰值电流与延迟的定义 |
| [docs/agent-interface.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/agent-interface.md) | JSON、工具、状态变更操作与 context 预算 |
| [docs/sources.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/sources.md) | Nordic 官方文档与仓库参考 |
| [SECURITY.md](https://github.com/tipoLi5890/ppk2lab/blob/main/SECURITY.md) | 硬件、USB、文件与不可信输入的安全模型 |

## 联系方式

问题、bug、兼容性报告与功能请求，请通过 [GitHub Issues](https://github.com/tipoLi5890/ppk2lab/issues) 提交。

## 许可证

以 [MIT License](https://github.com/tipoLi5890/ppk2lab/blob/main/LICENSE) 发布。此许可证仅覆盖本仓库中的原创内容——PPK2 硬件、Nordic 固件、Nordic 文档与商标仍归各自所有者所有。

## 开发

本项目在 Claude Code 与 OpenAI Codex 的协助下开发。
