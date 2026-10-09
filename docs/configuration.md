# 统一 JSON 配置（config.json）

本项目所有可调项（渠道、录音、UI、提示词……）统一走一份 JSON 配置。
本文说明四层优先级、`config.json` 与 `config/defaults.json` 的关系、CLI 用法、
提示词自定义、完整键位参考，以及老用户（environment.d 时代）的迁移路径。

实现：`src/ibus_voice_ime/config.py`；默认值：`config/defaults.json`；
守护测试：`tests/test_config.py` / `tests/test_channel_config.py`。

## 四层优先级

每个配置键按下面的顺序解析，**先命中先用**（first hit wins）：

```text
配置解析顺序（高 → 低）
│
├─ 1. 进程环境变量 VOICE_IME_*        ← 救援值 / BWS 注入 / 临时调试；
│                                       完整兼容 env-only 时代的行为
│
├─ 2. 用户配置 ~/.config/ibus-voice-ime/config.json
│                                       ← 只放需要覆盖默认值的键（稀疏文件）；
│                                       位置可用 VOICE_IME_CONFIG 改写
│
├─ 3. 仓库默认值 config/defaults.json  ← 随版本更新；不建议直接改它
│
└─ 4. 调用点默认值（代码里 get(..., default=...)）
                                        ← 最后兜底；defaults.json 中为 null
                                          的叶子表示"无静态默认"，落到这一层
```

要点：

- **环境变量仍然最高优先**——临时覆盖（`VOICE_IME_ASR_BACKEND=siliconflow-asr …`）
  永远有效，这也是 BWS 密钥注入的通道；
- 用户 `config.json` 是**稀疏覆盖**：只写想改的键，删掉某键即回落默认值，
  兄弟键/兄弟组不受影响（深合并语义）；
- `defaults.json` 里值为 `null` 的叶子（如 `llm.temperature`、`recording.max_seconds`）
  表示默认值在运行期动态计算，落到调用点默认。

## config.json 与 defaults.json 的关系

| 文件 | 角色 | 谁写它 |
|---|---|---|
| `config/defaults.json` | 仓库默认值，进 git，含全部键 | 维护者（随版本演进） |
| `~/.config/ibus-voice-ime/config.json` | 用户覆盖层，只含改动过的键 | `config set` CLI / switch 脚本 / 你本人 |

用户文件里**不需要**拷贝整棵默认树；两份文件深合并后生效。
`config validate` 会检查用户文件里的未知键与类型错误，
`config.py` 与 `defaults.json` 之间还有一张双向锁（ENV_MAP ↔ 叶子集合，
见 `tests/test_config.py`）保证文档表不漂移。

**密钥与敏感信息不进任何 JSON**：API Key 由环境变量 / Bitwarden Secrets
Manager 在运行时注入；config.json 里持久化的只有 BWS **密钥名**
（`asr.*.api_key_secret`）。LLM 连接配置走 `~/.config/ibus-voice-ime/llm.json`
（`setup-llm-cloud.sh` 体系，0600，gitignore）。

## 渠道选择：asr.backend

`asr.backend` 是渠道唯一事实源（默认 `qwen3-asr`）。`scripts/switch-*.sh`
现在把渠道与后端参数写进 `config.json`（不再写 environment.d 渠道行），例如：

```bash
VOICE_IME_SILICONFLOW_API_KEY='sk-xxx' ./scripts/switch-siliconflow-asr.sh
# → config.json: {"asr": {"backend": "siliconflow-asr", "siliconflow": {...}}}
```

手动切换渠道不需要碰脚本（同样在仓库根目录）：

```bash
PYTHONPATH=src python3 -m ibus_voice_ime.config set asr.backend volc-bigmodel-asr
```

`VOICE_IME_ASR_BACKEND` 环境变量仍可临时压过 config.json（优先级语义），
各后端的 legacy flag（`VOICE_IME_QWEN_ASR=1` 等）同理。

## CLI（六个命令）

以下命令在仓库根目录执行（`PYTHONPATH=src` 使包可导入；
`./install.sh` 内部同样以该方式调用）：

```bash
# 1) 读某个键的生效值（按四层优先级解析）
PYTHONPATH=src python3 -m ibus_voice_ime.config get asr.backend

# 2) 写一个键到用户 config.json（按 defaults.json 的叶子类型自动转换；
#    也可显式 --bool / --int / --float）
PYTHONPATH=src python3 -m ibus_voice_ime.config set asr.backend siliconflow-asr
PYTHONPATH=src python3 -m ibus_voice_ime.config set recording.max_seconds 120  # 数值型会存成 JSON number
PYTHONPATH=src python3 -m ibus_voice_ime.config set llm.timeout 8 --float

# 3) 打印全部生效值为 KEY=VALUE 行（供 shell 消费；跳过密钥与多行提示词）
PYTHONPATH=src python3 -m ibus_voice_ime.config env

# 4) 生成带注释性占位键的用户 config.json 骨架（已存在则保持不动）
PYTHONPATH=src python3 -m ibus_voice_ime.config init

# 5) 校验用户 config.json（未知键/类型错误；doctor.sh 的"统一配置"节也用它）
PYTHONPATH=src python3 -m ibus_voice_ime.config validate

# 6) 把 environment.d 等 env 文件里的映射键迁入 config.json，
#    stdout 输出清理后的文件内容（调用方重定向回写）
PYTHONPATH=src python3 -m ibus_voice_ime.config migrate-env-file ~/.config/environment.d/ibus-voice-ime.conf
```

引擎进程内 API：`from ibus_voice_ime import config` 后用
`config.get / env_str / env_bool / env_int / env_float / get_prompt`，
语义与上面完全一致。

## 提示词自定义

所有内置提示词都外置到了 `llm.prompts.*` / `asr.prompts.*`，可直接在
config.json 覆盖（多行文本写进 JSON 最自然；环境变量通道只作逃生口）：

```json
{
  "llm": {
    "prompts": {
      "system": "你是我的听写整理助手。只做两件事：补齐中文标点；把并列内容拆成 - 列表。不要润色、不要扩写、不要加任何解释。"
    }
  },
  "asr": {
    "prompts": {
      "whisper_punctuation": "请在转写中输出自然标点（逗号/句号/问号），不要输出解释。"
    }
  }
}
```

等效的 CLI 单行写法（仓库根目录执行；多行提示词建议直接编辑 config.json）：

```bash
PYTHONPATH=src python3 -m ibus_voice_ime.config set llm.prompts.system '你是我的听写整理助手……'
PYTHONPATH=src python3 -m ibus_voice_ime.config set asr.prompts.whisper_punctuation '请输出自然标点。'
```

缺省提示词全文见 `config/defaults.json`（下表中对长文本做了截断展示）。

## 配置键参考表

完整键位、默认值、对应环境变量与说明（共 200+ 个叶子）：

<!-- BEGIN: 由 tools/gen-config-docs.py 生成的 schema 表（勿手改） -->

| 点路径 | 默认值 | 环境变量（最高优先覆盖） | 说明 |
|---|---|---|---|
| `asr.backend` | `"qwen3-asr"` | `VOICE_IME_ASR_BACKEND` | 渠道唯一事实源：qwen3-asr / mimo-asr / mimo-cloud(-asr) / volc-bigmodel-asr / siliconflow(-asr) / faster-whisper / vosk / 自定义命令 |
| `asr.command` | `""`（空） | `VOICE_IME_ASR_CMD` | 自定义 STT 命令（`{wav}` 占位），stdout 即识别文本 |
| `asr.sidecar_max_body_bytes` | `1048576` | `VOICE_IME_ASR_SIDECAR_MAX_BODY_BYTES` | sidecar HTTP 请求体上限（字节） |
| `asr.qwen3.enabled` | `false` | `VOICE_IME_QWEN_ASR` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.host` | `"127.0.0.1"` | `VOICE_IME_QWEN_ASR_HOST` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.port` | `18081` | `VOICE_IME_QWEN_ASR_PORT` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.python` | `""`（空） | `VOICE_IME_QWEN_ASR_PYTHON` | sidecar 专用 venv 的 python 路径（switch 脚本自动写入） |
| `asr.qwen3.model` | `"1.7b"` | `VOICE_IME_QWEN_ASR_MODEL` | 本地 Qwen3-ASR 档位：0.6b / 1.7b |
| `asr.qwen3.model_path` | `""`（空） | `VOICE_IME_QWEN_ASR_MODEL_PATH` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.language` | `null`（动态默认/回退调用点） | `VOICE_IME_QWEN_ASR_LANGUAGE` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.dtype` | `"bfloat16"` | `VOICE_IME_QWEN_ASR_DTYPE` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.device_map` | `"cuda:0"` | `VOICE_IME_QWEN_ASR_DEVICE_MAP` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.device_offload_target` | `"cpu"` | `VOICE_IME_QWEN_ASR_DEVICE_OFFLOAD_TARGET` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.max_batch` | `1` | `VOICE_IME_QWEN_ASR_MAX_BATCH` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.max_new_tokens` | `1024` | `VOICE_IME_QWEN_ASR_MAX_NEW_TOKENS` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.attn` | `""`（空） | `VOICE_IME_QWEN_ASR_ATTN` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.start_timeout` | `180` | `VOICE_IME_QWEN_ASR_START_TIMEOUT` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.timeout` | `180` | `VOICE_IME_QWEN_ASR_TIMEOUT` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.fail_cooldown` | `180` | `VOICE_IME_QWEN_ASR_FAIL_COOLDOWN` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.idle_timeout` | `5.0` | `VOICE_IME_QWEN_ASR_IDLE_TIMEOUT` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.idle_check_interval` | `1.0` | `VOICE_IME_QWEN_ASR_IDLE_CHECK_INTERVAL` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.vram_min_mib_1_7b` | `5000` | `VOICE_IME_QWEN_ASR_VRAM_MIN_MIB_1_7B` | 本地 Qwen3-ASR sidecar |
| `asr.qwen3.vram_min_mib_0_6b` | `2000` | `VOICE_IME_QWEN_ASR_VRAM_MIN_MIB_0_6B` | 本地 Qwen3-ASR sidecar |
| `asr.mimo.enabled` | `false` | `VOICE_IME_MIMO_ASR` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.host` | `"127.0.0.1"` | `VOICE_IME_MIMO_ASR_HOST` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.port` | `18082` | `VOICE_IME_MIMO_ASR_PORT` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.python` | `""`（空） | `VOICE_IME_MIMO_ASR_PYTHON` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.models_dir` | `""`（空） | `VOICE_IME_MIMO_ASR_MODELS_DIR` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.model` | `"XiaomiMiMo/MiMo-V2.5-ASR"` | `VOICE_IME_MIMO_ASR_MODEL` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.model_path` | `""`（空） | `VOICE_IME_MIMO_ASR_MODEL_PATH` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.tokenizer` | `"XiaomiMiMo/MiMo-Audio-Tokenizer"` | `VOICE_IME_MIMO_ASR_TOKENIZER` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.tokenizer_path` | `""`（空） | `VOICE_IME_MIMO_ASR_TOKENIZER_PATH` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.source` | `""`（空） | `VOICE_IME_MIMO_ASR_SOURCE` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.device` | `""`（空） | `VOICE_IME_MIMO_ASR_DEVICE` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.start_timeout` | `300` | `VOICE_IME_MIMO_ASR_START_TIMEOUT` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.timeout` | `300` | `VOICE_IME_MIMO_ASR_TIMEOUT` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.language` | `"auto"` | `VOICE_IME_MIMO_ASR_LANGUAGE` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo.audio_tag` | `""`（空） | `VOICE_IME_MIMO_ASR_AUDIO_TAG` | 本地 MiMo-V2.5-ASR sidecar |
| `asr.mimo_cloud.enabled` | `false` | `VOICE_IME_MIMO_CLOUD_ASR` | MiMo 云端 ASR / Token Plan |
| `asr.mimo_cloud.base_url` | `null`（动态默认/回退调用点） | `VOICE_IME_MIMO_CLOUD_BASE_URL` | MiMo 云端 ASR / Token Plan |
| `asr.mimo_cloud.model` | `"mimo-v2.5-asr"` | `VOICE_IME_MIMO_CLOUD_ASR_MODEL` | MiMo 云端 ASR / Token Plan |
| `asr.mimo_cloud.language` | `null`（动态默认/回退调用点） | `VOICE_IME_MIMO_CLOUD_ASR_LANGUAGE` | MiMo 云端 ASR / Token Plan |
| `asr.mimo_cloud.auth_header` | `"api-key"` | `VOICE_IME_MIMO_CLOUD_AUTH_HEADER` | MiMo 云端 ASR / Token Plan |
| `asr.mimo_cloud.timeout` | `120` | `VOICE_IME_MIMO_CLOUD_ASR_TIMEOUT` | MiMo 云端 ASR / Token Plan |
| `asr.mimo_cloud.max_data_mb` | `10` | `VOICE_IME_MIMO_CLOUD_ASR_MAX_DATA_MB` | MiMo 云端 ASR / Token Plan |
| `asr.mimo_cloud.api_key_secret` | `"XIAOMI_TOKEN_PLAN_CN_API_KEY"` | `VOICE_IME_MIMO_API_KEY_SECRET` | BWS 密钥名（不是密钥本体）；run-engine.sh 按名注入 |
| `asr.volc.enabled` | `false` | `VOICE_IME_VOLC_BIGMODEL_ASR` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.base_url` | `null`（动态默认/回退调用点） | `VOICE_IME_VOLC_BIGMODEL_BASE_URL` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.resource_id` | `"volc.bigasr.auc_turbo"` | `VOICE_IME_VOLC_BIGMODEL_RESOURCE_ID` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.model_name` | `"bigmodel"` | `VOICE_IME_VOLC_BIGMODEL_MODEL_NAME` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.language` | `""`（空） | `VOICE_IME_VOLC_BIGMODEL_LANGUAGE` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.enable_itn` | `true` | `VOICE_IME_VOLC_BIGMODEL_ENABLE_ITN` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.enable_punc` | `true` | `VOICE_IME_VOLC_BIGMODEL_ENABLE_PUNC` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.enable_ddc` | `true` | `VOICE_IME_VOLC_BIGMODEL_ENABLE_DDC` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.show_utterances` | `true` | `VOICE_IME_VOLC_BIGMODEL_SHOW_UTTERANCES` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.hotwords` | `true` | `VOICE_IME_VOLC_BIGMODEL_HOTWORDS` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.hotwords_max` | `5000` | `VOICE_IME_VOLC_BIGMODEL_HOTWORDS_MAX` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.boosting_table` | `""`（空） | `VOICE_IME_VOLC_BIGMODEL_BOOSTING_TABLE` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.correct_table` | `""`（空） | `VOICE_IME_VOLC_BIGMODEL_CORRECT_TABLE` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.submit_timeout` | `30` | `VOICE_IME_VOLC_BIGMODEL_SUBMIT_TIMEOUT` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.query_timeout` | `30` | `VOICE_IME_VOLC_BIGMODEL_QUERY_TIMEOUT` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.poll_interval` | `1.0` | `VOICE_IME_VOLC_BIGMODEL_POLL_INTERVAL` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.total_timeout` | `120` | `VOICE_IME_VOLC_BIGMODEL_TOTAL_TIMEOUT` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.max_data_mb` | `25` | `VOICE_IME_VOLC_BIGMODEL_MAX_DATA_MB` | 火山引擎豆包 bigmodel ASR |
| `asr.volc.api_key_secret` | `"VOLC_BIGMODEL_ASR_API_KEY"` | `VOICE_IME_VOLC_API_KEY_SECRET` | BWS 密钥名（不是密钥本体）；run-engine.sh 按名注入 |
| `asr.siliconflow.enabled` | `false` | `VOICE_IME_SILICONFLOW_ASR` | 硅基流动 SiliconFlow ASR（Beta） |
| `asr.siliconflow.base_url` | `null`（动态默认/回退调用点） | `VOICE_IME_SILICONFLOW_BASE_URL` | 硅基流动 SiliconFlow ASR（Beta） |
| `asr.siliconflow.model` | `null`（动态默认/回退调用点） | `VOICE_IME_SILICONFLOW_MODEL` | 模型名，如 FunAudioLLM/SenseVoiceSmall（免费）/ Qwen/Qwen3-ASR-1.7B |
| `asr.siliconflow.timeout` | `120` | `VOICE_IME_SILICONFLOW_TIMEOUT` | 硅基流动 SiliconFlow ASR（Beta） |
| `asr.siliconflow.max_data_mb` | `50` | `VOICE_IME_SILICONFLOW_MAX_DATA_MB` | 硅基流动 SiliconFlow ASR（Beta） |
| `asr.siliconflow.api_key_secret` | `"SILICONFLOW_API_KEY"` | `VOICE_IME_SILICONFLOW_API_KEY_SECRET` | BWS 密钥名（不是密钥本体）；run-engine.sh 按名注入 |
| `asr.whisper.model` | `"large-v3"` | `VOICE_IME_WHISPER_MODEL` | faster-whisper 诊断/兜底后端 |
| `asr.whisper.device` | `"cuda"` | `VOICE_IME_WHISPER_DEVICE` | faster-whisper 诊断/兜底后端 |
| `asr.whisper.device_index` | `0` | `VOICE_IME_WHISPER_DEVICE_INDEX` | faster-whisper 诊断/兜底后端 |
| `asr.whisper.compute` | `"float16"` | `VOICE_IME_WHISPER_COMPUTE` | faster-whisper 诊断/兜底后端 |
| `asr.whisper.language` | `"zh"` | `VOICE_IME_WHISPER_LANGUAGE` | faster-whisper 诊断/兜底后端 |
| `asr.whisper.prompt` | `""`（空） | `VOICE_IME_WHISPER_PROMPT` | faster-whisper 诊断/兜底后端 |
| `asr.whisper.require_gpu` | `false` | `VOICE_IME_REQUIRE_GPU_STT` | 显式要求 GPU STT，防误回退 CPU |
| `asr.vosk.model` | `""`（空） | `VOICE_IME_VOSK_MODEL` | vosk 后端 |
| `asr.prompts.whisper_punctuation` | `"请更积极地输出自然中文标点：根据语义加入逗号、句号、问号、顿号、分号或冒号，避免…"`（长文本，全文见 defaults.json） | `VOICE_IME_PROMPT_WHISPER_PUNCTUATION` | faster-whisper 标点提示词（多行文本） |
| `llm.postprocess` | `false` | `VOICE_IME_LLM_POSTPROCESS` | LLM 后处理总开关 |
| `llm.internal` | `null`（动态默认/回退调用点） | `VOICE_IME_LLM_INTERNAL` | 内部（引擎内嵌）LLM 路径开关；null = 跟随部署形态 |
| `llm.rerank` | `false` | `VOICE_IME_LLM_RERANK` |  |
| `llm.trust_output` | `true` | `VOICE_IME_LLM_TRUST_OUTPUT` |  |
| `llm.aggressive` | `false` | `VOICE_IME_LLM_AGGRESSIVE` |  |
| `llm.base_url` | `"http://127.0.0.1:18080/v1"` | `VOICE_IME_LLM_BASE_URL` | OpenAI-compatible LLM 端点；连接密钥走 llm.json，不放这里 |
| `llm.model` | `"qwen3.5-0.8b"` | `VOICE_IME_LLM_MODEL` |  |
| `llm.max_tokens` | `1024` | `VOICE_IME_LLM_MAX_TOKENS` |  |
| `llm.timeout` | `4.0` | `VOICE_IME_LLM_TIMEOUT` | LLM 请求超时（秒） |
| `llm.temperature` | `null`（动态默认/回退调用点） | `VOICE_IME_LLM_TEMPERATURE` |  |
| `llm.min_chars` | `50` | `VOICE_IME_LLM_MIN_CHARS` | 短于该字符数的识别结果跳过 LLM 后处理 |
| `llm.candidates` | `1` | `VOICE_IME_LLM_CANDIDATES` | LLM 标点候选数 |
| `llm.protect_tokens` | `true` | `VOICE_IME_LLM_PROTECT_TOKENS` |  |
| `llm.strict_safety` | `true` | `VOICE_IME_LLM_STRICT_SAFETY` |  |
| `llm.max_expansion_ratio` | `null`（动态默认/回退调用点） | `VOICE_IME_LLM_MAX_EXPANSION_RATIO` |  |
| `llm.conservative_margin` | `18.0` | `VOICE_IME_LLM_CONSERVATIVE_MARGIN` |  |
| `llm.conservative_min_similarity` | `null`（动态默认/回退调用点） | `VOICE_IME_LLM_CONSERVATIVE_MIN_SIMILARITY` |  |
| `llm.conservative_min_ratio` | `0.78` | `VOICE_IME_LLM_CONSERVATIVE_MIN_RATIO` |  |
| `llm.conservative_max_ratio` | `1.18` | `VOICE_IME_LLM_CONSERVATIVE_MAX_RATIO` |  |
| `llm.fallback_raw` | `true` | `VOICE_IME_LLM_FALLBACK_RAW` |  |
| `llm.extra_prompt` | `""`（空） | `VOICE_IME_LLM_EXTRA_PROMPT` | 追加到 system 提示词的额外要求 |
| `llm.terms_max_chars` | `4000` | `VOICE_IME_LLM_TERMS_MAX_CHARS` |  |
| `llm.llama_legacy.host` | `"127.0.0.1"` | `VOICE_IME_LLAMA_HOST` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.port` | `18080` | `VOICE_IME_LLAMA_PORT` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.server` | `""`（空） | `VOICE_IME_LLAMA_SERVER` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.model_path` | `""`（空） | `VOICE_IME_LLAMA_MODEL_PATH` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.ctx_size` | `2048` | `VOICE_IME_LLAMA_CTX_SIZE` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.threads` | `null`（动态默认/回退调用点） | `VOICE_IME_LLAMA_THREADS` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.batch_size` | `128` | `VOICE_IME_LLAMA_BATCH_SIZE` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.n_gpu_layers` | `""`（空） | `VOICE_IME_LLAMA_N_GPU_LAYERS` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.no_webui` | `true` | `VOICE_IME_LLAMA_NO_WEBUI` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.jinja` | `true` | `VOICE_IME_LLAMA_JINJA` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.reasoning` | `"off"` | `VOICE_IME_LLAMA_REASONING` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.chat_template_kwargs` | `""`（空） | `VOICE_IME_LLAMA_CHAT_TEMPLATE_KWARGS` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.args` | `""`（空） | `VOICE_IME_LLAMA_ARGS` | 已弃用的本地 llama.cpp sidecar |
| `llm.llama_legacy.start_timeout` | `20` | `VOICE_IME_LLAMA_START_TIMEOUT` | 已弃用的本地 llama.cpp sidecar |
| `llm.prompts.system` | `"你是语音输入法的后处理器。用户的听写绝大多数是发给 AI 编程助手的指令，你的任…"`（长文本，全文见 defaults.json） | `VOICE_IME_PROMPT_SYSTEM` | LLM 后处理 system 提示词（多行文本，覆盖示例见文档） |
| `llm.prompts.candidate_system` | `"你是语音输入法的轻量后处理器。
只生成积极补齐标点后的文本；候选之间主要只能有标…"`（长文本，全文见 defaults.json） | `VOICE_IME_PROMPT_CANDIDATE_SYSTEM` | 候选生成专用轻量 system 提示词 |
| `llm.prompts.format_requirement` | `"格式要求：输出规范 Markdown，能分点就分点——并列的要点/步骤/条件/要…"`（长文本，全文见 defaults.json） | `VOICE_IME_PROMPT_FORMAT_REQUIREMENT` | 输出格式要求片段 |
| `llm.prompts.mode_instructions.dictation` | `"主要任务：补齐标点，并把内容整理成适合 AI 消费的规范 Markdown——任…"`（长文本，全文见 defaults.json） | `VOICE_IME_PROMPT_MODE_DICTATION` | 听写模式附加指令 |
| `llm.prompts.mode_instructions.literal` | `"尽量原样输出；除添加/调整标点、空白和明显识别错误外，不要改动文字。"` | `VOICE_IME_PROMPT_MODE_LITERAL` | 原样模式附加指令 |
| `llm.prompts.mode_instructions.markdown` | `"积极补齐标点；不要主动整理结构，除非原文已经明显是 Markdown。"` | `VOICE_IME_PROMPT_MODE_MARKDOWN` | Markdown 模式附加指令 |
| `llm.prompts.mode_instructions.prompt` | `"补齐标点并整理成清晰的提示词/任务简报结构（目标/背景/任务/约束/验收）；不得…"`（长文本，全文见 defaults.json） | `VOICE_IME_PROMPT_MODE_PROMPT` | 提示词模式附加指令 |
| `llm.prompts.mode_instructions.command` | `"尽量原样保留命令、路径、参数和英文符号；只在安全时补标点。"` | `VOICE_IME_PROMPT_MODE_COMMAND` | 命令模式附加指令 |
| `recording.trigger_mode` | `"toggle"` | `VOICE_IME_TRIGGER_MODE` | toggle：按一次开始再按一次停止；fixed：固定时长 |
| `recording.hotkeys` | `null`（动态默认/回退调用点） | `VOICE_IME_HOTKEYS` | 语音热键（仅支持 Ctrl+Alt+字母） |
| `recording.hotkey` | `""`（空） | `VOICE_IME_HOTKEY` | 录音/热键/VAD |
| `recording.raw_hotkeys` | `"Ctrl+Alt+B"` | `VOICE_IME_RAW_HOTKEYS` | 原文语音热键（跳过 LLM 后处理） |
| `recording.clipboard_hotkeys` | `null`（动态默认/回退调用点） | `VOICE_IME_CLIPBOARD_HOTKEYS` | 输入法内粘贴热键 |
| `recording.clipboard_hotkey` | `""`（空） | `VOICE_IME_CLIPBOARD_HOTKEY` | 录音/热键/VAD |
| `recording.clipboard_max_chars` | `20000` | `VOICE_IME_CLIPBOARD_MAX_CHARS` | 录音/热键/VAD |
| `recording.clipboard_prepare_delay_seconds` | `0.3` | `VOICE_IME_CLIPBOARD_PREPARE_DELAY_SECONDS` | 录音/热键/VAD |
| `recording.clipboard_prepare_delay_ms` | `null`（动态默认/回退调用点） | `VOICE_IME_CLIPBOARD_PREPARE_DELAY_MS` | 录音/热键/VAD |
| `recording.clipboard_prepare_hint_ms` | `null`（动态默认/回退调用点） | `VOICE_IME_CLIPBOARD_PREPARE_HINT_MS` | 录音/热键/VAD |
| `recording.arecord_device` | `""`（空） | `VOICE_IME_ARECORD_DEVICE` | 直采 ALSA 设备（如 plughw:M2,0）；空 = 系统默认源 |
| `recording.voice_mode` | `"dictation"` | `VOICE_IME_VOICE_MODE` | 默认语音模式：dictation / literal / markdown / prompt / command |
| `recording.record_seconds` | `5` | `VOICE_IME_RECORD_SECONDS` | fixed 模式固定录音时长（秒） |
| `recording.max_seconds` | `null`（动态默认/回退调用点） | `VOICE_IME_MAX_RECORD_SECONDS` | toggle 模式最长录音时长（秒） |
| `recording.min_record_seconds` | `0.4` | `VOICE_IME_MIN_RECORD_SECONDS` | 录音/热键/VAD |
| `recording.silence_stop_ms` | `1000.0` | `VOICE_IME_SILENCE_STOP_MS` | 录音/热键/VAD |
| `recording.no_speech_timeout` | `2.5` | `VOICE_IME_NO_SPEECH_TIMEOUT` | 录音/热键/VAD |
| `recording.vad_auto_stop` | `false` | `VOICE_IME_VAD_AUTO_STOP` | fixed 模式静音自动停止 |
| `recording.vad_fallback_fixed` | `true` | `VOICE_IME_VAD_FALLBACK_FIXED` | VAD 失败时回退固定时长录音 |
| `recording.vad_rms_threshold` | `500.0` | `VOICE_IME_VAD_RMS_THRESHOLD` | 录音/热键/VAD |
| `recording.toggle_silence_auto_stop` | `true` | `VOICE_IME_TOGGLE_SILENCE_AUTO_STOP` | 录音/热键/VAD |
| `recording.toggle_sound_level` | `0.04` | `VOICE_IME_TOGGLE_SOUND_LEVEL` | 录音/热键/VAD |
| `recording.toggle_min_record_seconds` | `0.8` | `VOICE_IME_TOGGLE_MIN_RECORD_SECONDS` | 录音/热键/VAD |
| `recording.toggle_silence_seconds` | `2.5` | `VOICE_IME_TOGGLE_SILENCE_SECONDS` | 录音/热键/VAD |
| `recording.toggle_no_speech_timeout` | `8.0` | `VOICE_IME_TOGGLE_NO_SPEECH_TIMEOUT` | 录音/热键/VAD |
| `audio.preprocess` | `true` | `VOICE_IME_AUDIO_PREPROCESS` | 音频预处理 |
| `audio.highpass` | `true` | `VOICE_IME_AUDIO_HIGHPASS` | 音频预处理 |
| `audio.highpass_freq` | `null`（动态默认/回退调用点） | `VOICE_IME_AUDIO_HIGHPASS_FREQ` | 音频预处理 |
| `audio.notch` | `true` | `VOICE_IME_AUDIO_NOTCH` | 音频预处理 |
| `audio.denoise` | `false` | `VOICE_IME_AUDIO_DENOISE` | 音频预处理 |
| `audio.denoise_amount` | `0.3` | `VOICE_IME_AUDIO_DENOISE_AMOUNT` | 音频预处理 |
| `audio.noise_profile_ms` | `400` | `VOICE_IME_AUDIO_NOISE_PROFILE_MS` | 音频预处理 |
| `audio.normalize` | `true` | `VOICE_IME_AUDIO_NORMALIZE` | 音频预处理 |
| `audio.normalize_headroom` | `3.0` | `VOICE_IME_AUDIO_NORMALIZE_HEADROOM` | 音频预处理 |
| `audio.denoise_tier` | `"rnnoise"` | `VOICE_IME_DENOISE_TIER` | 降噪档位：none / notch / rnnoise |
| `audio.rnnoise_model` | `""`（空） | `VOICE_IME_AUDIO_RNNOISE_MODEL` | 音频预处理 |
| `overlay.enabled` | `false` | `VOICE_IME_OVERLAY` | 独立语音状态弹窗开关 |
| `overlay.position` | `"top-center"` | `VOICE_IME_OVERLAY_POSITION` | 语音状态弹窗 |
| `overlay.catch_hotkey` | `false` | `VOICE_IME_OVERLAY_CATCH_HOTKEY` | 语音状态弹窗 |
| `overlay.buttons` | `false` | `VOICE_IME_OVERLAY_BUTTONS` | 语音状态弹窗 |
| `overlay.rms_full_scale` | `3000.0` | `VOICE_IME_OVERLAY_RMS_FULL_SCALE` | 语音状态弹窗 |
| `ui.candidate_ui` | `"popup"` | `VOICE_IME_CANDIDATE_UI` | popup：传统 IBus/GNOME 竖向候选框；inline：行内候选 |
| `ui.candidate_page_size` | `5` | `VOICE_IME_CANDIDATE_PAGE_SIZE` | 候选框/输入 UI |
| `ui.candidate_adaptive_anchor` | `true` | `VOICE_IME_CANDIDATE_ADAPTIVE_ANCHOR` | 候选框/输入 UI |
| `ui.candidate_row_px` | `58` | `VOICE_IME_CANDIDATE_ROW_PX` | 候选框/输入 UI |
| `ui.candidate_padding_px` | `28` | `VOICE_IME_CANDIDATE_PADDING_PX` | 候选框/输入 UI |
| `ui.candidate_gap_px` | `8` | `VOICE_IME_CANDIDATE_GAP_PX` | 候选框/输入 UI |
| `ui.preedit_mirror` | `"off"` | `VOICE_IME_PREEDIT_MIRROR` | 候选框/输入 UI |
| `ui.preedit_line_px` | `36` | `VOICE_IME_PREEDIT_LINE_PX` | 候选框/输入 UI |
| `ui.inline_candidates` | `null`（动态默认/回退调用点） | `VOICE_IME_INLINE_CANDIDATES` | 行内候选数量；null = 跟随 candidate_page_size |
| `ui.inline_label_chars` | `10` | `VOICE_IME_INLINE_LABEL_CHARS` | 候选框/输入 UI |
| `ui.inline_max_chars` | `96` | `VOICE_IME_INLINE_MAX_CHARS` | 候选框/输入 UI |
| `ui.commit_delay_ms` | `200` | `VOICE_IME_COMMIT_DELAY_MS` | 候选框/输入 UI |
| `ui.keyboard_backend` | `"rime"` | `VOICE_IME_KEYBOARD_BACKEND` | rime：RIME 键盘引擎；demo：core.py 演示词库 |
| `ui.start_ascii` | `"0"` | `VOICE_IME_START_ASCII` | 启动后先进入英文模式 |
| `ui.shift_toggle_ascii` | `true` | `VOICE_IME_SHIFT_TOGGLE_ASCII` | 按一下 Shift 切换中/英 |
| `ui.ascii_slash` | `"1"` | `VOICE_IME_ASCII_SLASH` | 候选框/输入 UI |
| `text.chinese_script` | `"simplified"` | `VOICE_IME_CHINESE_SCRIPT` | simplified / traditional / none（最终提交前繁简转换） |
| `text.opencc_t2s_config` | `"/usr/share/opencc/t2s.json"` | `VOICE_IME_OPENCC_T2S_CONFIG` | 文本规整 |
| `text.opencc_s2t_config` | `"/usr/share/opencc/s2t.json"` | `VOICE_IME_OPENCC_S2T_CONFIG` | 文本规整 |
| `text.cjk_latin_space` | `true` | `VOICE_IME_CJK_LATIN_SPACE` | 中文与拉丁字符间补空格 |
| `text.voice_commands` | `true` | `VOICE_IME_VOICE_COMMANDS` | 语音口令转符号（换行/逗号/句号…） |
| `text.inline_voice_commands` | `false` | `VOICE_IME_INLINE_VOICE_COMMANDS` | 识别非独立口令（默认关闭，防误触发） |
| `text.remove_fillers` | `true` | `VOICE_IME_REMOVE_FILLERS` | 移除独立口头禅 |
| `text.voice_replacements` | `true` | `VOICE_IME_VOICE_REPLACEMENTS` | 文本规整 |
| `text.auto_punct_max_cjk_per_clause` | `24` | `VOICE_IME_AUTO_PUNCT_MAX_CJK_PER_CLAUSE` | 文本规整 |
| `text.asr_context_max_chars` | `3000` | `VOICE_IME_ASR_CONTEXT_MAX_CHARS` | 文本规整 |
| `text.voice_dictionary` | `"~/.local/share/ibus-voice-ime/voice-dict…"`（长文本，全文见 defaults.json） | `VOICE_IME_VOICE_DICTIONARY` | 热词/纠词词典路径（volc 热词直传也用它） |
| `text.chinese_user_dict` | `"~/.local/share/ibus-voice-ime/chinese.json"` | `VOICE_IME_CHINESE_USER_DICT` | 文本规整 |
| `text.english_user_dict` | `"~/.local/share/ibus-voice-ime/english.json"` | `VOICE_IME_ENGLISH_USER_DICT` | 文本规整 |
| `text.english_memory_enabled` | `true` | `VOICE_IME_VOICE_USE_ENGLISH_MEMORY` | 文本规整 |
| `text.english_memory_min_freq` | `2` | `VOICE_IME_VOICE_ENGLISH_MEMORY_MIN_FREQ` | 文本规整 |
| `text.english_memory_max_terms` | `80` | `VOICE_IME_VOICE_ENGLISH_MEMORY_MAX_TERMS` | 文本规整 |
| `text.english_memory_min_len` | `2` | `VOICE_IME_VOICE_ENGLISH_MEMORY_MIN_LEN` | 文本规整 |
| `text.english_memory_exclude_pinyin` | `true` | `VOICE_IME_VOICE_ENGLISH_MEMORY_EXCLUDE_PINYIN` | 文本规整 |
| `rime.schema` | `"rime_ice"` | `VOICE_IME_RIME_SCHEMA` | RIME 方案；未部署雾凇时自动回退 luna_pinyin_simp |
| `rime.library` | `""`（空） | `VOICE_IME_RIME_LIBRARY` | librime.so 路径（Fedora 可指系统库） |
| `rime.shared_data_dir` | `""`（空） | `VOICE_IME_RIME_SHARED_DATA_DIR` | RIME 键盘后端 |
| `rime.staging_dir` | `""`（空） | `VOICE_IME_RIME_STAGING_DIR` | RIME 键盘后端 |
| `rime.user_data_dir` | `"~/.local/share/ibus-voice-ime/rime-user"` | `VOICE_IME_RIME_USER_DATA_DIR` | RIME 键盘后端 |
| `rime.allow_system` | `false` | `VOICE_IME_RIME_ALLOW_SYSTEM` | RIME 键盘后端 |
| `rime.log_level` | `null`（动态默认/回退调用点） | `VOICE_IME_RIME_LOG_LEVEL` | RIME 键盘后端 |
| `rime.log_dir` | `""`（空） | `VOICE_IME_RIME_LOG_DIR` | RIME 键盘后端 |
| `logging.keep_lines` | `1000` | `VOICE_IME_LOG_KEEP_LINES` | 每次启动裁剪日志保留行数；0 关闭 |
| `logging.log_dir` | `"~/.local/share/ibus-voice-ime"` | `VOICE_IME_LOG_DIR` | 日志 |
| `logging.error_log` | `"~/.local/share/ibus-voice-ime/error.log"` | `VOICE_IME_ERROR_LOG` | 日志 |
| `logging.llm_log` | `null`（动态默认/回退调用点） | `VOICE_IME_LLM_LOG` | 日志 |
| `ipc.enabled` | `true` | `VOICE_IME_IPC` | 本地 IPC socket（voice-toggle.sh / 粘贴热键用） |
| `ipc.socket_path` | `""`（空） | `VOICE_IME_IPC_SOCKET` | 本地 IPC |

<!-- END: schema 表 -->

表由 `tools/gen-config-docs.py` 从 `config/defaults.json` 与
`src/ibus_voice_ime/config.py` 的 `ENV_MAP` 自动生成；改了默认值后运行
`env python3 tools/gen-config-docs.py --write` 刷新本文档。

## 老用户迁移（environment.d 时代 → config.json）

- **旧的 `~/.config/environment.d/ibus-voice-ime.conf` 渠道行仍然生效**：
  run-engine.sh 启动时仍会行级解析该文件（路径键长期保留）。但注意——
  environment.d 属于 env 层，**会压过 config.json 的渠道选择**。如果发现
  `config set asr.backend …` "不生效"，先检查这份文件里是否残留
  `VOICE_IME_ASR_BACKEND=` / `VOICE_IME_QWEN_ASR=` 等渠道行；
- **重跑 `./install.sh` 自动迁移**：安装脚本会 `config init` 生成骨架，并把
  environment.d 里的映射键 `migrate-env-file` 进 config.json（原文件只删
  渠道行、保留路径行；路径型渠道键（如 `VOICE_IME_QWEN_ASR_MODEL_PATH`）
  拒绝迁移、留在 env 文件以保住 run-engine.sh 的陈旧路径自愈层；密钥本体
  与无对应配置键的行会在迁移报告中逐行点名，不静默丢弃）；
- **switch 脚本自动清理**：任一 `scripts/switch-*-asr.sh` 运行时都会
  删除 environment.d 里的全部渠道行并 `systemctl --user unset-environment`
  同步清掉 systemd 用户管理器里的渠道键，防止残留 env 压过 config.json；
  同时写全五个渠道 `enabled` 叶子（目标=1 其余=0），清掉迁移可能残留的
  `asr.<x>.enabled=true` 旧渠道标志（否则 voice.py 渠道链先命中旧渠道）；
- `./scripts/doctor.sh` 的"统一配置"体检节会报告 config.json 校验问题、
  environment.d 渠道行残留，以及 enabled 标志与 asr.backend 不一致。

## 相关文档

- [README](../README.md) —— 安装、渠道切换入口
- [asr-backends](asr-backends.md) —— 各 STT 后端细节与回退方式
- [architecture](architecture.md) —— 引擎结构（配置层在其中的位置）
