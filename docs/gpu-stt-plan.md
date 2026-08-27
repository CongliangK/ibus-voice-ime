> 历史文档：本文写于项目以 faster-whisper 为默认 STT 后端的阶段。
> 现状：默认后端是本地 Qwen3-ASR（见 README「语音输入」），faster-whisper 仅作为
> 诊断/兜底后端保留（`./setup-asr.sh` 可切换）。文中"Fedora 43"为写作时环境。

# GPU STT 调研与落地方案

## 结论

本项目继续使用 `faster-whisper` 作为默认 STT 后端，但运行设备从 CPU 切换为 NVIDIA CUDA：

- 目标机器：Fedora 43，NVIDIA GeForce RTX 4080 16GB，驱动 580.159.03，`nvidia-smi` 可见 CUDA 13.0。
- 当前方案：`faster-whisper` + `CTranslate2` + `device=cuda` + `compute_type=float16`。
- 默认模型：`small`，保证输入法短句低延迟；如更重视准确率，可改 `medium` 或 `large-v3`。
- 防误回退：默认设置 `VOICE_IME_REQUIRE_GPU_STT=1`，如果设备不是 `cuda` 会报错而不是静默走 CPU。

## 调研要点

1. `faster-whisper` 是基于 CTranslate2 的 Whisper 实现，官方 benchmark 显示 GPU 上比 OpenAI Whisper 更快，并支持 FP16/INT8 等量化。
2. `faster-whisper` GPU 运行依赖 NVIDIA CUDA 运行时库，当前 CTranslate2 新版本主要面向 CUDA 12 + cuDNN 9；如果系统没有 cuBLAS/cuDNN，可通过 NVIDIA pip wheels 或系统 CUDA 包补齐。
3. GPU 上推荐：
   - `float16`：RTX 4080 上速度和准确率平衡最好。
   - `int8_float16`：显存更省，速度也好，但可能有轻微准确率损失。
   - CPU 上才推荐 `int8`，本项目已不把 CPU 作为默认路径。
4. 输入法场景是短音频、低延迟，优先级是：热启动缓存模型、固定 CUDA 设备、避免 PyAV 解码问题、保持后处理链路不变。

## 已实施改动

- `voice.py`
  - 默认 `VOICE_IME_WHISPER_DEVICE=cuda`、`VOICE_IME_WHISPER_COMPUTE=float16`。
  - 支持 `VOICE_IME_WHISPER_DEVICE_INDEX`。
  - 支持 `VOICE_IME_REQUIRE_GPU_STT=1` 防止误用 CPU。
- `run-engine.sh`
  - IBus 运行时默认启用 CUDA STT。
  - 自动把 NVIDIA pip wheel 的 cuBLAS/cuDNN lib 目录加入 `LD_LIBRARY_PATH`。
- `setup-asr.sh` / `install.sh`
  - 持久化 GPU STT 环境变量。
  - 未检测到 CUDA 且未显式允许 CPU 时，`setup-asr.sh` 直接失败。
- `scripts/check-gpu-stt.sh`
  - 快速验证 CTranslate2 是否能看到 CUDA，并实际执行一次 faster-whisper CUDA 转写。

## 使用方案

```bash
cd ~/ibus-voice-ime
./setup-asr.sh
./scripts/check-gpu-stt.sh
```

常用配置：

```bash
export VOICE_IME_WHISPER_MODEL=small      # 低延迟默认
export VOICE_IME_WHISPER_MODEL=medium     # 更高准确率
export VOICE_IME_WHISPER_MODEL=large-v3   # 最高准确率，首次加载更慢
export VOICE_IME_WHISPER_DEVICE=cuda
export VOICE_IME_WHISPER_DEVICE_INDEX=0
export VOICE_IME_WHISPER_COMPUTE=float16
export VOICE_IME_REQUIRE_GPU_STT=1
```

如 CUDA 库缺失，可补齐后重启 IBus：

```bash
cd ~/ibus-voice-ime
.venv/bin/python -m pip install 'nvidia-cublas-cu12' 'nvidia-cudnn-cu12==9.*'
./setup-asr.sh
ibus restart
```

## 验收标准

1. `nvidia-smi` 能看到 RTX 4080。
2. `./scripts/check-gpu-stt.sh` 输出 `OK: faster-whisper is running on cuda:0 / float16`。
3. 输入法语音状态显示 `faster-whisper small / cuda:0 / float16`。
4. 录音识别期间 `nvidia-smi` 中能看到 Python/IBus 进程出现短暂 GPU 计算/显存占用。
