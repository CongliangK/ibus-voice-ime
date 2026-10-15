# IBus 自定义语音输入法原型

Linux 上的 IBus 输入法引擎：键盘输入复用 Rime（librime + 雾凇拼音），语音输入按 `Ctrl+Alt+V` 说话、识别后直接上屏，支持本地 Qwen3-ASR / 云端 MiMo / 火山引擎等多种识别后端；另有 `Ctrl+Alt+B`「原文语音输入」，识别结果不做 LLM 后处理。

- 面向使用者的安装与使用说明：本文件
- 面向 AI 代理 / 贡献者的实现细节与环境变量全量说明：[AGENTS.md](AGENTS.md)
- 技术架构与选型理由、测试平台、已知局限：[docs/README.md](docs/README.md)

## 项目状态（请先读）

**这是一个个人项目，只在作者自己的机器上完整验证过，未做广泛的发行版适配。**

- **已验证环境**：Fedora 44 Workstation + GNOME（Wayland）、x86_64、NVIDIA RTX 4080（本地 ASR）、PipeWire 音频栈。
- **未验证**：其他发行版（Ubuntu / Arch / Debian…）、其他桌面环境（KDE / X11 / sway…）、ARM 架构、无 NVIDIA GPU 的本地识别。理论上 IBus + GNOME + PipeWire 的组合都能跑，遇到问题欢迎提 issue。

## 环境要求

| 组件 | 必需性 | 说明 |
|---|---|---|
| IBus | 必需 | 输入法框架本体（Fedora 默认自带） |
| GNOME 桌面 | 强烈建议 | 安装脚本的输入源注册、全局热键均按 GNOME 设计 |
| Python ≥ 3.10 + PyGObject | 必需 | Fedora：`sudo dnf install python3-gobject ibus` |
| alsa-utils（`arecord`） | 必需 | 录音主链路 |
| PipeWire（`pw-record`）+ ffmpeg + sox + OpenCC | 建议 | 兜底录音、降噪预处理、繁简转换；缺失时自动降级 |
| NVIDIA GPU | 本地 ASR 需要 | Qwen3-ASR 1.7B bf16 约需 4GB+ 显存；无 GPU 请用云端后端 |

新机器上先跑环境自检（FAIL 项需解决，WARN 项为可选/降级提示）：

```bash
./scripts/check-environment.sh            # 全量
./scripts/check-environment.sh --no-gpu   # 只关心键盘输入链路时
```

## 安装

### 一键安装（推荐）

已安装 `git` 与 `curl` 时（普通用户运行，无需 sudo）：

```bash
curl -fsSL https://raw.githubusercontent.com/CongliangK/ibus-voice-ime/main/remote-install.sh | bash
# 全默认零提问（CI/无人值守）：
curl -fsSL https://raw.githubusercontent.com/CongliangK/ibus-voice-ime/main/remote-install.sh | bash -s -- --non-interactive
```

### 手动安装

```bash
git clone https://github.com/CongliangK/ibus-voice-ime.git
cd ibus-voice-ime
./setup.sh
```

安装向导会交互询问语音方案（本地 / 云端 / 仅键盘）、模型分档、热键等；**每题都有默认值，直接回车即可**。常用选项：

- `--keyboard-only`：只装键盘输入（Rime 拼音），不下载语音模型
- `--cloud siliconflow` / `--cloud mimo`：语音走云端（需自备 API Key）
- `--dry-run`：只打印执行计划，不安装
- `--reconfigure`：已装机器改配置（热键、语音方案等）
- `--non-interactive`：全默认零提问安装

setup.sh 全量选项、两段式无人值守安装（headless prepare → desktop activate）、安装系统内部机制见 [AGENTS.md](AGENTS.md)。

无 NVIDIA GPU 时语音步骤自动跳过并打印云端替代命令，不会盲目 FAIL。

## 使用

安装完成后，`Super+Space` 切换到「自定义语音输入法」：

- **键盘拼音**：内置 Rime + 雾凇拼音大词库；`nihao` + Space → `你好`；按一下 `Shift` 切换中/英；自带中英文常用词记忆
- **`Ctrl+Alt+V` 语音输入**：按一次开始录音，再按一次停止并识别上屏；首次按键后模型加载需数十秒属正常
- **`Ctrl+Alt+B` 原文语音输入**：识别结果不做 LLM 后处理，适合念原文、口令、不想被改写的场合
- **`Ctrl+Alt+P` 键盘粘贴**：把剪贴板文本通过输入法提交到光标，绕过网页禁粘贴

三组热键均可在安装向导或配置文件中自定义。

### 切换语音后端

```bash
./scripts/switch-qwen-asr.sh 0.6b                          # 本地 Qwen3-ASR（默认 1.7b）
VOICE_IME_MIMO_API_KEY='tp-xxx' ./scripts/switch-mimo-cloud-asr.sh cn        # 小米 MiMo 云端
VOICE_IME_VOLC_API_KEY='xxx' ./scripts/switch-volc-bigmodel-asr.sh           # 火山引擎豆包
VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' ./scripts/switch-siliconflow-asr.sh   # 硅基流动（Beta）
```

共支持八种 STT 后端（含自定义命令、faster-whisper、vosk），详见 [AGENTS.md](AGENTS.md) 与 [docs/asr-backends.md](docs/asr-backends.md)。

### LLM 云端润色（可选）

听写结果可交给任意 OpenAI 兼容的云端大模型做润色（补标点、整理成 Markdown、去口水词）：

```bash
./scripts/setup-llm-cloud.sh    # 交互式生成 ~/.config/ibus-voice-ime/llm.json
```

短句默认不整理（`min_chars: 50`），LLM 失败时自动回退提交原始识别文本。设计细节见 [AGENTS.md](AGENTS.md)。

### 常用配置

配置统一在 `~/.config/ibus-voice-ime/config.json`，优先级：环境变量 > 用户 config.json > 默认值。

```bash
PYTHONPATH=src python3 -m ibus_voice_ime.config set recording.max_seconds 120
```

全部 200+ 配置键见 [docs/configuration.md](docs/configuration.md)。

## 故障排查

第一步永远是项目体检 + 看引擎日志：

```bash
./scripts/doctor.sh                    # 体检：只检查
./scripts/doctor.sh fix                # 自动修复
tail -f ~/.local/share/ibus-voice-ime/engine.log
```

常见问题速查：

- **重启电脑后输入法消失**：IBus 默认缓存模式被系统级注册缓存短路，用户组件开机不被扫描；安装已自动写入 systemd 自愈（强制重扫）。旧版安装的机器跑 `./scripts/doctor.sh fix` 或重跑 `./install.sh` 一次即愈（机制见 [AGENTS.md](AGENTS.md)「注册缓存机制」）
- **键盘无候选**：部署大词库 `./scripts/setup-rime-ice.sh`；内置 librime 加载失败时会自动回退系统 librime
- **语音没反应**：`arecord -l` 确认有麦克风；首次语音需加载模型，等数十秒属正常
- **无 NVIDIA GPU**：切换云端后端（见上文「切换语音后端」）
- **Ctrl+Alt+V 在某些终端无效**：安装脚本已注册 GNOME 全局快捷键兜底

### Python 依赖包源被拒绝（HTTP 403）

403 可能是镜像的访问控制/风控，不等于包不存在，也不能直接断言用户代理有问题。默认清华源安装失败后，安装器对可恢复的索引/下载错误会依次探测 **阿里云 → 官方 PyPI**，确认目标版本可见才尝试安装；每源最多一次，全部失败则保留错误退出。证书、哈希、明确的代理配置、依赖冲突及本地环境错误不会靠换源掩盖。

也可在仓库根目录显式选择阿里云续装（无需卸载或清缓存）：

```bash
VOICE_IME_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
VOICE_IME_QWEN_ASR_SIZES=1.7B VOICE_IME_QWEN_ASR_MODEL=1.7b \
  ./scripts/setup-qwen-asr.sh
```

将包源值改为 `default` 即使用官方 PyPI。显式包源不会被自动覆盖；`VOICE_IME_PIP_INDEX_FALLBACK=off` 可禁用默认自动换源。包源回退仅影响 Qwen 的 pip 依赖，不改变 ModelScope 模型下载或 Python 下载源；完整依赖是否可用仍以实际安装和推理验收为准。

完整排查指南（三处日志、环境变量、非 GNOME 桌面手工集成等）见 [AGENTS.md](AGENTS.md)。

## 卸载

```bash
./uninstall-all.sh                # 删除全部安装足迹，保留个人词库与配置
./uninstall-all.sh --purge        # 彻底清除：连同用户数据、API key 一并删除
./uninstall.sh                    # 轻量卸载：仅移除 IBus 组件注册与环境变量
```

### 一键彻底卸载（任意目录，curl 调用）

对 `curl | bash` 安装的机器，在任何文件夹都能一条命令回到安装前的干净状态（删除注册/热键/环境、用户数据与 API key、Qwen 模型缓存、整个克隆含 `.venv*`/`vendor/` 等全部下载产物；跨项目共享缓存与系统文件不触碰）：

```bash
# 有终端时会要求输入 yes 确认
curl -fsSL https://raw.githubusercontent.com/CongliangK/ibus-voice-ime/main/remote-uninstall.sh | bash
# 无人值守 / 无终端（SSH、CI）必须显式 --yes
curl -fsSL https://raw.githubusercontent.com/CongliangK/ibus-voice-ime/main/remote-uninstall.sh | bash -s -- --yes
# 想保留个人词库/配置/热词，其余照删
curl -fsSL https://raw.githubusercontent.com/CongliangK/ibus-voice-ime/main/remote-uninstall.sh | bash -s -- --keep-user-data
```

克隆目录由与 `remote-install.sh` 相同的 `IBUS_VOICE_IME_DIR` 等环境变量控制。引导器确认后会先把本地克隆更新到远端最新版再运行其中的卸载脚本（仅限 curl 安装的克隆；更新失败自动回退本地版本，不阻断卸载）；克隆里的 `uninstall-all.sh` 即使是旧版也完全兼容——版本探测只传支持的参数，模型缓存与克隆目录由引导器兜底删除。也可以在克隆内手动分级卸载：

```bash
./uninstall-all.sh --purge --purge-models                 # 清注册+数据+模型缓存，保留克隆
./uninstall-all.sh --purge --purge-models --purge-repo    # 连克隆一起彻底删除（仅限 curl 安装的克隆）
```

`--purge-repo` 只删除带引导器标记 `.git/ibus-voice-ime-bootstrap` 的克隆（即 `remote-install.sh` 安装的）；普通开发克隆会被拒绝，防止误删未提交的工作副本。

## 许可证

本项目整体以 [GPL-3.0](LICENSE) 发布。

仓库内捆绑的第三方组件（librime 运行时、Rime 词库方案、雾凇拼音 rime-ice、OpenCC 数据等）各自保留原始许可证，完整清单见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

大型模型文件（Qwen3-ASR / Qwen3.5 / MiMo）与扩展词库（rime-ice cn_dicts / zhwiki / moegirl）不随仓库分发，由安装脚本按需下载。
