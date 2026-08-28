# IBus 自定义语音输入法原型

Linux 上的 IBus 输入法引擎：键盘输入复用 Rime（librime + 雾凇拼音），语音输入按 `Ctrl+Alt+V` 说话、识别后直接上屏，支持本地 Qwen3-ASR / 云端 MiMo / 火山引擎等多种识别后端。

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
./scripts/check-environment.sh     # 环境自检，按提示解决 FAIL 项

./install.sh                       # 注册 IBus 组件 + GNOME 输入源/热键

# 选一个语音识别后端（二选一）：
./scripts/setup-qwen-asr.sh        # 本地 Qwen3-ASR（默认后端，需 NVIDIA GPU）
# 或：VOICE_IME_MIMO_API_KEY='tp-xxxxx' ./scripts/switch-mimo-cloud-asr.sh cn

ibus restart                       # 或重新登录
# Super+Space 切换到「自定义语音输入法」；nihao + Space -> 你好；Ctrl+Alt+V 语音
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

推荐部署**雾凇拼音（rime-ice）+ zhwiki + moegirl** 大词库以获得接近商业输入法的候选质量（默认方案为 rime_ice；脚本会自动用内置 librime 编译词库，首次约 1-3 分钟）：

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

默认安装会把 `Ctrl+Alt+P` 注册为 GNOME 全局快捷键，执行 `./scripts/clipboard-paste.sh`。脚本只负责读取桌面剪贴板、显示“📋 正在粘贴……”通知，并把准备好的文本写入用户运行目录，然后立刻通过输入法 IPC 通知 `engine.py` 并退出；`engine.py`（位于 `src/ibus_voice_ime/`）再等待约 1 秒，让焦点像 `Ctrl+Alt+V` 语音链路一样回到网页输入框，随后走语音识别结果同款 `_commit_voice_result()` / IBus `commit_text()` 提交路径。输入法内部的 direct `Ctrl+Alt+P` 处理默认关闭，避免 GNOME 全局快捷键和 IBus 同时处理同一次按键导致输入法状态异常。脚本提交后默认会延迟短暂切到英文输入源再切回 `voice-custom`，自动恢复少数网页导致的 IBus 状态损坏。

旧的外部虚拟键盘粘贴链路仍保留为 `./scripts/keyboard-paste.sh`：它不走浏览器 `paste` 事件，也不依赖 IBus `commit_text()`；它会读取剪贴板，然后通过 Linux `/dev/uinput` 创建一个虚拟键盘，逐字模拟真实键盘输入。可在遇到不接受 IBus commit 的网页/应用时手动作为兜底使用。

使用步骤：

1. 正常复制一段文本。
2. 切到目标网页/应用输入框，并确保光标在输入框内。
3. 按 `Ctrl+Alt+P`，等待约 1 秒后由输入法一次性提交剪贴板文本。

检查虚拟键盘兜底链路权限：

```bash
cd ~/ibus-voice-ime
./scripts/keyboard-paste.sh --check
```

如果提示无法写入 `/dev/uinput`，可临时授权：

```bash
sudo modprobe uinput
sudo setfacl -m u:$USER:rw /dev/uinput
```

常用配置：

```bash
export VOICE_IME_INTERNAL_CLIPBOARD_HOTKEY=0          # 默认关闭 IBus 内部 Ctrl+Alt+P，避免和 GNOME 全局快捷键重复触发
export VOICE_IME_CLIPBOARD_RECOVER_ENGINE=1           # 粘贴后自动切换输入源再切回，恢复少数网页导致的 IBus 状态异常
export VOICE_IME_CLIPBOARD_RECOVER_DELAY_SECONDS=2.0  # 自动恢复输入源的延迟，应晚于粘贴提交
export VOICE_IME_CLIPBOARD_ACTIVATE_DELAY_SECONDS=0.25 # IPC 不可达、激活 voice-custom 后等待可用的秒数
export VOICE_IME_CLIPBOARD_PREPARE_DELAY_SECONDS=1.0  # 外部脚本显示“正在粘贴”并等待焦点稳定的秒数
export VOICE_IME_CLIPBOARD_PREPARE_DELAY_MS=1000      # 同上，毫秒配置；设置后优先于 seconds
export VOICE_IME_CLIPBOARD_PREPARE_HINT_MS=1200       # “正在粘贴”提示显示时长
export VOICE_IME_CLIPBOARD_COMMIT_DELAY_MS=300        # 读取剪贴板后额外提交延迟
export VOICE_IME_CLIPBOARD_MAX_CHARS=20000            # 最大提交字符数

# 仅用于手动运行 ./scripts/keyboard-paste.sh 兜底链路：
export VOICE_IME_KEYBOARD_PASTE_DELAY_MS=1000
export VOICE_IME_KEYBOARD_PASTE_MODE=smart
export VOICE_IME_KEYBOARD_PASTE_TYPING_ENGINE=xkb:us::eng
export VOICE_IME_KEYBOARD_PASTE_KEY_DELAY_MS=4
export VOICE_IME_KEYBOARD_PASTE_MAX_CHARS=20000
```

外部虚拟键盘逐字输入脚本仍保留为 `./scripts/keyboard-paste.sh`，仅作为不接受 IBus commit 的手动兜底。

## 语音输入

热键：`Ctrl+Alt+V`。

在部分终端程序中，应用/工具包可能不会把 `Ctrl+Alt+V` 转发给 IBus；安装脚本会额外注册一个 GNOME 用户级全局快捷键，把同一个 `Ctrl+Alt+V` 转发给输入法进程。

默认是 toggle 模式：按一次开始录音，再按一次停止录音、识别并提交到当前光标位置。状态弹窗默认关闭。现在语音流水线是：

```text
录音 wav -> STT 初稿 -> 规则清理 ->（LLM 重排为实验功能，默认禁用）-> 提交到当前光标
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
export VOICE_IME_TRIGGER_MODE=toggle         # toggle：按一次开始，再按一次停止；fixed：固定时长旧模式
export VOICE_IME_HOTKEYS=Ctrl+Alt+V # 默认语音热键；仅支持 Ctrl+Alt+字母
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

### LLM 后处理 / 重排（实验功能，当前已禁用）

**现状声明**：LLM 后处理是实验功能，当前被引擎**强制关闭**——`run-engine.sh` 在启动时无条件设置 `VOICE_IME_LLM_POSTPROCESS=0 / VOICE_IME_LLM_INTERNAL=0 / VOICE_IME_LLM_RERANK=0`，且 `llm_postprocess.enabled()` 硬编码返回 False。也就是说：设置环境变量开启不了它，`scripts/setup-llm.sh` 安装的 llama.cpp sidecar 不会被语音流程调用。默认链路是 **Qwen3-ASR + 规则清理**后直接提交。

这样设计的原因：实测小模型后处理会改坏原始听写内容（改词、吞字、加不存在的内容），确定性规则清理已覆盖绝大多数脏数据。要真正启用需修改引擎代码解除强制关闭并自担改写质量风险（`src/ibus_voice_ime/asr/voice.py` 的 postprocess 链、`run-engine.sh` 的 Strict policy 段、`text/llm_postprocess.py` 的 `enabled()`）。

实验用的基础设施仍然保留：内置管理的 `llama.cpp` sidecar（`llama-server` + GGUF 模型，ModelScope `Qwen/Qwen3.5-0.8B`），`./scripts/setup-llm.sh` 可完成安装与预置。

```bash
cd ~/ibus-voice-ime
./scripts/setup-llm.sh
ibus restart
```

常用配置：

```bash
export VOICE_IME_LLM_POSTPROCESS=0      # 默认关闭；如需试验 LLM 后处理，手动改为 1
export VOICE_IME_LLM_INTERNAL=1
export VOICE_IME_LLM_TRUST_OUTPUT=1      # 启用 LLM 时采用单次输出，不做多候选回退
export VOICE_IME_LLM_RERANK=0            # 默认关闭重排层，避免规则打分再次干预
export VOICE_IME_LLM_BASE_URL=http://127.0.0.1:18080/v1
export VOICE_IME_LLM_API_KEY=local
export VOICE_IME_LLM_MODEL=qwen3.5-0.8b
export VOICE_IME_LLM_CANDIDATES=1
export VOICE_IME_LLM_TIMEOUT=4
export VOICE_IME_LLM_FALLBACK_RAW=1      # 仅在接口错误/超时时回退，不参与正常文本选择
```

实验时如果已经有 Ollama、LM Studio 或其它 OpenAI-compatible 服务，也可以不用内置 sidecar：

```bash
export VOICE_IME_LLM_INTERNAL=0
export VOICE_IME_LLM_BASE_URL=http://127.0.0.1:11434/v1
export VOICE_IME_LLM_API_KEY=ollama
export VOICE_IME_LLM_MODEL=your-model
```

再强调一次：仅设置 `VOICE_IME_LLM_POSTPROCESS=1` 不会生效（引擎会覆盖回 0）；见本节开头的现状声明。

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

第一步永远是环境自检 + 看引擎日志：

```bash
./scripts/check-environment.sh
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
