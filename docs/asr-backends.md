# 语音后端支持

本项目把「语音识别」抽象成可插拔后端，通过统一配置的 `asr.backend`（环境变量 `VOICE_IME_ASR_BACKEND` 仍可临时覆盖）与一组开关选择。switch 脚本现在把渠道与后端设置写入 `~/.config/ibus-voice-ime/config.json`（渠道 = `asr.backend`，单一事实源），不再写 environment.d 渠道行（脚本会顺带清掉旧的渠道行残留）。完整配置键位见 [configuration.md](configuration.md)。

## 后端总览

| 后端 | 形态 | 硬件/账号要求 | 状态定位 |
|---|---|---|---|
| **Qwen3-ASR sidecar**（0.6B / 1.7B） | 本地 HTTP sidecar（127.0.0.1:18081） | NVIDIA GPU（1.7B bf16 约 4GB+ 显存） | **默认后端**（1.7B） |
| MiMo-V2.5-ASR sidecar | 本地 HTTP sidecar（127.0.0.1:18082） | 更大显存/内存，需下载源码与 Audio Tokenizer | 可选本地大模型 |
| MiMo 云端 ASR（Token Plan） | OpenAI-compatible 云 API | 无 GPU；需小米 API Key | 无 GPU 机器的推荐选择 |
| 火山引擎豆包 bigmodel ASR（极速版） | 云 API（异步提交 + 轮询，base64 直传） | 无 GPU；需火山 API Key | 云端备选，热词支持最全 |
| 硅基流动 SiliconFlow ASR（Beta） | 云 API（multipart 直传） | 无 GPU；需硅基流动 API Key（SenseVoiceSmall 官方标注免费） | 无 GPU 低门槛首选（未经深度测试，见 Beta 说明） |
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

### 硅基流动 SiliconFlow ASR（低门槛云后端，**Beta**）

> **Beta 状态说明（未经深度测试）**：本后端 2026-10-08 才合入，只做过**一次性验收**
> 与 mock 单元测试，没有经过日常长期使用的深度验证。能用、可回退，但请知悉以下边界：
>
> - **已验证**（一次性真实验收）：SenseVoiceSmall 与 `Qwen/Qwen3-ASR-1.7B` 各完成一次真实
>   转写并成功返回中文文本；401 错误路径返回带指引的中文报错；完整引擎链路
>   （`voice.transcribe`，含 sox 预处理）跑通；切换脚本的配置写入/切走清理在沙箱验证通过；
>   20 项 mock 单元测试（multipart 字节结构、错误分支、密钥不落盘策略）。
> - **未验证 / 已知风险**：
>   - 长期高频使用下的稳定性、免费/低峰档 **429 限流的真实阈值与恢复行为**未实测；
>   - **计费信息存在官方渠道间不一致**：营销定价页标注 SenseVoiceSmall 免费，但 Qwen3-ASR
>     实际按秒计费（¥0.00022/秒）——一切以你的控制台账单为准；
>   - 接近 5 分钟长录音（约 9.6MB WAV）与官方 ≤50MB/≤1h 上限的交互未实测；
>   - 验收样本为合成语音：SenseVoiceSmall 对非常规音色的识别质量明显弱于 Qwen3-ASR-1.7B，
>     标点/数字规整（ITN）风格与本地 Qwen3-ASR 的差异未做系统对比；
>   - 与 LLM 云端后处理叠加长听写的组合场景未实测。
> - **回退**：遇到问题随时切回其他后端——`./scripts/switch-qwen-asr.sh`（本地 GPU）、
>   `./scripts/switch-mimo-cloud-asr.sh cn`、`./scripts/switch-volc-bigmodel-asr.sh`；
>   或提 issue 附 `~/.local/share/ibus-voice-ime/*.log`。
> - API 文档：<https://api-docs.siliconflow.cn/docs/api/audio-transcriptions-post>

- **切换**：`VOICE_IME_SILICONFLOW_API_KEY='sk-xxxxx' ./scripts/switch-siliconflow-asr.sh`。
- 注册 [cloud.siliconflow.cn](https://cloud.siliconflow.cn) 创建 API Key；默认模型 `FunAudioLLM/SenseVoiceSmall`（官方定价页标注**免费**），可选 `Qwen/Qwen3-ASR-1.7B`（经 `VOICE_IME_SILICONFLOW_MODEL` 切换，识别质量更好，按音频时长计费约 **¥0.00022/秒 ≈ 0.0132 元/分钟**）。
- **计费以控制台账单为准**（营销定价页信息可能滞后）；有 RPM/TPM 限流（HTTP 429），触发时稍等重试或换模型。
- 接口为 `POST /v1/audio/transcriptions`（multipart/form-data 直传音频）；官方限额单文件 **≤50MB**、时长 **≤1h**。
- `api.siliconflow.cn` 大陆可直连，无需代理；音频会上传硅基流动服务器（隐私取舍自担）。

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
- 云端后端（MiMo 云 / 火山 / 硅基流动）音频会上传服务商，按需取舍。

## 选型建议

- 有 NVIDIA GPU：**默认 Qwen3-ASR 1.7B**，无需任何改动。
- 无 GPU：**硅基流动（Beta，未经深度测试）**（SenseVoiceSmall 官方标注免费；追求质量可换 Qwen3-ASR-1.7B，约 ¥0.0132/分钟）首选；或 MiMo 云（Token Plan）/ 火山豆包，自备 Key；在意隐私则考虑 vosk / 自定义命令接自托管服务。
- 排查识别问题：切 faster-whisper 做对照，配合三份日志定位（`engine.log` / `error.log` / `qwen-asr-server.log`）。
