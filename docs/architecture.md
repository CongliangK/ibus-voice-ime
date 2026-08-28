# 技术框架与选型

本文说明项目的技术框架、各组件的选型理由（包括没有选择什么、为什么），以及语音输入的完整数据流。配置项与命令用法见根 [README.md](../README.md)。

## 总体结构

本项目是一个 **Python 编写的 IBus 输入法引擎**，一个进程内同时承载两条输入链路：

```text
                    ┌────────────────────────────────────────────────┐
                    │  engine.py（IBus Engine，PyGObject）            │
                    │                                                │
 键盘按键 ──────────▶│  按键路由 / 候选 UI / 翻页 / Shift 中英切换      │
                    │      │                                         │
                    │      ▼                                         │
                    │  rime_backend.py（自研 ctypes 绑定）            │
                    │      │                                         │
                    │      ▼                                         │
                    │  vendor/rime/ 内置 librime（雾凇拼音方案）      │
                    └────────────────┬───────────────────────────────┘
                                     │ Ctrl+Alt+V（toggle）
                                     ▼
 ┌───────────┐   ┌──────────────┐   ┌──────────────┐   ┌────────────┐   ┌──────────┐
 │ arecord 录音│──▶│ 音频预处理    │──▶│ ASR 后端      │──▶│ 规则清理    │──▶│commit_text│
 │ (默认音源，  │   │ sox 陷波/归一 │   │ (默认本地     │   │ 口头禅/口令 │   │ 提交到光标 │
 │  设备回退)  │   │ +RNNoise 降级)│   │  Qwen3-ASR   │   │ /繁简转换   │   │          │
 └───────────┘   └──────────────┘   │  sidecar)    │   └────────────┘   └──────────┘
                                    └──────────────┘
                                     127.0.0.1:18081（独立进程，仅回环）
```

进程模型：

- `ibus-daemon` 通过组件 XML 中的 `<exec>` 拉起 `run-engine.sh` → `engine.py`。`run-engine.sh` 是环境装配层：加载 `~/.config/environment.d/ibus-voice-ime.conf`、设置全部运行时默认值、必要时从 Bitwarden Secrets Manager（BWS）运行时注入云端后端密钥，然后 `exec` 引擎。
- ASR sidecar 是**独立进程**（本地 HTTP，仅绑定 127.0.0.1），首次语音时按需拉起，不随引擎启动。
- GNOME 全局快捷键（`Ctrl+Alt+V` / `Ctrl+Alt+P`）指向 `scripts/voice-toggle.sh` / `scripts/clipboard-paste.sh`，它们通过引擎的 **Unix domain socket IPC**（`$XDG_RUNTIME_DIR/ibus-voice-ime/voice.sock`）下发指令，不在引擎进程内抢按键。

## 关键选型与理由

### 为什么选 IBus 输入框架

IBus 是 GNOME/Fedora 的原生输入法框架，选它有三个实际原因：

1. **作者日用环境就是 GNOME（Wayland）+ Fedora**。IBus 在这个组合里是系统默认、集成最深：输入源切换（Super+Space）、候选框、面板行为都是现成的，遇到按键转发之类的怪癖时社区经验也最多。个人项目优先在日用环境里做到「零妥协可用」。
2. **Python 一等公民支持**。通过 PyGObject（GObject Introspection）可以直接用 Python 写引擎（`IBus.Engine` 子类 + 组件 XML），不需要写 C，迭代速度快。
3. **用户级组件注册**。组件 XML 放进 `~/.local/share/ibus/component/`、配合 `IBUS_COMPONENT_PATH` 即可让 daemon 发现引擎，不需要 root、不需要打包成发行版软件包——契合「git clone 后 `./install.sh` 即用」的发布方式。

**诚实说明**：fcitx5 同样是一个优秀的框架，能力上并不输 IBus；本项目没有选它不是因为技术劣势，而是因为作者环境与 IBus 绑定更深。引擎的核心逻辑（Rime 绑定、语音流水线、后处理）并不依赖 IBus 的独有能力，理论上移植到 fcitx5 主要是重写引擎接入层。

### 为什么键盘核心复用 Rime（librime）

拼音输入法的核心——音节切分、词库、用户词频学习、候选排序、方案体系——是已被 Rime 生态打磨了十几年的成熟问题。自研这套东西投入巨大且几乎不可能达到同等质量，因此本项目**不自研键盘算法**，而是通过 librime 复用。

具体做法上有一个关键选择：**不走现成的 `ibus-rime`，而是自写 ctypes 绑定**（`src/ibus_voice_ime/rime/rime_backend.py`）。原因：

- `ibus-rime` 是一个独立的完整输入法，接管全部按键与 UI；本项目需要在按键层做自己的事——语音热键、粘贴热键、Shift 中英切换、ASCII 直通等。自己绑定 librime 才能保留完整的按键控制权，把 Rime 仅当作「转换引擎」调用。
- 运行时与系统隔离：引擎默认加载 `vendor/rime/` 下的**内置 librime 运行时**（库、方案、部署产物），不读取系统 `ibus-rime` / `fcitx-rime` 的任何配置，用户词库存放在本项目私有的 `~/.local/share/ibus-voice-ime/rime-user/`。内置二进制加载失败（glibc/架构不匹配）时自动回退系统 librime。

### 为什么选雾凇拼音（rime-ice）

Rime 只是引擎，候选质量取决于**方案与词库**。默认方案选择雾凇拼音（rime-ice），理由：

- **维护活跃、口碑成熟的现代方案**：注音/拼写规则、繁简、符号、emoji、中英混输等开箱即用，实测候选质量接近商业输入法。
- **大词库生态可组合**：可选部署 zhwiki、moegirl 扩展词典（`scripts/setup-rime-ice.sh` 一键完成并用内置 librime 编译）。
- **许可证兼容**：rime-ice 与 Rime 官方数据均为 GPL-3.0，与本项目整体许可证一致（见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)）。
- 未部署雾凇时自动回退内置的 `luna_pinyin_simp`，不会崩溃——「装完即用，装词库后好用」是设计目标。

模糊音由 rime-ice 原生提供但**默认禁用**（实测开启后候选污染明显），需要时编辑 `rime_ice.custom.yaml` 再部署。

### 默认语音模型：本地 Qwen3-ASR 1.7B sidecar

默认后端是 **Qwen3-ASR 1.7B**（bf16、`cuda:0`，约需 4GB+ 显存），以本地 HTTP sidecar 形式运行。选它的理由与如实说明的代价：

**为什么是它**：
- **本地优先**：音频不出本机、无 API 费用、无网络依赖——语音输入是高频日用功能，这三点比极限准确率更重要。
- **2026 代中文 ASR 的代表性模型**：中文（及多语种）识别质量好；实测对麦克风残余底噪鲁棒（原始/陷波/RNNoise 三版音频识别结果逐字一致，见降噪链说明）。
- **档位可调**：同链路可切 0.6B（`switch-qwen-asr.sh 0.6b`），显存小的机器有退路。

**为什么做成 sidecar 而不是引擎进程内推理**：
- torch/CUDA 与 PyGObject（GLib 主循环）共存一个进程容易踩运行时冲突，独立进程各自干净。
- 独立生命周期：首次语音才拉起、常驻预热、空闲看门狗把模型迁移到内存并释放显存（配合 `expandable_segments` 分配器真正归还 VRAM）。
- 健康检查与错误隔离：sidecar 暴露 `/health`、`/warm`，模型加载失败会给出可读的中文错误而不是让引擎假死。

**如实说明的代价**：需要 NVIDIA GPU（无 GPU 请切云端后端）；首次使用要等模型加载进显存（视盘速 30 秒到数分钟）；sidecar HTTP 服务是我们自己写的薄封装（`qwen_asr_server.py`），只绑定 127.0.0.1。

## 语音流水线（数据流细节）

```text
Ctrl+Alt+V（toggle：按一次开始，再按一次结束）
  → 录音：arecord 直采配置设备；设备不存在自动回退系统默认源；arecord 失败回退 pw-record+ffmpeg
  → 预处理（VOICE_IME_DENOISE_TIER，默认 rnnoise 档）：
      sox 高通 80Hz + 50/100/150Hz 工频陷波 + RNNoise(bd) + 归一化；缺件自动降级 notch 档
  → ASR 后端（见 asr-backends.md；默认本地 Qwen3-ASR sidecar）
  → 规则清理：空白归一、独立口头禅移除、语音标点口令转符号、自定义词典整词替换、CJK/拉丁间距
  → OpenCC 繁→简兜底转换（默认 simplified）
  → IBus commit_text 提交到当前光标
```

- **LLM 云端润色（可选）**：OpenCC 之后可接一个云端大模型做润色（补标点/去口水词），通过用户自有的 `~/.config/ibus-voice-ime/llm.json` 开启（OpenAI 兼容接口：base_url + api_key + 精确 model ID，不做模型列表查询；`scripts/setup-llm-cloud.sh` 生成）。接口失败自动回退原文。本地小模型路径（llama.cpp sidecar）实测会改坏听写原文，已被云端方案取代并被 `run-engine.sh` 钉死关闭。
- 识别结果同时受**自定义词典**影响：三列格式（标准词 | 别名 | 常见误识别），前两列作为热词直传支持热词的云端后端，第三列做本地确定性整词替换。

## 配置体系

- **单一事实源**：`~/.config/environment.d/ibus-voice-ime.conf`。`install.sh` 与各 `switch-*` / `setup-*` 脚本写入，`run-engine.sh` 每次启动加载（显式 export 的救援配置优先于文件旧值）。
- **默认值层**：`run-engine.sh` 对全部 `VOICE_IME_*` 变量给出合理默认，环境文件缺项时引擎仍可运行。
- **密钥策略**：ASR 云端 Key 不落盘，运行时由 `run-engine.sh` 的 BWS 包装注入到引擎进程（有单元测试守护该策略）。唯一例外是 LLM 云端润色：用户以 0600 权限自持 `~/.config/ibus-voice-ime/llm.json`（`.gitignore` 双保险，绝不入库）。
