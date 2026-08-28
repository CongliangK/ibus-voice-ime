# IBus 自定义语音输入法原型

Linux 上的 IBus 输入法引擎：键盘输入复用 Rime（librime + 雾凇拼音），语音输入按 `Ctrl+Alt+V` 说话、识别后直接上屏，支持本地 Qwen3-ASR / 云端 MiMo / 火山引擎等多种识别后端；另有 `Ctrl+Alt+B`「原文语音输入」，识别结果不做 LLM 后处理。

深度文档（技术架构与选型理由、语音后端、自研范围、测试平台、已知局限）见 [docs/README.md](docs/README.md)。

## 项目状态与已测试环境（请先读）

**这是一个个人项目，只在我自己的机器上完整验证过，未做广泛的发行版适配。** 具体来说：

- **已验证环境**：Fedora 44 Workstation + GNOME（Wayland）、x86_64、NVIDIA RTX 4080（本地 ASR）、PipeWire 音频栈、MOTU M2 USB 声卡。
- **未验证**：其他发行版（Ubuntu / Arch / Debian…）、其他桌面环境（KDE / X11 / sway…）、ARM 架构、无 NVIDIA GPU 的本地识别。理论上 IBus + GNOME + PipeWire 的组合都能跑，但作者没有实测；在任何机器上遇到问题欢迎提 issue 反馈。
- 仓库内置的 librime 二进制（`vendor/rime/lib/`）复制自 Fedora x86_64 系统，其他环境可能因 glibc/架构不匹配加载失败——届时改用系统 librime 即可（见「故障排查」）。
- 文档中出现的 `plughw:M2,0` 是作者声卡的直采配置**示例**，其他机器不需要也不适用；录音默认走系统默认音源，配置的直采设备不存在时会自动回退。

## 环境要求

| 组件 | 必需性 | 说明 |
|---|---|---|
| IBus | 必需 | 输入法框架本体（Fedora 默认自带） |
| GNOME 桌面 | 强烈建议 | 安装脚本的输入源注册、全局热键均按 GNOME 设计；其他桌面需手工配置组件与热键 |
| Python ≥ 3.10 | 必需 | 引擎本体 |
| PyGObject（`gi` + IBus 内省） | 必需 | Fedora：`sudo dnf install python3-gobject ibus` |
| alsa-utils（`arecord`） | 必需 | 录音主链路 |
| PipeWire（`pw-record`）+ ffmpeg | 建议 | arecord 失败时的兜底录音链路 |
| sox | 建议 | 录音预处理（高通/陷波/归一化），缺失时自动跳过 |
| OpenCC | 建议 | 语音结果繁→简兜底转换 |
| NVIDIA GPU | 本地 ASR 需要 | Qwen3-ASR 1.7B bf16 约需 4GB+ 显存；无 GPU 请用云端后端 |
| glibc ≥ 2.38 | 内置 librime 需要 | 仓库内置的 librime 二进制在 Fedora 39+/Ubuntu 24.04+ 可直接用；更老的系统上引擎会自动回退系统 librime（安装 librime 包即可），见「故障排查」 |

新机器上先跑环境自检（FAIL 项需解决，WARN 项为可选/降级提示）：

```bash
./scripts/check-environment.sh            # 全量
./scripts/check-environment.sh --no-gpu   # 只关心键盘输入链路时
```

## 快速开始

```bash
git clone https://github.com/CongliangK/ibus-voice-ime.git
cd ibus-voice-ime
./init.sh                          # 一次性初始化：词库 + 语音模型 + 降噪，默认全量
# 可选参数（均幂等，已就绪自动跳过）：
#   --no-voice            不安装语音输入（不下载 ASR 模型与 venv）
#   --no-denoise          不要 RNNoise 降噪模型（录音降级 notch 档）
#   --no-rime-ice         跳过雾凇拼音大词库（键盘回退内置 luna）
#   --no-zhwiki --no-moegirl / --proxy http://127.0.0.1:7890 / --skip-install
# 无 NVIDIA GPU 时语音步骤自动跳过，并打印云端后端替代命令。

# Super+Space 切换到「自定义语音输入法」；nihao + Space -> 你好；Ctrl+Alt+V 语音；Ctrl+Alt+B 原文语音（免整理）
```

两个安装脚本的分工：

- **`init.sh`（一次性初始化）**：拿到仓库后的第一步。下载并配置基础资产（rime-ice 词库、本地 Qwen3-ASR 模型与 venv、RNNoise 降噪模型），完成后自动调用 `install.sh` 完成注册。参数可配置装什么。
- **`install.sh`（每次轻量注册）**：把引擎安装进当前电脑会话——注册 IBus 组件、写 environment.d、注册 GNOME 输入源/热键、重启输入法。**不下载任何东西**，重启/重新登录后想恢复激活就再跑一次。

健康检查（体检，不修改不下载）：

```bash
./scripts/doctor.sh        # 只检查并报告
./scripts/doctor.sh fix    # 检查并自动修复（补词库/模型下载、修执行位、重注册热键等）
```

可选：如需 faster-whisper 兜底后端，创建带系统包的 venv 并安装依赖（引擎检测到 `.venv` 会优先使用）：

```bash
python3 -m venv .venv --system-site-packages
.venv/bin/pip install -r requirements-asr.txt
```

## 安装/启用（细节）

本项目使用内置 Rime 运行时：`vendor/rime/`。如需重新生成内置运行时：

```bash
cd ~/ibus-voice-ime
./scripts/vendor-rime-runtime.sh
./scripts/test-rime-runtime.sh
```

推荐部署**雾凇拼音（rime-ice）+ zhwiki + moegirl** 大词库以获得接近商业输入法的候选质量（默认方案为 rime_ice；`./init.sh` 已默认包含此步；脚本会自动用内置 librime 编译词库，首次约 1-3 分钟）。手动（重新）部署：

```bash
./scripts/setup-rime-ice.sh                   # 默认走直连，网络不通可加代理
./scripts/setup-rime-ice.sh --proxy http://127.0.0.1:7890
./scripts/setup-rime-ice.sh --no-zhwiki --no-moegirl   # 只要雾凇核心词库
```

未部署雾凇时，引擎会自动回退到内置的 `luna_pinyin_simp`，不会崩溃。

安装/更新 IBus 组件：

```bash
cd ~/ibus-voice-ime
./install.sh
```

然后用 GNOME 顶栏输入法菜单或 `Super+Space` 切换到 **自定义语音输入法**。

## 键盘输入

默认键盘后端已经改为项目内置的 `librime`，直接复用 Rime 的拼音切分、词库、用户词频学习、候选排序、翻页、模糊音/方案配置等能力；语音输入仍由本项目提供。

运行时默认不读取系统 `ibus-rime/fcitx-rime` 的库和配置：

- 内置库：`vendor/rime/lib/librime.so.1`
- 内置词库/方案：`vendor/rime/share/rime-data`
- 内置部署数据：`vendor/rime/build`
- 本项目用户词频：`~/.local/share/ibus-voice-ime/rime-user`

默认方案：`rime_ice`（需先跑 `scripts/setup-rime-ice.sh`；未部署时自动回退 `luna_pinyin_simp`）

按一下左/右 `Shift` 可以在中文模式和英文模式之间切换；英文模式下按键直接传给应用。从中文切到英文时，如果当前有未提交的拉丁输入码（例如 `ZSH`），会先按原样提交到光标位置。可用 `VOICE_IME_SHIFT_TOGGLE_ASCII=0` 关闭此行为。

默认 `/` 不再转换成 `、`，而是直接作为 ASCII slash 传给应用，方便输入 pi 命令如 `/new`。如需恢复 Rime 原始标点行为：

```bash
export VOICE_IME_ASCII_SLASH=0
```

常用环境变量：

```bash
export VOICE_IME_KEYBOARD_BACKEND=rime      # 默认；设为 demo 可回到 core.py 演示词库（src/ibus_voice_ime/core.py）
export VOICE_IME_SHIFT_TOGGLE_ASCII=1        # 默认；按一下 Shift 切换中/英
export VOICE_IME_START_ASCII=0               # 默认；设为 1 启动后先进入英文模式
export VOICE_IME_RIME_SCHEMA=rime_ice        # 默认；未部署雾凇时自动回退 luna_pinyin_simp
export VOICE_IME_RIME_LIBRARY=~/ibus-voice-ime/vendor/rime/lib/librime.so.1
export VOICE_IME_RIME_SHARED_DATA_DIR=~/ibus-voice-ime/vendor/rime/share/rime-data
export VOICE_IME_RIME_STAGING_DIR=~/ibus-voice-ime/vendor/rime/build
export VOICE_IME_RIME_USER_DATA_DIR=~/.local/share/ibus-voice-ime/rime-user
export VOICE_IME_CLIPBOARD_HOTKEYS=Ctrl+Alt+P # 输入法内粘贴热键；默认另有外部键盘粘贴兜底
export VOICE_IME_CANDIDATE_UI=popup         # 默认；始终使用传统 IBus/GNOME 竖向候选框；如需行内候选可显式设为 inline
export VOICE_IME_PREEDIT_MIRROR=off         # 默认关闭候选框内“输入码：...”镜像，保持传统候选框样式
```

### 拼音模糊音

模糊音（`z/s/c` 与 `zh/sh/ch`、`an/ang`、`en/eng`、`in/ing`、`n/l`、`f/h`）由 **rime-ice 原生提供**，通过 `vendor/rime/share/rime-data/rime_ice.custom.yaml` 启用，由 Rime 语言模型统一排序。（默认禁用，经实测候选污染严重；如需启用编辑 rime_ice.custom.yaml）。

如需关闭某组模糊音，编辑 `rime_ice.custom.yaml` 在对应行前加 `#`，然后重新部署（`./scripts/setup-rime-ice.sh` 或 `ibus restart`）。

### 中文常用词记忆

除了 Rime 自身的用户词库，本项目还加了一层确定性的中文候选记忆：

- 用 `Space` / `1-9` 提交中文候选后，会记录「输入码 -> 中文词」
- 下次输入同样编码时，已提交过的中文词会作为 `常用` 候选排在前面
- 频率越高、越新使用，排序越靠前

用户中文词库默认保存到：

```bash
~/.local/share/ibus-voice-ime/chinese.json
```

可用环境变量修改：

```bash
export VOICE_IME_CHINESE_USER_DICT=~/.local/share/ibus-voice-ime/chinese.json
```

### 英文常用词记忆

支持把常用英文 token 记为候选词：

1. 第一次输入 `opencode`
2. 按 `Enter`：直接提交 `opencode`，并记入英文词库
3. 下次输入 `opencode` 或前缀 `open...` 时，会出现英文候选
4. 对于不像拼音的英文词，英文候选排在前面，可以直接 `Space` 提交

用户英文词库默认保存到：

```bash
~/.local/share/ibus-voice-ime/english.json
```

可用环境变量修改：

```bash
export VOICE_IME_ENGLISH_USER_DICT=~/.local/share/ibus-voice-ime/english.json
```

如果设置 `VOICE_IME_KEYBOARD_BACKEND=demo`，会使用 `src/ibus_voice_ime/core.py` 里的最小演示词库：

- `nihao` + `Space` -> `你好`
- `zhongwen` + `Space` -> `中文`

自定义核心现在建议优先通过 Rime 方案/词库配置实现；`src/ibus_voice_ime/core.py` 仅作为无 Rime 时的兜底原型。

## 键盘粘贴 / 防禁粘贴

热键：`Ctrl+Alt+P`。

粘贴只有一条链路：默认安装把 `Ctrl+Alt+P` 注册为 GNOME 全局快捷键，执行 `./scripts/clipboard-paste.sh`。脚本读取桌面剪贴板（**xclip 经 XWayland 优先**，wl-paste/GTK/xsel 逐级回退）、显示"📋 正在粘贴……"通知、把文本写入暂存文件后立刻通过输入法 IPC（`paste-file`）通知 `engine.py` 并退出；`engine.py` 等待约 300ms（与语音提交同节奏）后走语音识别结果同款 `commit_text()` 提交路径，光标附近显示「📋 正在粘贴……」辅助提示条（与「正在录音」同一 IBus 通道）。依赖：`sudo dnf install xclip`（X11 回退工具无需手动安装）。

> **为什么 xclip 优先**：Fedora 44 GNOME (Wayland) 下，粘贴瞬间新建的原生 Wayland 剪贴板客户端（wl-paste）会触发 compositor 级输入上下文焦点抖动（FocusOut 风暴 + 引擎实例重建），随后的 `commit_text` 投递全部静默丢失——这是该功能自某次系统升级后失效的根因；语音链路不碰剪贴板，故从未受影响。XWayland 的剪贴板转发由常驻 X 服务承担，读取不新建 Wayland 客户端（实测零抖动）。

使用步骤：

1. 正常复制一段文本。
2. 切到目标网页/应用输入框，并确保光标在输入框内。
3. 按 `Ctrl+Alt+P`，约半秒后由输入法一次性提交剪贴板文本。

粘贴链路故障排查：`./scripts/diagnose-paste.sh` 按三个检查点（剪贴板→暂存文件 / 文件→引擎 / 引擎→应用）逐段定位，并在引擎→应用一段用单行/多行、200ms/1000ms 延迟做差分测试，自动给出结论。

常用配置：

```bash
export VOICE_IME_CLIPBOARD_PREPARE_DELAY_SECONDS=0.3  # 提交前等待（默认与语音同节奏）
export VOICE_IME_CLIPBOARD_PREPARE_DELAY_MS=300       # 同上，毫秒配置；设置后优先于 seconds
export VOICE_IME_CLIPBOARD_NOTIFY=1                   # 触发时的桌面通知开关（诊断用）
export VOICE_IME_CLIPBOARD_MAX_CHARS=20000            # 最大提交字符数
```

## 语音输入

热键：`Ctrl+Alt+V`。另有 `Ctrl+Alt+B`「原文语音输入」：同一条录音/识别链路，但识别结果**绝不经过 LLM 后处理**（无论文本多长），只做确定性规整（去口头禅、繁简转换等）后上屏，适合念原文、口令、不想被改写的场合。

在部分终端程序中，应用/工具包可能不会把 `Ctrl+Alt+V` 转发给 IBus；安装脚本会额外注册一个 GNOME 用户级全局快捷键，把同一个 `Ctrl+Alt+V` 转发给输入法进程（`Ctrl+Alt+B` 同样注册了 GNOME 兜底绑定）。

默认是 toggle 模式：按一次开始录音，再按一次停止录音、识别并提交到当前光标位置；录音中按**任意一个**语音热键（V 或 B）都会停止录音。fixed 模式（`VOICE_IME_TRIGGER_MODE=fixed`，固定时长录音）下 V/B 的差异同样保留：B 键触发的识别结果依旧跳过 LLM 后处理与云端语义平滑（volc-bigmodel 后端的 DDC），只做确定性规整。状态弹窗默认关闭。现在语音流水线是：

```text
录音 wav -> STT 初稿 -> 规则清理 ->（可选：云端 LLM 润色，JSON 配置开启）-> 提交到当前光标
```

默认 STT 后端是本地 `Qwen3-ASR` sidecar（0.6B / 1.7B，默认 1.7B）：模型从 `vendor/models/qwen3-asr/` 加载，首次语音时由 engine 进程自动拉起本地 HTTP sidecar（端口 18081，bf16，cuda:0）。如需切换模型：

```bash
./scripts/switch-qwen-asr.sh 0.6b
./scripts/switch-qwen-asr.sh 1.7b   # 默认
```

如需切回云端小米 `mimo-v2.5-asr`（Token Plan，中国集群 `https://token-plan-cn.xiaomimimo.com/v1`）：

```bash
./scripts/switch-mimo-cloud-asr.sh cn
```
API Key 推荐通过 BWS 运行时注入 `XIAOMI_TOKEN_PLAN_CN_API_KEY`，不要写入文档或提交到仓库。

本原型支持七种 STT 后端：

1. `VOICE_IME_ASR_CMD` 自定义命令，命令 stdout 作为识别文本。例：
   ```bash
   export VOICE_IME_ASR_CMD='my-asr-command {wav}'
   ```
2. `Qwen3-ASR` sidecar（推荐用于测试 2026 新中文/多语 ASR）：
   ```bash
   ./scripts/setup-qwen-asr.sh   # 下载 Qwen3-ASR-0.6B 和 1.7B，并默认启用 1.7B（两档都下，方便切换）
   # 快速切换测试：
   ./scripts/switch-qwen-asr.sh 0.6b
   ./scripts/switch-qwen-asr.sh 1.7b
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
   # API Key（X-Api-Key）推荐通过 BWS 运行时注入；这里仅当未配置 bws 时手动提供
   VOICE_IME_VOLC_API_KEY='xxxxx' ./scripts/switch-volc-bigmodel-asr.sh
   ```

常用语音/STT 环境变量：

```bash
export VOICE_IME_TRIGGER_MODE=toggle         # toggle：按一次开始，再按一次停止；fixed：固定时长旧模式（V/B 差异同 toggle：B 仍跳过 LLM 后处理）
export VOICE_IME_HOTKEYS=Ctrl+Alt+V # 默认语音热键；仅支持 Ctrl+Alt+字母
export VOICE_IME_RAW_HOTKEYS=Ctrl+Alt+B # 原文语音热键（跳过 LLM 后处理）；与主热键或剪贴板热键（Ctrl+Alt+P）重叠的字母被剔除，全部重叠时自动禁用
export VOICE_IME_MAX_RECORD_SECONDS=120      # toggle 模式最长录音时长
export VOICE_IME_OVERLAY=0                   # 默认关闭独立语音状态弹窗；设为 1 可开启
export VOICE_IME_OVERLAY_POSITION=top-center # top-center 或 center
export VOICE_IME_RECORD_SECONDS=5            # fixed 模式固定录音时长
export VOICE_IME_ASR_BACKEND=qwen3-asr # 默认：本地 Qwen3-ASR (1.7B)
export VOICE_IME_QWEN_ASR=1
export VOICE_IME_QWEN_ASR_MODEL=1.7b
export VOICE_IME_MIMO_ASR=0
export VOICE_IME_MIMO_CLOUD_ASR=0
export VOICE_IME_MIMO_CLOUD_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
export VOICE_IME_MIMO_CLOUD_ASR_MODEL=mimo-v2.5-asr
export VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE=auto

# faster-whisper 仅作为本地兜底/诊断后端；显式切换时使用：
# export VOICE_IME_ASR_BACKEND=faster-whisper
# export VOICE_IME_MIMO_CLOUD_ASR=0
export VOICE_IME_QWEN_ASR=0
export VOICE_IME_WHISPER_MODEL=large-v3    # Whisper 最大通用模型；RTX 4080 可用 float16 跑 GPU
export VOICE_IME_WHISPER_DEVICE=cuda       # faster-whisper 兜底用 GPU STT；不默认使用 CPU
export VOICE_IME_WHISPER_DEVICE_INDEX=0
export VOICE_IME_WHISPER_COMPUTE=float16   # 省显存可改 int8_float16
export VOICE_IME_REQUIRE_GPU_STT=1          # 防止误回退到 CPU
export VOICE_IME_WHISPER_LANGUAGE=zh        # 设为空可自动检测

# Qwen3-ASR 常用变量
export VOICE_IME_ASR_BACKEND=qwen3-asr
export VOICE_IME_QWEN_ASR=1
export VOICE_IME_QWEN_ASR_MODEL=0.6b        # 0.6b 或 1.7b
export VOICE_IME_QWEN_ASR_LANGUAGE=Chinese
export VOICE_IME_QWEN_ASR_PORT=18081
export VOICE_IME_QWEN_ASR_DTYPE=bfloat16
export VOICE_IME_QWEN_ASR_DEVICE_MAP=cuda:0

# MiMo-V2.5-ASR 本地 sidecar 常用变量
export VOICE_IME_ASR_BACKEND=mimo-asr
export VOICE_IME_MIMO_ASR=1
export VOICE_IME_MIMO_ASR_MODEL_PATH=$PWD/vendor/models/mimo-asr/MiMo-V2.5-ASR
export VOICE_IME_MIMO_ASR_TOKENIZER_PATH=$PWD/vendor/models/mimo-asr/MiMo-Audio-Tokenizer
export VOICE_IME_MIMO_ASR_SOURCE=$PWD/vendor/MiMo-V2.5-ASR
export VOICE_IME_MIMO_ASR_LANGUAGE=auto     # auto / Chinese / English
export VOICE_IME_MIMO_ASR_PORT=18082
export VOICE_IME_MIMO_ASR_DEVICE=cuda

# MiMo 云端 ASR / Token Plan 常用变量
export VOICE_IME_ASR_BACKEND=mimo-cloud-asr
export VOICE_IME_MIMO_CLOUD_ASR=1
export VOICE_IME_MIMO_API_KEY=tp-xxxxx      # Token Plan Key；pay-as-you-go 为 sk-xxxxx
export VOICE_IME_MIMO_CLOUD_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
export VOICE_IME_MIMO_CLOUD_ASR_MODEL=mimo-v2.5-asr
export VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE=auto # auto / zh / en

# 火山引擎豆包 bigmodel ASR（极速版，base64 直传）常用变量
export VOICE_IME_ASR_BACKEND=volc-bigmodel-asr
export VOICE_IME_VOLC_BIGMODEL_ASR=1
export VOICE_IME_VOLC_API_KEY=xxxxx           # X-Api-Key；推荐用 bws 注入，不写这里
export VOICE_IME_VOLC_API_KEY_SECRET=VOLC_BIGMODEL_ASR_API_KEY  # BWS 密钥名，不持久化原始 key
export VOICE_IME_VOLC_BIGMODEL_BASE_URL=https://openspeech.bytedance.com
export VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID=volc.bigasr.auc_turbo
export VOICE_IME_VOLC_BIGMODEL_MODEL_NAME=bigmodel
export VOICE_IME_VOLC_BIGMODEL_ENABLE_PUNC=1  # 默认开启标点
export VOICE_IME_VOLC_BIGMODEL_ENABLE_ITN=1   # 默认开启 ITN（“一百二十三美元”->“$123”）
export VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC=1   # 默认开启云端语义顺滑（替代本地 filler 移除）
export VOICE_IME_VOLC_BIGMODEL_LANGUAGE=       # 留空=中英+方言混说最佳；zh=en-only 等可钉死单语
export VOICE_IME_VOLC_BIGMODEL_HOTWORDS=1      # 默认把 voice-dictionary.txt + english.json 作为热词直传 corpus.context
export VOICE_IME_VOLC_BIGMODEL_HOTWORDS_MAX=5000  # 热词上限，文档上限 5000
# 可选：火山控制台“自学习平台”配置的持久热词/替换词表（优先级低于 context 直传）
# export VOICE_IME_VOLC_BIGMODEL_BOOSTING_TABLE=my_boosting
# export VOICE_IME_VOLC_BIGMODEL_CORRECT_TABLE=my_correct
```

GPU 验证：

```bash
cd ~/ibus-voice-ime
./scripts/check-gpu-stt.sh
# 如需用大模型验证 GPU 链路：VOICE_IME_WHISPER_MODEL=large-v3 ./scripts/check-gpu-stt.sh
```

`fixed` 模式可选启用简易静音自动停止，开始说话后静音约 1 秒自动结束；如果 VAD 录音失败会回退到固定时长录音：

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

录音默认走**系统默认音源**（PipeWire/Pulse 兼容性最好，开箱即用）。如需绕开默认源漂移（蓝牙抢占等），可直采指定 ALSA 卡，例如作者的 MOTU M2（`plughw:M2,0`；M2 为 48k 设备，`plughw` 前缀自动重采样到 16k，不能写 `hw:M2,0`）。设备名用 `arecord -l` 查询；配置的设备不存在时引擎会记录日志并自动回退系统默认源，不会直接失败：

```bash
export VOICE_IME_ARECORD_DEVICE="plughw:M2,0"   # 示例：直采 MOTU M2；空值/"default" = 系统默认源（默认）
```

录音 WAV 送 ASR 前经过分级降噪链（`VOICE_IME_DENOISE_TIER`，默认 `rnnoise`）：

| 档位 | 链路 | 延迟 | 说明 |
|---|---|---|---|
| `none` | 旧行为：highpass + 可选 sox noisered + normalize | — | 兼容保留 |
| `notch` | 纯 sox：highpass 80 + 50/100/150Hz 工频陷波 + normalize | ~5ms | 零新依赖；稳态 -39dB@50Hz，语音频段零损耗 |
| `rnnoise` | ffmpeg：陷波 + RNNoise(bd) → sox normalize | ~130ms | 默认档；缺 ffmpeg/模型自动降级 `notch` |

- 结论依据（2026-08-26 实测）：电容麦底噪 99% 为 50Hz 工频哼声；Qwen3-ASR 对
  残余底噪鲁棒（原始/陷波/RNNoise 三版识别逐字一致），降噪的作用是恢复「电容麦
  可用性」而非提升识别率；无需引入大型神经降噪模型（延迟/常驻进程代价不划算）。
- RNNoise 模型在 `vendor/models/rnnoise/bd.rnnn`（该目录不进 git），换机器后执行
  `./scripts/fetch-rnnoise-model.sh` 下载。
- 谱减类 sox `noisered` 保持默认关闭（`VOICE_IME_AUDIO_DENOISE=0`）：同族的
  谱减方案实测伤齿音（4-10kHz -3.1dB），已由陷波链取代。
- 陷波深度的验收必须看稳态（尾部 RMS）：IIR 滤波器起始瞬态几乎直通，时域峰值
  口径会把 -30dB 低估成 -4dB。

### 规则清理 / 语音命令

默认会做规则清理：空白归一化、去掉独立口头禅、把常见语音标点口令转成符号；不再启用本地自动补标点规则。LLM 开启后仍可能尝试更自然的标点润色。

```bash
export VOICE_IME_VOICE_COMMANDS=1
export VOICE_IME_REMOVE_FILLERS=1
export VOICE_IME_AUTO_PUNCTUATION=0  # 本地自动补标点已禁用
export VOICE_IME_INLINE_VOICE_COMMANDS=0  # 默认只识别独立/标点分隔的口令，避免误把普通文本里的“换行”等变成操作
export VOICE_IME_CHINESE_SCRIPT=simplified # 默认最终提交前强制繁转简；设为 none 可关闭
```

支持的常用口令：`换行`、`空一行`、`逗号`、`句号`、`问号`、`冒号`、`斜杠`、`空格` 等。为避免越权操作，默认只处理独立口令或由空格/标点分隔的口令；如需恢复旧的全文替换行为，可设置 `VOICE_IME_INLINE_VOICE_COMMANDS=1`。

### 繁简转换 / 默认简体输出

语音识别后端通常只能限制为“中文”，不能保证一定输出简体中文。项目默认在语音最终提交前使用系统 OpenCC 做一次兜底转换：

```bash
export VOICE_IME_CHINESE_SCRIPT=simplified  # 默认：繁体转简体
export VOICE_IME_CHINESE_SCRIPT=none        # 不做繁简转换
export VOICE_IME_CHINESE_SCRIPT=traditional # 可选：简体转繁体
```

默认使用系统配置 `/usr/share/opencc/t2s.json`；如需自定义可设置 `VOICE_IME_OPENCC_T2S_CONFIG=/path/to/t2s.json`。LLM 后处理开启时，也会被提示默认输出简体中文，但最终仍以 OpenCC 兜底转换为准。

### LLM 云端后处理（OpenAI 兼容接口，可选）

STT 之后的文本润色（补标点、**整理成规范 Markdown**、去口水词、修错字）可以交给任意**云端大模型**，只要它提供 OpenAI 兼容的 `chat/completions` 接口。这是**唯一受支持的开启方式**：一个用户自有的 JSON 配置文件，填三样东西——网址、API Key、模型 ID。

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

- **短句不整理**：默认 `min_chars: 50`——不足 50 字的听写直接提交规则清理结果，不为几个字付云端延迟和费用（短句也几乎不需要补标点）；想调整改这个字段即可。用 `Ctrl+Alt+B`「原文语音输入」触发的听写则**完全跳过** LLM 后处理，与长度无关（见「语音输入」）。
- **Markdown 输出（面向 AI 消费）**：整理结果默认是规范 Markdown，且**任务/指令类听写会整理成智能体任务简报**——`## 目标` / `## 背景` / `## 任务`（编号步骤）/ `## 约束` / `## 验收`，只为听写中实际存在的部分生成小节；叙述/讨论类内容按主题分节或保持自然段落，不强行套简报；命令/路径/代码用行内代码或代码块。文字内容本身不改，尤其**不会自作主张追加用户没说过的要求**（标题和目标只能来自原文关键词）。提示词在 `src/ibus_voice_ime/text/llm_postprocess.py`（`SYSTEM_PROMPT` / `MODE_INSTRUCTIONS` / `_build_messages`），可按需手工调整，改完重启引擎生效。
- **不做模型列表查询**：本工具不会请求 `/models` 帮你挑模型。`model` 必须填与服务商**完全一致**的 ID；填错的后果是第一次调用返回明确的 HTTP 404/400 错误（错误信息会提示检查 `base_url` 是否以 `/v1` 结尾、model ID 是否正确），而不是静默选错模型。
- **服务商特有参数走 `extra_body`**：该字段的内容会合并进请求体。典型用途是思考型模型关思考（输入法后处理要快而直接）——例如 GLM 系列：`"extra_body": {"thinking": {"type": "disabled"}}`（实测同一请求从 15 秒超时降到约 2 秒）；不关思考的模型若返回空结果并提示 `finish_reason=length`，就是思考耗尽了 `max_tokens`。
- **JSON 配置优先于环境变量**：文件存在且合法时，`run-engine.sh` 里的 `VOICE_IME_LLM_*` 默认值（本地 sidecar 地址、4 秒超时等）不会泄漏进云端调用。
- **即刻生效**：引擎在每次语音后处理前重新读取该文件，改完配置无需重启输入法。把 `enabled` 改为 `false` 即关闭。
- **信任模型输出**：LLM 返回的 Markdown **原样提交**——只做"去包裹"（整体被代码围栏/引号包住时解开），不再做机械的标点/空格重排（重复标点折叠、CJK-拉丁空格删除等）。那些确定性规则只服务于无 LLM 的回退链路，接在模型后面反而会破坏刻意排的格式。繁简方向仍以 OpenCC 兜底转换为准。
- **失败兜底**：接口错误/超时/返回异常时自动回退提交原始识别文本，语音输入永远不会因为 LLM 挂了而断掉。
- **密钥边界**：该文件只存在于你的 `~/.config`（已加入 `.gitignore` 双保险），绝不入库；`./scripts/doctor.sh` 会检查它的 JSON 合法性与文件权限（0600）。

历史包袱说明：早期的本地小模型路径（llama.cpp sidecar + Qwen3.5-0.8B GGUF，`scripts/setup-llm.sh`）实测会改坏听写原文（改词、吞字、加不存在内容），已被云端方案取代且默认压制——`run-engine.sh` 仍会把遗留的 `VOICE_IME_LLM_POSTPROCESS`/`VOICE_IME_LLM_INTERNAL` 环境变量钉死为 0。仅设置环境变量开启不了本地 LLM 后处理。

模式配置：

```bash
export VOICE_IME_VOICE_MODE=dictation  # 普通听写/聊天：只做最小清理
export VOICE_IME_VOICE_MODE=literal    # 原样/命令/代码：尽量少改写
export VOICE_IME_VOICE_MODE=markdown   # 仍只做最小清理，除非原文已明显是 Markdown
export VOICE_IME_VOICE_MODE=prompt     # 仍只做最小清理，不主动强化提示词
export VOICE_IME_VOICE_MODE=command    # 保留命令、路径、参数
```

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

如果希望这些变量对 IBus 生效，可以写入 `~/.profile`、`~/.config/environment.d/*.conf`，或用 `systemctl --user set-environment ...` 后重启 IBus。

## 故障排查

第一步永远是项目体检 + 看引擎日志：

```bash
./scripts/doctor.sh                    # 体检：半残态/断链热键/缺失资产等，只检查
./scripts/doctor.sh fix                # 自动修复（含词库/模型下载、重注册）
tail -f ~/.local/share/ibus-voice-ime/engine.log
```

日志一共有三处，按问题层次查：

```bash
tail -f ~/.local/share/ibus-voice-ime/engine.log          # 引擎主日志（键盘/录音/提交链路）
tail -f ~/.local/share/ibus-voice-ime/error.log           # 引擎错误日志
tail -f ~/.local/share/ibus-voice-ime/qwen-asr-server.log # 本地 Qwen3-ASR sidecar（模型加载/CUDA 问题在这里）
```

日志默认保留**最近 1000 行**（引擎启动时与写入过程中自动裁剪，防止长期使用日志无限增长；报 bug 时直接附上整个文件即可）。可用 `VOICE_IME_LOG_KEEP_LINES=N` 调整，设为 `0` 关闭裁剪。

常见问题：

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

## 卸载

```bash
cd ~/ibus-voice-ime
./uninstall.sh
```

## 许可证

本项目整体以 [GPL-3.0](LICENSE) 发布。

仓库内捆绑的第三方组件（librime 运行时、Rime 词库方案、雾凇拼音 rime-ice、OpenCC 数据等）各自保留原始许可证，完整清单见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

大型模型文件（Qwen3-ASR / Qwen3.5 / MiMo）与扩展词库（rime-ice cn_dicts / zhwiki / moegirl）不随仓库分发，由安装脚本按需下载。
