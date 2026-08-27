# 第三方组件与许可证声明

本项目是一个"胶水"工程，自身代码之外捆绑/引用了大量第三方组件。本文件是完整的第三方清单，各自保留其原始许可证。

## 一、仓库内捆绑（vendor/）

### vendor/rime/ — librime 运行时与 Rime 数据

| 组件 | 路径 | 许可证 | 上游 |
|---|---|---|---|
| librime | `vendor/rime/lib/librime.so*` | BSD-3-Clause（上游声明）；Fedora 打包元数据记录为 GPL-3.0-only，本仓库按更严格的 GPL-3.0 对待 | https://github.com/rime/librime |
| librime-lua 插件 | `vendor/rime/lib/rime-plugins/librime-lua.so` | 同上（librime 仓库插件） | https://github.com/rime/librime |
| Rime 官方数据（brise） | `vendor/rime/share/rime-data/` 中的 luna_pinyin / pinyin_simp / cangjie5 / stroke / wubi86 / terra_pinyin / essay 等及其 `build/` 编译产物 | GPL-3.0 | https://github.com/rime / 发行版 `brise` 包 |
| 雾凇拼音 rime-ice | `vendor/rime/share/rime-data/` 中的 `rime_ice.*`、`cn_en*.txt`、`lua/`、`melt_eng*`、`en_dicts/`、`symbols*.yaml`、`emoji*` 等 | GPL-3.0 | https://github.com/iDvel/rime-ice |
| OpenCC 数据与库 | `vendor/rime/share/opencc/`、`vendor/rime/lib/libopencc.so*` | Apache-2.0 | https://github.com/BYVoid/OpenCC |

### vendor/rime/lib/ — 随 librime 捆绑的动态库依赖

| 库 | 许可证 |
|---|---|
| libglog / libgflags / libleveldb / libsnappy | BSD-3-Clause |
| liblua-5.4 | MIT |
| libmarisa | LGPL-2.1-or-later |
| libyaml-cpp | MIT |
| libgcc_s / libstdc++ | GPL-3.0-with-GCC-exception（运行库例外） |

以上二进制文件均复制自本机发行版软件包（`scripts/vendor-rime-runtime.sh` 可重新生成）。

## 二、安装/设置时下载（不进 git 仓库）

| 组件 | 获取脚本 | 许可证 |
|---|---|---|
| rime-ice 完整词库（cn_dicts 等） | `scripts/setup-rime-ice.sh` | GPL-3.0 |
| zhwiki 词典 | 同上 | Unlicense（https://github.com/felixonmars/fcitx5-pinyin-zhwiki） |
| moegirl 词典 | 同上 | 数据源自萌娘百科，内容为 CC-BY-NC-SA（非商用）；由 mw2fcitx 生成（https://github.com/outloudvi/mw2fcitx） |
| Qwen3-ASR 0.6B / 1.7B | `scripts/setup-qwen-asr.sh` | Apache-2.0 |
| Qwen3.5-0.8B GGUF | `scripts/setup-llm.sh` | Apache-2.0 |
| MiMo-V2.5-ASR / MiMo-Audio-Tokenizer | `scripts/setup-mimo-asr.sh` | Apache-2.0 |
| RNNoise 模型 `bd.rnnn` | `scripts/fetch-rnnoise-model.sh` | RNNoise 系 BSD-3-Clause 生态的训练模型 |

## 三、Python 依赖（requirements-asr.txt）

| 包 | 许可证 |
|---|---|
| faster-whisper | MIT |
| httpx[socks] | BSD-3-Clause |
| nvidia-cublas/cudnn/cuda-nvrtc pip wheels | NVIDIA CUDA 组件，按 NVIDIA 最终用户许可协议再分发限制，由 pip 自行下载 |

## 四、云服务 API（仅代码集成，无内容再分发）

- 小米 MiMo 云端 ASR（Token Plan）、火山引擎豆包 bigmodel ASR：本仓库只包含 API 调用代码，使用需自备账号与密钥。

## 许可证结论

本仓库整体以 **GPL-3.0** 发布（见 [LICENSE](LICENSE)）。该选择与仓库内捆绑的 Rime 数据（GPL-3.0）、rime-ice（GPL-3.0）兼容；各第三方组件的原始许可证与版权声明以本文件与上游仓库为准。
