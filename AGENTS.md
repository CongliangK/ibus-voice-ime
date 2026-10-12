# AGENTS.md — 供 AI 代理阅读的实现细节

本文件面向在本仓库上工作的人工智能代理（以及想深挖细节的贡献者），收录安装系统内部机制、各链路实现细节与环境变量全量说明。面向使用者的安装与使用说明见 [README.md](README.md)；技术架构与选型理由见 [docs/README.md](docs/README.md)。

## 安装系统内部机制

### 脚本分工

- **`setup.sh`（一键入口）**：新机安装只需这一个命令——编排 init.sh + 语音验收 + doctor 体检，支持 `--dry-run` 预览与 `--keyboard-only` / `--cloud` 等选项。语音验收是模式感知的：仅当所选方案为本地 Qwen 且 init 语音步骤就绪（汇总标记 `VOICE_IME_INIT_STATE` 为 `voice=ok`）时才执行 `--verify-only`；无 GPU / 驱动不可用导致语音跳过时，安装后的验收同样跳过并说明原因（不再盲目验收打 FAIL）。`./setup.sh --verify-only` 显式调用不受此门控。
- **`init.sh`（一次性初始化）**：下载并配置基础资产（rime-ice 词库、本地 Qwen3-ASR 模型与 venv、RNNoise 降噪模型），完成后自动调用 `install.sh` 完成注册。参数可配置装什么。
- **`install.sh`（每次轻量注册）**：把引擎安装进当前电脑会话——注册 IBus 组件、写 environment.d、注册 GNOME 输入源/热键、重启输入法。**不下载任何东西**，重启/重新登录后想恢复激活就再跑一次。注册成功不代表语音已通过推理验收。

### setup.sh 全量选项

| 选项 | 说明 |
|---|---|
| `--defaults` | 向导全部采用当前配置/默认值，不逐题提问；终端仍会确认执行，可加 `--yes` |
| `--non-interactive` / `-n` | 全默认零提问安装（= `--defaults --yes` 且跳过终端下的 API Key 交互）：CI/管道/无人值守适用 |
| `--reconfigure` | 已装机器改配置：只合并并备份用户配置，热键/语音方案或模型变化时轻量重注册；不初始化、不下载资产 |
| `--yes` / `-y` | 无人值守，自动默认且免确认；非终端也自动默认，保留旧入口两档本地模型下载行为 |
| `--keyboard-only` / `--no-voice` | 仅键盘安装，不下载语音模型 |
| `--cloud siliconflow` / `--cloud mimo` | 显式选择云端，沿用既有切换脚本和密钥指引 |
| `--qwen-sizes "0.6B 1.7B"` | 显式选择本地 Qwen 下载集合：非空子集，仅支持 `0.6B` / `1.7B`；正常安装跳过向导，透传 `VOICE_IME_QWEN_ASR_SIZES` |
| `--qwen-model 0.6b` / `--qwen-model 1.7b` | 显式选择本地默认模型，透传 `VOICE_IME_QWEN_ASR_MODEL`；仅指定单档下载时默认采用该档。默认模型需在集合内或已有就绪模型中，与云端/仅键盘选项互斥 |
| `--no-rime-ice` / `--no-zhwiki` / `--no-moegirl` / `--no-denoise` | 显式跳过对应资产 |
| `--proxy URL` | 向资产初始化和验收传递代理 |
| `--strict-assets` | 透传给 [init.sh](init.sh)：请求资产出现降级也返回 exit 1，供 CI 严格验收 |
| `--verify-only` | 不安装，只做语音验收和体检 |
| `--prepare-only` | 两段式无人值守第一阶段：只准备/验收资产（无桌面会话可运行），不注册、不激活、不做语音验收/体检；第二阶段需在 GNOME 会话**先运行 `./install.sh`（激活，必须）**，再可选 `./setup.sh --verify-only`（验收+体检，不激活） |
| `--skip-verify` / `--skip-doctor` | 跳过安装后的验收或体检 |
| `--dry-run` | 打印实际选择（方案/模型分档/热键变化）与计划，不应用配置、不安装、不下载 |

优先级规则：正常安装中，显式方案/资产选项（包括 Qwen 模型 flags）优先，出现任一此类选项时跳过整个向导，保持原有脚本化调用方式；`--reconfigure` 中则传给向导跳过相应题目；显式 Qwen 分档/模型 flags 采用整套当前默认值（不重复提问），再覆盖模型选择。显式 `--defaults` 按向导的默认下载集合执行（新机默认仅 1.7B），与自动默认保留旧下载集合的兼容路径不同。

安装（包括跳过体检、云端方案）及重配置成功后，setup.sh 统一打印「开始使用」四步卡片：切换输入法、焦点文本框、从生效配置读取并规范化的语音/原文/粘贴热键，以及首次本地语音 sidecar 冷启动提示。首次按键后模型加载需数十秒属正常；仅键盘安装不代表语音已就绪。`--dry-run` / `--prepare-only` 不打印可直接使用的卡片。

### remote-install.sh 引导器

环境变量：`IBUS_VOICE_IME_DIR` 指定克隆目录（默认 `$HOME/.local/share/ibus-voice-ime-repo`，刻意避开运行时数据根 `~/.local/share/ibus-voice-ime`，显式指过去也会被拒绝），`IBUS_VOICE_IME_REPO` 指定可信仓库/镜像（默认 `https://github.com/CongliangK/ibus-voice-ime.git`），`IBUS_VOICE_IME_REF` 指定分支（默认 `main`）；应 `export` 给管道右侧的 `bash`，值不得为空或含空白。网络受限时可设置 `https_proxy`，引导器不会用 `setup.sh --proxy` 代理首次克隆。重复运行会更新干净克隆并重跑安装，有未提交变更则拒绝覆盖；已有非 Git 数据目录也不会覆盖，可另设 `IBUS_VOICE_IME_DIR`。有可用终端时向导仍交互（`--defaults` 仍需终端确认，可加 `--yes`；彻底零提问用 `--non-interactive`）；真正无人值守时由安装入口自动采用默认值。

### 向导交互细节

安装时会交互询问语音方案、本地模型分档、语音/原文/粘贴热键、LLM 后处理和词库/降噪资产；**每题都有当前值或默认值，可以直接回车**。API Key 不进入向导结果或用户配置；选择 SiliconFlow 后，可在安装完成时隐藏输入密钥，仅传给切换子进程。

终端选择菜单支持 ↑↓ 或 j/k 移动、Home/End 跳转、数字 1–9 单选直选、回车确认、Esc/q 取消；资产多选用空格或数字切换勾选后回车。热键题可选「手动输入（推荐）」「直接按键录入（实验性）」「保持当前/默认」，回车仍保持当前值；管道输入和 `--defaults` 保持原有文本/默认行为。

管道/录制场景自动降级为编号列表：输入一基编号（多选可用逗号或空格分隔，`0` 表示清空全部勾选），空行采用默认值，非法编号重问，输入结束则失败；`--defaults` 无需输入。

热键**直接按键录入（实验性）**的实现约束：按住修饰键后再按一个字母完成录入，随后选择「使用」「重录」或「手动输入」；Esc 取消且恢复终端。方向键、功能键、Tab、回车、空格不是合法主键，会提示后继续等待。大写字母识别为 Shift（如 `V` → `Shift+V`），小写裸字母依引擎规则转换为 Ctrl+Alt+字母。**为什么是实验性**：终端与桌面可能先截获组合——实测 `Ctrl+B` 这类单修饰组合能录入，`Ctrl+Alt+字母` 常被 GNOME/IBus 全局快捷键抢走而毫无反应。终端无法单独报告修饰键：**Super 键与纯修饰键组合（如 `Ctrl+Super`）捕获不到**——且 GNOME 自定义快捷键与引擎热键模型都要求一个非修饰主键，纯修饰键组合目前无法注册。传统终端也无法区分 Tab/换行/回车与 Ctrl+I/J/M，Ctrl+Shift 组合可能丢失 Shift。

### 重配置语义

配置按白名单稀疏合并到用户配置（`VOICE_IME_CONFIG` 可覆盖），已有文件自动备份为 `config.json.bak.<时间戳>`。重配置不会准备新后端资产或保存密钥；切换尚未准备的后端后，仍需按向导指引补齐运行条件。`keyboard-only` 表示跳过语音资产安装，并不删除已有模型或禁用已有语音配置。

### IBus 重启策略

同一安装流程最多两次 IBus 重启：仅键盘或 GPU 不可用时末尾重启一次；本地语音保留预注册重启以让新组件进入会话，语音切换最多再重启一次，末尾只刷新注册/配置（共两次）。语音失败仍返回失败并给出独立续装命令，但不盲目第三次重启（避免切换已重启后激活才失败的情况）。[install.sh](install.sh) 支持 `VOICE_IME_NO_RESTART=1`：写组件、环境和配置，但跳过 IBus 重启与 GNOME 激活；独立运行默认仍完整注册激活。

**注册缓存机制（重启电脑后输入法消失的根因）**：IBus 1.5.3x 的 `ibus_registry_load` 在进程环境没有 `IBUS_COMPONENT_PATH` 时只扫系统组件目录（用户目录支持在上游源码中被 `#if 0` 禁用），且 ibus-daemon 默认 `--cache auto` 的加载顺序为「系统级缓存 `/var/cache/ibus/bus/registry`（Fedora 由 ibus 包更新时以 root 预置）→ 用户缓存 `~/.cache/ibus/bus/registry` → 重扫 `IBUS_COMPONENT_PATH`」——**系统组件目录不变则系统缓存永远"有效"，开机由 systemd 拉起的 daemon 在第一层就被短路，用户组件目录永不被扫描**（strace 实证：auto daemon 只 open 系统缓存，用户缓存与组件 XML 均不碰）。叠加第二个坑：任何一次无 `IBUS_COMPONENT_PATH` 的 daemon 重启（refresh 重扫覆写 / auto 失效重扫写缓存）都会把用户缓存也抹成只含系统组件。两者症状一致：安装时手动 refresh 重启正常（会话内存状态），重启电脑后 voice-custom 从引擎列表消失且不自愈。**修复是双层的**：① [ensure-ibus-cache-refresh.sh](scripts/ensure-ibus-cache-refresh.sh)（由 [install.sh](install.sh) 与 doctor fix 调用）给 GNOME 的 ibus systemd 用户单元写 drop-in，把 daemon 启动参数改为 `--cache refresh`——每次启动无视缓存强制重扫组件路径（daemon 环境的 `IBUS_COMPONENT_PATH` 由 environment.d 提供），纯用户目录操作无 sudo，无该 unit 的环境静默跳过；② [ibus-restart.sh](scripts/ibus-restart.sh) 自带 `IBUS_COMPONENT_PATH` 推导（调用方显式值优先）并在重启前 `ibus write-cache` 固化用户缓存，保证手动重启路径也不再写坏缓存。[doctor](scripts/doctor.sh) 的 daemon 视角检测（`ibus list-engine` 无 voice-custom 但 XML 在 → FAIL `ibus-registry`；引擎进程仍存活时降为 WARN「重启后会消失」）与 fix 分派（补 drop-in + 重建缓存 + 统一重启）覆盖两层根因；[uninstall-all.sh](uninstall-all.sh) 会移除 drop-in 并 daemon-reload。

### 初始化三态与幂等

初始化采用 `ready / degraded / blocked` 三态：rime-ice 失败时键盘回退 luna，RNNoise 失败时降噪回退 notch，两者以「⚠ 降级项」列明影响与精确续装命令，默认仍 exit 0；所选本地语音安装/验收失败或 IBus 注册失败为 blocked（exit 1）。[init.sh](init.sh) 与 [setup.sh](setup.sh) 均支持 `--strict-assets`，请求资产降级也 exit 1；无 GPU/驱动 partial 的语音跳过仍不算失败。失败出口会复述子步骤续装命令（保留本次代理/词库选项），子步骤幂等，不必重跑向导或重复下载已就绪资产。

### 两段式无人值守安装（headless prepare → desktop activate）

桌面激活（GNOME 输入源/热键注册、重启输入法）必须有桌面会话；SSH/CI 等无会话环境可先完成全部资产准备与验收，激活留到登录桌面后：

```bash
# 第一阶段（无桌面会话可用）：init --skip-install 语义 + Qwen 准备不激活
# （VOICE_IME_ENABLE_QWEN_ASR=0：准备+推理验收，但不写配置/不切后端/不重启）。
# 不注册、不切云端、不做语音验收/体检，跳过的原因见输出。
./setup.sh --prepare-only --non-interactive        # 可按需加 --proxy / 资产选项

# 第二阶段（登录 GNOME 后）：注册组件/输入源/热键并重启输入法
./install.sh
./setup.sh --verify-only                           # 语音验收 + 体检
```

两阶段均可重复运行，子步骤幂等、已就绪资产不重复下载。`--prepare-only` 与 `--verify-only` / `--reconfigure` / `--cloud` 互斥（云端切换会重启输入法，属于第二阶段）。无 GPU 机器第一阶段会跳过本地语音（属预期，不算失败），第二阶段体检时按「无 GPU + qwen 后端」如实 FAIL 并给出换云端/仅键盘指引。

## 本地 Qwen 语音的安装验收（内部细节）

Qwen 默认自动准备**仓库内 CPython 3.12（含开发头文件）**，不沿用系统 `python3`。Fedora 即使只有系统 3.14、没有 pip/uv，也不必先找系统 3.12 包：安装器使用系统 Python 的标准库校验 uv wheel 并自举到仓库，再由 uv 安装 3.12。已有可用 PATH uv 会经版本检查后使用；否则自举固定版本到仓库。**不运行 sudo，不修改系统 Python、全局 uv 或用户 pip 配置**；IBus/PyGObject 的系统环境不变。仍需 NVIDIA/CUDA 驱动、C 编译器、磁盘空间与网络，不能把自举成功当作推理成功。

直接调用 [setup-qwen-asr.sh](scripts/setup-qwen-asr.sh) 的相关命令（仓库根目录运行；已有旧 3.14 venv 会自动备份迁移，不必手动删除）：

```bash
# 推荐：先准备/验收，保留用户渠道配置，不重启 IBus：
VOICE_IME_ENABLE_QWEN_ASR=0 ./scripts/setup-qwen-asr.sh
# 海外下载不可达时使用实际代理地址：
VOICE_IME_ENABLE_QWEN_ASR=0 ./scripts/setup-qwen-asr.sh --proxy http://127.0.0.1:7890
# 准备成功后，明确激活 0.6B（写用户配置并重启 IBus）：
./scripts/switch-qwen-asr.sh 0.6b
# 检查实际持久化解释器/模型，不下载、不切换配置：
./scripts/setup-qwen-asr.sh --verify-only
# 必要时明确使用系统 3.12（必须已有 venv/ensurepip、Python.h）：
VOICE_IME_QWEN_ASR_SETUP_PYTHON=/usr/bin/python3.12 ./scripts/setup-qwen-asr.sh
# 每个推理验收子进程最长 1800 秒（示例改为 300）：
VOICE_IME_QWEN_ASR_VERIFY_TIMEOUT=300 ./scripts/setup-qwen-asr.sh --verify-only
```

未设置 `VOICE_IME_QWEN_ASR_SIZES` 仍下载两档；可用 `VOICE_IME_QWEN_ASR_SIZES='0.6B' VOICE_IME_QWEN_ASR_MODEL=0.6b` 仅准备并使用小档，或用 `'0.6B 1.7B'` 准备两档。集合外已有模型不删除、不校验；默认模型应位于所选集合或已有就绪模型中。空集合/非法分档会在安装前报错。

下载预算：0.6B 模型约 1.8GB、1.7B 约 4.4GB，torch cu13 全家桶依赖 venv 典型约 6GB；磁盘峰值需另考虑 pip 缓存、解包临时文件和重建保留的旧 venv。只检测 `~/.cache/pip` 是否存在，不扫描缓存大小；同机重装依赖通常直接命中缓存。中断后重跑同一命令可续装，已就绪资产复用，未完成的临时模型下载可能重下。

### 模型共享缓存

各分档的真实权重统一下载到共享缓存 `$VOICE_IME_MODEL_CACHE`（默认 `${XDG_CACHE_HOME:-~/.cache}/ibus-voice-ime/models`），仓库内 `vendor/models/qwen3-asr/Qwen3-ASR-<size>` 只是指向缓存的**符号链接**——既有 vendor 路径语义不变（引擎、验收、REUSE 判据照旧读 vendor 路径），但换克隆目录、重装系统组件或卸载本输入法都不再丢失 6.2GB 模型。行为要点：

- 缓存中分档已完整（配置和全部权重分片存在）→ 跳过下载，只重建 vendor 符号链接；链接已正确指向完整缓存 → 完全跳过。
- 全新下载先落缓存内临时目录（`.part-<pid>`），成功后原子定稿；并发克隆互不覆盖，失败/中断自动清理。
- 旧布局（vendor 下真实目录）会在下次运行时自动迁入缓存并在原位留符号链接（迁移失败则保留原状，不阻断安装）。
- 自定义位置：`VOICE_IME_MODEL_CACHE=/path/to/models ./scripts/setup-qwen-asr.sh`；多克隆共用同一缓存即可零重复下载。

不加 `VOICE_IME_ENABLE_QWEN_ASR=0` 时，安装器验收成功会默认激活 1.7B 并重启 IBus。**准备环境、写入配置、桌面激活、真实热键语音是四种不同状态**：switch 的重启/引擎选择失败会非零退出，保留已写配置与已验收环境，不能声称桌面就绪。旧 JSON 或 environment.d 中显式指定的外部解释器不会被修复模式覆盖；完整迁移激活需检查这些覆盖并运行 switch，再执行只读验收。

### 下载源

下载源互不替代：

- **pip 依赖包**：默认清华；`VOICE_IME_PIP_INDEX_URL=default` 明确使用 `https://pypi.org/simple`，忽略用户 pip.conf 和额外索引；可按次传入自己的包源 URL。
- **CPython**：uv 的官方 GitHub python-build-standalone 发布；可设置 `UV_PYTHON_INSTALL_MIRROR` 为兼容的 HTTPS 镜像根地址。清华 pip 源不提供解释器。
- **uv 自举**：PyPI 固定版本元数据与 wheel，通过发布 SHA256、解压大小及 `uv --version` 校验；元数据源可用 `VOICE_IME_UV_METADATA_URL` 指定可信兼容 HTTPS JSON 源。SHA256 是来源一致性校验，不是独立签名认证。

自动缓存均在仓库（`.python`、`.tools`、`.cache/uv`），拒绝这些目录的符号链接。损坏的缓存 uv 会明确失败：检查并移走项目内损坏的二进制再重试，不会静默执行坏缓存或回退系统 Python。uv 自举总下载时限 180s、CPython 安装 600s；失败不会提前替换旧 venv。

### 验收链路与故障恢复

安装阶段依次检查：目标解释器和 `Python.h` 的实际编译 → pip 依赖一致性 → CUDA 张量运算和 Triton 驱动辅助模块编译/加载 → 模型配置及全部权重分片 → 短音频推理。短音频为静音，只验证执行链路，不验证识别准确率。GPU 显存不足时可使用现有 0.6B 自动降档，输出显示实际验收模型。

- 应用依赖基线在 [requirements-qwen-asr.txt](requirements-qwen-asr.txt)；不是跨平台 CUDA 锁文件。torch/Triton 仍需匹配驱动，由运行验收兜底。已有解释器和基线匹配时不盲目升级包；实际版本记录在 venv 的 `installed-requirements.txt` 中。
- 重建失败时恢复原 venv，失败环境和旧环境备份保留供排查，不删除模型或用户配置。使用文件锁阻止并发安装；拒绝修改指向其他项目的 venv 符号链接。安装中的 venv 重命名会写入 journal（`.qwen-asr-setup.state`）：SIGKILL/断电中断后，下次安装自动恢复原环境并把半成品移入 `.failed-crash-*` 留档。
- `--force` 只允许无 GPU 时准备调试环境：不做 GPU 推理验收、不启用后端、不宣称语音就绪。
- doctor 检查实际 sidecar 解释器、编译条件、CUDA 运算、Triton 和模型分片；不会默认加载整个模型。完整推理验收使用 `--verify-only`。[doctor](scripts/doctor.sh) 的 `fix` 对缺头文件/旧 venv 调用同一个新 setup（每次修复只调用一次），保留当前渠道及显式覆盖，修复成功可能重启 IBus，也可能修复其他资产；仅修复 Qwen 且不动桌面请用上面的 `VOICE_IME_ENABLE_QWEN_ASR=0` 命令。
- **验收 receipt 降噪**：smoke 验收或 `--verify-only` 成功后，安装器把当时的解释器、`requirements-qwen-asr.txt` 的 sha256 和模型文件清单（名称/大小/mtime）写入 `.venv-qwen-asr/verify-receipt.json`（附加产物，写失败不影响验收结果）。doctor 在 GPU 正常路径上做快速 stat 对比：三项均与当前实际一致时，原「模型目录存在但不能证明推理可用」WARN 降为一条 `[INFO]`（不计 WARN，附最近验收时间与强制复验命令）；receipt 缺失、依赖文件变化、解释器或模型文件有变动时恢复原 WARN。
- doctor 的语音段只检查 config.json 生效的 `asr.backend`。`backend=qwen3-asr` 而本机无可用 NVIDIA GPU（none/partial）属于环境与所选后端不匹配：体检**如实 FAIL**，但明确指引「换云端 `./scripts/switch-siliconflow-asr.sh`（自备 API Key）或仅键盘」，并跳过注定失败的 CUDA 运行时验收；`fix` 此时也不会盲目重装本地 Qwen（装不出来），只复述出路。仅装键盘（`--keyboard-only` / `--no-voice`）不改 backend——backend 表示「语音若启用走什么」，体检按 qwen 检查属预期（init 汇总会提示这句）。

### sidecar 运行时

- 识别/预热超时只回收本输入法拥有的 sidecar，外部服务交给其管理者处理；下一次听写可重新启动。总处理 watchdog 恢复 UI 空闲并丢弃迟到结果，可按次设置 `VOICE_IME_PROCESSING_TIMEOUT`（秒，最大 1800）。它不是任意 Python 工作线程的强制取消器。录音停止采用有界读、TERM→KILL→wait 和非管道 stderr 诊断；成功返回前必须封口 WAV。
- `/health` 的 `idle/ready/error` 区分懒加载/空闲卸载、模型就绪、加载失败；HTTP 200 只代表服务存活。`/warm` 只有模型真正加载成功才返回成功。稳定加载故障会冷却重试；显存不足不缓存，释放显存后可立即再试。

## 键盘输入链路（Rime）

默认键盘后端为项目内置的 `librime`，直接复用 Rime 的拼音切分、词库、用户词频学习、候选排序、翻页、模糊音/方案配置等能力；语音输入仍由本项目提供。

运行时默认不读取系统 `ibus-rime/fcitx-rime` 的库和配置：

- 内置库：`vendor/rime/lib/librime.so.1`
- 内置词库/方案：`vendor/rime/share/rime-data`
- 内置部署数据：`vendor/rime/build`
- 本项目用户词频：`~/.local/share/ibus-voice-ime/rime-user`

内置 Rime 运行时如需重新生成：

```bash
cd <仓库克隆目录>
./scripts/vendor-rime-runtime.sh
./scripts/test-rime-runtime.sh
```

大词库部署（雾凇拼音 rime-ice + zhwiki + moegirl，默认方案 `rime_ice`；`./init.sh` 已默认包含；脚本自动用内置 librime 编译词库，首次约 1-3 分钟）：

```bash
./scripts/setup-rime-ice.sh                   # 默认走直连，网络不通可加代理
./scripts/setup-rime-ice.sh --proxy http://127.0.0.1:7890
./scripts/setup-rime-ice.sh --no-zhwiki --no-moegirl   # 只要雾凇核心词库
```

未部署雾凇时，引擎自动回退内置 `luna_pinyin_simp`，不会崩溃。

### 中英文切换与标点

按一下左/右 `Shift` 在中文/英文模式间切换；英文模式下按键直接传给应用。从中文切到英文时，如果当前有未提交的拉丁输入码（例如 `ZSH`），会先按原样提交到光标位置。可用 `VOICE_IME_SHIFT_TOGGLE_ASCII=0` 关闭此行为。

默认 `/` 不转换成 `、`，而是直接作为 ASCII slash 传给应用（方便输入 pi 命令如 `/new`）。恢复 Rime 原始标点行为：`export VOICE_IME_ASCII_SLASH=0`。

### 键盘相关环境变量全表

```bash
export VOICE_IME_KEYBOARD_BACKEND=rime      # 默认；设为 demo 可回到 core.py 演示词库（src/ibus_voice_ime/core.py）
export VOICE_IME_SHIFT_TOGGLE_ASCII=1        # 默认；按一下 Shift 切换中/英
export VOICE_IME_START_ASCII=0               # 默认；设为 1 启动后先进入英文模式
export VOICE_IME_RIME_SCHEMA=rime_ice        # 默认；未部署雾凇时自动回退 luna_pinyin_simp
# 以下 Rime 路径为示例，<repo> 指仓库克隆目录；不设置时引擎自动使用仓库内置路径（任意位置克隆均可）
export VOICE_IME_RIME_LIBRARY=<repo>/vendor/rime/lib/librime.so.1
export VOICE_IME_RIME_SHARED_DATA_DIR=<repo>/vendor/rime/share/rime-data
export VOICE_IME_RIME_STAGING_DIR=<repo>/vendor/rime/build
export VOICE_IME_RIME_USER_DATA_DIR=~/.local/share/ibus-voice-ime/rime-user
export VOICE_IME_CLIPBOARD_HOTKEYS=Ctrl+Alt+P # 输入法内粘贴热键；默认另有外部键盘粘贴兜底
export VOICE_IME_CANDIDATE_UI=popup         # 默认；始终使用传统 IBus/GNOME 竖向候选框；如需行内候选可显式设为 inline
export VOICE_IME_PREEDIT_MIRROR=off         # 默认关闭候选框内“输入码：...”镜像，保持传统候选框样式
```

### 拼音模糊音

模糊音（`z/s/c` 与 `zh/sh/ch`、`an/ang`、`en/eng`、`in/ing`、`n/l`、`f/h`）由 **rime-ice 原生提供**，通过 `vendor/rime/share/rime-data/rime_ice.custom.yaml` 启用，由 Rime 语言模型统一排序。（默认禁用，经实测候选污染严重；如需启用编辑 rime_ice.custom.yaml）。关闭某组模糊音：编辑该文件在对应行前加 `#`，然后重新部署（`./scripts/setup-rime-ice.sh` 或 `ibus restart`）。

### 中文常用词记忆

除 Rime 自身用户词库外，还有一层确定性中文候选记忆：用 `Space` / `1-9` 提交中文候选后记录「输入码 -> 中文词」；下次输入同样编码时，已提交过的中文词作为 `常用` 候选排在前面；频率越高、越新使用，排序越靠前。词库默认保存到 `~/.local/share/ibus-voice-ime/chinese.json`，可用 `VOICE_IME_CHINESE_USER_DICT` 修改。

### 英文常用词记忆

支持把常用英文 token 记为候选词：第一次输入 `opencode`，按 `Enter` 直接提交并记入英文词库；下次输入 `opencode` 或前缀 `open...` 时出现英文候选；不像拼音的英文词候选排前，可直接 `Space` 提交。词库默认保存到 `~/.local/share/ibus-voice-ime/english.json`，可用 `VOICE_IME_ENGLISH_USER_DICT` 修改。

### demo 后端

设置 `VOICE_IME_KEYBOARD_BACKEND=demo` 时使用 `src/ibus_voice_ime/core.py` 里的最小演示词库（`nihao`+Space -> `你好`、`zhongwen`+Space -> `中文`）。自定义核心建议优先通过 Rime 方案/词库配置实现；core.py 仅作为无 Rime 时的兜底原型。

## 键盘粘贴 / 防禁粘贴（内部机制）

热键：`Ctrl+Alt+P`。粘贴只有一条链路：默认安装把 `Ctrl+Alt+P` 注册为 GNOME 全局快捷键，执行 `./scripts/clipboard-paste.sh`。脚本读取桌面剪贴板（**xclip 经 XWayland 优先**，wl-paste/GTK/xsel 逐级回退）、显示"📋 正在粘贴……"通知、把文本写入暂存文件后立刻通过输入法 IPC（`paste-file`）通知 `engine.py` 并退出；`engine.py` 等待约 300ms（与语音提交同节奏）后走语音识别结果同款 `commit_text()` 提交路径，光标附近显示「📋 正在粘贴……」辅助提示条（与「正在录音」同一 IBus 通道）。依赖：Fedora `sudo dnf install xclip`；Debian/Ubuntu `sudo apt install xclip`（X11 回退工具无需手动安装）。

> **为什么 xclip 优先**：Fedora 44 GNOME (Wayland) 下，粘贴瞬间新建的原生 Wayland 剪贴板客户端（wl-paste）会触发 compositor 级输入上下文焦点抖动（FocusOut 风暴 + 引擎实例重建），随后的 `commit_text` 投递全部静默丢失——这是该功能自某次系统升级后失效的根因；语音链路不碰剪贴板，故从未受影响。XWayland 的剪贴板转发由常驻 X 服务承担，读取不新建 Wayland 客户端（实测零抖动）。完整复盘见 [docs/paste-postmortem.md](docs/paste-postmortem.md)。

粘贴链路故障排查：`./scripts/diagnose-paste.sh` 按三个检查点（剪贴板→暂存文件 / 文件→引擎 / 引擎→应用）逐段定位，并在引擎→应用一段用单行/多行、200ms/1000ms 延迟做差分测试，自动给出结论。

配置：

```bash
export VOICE_IME_CLIPBOARD_PREPARE_DELAY_SECONDS=0.3  # 提交前等待（默认与语音同节奏）
export VOICE_IME_CLIPBOARD_PREPARE_DELAY_MS=300       # 同上，毫秒配置；设置后优先于 seconds
export VOICE_IME_CLIPBOARD_NOTIFY=1                   # 触发时的桌面通知开关（诊断用）
export VOICE_IME_CLIPBOARD_MAX_CHARS=20000            # 最大提交字符数
```

## 语音输入链路（内部机制）

流水线：

```text
录音 wav -> STT 初稿 -> 规则清理 ->（可选：云端 LLM 润色，JSON 配置开启）-> 提交到当前光标
```

热键模型：默认 `Ctrl+Alt+V` 语音输入；`Ctrl+Alt+B`「原文语音输入」走同一录音/识别链路，但识别结果**绝不经过 LLM 后处理**（无论文本多长），只做确定性规整。两组热键都可在配置中自定义（`recording.hotkeys` / `recording.raw_hotkeys`，或环境变量 `VOICE_IME_HOTKEYS` / `VOICE_IME_RAW_HOTKEYS`）：修饰键支持 Ctrl / Alt / Shift / Super 任意组合加单字母，例如 `Super+V`、`Ctrl+Shift+B`；裸字母（如 `v`）等价于 `Ctrl+Alt+v`。同修饰键同字母的热键会互相去重（主热键与粘贴热键优先），GNOME 全局兜底快捷键也会跟随配置自动注册。

部分终端程序不把 `Ctrl+Alt+V` 转发给 IBus；安装脚本额外注册 GNOME 用户级全局快捷键，把同一个 `Ctrl+Alt+V` 转发给输入法进程（`Ctrl+Alt+B` 同样注册了 GNOME 兜底绑定）。

默认 toggle 模式：按一次开始录音，再按一次停止、识别并提交到当前光标；录音中按**任意一个**语音热键（V 或 B）都会停止录音。fixed 模式（`VOICE_IME_TRIGGER_MODE=fixed`，固定时长录音）下 V/B 差异同样保留：B 键触发的识别结果依旧跳过 LLM 后处理与云端语义平滑（volc-bigmodel 后端的 DDC），只做确定性规整。状态弹窗默认关闭。

### 八种 STT 后端

1. `VOICE_IME_ASR_CMD` 自定义命令，命令 stdout 作为识别文本：
   ```bash
   export VOICE_IME_ASR_CMD='my-asr-command {wav}'
   ```
2. `Qwen3-ASR` sidecar（默认，推荐测试 2026 新中文/多语 ASR）：
   ```bash
   ./scripts/setup-qwen-asr.sh   # 下载 Qwen3-ASR-0.6B 和 1.7B，并默认启用 1.7B
   ./scripts/switch-qwen-asr.sh 0.6b
   ./scripts/switch-qwen-asr.sh 1.7b   # 默认
   ```
3. `MiMo-V2.5-ASR` sidecar（小米 MiMo，大模型，需独立源码、Audio Tokenizer、较大显存/内存）：
   ```bash
   ./scripts/setup-mimo-asr.sh   # 下载 XiaomiMiMo/MiMo-V2.5-ASR 和 MiMo-Audio-Tokenizer，并默认启用
   ./scripts/switch-mimo-asr.sh
   ```
4. `MiMo 云端 ASR / Token Plan`（OpenAI-compatible API，避免本地大模型显存占用，适合无 NVIDIA GPU 的机器）：
   ```bash
   # Token Plan 中国集群；也可用 sgp / ams / payg / 完整 Base URL
   VOICE_IME_MIMO_API_KEY='tp-xxxxx' ./scripts/switch-mimo-cloud-asr.sh cn
   ```
   API Key 推荐通过 BWS 运行时注入 `XIAOMI_TOKEN_PLAN_CN_API_KEY`，不要写入文档或提交到仓库。
5. `faster-whisper`（稳定兜底/诊断后端；显式切换时默认使用 NVIDIA GPU/CUDA STT）：
   ```bash
   ./setup-asr.sh
   ./scripts/check-gpu-stt.sh
   ```
6. `vosk`：安装 `vosk` 并设置模型目录：
   ```bash
   export VOICE_IME_VOSK_MODEL=/path/to/vosk-model-cn
   ```
7. `火山引擎豆包 bigmodel ASR（极速版）`（异步提交+轮询，base64 直传本地音频，资源 ID `volc.bigasr.auc_turbo`）：
   ```bash
   VOICE_IME_VOLC_API_KEY='xxxxx' ./scripts/switch-volc-bigmodel-asr.sh
   ```
8. `硅基流动 SiliconFlow ASR`（**Beta，未经深度测试**，边界与回退方式见 [docs/asr-backends.md](docs/asr-backends.md#硅基流动-siliconflow-asr低门槛云后端beta)；`POST /v1/audio/transcriptions` multipart 直传本地音频，默认模型 `FunAudioLLM/SenseVoiceSmall` 官方定价页标注**免费**，无需 GPU，`api.siliconflow.cn` 大陆直连）：
   ```bash
   # 注册 https://cloud.siliconflow.cn 创建 API Key（sk-xxx）；
   # 单文件 <=50MB、时长 <=1h；有 RPM/TPM 限流（429 稍后重试）；
   # 计费以控制台账单为准：SenseVoiceSmall 官方标注免费；
   #   Qwen/Qwen3-ASR-1.7B 按音频时长计费（约 ¥0.00022/秒 ≈ 0.0132 元/分钟）
   # 换模型（如 Qwen3-ASR-1.7B）：VOICE_IME_SILICONFLOW_MODEL='Qwen/Qwen3-ASR-1.7B'
   VOICE_IME_SILICONFLOW_API_KEY='sk-xxxxx' ./scripts/switch-siliconflow-asr.sh
   ```

默认本地 sidecar 细节：模型从 `vendor/models/qwen3-asr/` 加载，首次语音时由 engine 进程自动拉起本地 HTTP sidecar（端口 18081，bf16，cuda:0）。

GPU 验证：

```bash
cd <仓库克隆目录>
./scripts/check-gpu-stt.sh
# 如需用大模型验证 GPU 链路：VOICE_IME_WHISPER_MODEL=large-v3 ./scripts/check-gpu-stt.sh
```

faster-whisper 兜底后端：引擎检测到 `.venv` 会优先使用，创建方式：

```bash
python3 -m venv .venv --system-site-packages
.venv/bin/pip install -r requirements-asr.txt
```

### 配置优先级与常用设置

常用语音/STT 配置统一收敛到 `~/.config/ibus-voice-ime/config.json`。优先级一句话：**环境变量 > 用户 config.json > 仓库 `config/defaults.json` > 代码内默认**（临时覆盖仍可用 `VOICE_IME_*` 环境变量）。全部 200+ 配置键的默认值、对应环境变量名与说明见 [docs/configuration.md](docs/configuration.md)。

```bash
PYTHONPATH=src python3 -m ibus_voice_ime.config set asr.backend siliconflow-asr   # 切换 STT 渠道
PYTHONPATH=src python3 -m ibus_voice_ime.config set llm.prompts.system '你是我的听写整理助手……'  # 自定义后处理提示词
PYTHONPATH=src python3 -m ibus_voice_ime.config set recording.max_seconds 120     # toggle 模式最长录音 2 分钟
```

API Key 等密钥仍走环境变量 / BWS 运行时注入，不写入 config.json。老用户迁移：environment.d 里的渠道行仍生效但会压过 config.json——重跑 `./install.sh` 自动迁移；switch 脚本每次运行也会自动清理这些旧渠道行。

### VAD / 静音自动停止（fixed 模式可选）

开始说话后静音约 1 秒自动结束；VAD 录音失败回退固定时长录音：

```bash
export VOICE_IME_VAD_AUTO_STOP=1
export VOICE_IME_MAX_RECORD_SECONDS=30
export VOICE_IME_MIN_RECORD_SECONDS=0.4
export VOICE_IME_SILENCE_STOP_MS=1000
export VOICE_IME_NO_SPEECH_TIMEOUT=2.5
export VOICE_IME_VAD_RMS_THRESHOLD=500
export VOICE_IME_VAD_FALLBACK_FIXED=1
```

### 录音设备与降噪链

录音默认走**系统默认音源**（PipeWire/Pulse 兼容性最好）。如需绕开默认源漂移（蓝牙抢占等），可直采指定 ALSA 卡，例如作者的 MOTU M2（`plughw:M2,0`；M2 为 48k 设备，`plughw` 前缀自动重采样到 16k，不能写 `hw:M2,0`）。设备名用 `arecord -l` 查询；配置的设备不存在时引擎记录日志并自动回退系统默认源：

```bash
export VOICE_IME_ARECORD_DEVICE="plughw:M2,0"   # 示例：直采 MOTU M2；空值/"default" = 系统默认源（默认）
```

录音 WAV 送 ASR 前经过分级降噪链（`VOICE_IME_DENOISE_TIER`，默认 `rnnoise`）：

| 档位 | 链路 | 延迟 | 说明 |
|---|---|---|---|
| `none` | 旧行为：highpass + 可选 sox noisered + normalize | — | 兼容保留 |
| `notch` | 纯 sox：highpass 80 + 50/100/150Hz 工频陷波 + normalize | ~5ms | 零新依赖；稳态 -39dB@50Hz，语音频段零损耗 |
| `rnnoise` | ffmpeg：陷波 + RNNoise(bd) → sox normalize | ~130ms | 默认档；缺 ffmpeg/模型自动降级 `notch` |

- 结论依据（2026-08-26 实测）：电容麦底噪 99% 为 50Hz 工频哼声；Qwen3-ASR 对残余底噪鲁棒（原始/陷波/RNNoise 三版识别逐字一致），降噪的作用是恢复「电容麦可用性」而非提升识别率；无需引入大型神经降噪模型（延迟/常驻进程代价不划算）。
- RNNoise 模型在 `vendor/models/rnnoise/bd.rnnn`（该目录不进 git），换机器后执行 `./scripts/fetch-rnnoise-model.sh` 下载。
- 谱减类 sox `noisered` 保持默认关闭（`VOICE_IME_AUDIO_DENOISE=0`）：同族的谱减方案实测伤齿音（4-10kHz -3.1dB），已由陷波链取代。
- 陷波深度的验收必须看稳态（尾部 RMS）：IIR 滤波器起始瞬态几乎直通，时域峰值口径会把 -30dB 低估成 -4dB。

### 规则清理 / 语音命令

默认规则清理：空白归一化、去掉独立口头禅、把常见语音标点口令转成符号；不再启用本地自动补标点规则。LLM 开启后仍可能尝试更自然的标点润色。

```bash
export VOICE_IME_VOICE_COMMANDS=1
export VOICE_IME_REMOVE_FILLERS=1
export VOICE_IME_AUTO_PUNCTUATION=0  # 本地自动补标点已禁用
export VOICE_IME_INLINE_VOICE_COMMANDS=0  # 默认只识别独立/标点分隔的口令，避免误把普通文本里的“换行”等变成操作
export VOICE_IME_CHINESE_SCRIPT=simplified # 默认最终提交前强制繁转简；设为 none 可关闭
```

支持的常用口令：`换行`、`空一行`、`逗号`、`句号`、`问号`、`冒号`、`斜杠`、`空格` 等。为避免越权操作，默认只处理独立口令或由空格/标点分隔的口令；恢复旧的全文替换行为可设 `VOICE_IME_INLINE_VOICE_COMMANDS=1`。

### 繁简转换

语音识别后端通常只能限制为“中文”，不能保证输出简体。项目默认在语音最终提交前用系统 OpenCC 做兜底转换：

```bash
export VOICE_IME_CHINESE_SCRIPT=simplified  # 默认：繁体转简体
export VOICE_IME_CHINESE_SCRIPT=none        # 不做繁简转换
export VOICE_IME_CHINESE_SCRIPT=traditional # 可选：简体转繁体
```

默认使用系统配置 `/usr/share/opencc/t2s.json`；自定义可设 `VOICE_IME_OPENCC_T2S_CONFIG=/path/to/t2s.json`。LLM 后处理开启时也会被提示默认输出简体中文，但最终仍以 OpenCC 兜底转换为准。

### LLM 云端后处理（OpenAI 兼容接口，可选）

STT 之后的文本润色（补标点、**整理成规范 Markdown**、去口水词、修错字）可交给任意**云端大模型**，只要它提供 OpenAI 兼容的 `chat/completions` 接口。这是**唯一受支持的开启方式**：一个用户自有的 JSON 配置文件，填三样东西——网址、API Key、模型 ID。

```bash
./scripts/setup-llm-cloud.sh        # 交互式生成配置，可选当场连通性测试
```

生成的配置文件在 `~/.config/ibus-voice-ime/llm.json`（权限 0600，字段模板见 `examples/llm-cloud.json.example`）：

```json
{
  "enabled": true,
  "base_url": "https://api.your-provider.com/v1",
  "api_key": "sk-...",
  "model": "the-exact-model-id",
  "timeout": 15,
  "temperature": 0.1,
  "max_tokens": 1024,
  "min_chars": 50,
  "extra_body": {"thinking": {"type": "disabled"}}
}
```

设计约定（最简方案换最大可靠性）：

- **短句不整理**：默认 `min_chars: 50`——不足 50 字的听写直接提交规则清理结果，不为几个字付云端延迟和费用。`Ctrl+Alt+B` 触发的听写则**完全跳过** LLM 后处理，与长度无关。
- **Markdown 输出（面向 AI 消费）**：整理结果默认是规范 Markdown，且**任务/指令类听写会整理成智能体任务简报**——`## 目标` / `## 背景` / `## 任务`（编号步骤）/ `## 约束` / `## 验收`，只为听写中实际存在的部分生成小节；叙述/讨论类内容按主题分节或保持自然段落，不强行套简报；命令/路径/代码用行内代码或代码块。文字内容本身不改，尤其**不会自作主张追加用户没说过的要求**（标题和目标只能来自原文关键词）。提示词在 `src/ibus_voice_ime/text/llm_postprocess.py`（`SYSTEM_PROMPT` / `MODE_INSTRUCTIONS` / `_build_messages`），可按需手工调整，改完重启引擎生效。
- **不做模型列表查询**：不会请求 `/models` 帮你挑模型。`model` 必须填与服务商**完全一致**的 ID；填错的后果是第一次调用返回明确的 HTTP 404/400 错误（错误信息会提示检查 `base_url` 是否以 `/v1` 结尾、model ID 是否正确），而不是静默选错模型。
- **服务商特有参数走 `extra_body`**：该字段内容会合并进请求体。典型用途是思考型模型关思考（输入法后处理要快而直接）——例如 GLM 系列：`"extra_body": {"thinking": {"type": "disabled"}}`（实测同一请求从 15 秒超时降到约 2 秒）；不关思考的模型若返回空结果并提示 `finish_reason=length`，就是思考耗尽了 `max_tokens`。
- **JSON 配置优先于环境变量**：文件存在且合法时，`run-engine.sh` 里的 `VOICE_IME_LLM_*` 默认值（本地 sidecar 地址、4 秒超时等）不会泄漏进云端调用。
- **即刻生效**：引擎在每次语音后处理前重新读取该文件，改完配置无需重启输入法。把 `enabled` 改为 `false` 即关闭。
- **信任模型输出**：LLM 返回的 Markdown **原样提交**——只做"去包裹"（整体被代码围栏/引号包住时解开），不再做机械的标点/空格重排（重复标点折叠、CJK-拉丁空格删除等）。那些确定性规则只服务于无 LLM 的回退链路，接在模型后面反而会破坏刻意排的格式。繁简方向仍以 OpenCC 兜底转换为准。
- **失败兜底**：接口错误/超时/返回异常时自动回退提交原始识别文本，语音输入永远不会因为 LLM 挂了而断掉。
- **密钥边界**：该文件只存在于你的 `~/.config`（已加入 `.gitignore` 双保险），绝不入库；`./scripts/doctor.sh` 会检查它的 JSON 合法性与文件权限（0600）。

历史包袱说明：早期的本地小模型路径（llama.cpp sidecar + Qwen3.5-0.8B GGUF，`scripts/setup-llm.sh`）实测会改坏听写原文（改词、吞字、加不存在内容），已被云端方案取代且默认压制——`run-engine.sh` 仍会把遗留的 `VOICE_IME_LLM_POSTPROCESS`/`VOICE_IME_LLM_INTERNAL` 环境变量钉死为 0。仅设置环境变量开启不了本地 LLM 后处理。

### 语音模式

```bash
export VOICE_IME_VOICE_MODE=dictation  # 普通听写/聊天：只做最小清理
export VOICE_IME_VOICE_MODE=literal    # 原样/命令/代码：尽量少改写
export VOICE_IME_VOICE_MODE=markdown   # 仍只做最小清理，除非原文已明显是 Markdown
export VOICE_IME_VOICE_MODE=prompt     # 仍只做最小清理，不主动强化提示词
export VOICE_IME_VOICE_MODE=command    # 保留命令、路径、参数
```

### 自定义词典（热词）

自定义词典会同时提示 Whisper、LLM，以及火山 bigmodel ASR（作为热词直传），用来提高技术词/人名/项目名准确率：

```bash
mkdir -p ~/.local/share/ibus-voice-ime
$EDITOR ~/.local/share/ibus-voice-ime/voice-dictionary.txt
# 可选：export VOICE_IME_VOICE_DICTIONARY=/path/to/voice-dictionary.txt
# 可选：export VOICE_IME_WHISPER_PROMPT='Pi, Rime, faster-whisper, whisper.cpp'
```

词典格式为三列，竖线分隔：`标准词 | 别名 | 常见误识别`。三列用途不同：

- **第1列（标准词）**：火山热词直传（软加权，纠正发音完整的生僻词）
- **第2列（别名）**：火山热词直传
- **第3列（常见误识别）**：火山热词直传 + **本地确定性整词替换**（强制纠正发音相近的误识别，如 `Rime | | Rim` 会在识别出独立 `Rim` 时替换成 `Rime`）

示例：

```
Open Code | opencode | open cold
Rime | | Rim
pi
```

热词直传处理「发音对但模型不敢输出」的词；第3列替换层处理「发音相近但模型听错」的词（如 Rim→Rime）。替换层只匹配独立的整词（中英文/标点/空格为边界），不会误伤 `Rims`/`XRim` 这类子串。可用 `VOICE_IME_VOICE_REPLACEMENTS=0` 关闭替换层。

环境变量对 IBus 生效的写入方式：`~/.profile`、`~/.config/environment.d/*.conf`，或 `systemctl --user set-environment ...` 后重启 IBus。

## 故障排查细节

第一步永远是项目体检 + 看引擎日志：

```bash
./scripts/doctor.sh                    # 体检：半残态/断链热键/缺失资产等，只检查
./scripts/doctor.sh fix                # 自动修复（含词库/模型下载、重注册）
tail -f ~/.local/share/ibus-voice-ime/engine.log
```

日志共三处，按问题层次查：

```bash
tail -f ~/.local/share/ibus-voice-ime/engine.log          # 引擎主日志（键盘/录音/提交链路）
tail -f ~/.local/share/ibus-voice-ime/error.log           # 引擎错误日志
tail -f ~/.local/share/ibus-voice-ime/qwen-asr-server.log # 本地 Qwen3-ASR sidecar（模型加载/CUDA 问题在这里）
```

日志默认保留**最近 1000 行**（引擎启动时与写入过程中自动裁剪，防止长期使用日志无限增长；报 bug 时直接附上整个文件即可）。可用 `VOICE_IME_LOG_KEEP_LINES=N` 调整，设为 `0` 关闭裁剪。

预检与 [doctor](scripts/doctor.sh) 的最终汇总逐条列出 WARN 的标题、实际影响和处理命令/可忽略条件；doctor 总 WARN 包含标注「[环境预检]」的预检项，只有 FAIL 会使体检非零退出。

常见问题（细节版）：

- **内置 librime 加载失败 / 键盘无候选**：`vendor/rime/lib/` 的二进制是 Fedora x86_64 上构建的（需 glibc ≥ 2.38），其他环境加载失败时**引擎会自动回退系统 librime**并在日志中说明；若系统也没装，安装后重启输入法即可（Fedora：`sudo dnf install librime`；Debian/Ubuntu：`sudo apt install librime`）。显式指定时把以下三行写进 `~/.config/environment.d/ibus-voice-ime.conf`（临时试验也可在启动前 export，引擎对显式设置继承优先）：
  ```bash
  export VOICE_IME_RIME_LIBRARY=/usr/lib64/librime.so.1        # Debian 系在 /usr/lib/x86_64-linux-gnu/
  export VOICE_IME_RIME_SHARED_DATA_DIR=/usr/share/rime-data
  export VOICE_IME_RIME_STAGING_DIR=~/.local/share/ibus-voice-ime/rime-build
  ```
- **语音输入没反应 / 录音失败**：`arecord -l` 确认有采集卡；日志出现「已回退系统默认录音源」属正常降级；确认没有其他程序独占麦克风。
- **首次按 Ctrl+Alt+V 后等很久**：Qwen3-ASR sidecar 首次启动要把模型加载进显存，视盘速 30 秒到数分钟；之后有常驻/预热机制。
- **无 NVIDIA GPU**：本地后端不可用，切换云端：`./scripts/switch-mimo-cloud-asr.sh cn`（小米 MiMo）或 `./scripts/switch-volc-bigmodel-asr.sh`（火山引擎豆包），均需自备 API Key。
- **非 GNOME 桌面**：`install.sh` 的 gsettings 输入源注册与热键脚本不适用。手工集成思路：运行 `engine.py --xml` 生成组件描述文件放进 IBus 扫描路径（`IBUS_COMPONENT_PATH`），热键用桌面自己的全局快捷键机制调 `scripts/voice-toggle.sh`。
- **Ctrl+Alt+V 在某些终端/应用无效**：部分工具链不把该组合键转发给 IBus，安装脚本已注册 GNOME 全局快捷键兜底；其他桌面需自行绑定。
- **候选质量一般**：跑 `./scripts/setup-rime-ice.sh` 部署雾凇拼音大词库；不跑则回退内置 `luna_pinyin_simp`。

## 卸载细节

`uninstall-all.sh` 会清理：IBus 组件注册、GNOME 输入源/引擎列表（四个 gsettings 键）、自定义快捷键（含 dconf 子树）、environment.d 与 systemd 用户管理器/DBus 激活环境、蓝牙恢复守护服务、登录 autostart、用户数据/配置/运行时目录，并重启 IBus；结束后做残留验证扫描，任何残留以非零退出码报警。跨项目共享缓存（`~/.triton/cache`、`~/.cache/pip`、`~/.cache/modelscope`、`~/.modelscope`）默认不在删除范围。

两个可选深清理 flag：

- `--purge-models`：额外删除 Qwen 模型共享缓存（默认整个 `~/.cache/ibus-voice-ime`；候选位置从当前环境、environment.d 与 systemd 用户管理器环境解析 `VOICE_IME_MODEL_CACHE`，HOME 外路径按安全边界拒绝并提示手动处理）。
- `--purge-repo`：额外删除脚本所在的仓库克隆本身（`.venv*`、`.python`、`.tools`、`.cache/uv`、`vendor/` 等全部下载产物一并清除）。**仅当克隆带 `.git/ibus-voice-ime-bootstrap` 引导器标记且含 `setup.sh`/`install.sh` 身份文件时执行**——即 `remote-install.sh` curl 安装的克隆；普通开发克隆拒绝删除并列为残留。

### remote-uninstall.sh 远程卸载引导器

`curl -fsSL .../remote-uninstall.sh | bash` 在任意目录可用：定位克隆（环境变量与 `remote-install.sh` 同名同义，`IBUS_VOICE_IME_DIR` 默认 `~/.local/share/ibus-voice-ime-repo`），默认即「彻底清空回到安装前状态」。三段式流程：① 确认后先把**引导器管理的克隆**（带 `.git/ibus-voice-ime-bootstrap` 标记）更新到远端最新版——克隆即将删除，安全要求低于 remote-install.sh，但绝不 reset 开发克隆；更新失败回退本地版本，不阻断。② **版本探测**（必须在更新之后）：运行 `--help` 检查卸载脚本是否支持 `--purge-models`，只传兼容参数子集——旧版卸载脚本照样能完成注册/环境/数据清理。③ **兜底删除**：模型共享缓存（候选在卸载脚本删除 environment.d **之前**从当前环境 + environment.d 抓取：`VOICE_IME_MODEL_CACHE` + 默认 `~/.cache/ibus-voice-ime`，仅限 HOME 内路径）与克隆目录本身（标记 + 身份文件判据，开发克隆拒绝）由引导器自己删除，不依赖卸载脚本版本；卸载脚本非零退出时保留克隆供排查。安全设计：绝不 sudo、不碰系统文件与跨项目共享缓存；有终端时要求输入 `yes` 确认，无控制终端（SSH/CI/管道）必须显式 `--yes` 才执行，未确认前零删除。选项：`--yes`、`--keep-user-data`（透传为 keep 模式）、`-h`。本地克隆缺失时尝试从 `IBUS_VOICE_IME_REPO` 推导 GitHub raw 地址下载 `uninstall-all.sh` 兜底；下载失败则报错并给出手动指引。

## 更多文档

- [docs/README.md](docs/README.md)：技术架构与选型理由、语音后端、自研范围、测试平台、已知局限
- [docs/asr-backends.md](docs/asr-backends.md)：各 ASR 后端对比
- [docs/configuration.md](docs/configuration.md)：全部 200+ 配置键
- [docs/paste-postmortem.md](docs/paste-postmortem.md)：粘贴链路失效复盘
