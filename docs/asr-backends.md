# 语音后端支持

本项目把「语音识别」抽象成可插拔后端，通过 `VOICE_IME_ASR_BACKEND` 与一组开关变量选择；`scripts/switch-*.sh` / `setup-*.sh` 负责切换并把配置持久化到 `~/.config/environment.d/ibus-voice-ime.conf`（单一事实源，重启输入法后生效）。各后端的完整环境变量清单见根 [README.md](../README.md)「语音输入」一节。

## 后端总览

| 后端 | 形态 | 硬件/账号要求 | 状态定位 |
|---|---|---|---|
| **Qwen3-ASR sidecar**（0.6B / 1.7B） | 本地 HTTP sidecar（127.0.0.1:18081） | NVIDIA GPU（1.7B bf16 约 4GB+ 显存） | **默认后端**（1.7B） |
| MiMo-V2.5-ASR sidecar | 本地 HTTP sidecar（127.0.0.1:18082） | 更大显存/内存，需下载源码与 Audio Tokenizer | 可选本地大模型 |
| MiMo 云端 ASR（Token Plan） | OpenAI-compatible 云 API | 无 GPU；需小米 API Key | 无 GPU 机器的推荐选择 |
| 火山引擎豆包 bigmodel ASR（极速版） | 云 API（异步提交 + 轮询，base64 直传） | 无 GPU；需火山 API Key | 云端备选，热词支持最全 |
| faster-whisper | 本地进程内（CTranslate2） | NVIDIA GPU（显式切换时 CUDA） | 诊断 / 兜底后端 |
| vosk | 本地进程内 | CPU 可跑；需自备模型目录 | 轻量可选 |
| `VOICE_IME_ASR_CMD` 自定义命令 | 任意可执行，stdout 即识别文本 | 无 | 扩展点（接任意自建服务） |

## 各后端说明

### Qwen3-ASR sidecar（默认）

- **安装**：`./scripts/setup-qwen-asr.sh`（同时下载 0.6B 与 1.7B，默认启用 1.7B，方便切换）。
- **切换**：`./scripts/switch-qwen-asr.sh 0.6b | 1.7b`。
- **运行形态**：首次语音时引擎自动拉起 sidecar 进程（独立 venv `.venv-qwen-asr`，torch + transformers），仅监听 127.0.0.1:18081；支持 `/health`（含加载错误）、`/warm`（预热）；空闲看门狗把模型迁回内存并释放显存。双路并发首拉由进程锁保护，不会双拉。
- **如实说明**：需要 NVIDIA GPU；首次启动加载模型需要 30 秒到数分钟；语音识别前有分级降噪链（默认 RNNoise 档，约 130ms 延迟）。

### MiMo-V2.5-ASR 本地 sidecar

小米 MiMo 大模型 ASR 的本地部署（`scripts/setup-mimo-asr.sh` 下载模型与 Audio Tokenizer，`switch-mimo-asr.sh` 启用）。模型大、资源占用高，属于「想本地跑更大模型」的进阶选项，日常使用与 Qwen3-ASR 无本质差异。

### MiMo 云端 ASR（Token Plan）

- **切换**：`VOICE_IME_MIMO_API_KEY='tp-xxxxx' ./scripts/switch-mimo-cloud-asr.sh cn`（cn = 中国集群，另有 sgp/ams/payg）。
- OpenAI-compatible 接口，适合**无 NVIDIA GPU** 的机器；音频会上传到小米服务器（隐私取舍自担）。

### 火山引擎豆包 bigmodel ASR（极速版）

- **切换**：`VOICE_IME_VOLC_API_KEY='xxxxx' ./scripts/switch-volc-bigmodel-asr.sh`。
- 录音文件 base64 直传、异步提交 + 轮询；资源 ID `volc.bigasr.auc_turbo`；支持标点、ITN（数字/单位规范化）、云端语义顺滑（DDC），并且是**热词直传支持最完整的后端**：自定义词典的标准词与别名列会作为 `corpus.context` 热词上传（上限 5000）。
- 同样无 GPU 门槛，音频上传火山服务器。

### faster-whisper（诊断 / 兜底）

历史上曾作为默认 STT 后端，现在定位是**诊断与对照工具**：`./setup-asr.sh` 建独立 venv 安装 CTranslate2 链路，`./scripts/check-gpu-stt.sh` 验证 GPU STT。显式切换时默认走 CUDA（`VOICE_IME_REQUIRE_GPU_STT=1` 防止误回退 CPU）。用它做识别质量对照、麦克风链路诊断都很方便。

### vosk

装 `vosk` 包并设置 `VOICE_IME_VOSK_MODEL` 指向模型目录即可。CPU 可跑、完全离线，适合极简环境；中文准确率与 Qwen3-ASR 有代差，不作为推荐后端。

### 自定义命令（扩展点）

```bash
export VOICE_IME_ASR_CMD='my-asr-command {wav}'
```

命令收到 WAV 路径、把识别文本打到 stdout 即可接入——任何自建识别服务（自托管 whisper-server、内网模型服务等）都能零代码接入本输入法。

## 密钥与隐私策略

- 云端 Key **不写入任何持久化配置**：推荐用 Bitwarden Secrets Manager，`run-engine.sh` 启动时运行时注入引擎进程；该策略由 `tests/test_secret_persistence_policy.py` 守护。
- 本地后端（Qwen3-ASR / MiMo sidecar / vosk / 自定义命令）音频不出本机。
- 云端后端（MiMo 云 / 火山）音频会上传服务商，按需取舍。

## 选型建议

- 有 NVIDIA GPU：**默认 Qwen3-ASR 1.7B**，无需任何改动。
- 无 GPU：MiMo 云（Token Plan）或火山豆包，二选一自备 Key；在意隐私则考虑 vosk / 自定义命令接自托管服务。
- 排查识别问题：切 faster-whisper 做对照，配合三份日志定位（`engine.log` / `error.log` / `qwen-asr-server.log`）。
