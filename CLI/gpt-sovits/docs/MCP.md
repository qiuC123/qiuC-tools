# GPT-SoVITS MCP

本地 stdio MCP，将现有 CLI 的推理功能提供给支持 MCP 的客户端。
底层仍使用真实 GPT-SoVITS；不需要操作 WebUI，不包含模型或音频。
协议实现使用 [官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)，
依赖固定在受维护的 v1 范围 `mcp>=1.30,<2`，避免未经验证的跨主版本升级。

## 安装

在 `CLI/gpt-sovits/agent-harness` 中执行：

```powershell
py -3.13 -m pip install -e ".[mcp]"
gpt-sovits-mcp --help
```

适配器使用 Python 3.10+，可以安装到独立虚拟环境。GPT-SoVITS 的 Python/PyTorch
运行环境由 `--runtime` 指定，不需要向模型运行环境安装 MCP SDK。
原有 `cli-anything-gpt-sovits` 命令保持兼容；只安装 CLI 不需要 MCP extra。

## 客户端配置

将下面条目合并进客户端的 MCP 配置，不要覆盖其他服务。把三个占位路径替换为本机绝对路径。
`command` 是安装了本项目 `[mcp]` 的 Python，`--runtime` 是能运行 GPT-SoVITS 的 Python；
两者通常不同。JSON 中 Windows 路径可使用 `/`，使用 `\` 时要写成 `\\`。

```json
{
  "mcpServers": {
    "gpt-sovits": {
      "command": "<MCP Python absolute path>",
      "args": [
        "-m", "cli_anything.gpt_sovits.mcp_server",
        "--checkout", "<GPT-SoVITS checkout absolute path>",
        "--runtime", "<GPT-SoVITS Python absolute path>"
      ],
      "env": {"PYTHONUTF8": "1"}
    }
  }
}
```

也可以用安装后的 `gpt-sovits-mcp` 可执行文件绝对路径作为 `command`，去掉 `-m` 及模块名。
服务使用 stdin/stdout 传输协议，正常启动不会显示交互菜单，不要向 stdout 写普通日志。
首次加载模型可能耗时几分钟，客户端工具调用超时建议至少 660 秒（并发排队或长文本需要更长）。

启动参数同时支持环境变量：

| 参数 | 环境变量 | 默认行为 |
| --- | --- | --- |
| `--checkout` | `GPT_SOVITS_CHECKOUT` | MCP 必须显式配置源码目录 |
| `--runtime` | `GPT_SOVITS_RUNTIME` | Windows 为源码下 `.conda/python.exe`；其他系统为 `.conda/bin/python` |
| `--api-url` | `GPT_SOVITS_API_URL` | `http://127.0.0.1:9880`，只允许本机地址 |
| `--tts-config` | `GPT_SOVITS_TTS_CONFIG` | 源码目录下 `GPT_SoVITS/configs/tts_infer.yaml` |
| `--state-dir` | `GPT_SOVITS_STATE_DIR` | 与现有 CLI 相同的本机状态目录 |

## 工具

| 工具 | 用途 |
| --- | --- |
| `gpt_sovits_doctor` | 检查源码、运行时、模型、GPU 和 API |
| `gpt_sovits_serve_start` | 启动后端并等待就绪 |
| `gpt_sovits_serve_status` | 查看状态与进程身份；CLI 会记录状态检查事件 |
| `gpt_sovits_serve_stop` | 停止身份验证通过、由 CLI 管理的后端 |
| `gpt_sovits_serve_logs` | 读取服务生命周期日志 |
| `gpt_sovits_model_list` | 列出本机模型 |
| `gpt_sovits_model_use_gpt` | 显式切换运行中服务的 GPT 权重 |
| `gpt_sovits_model_use_sovits` | 显式切换运行中服务的 SoVITS 权重 |
| `gpt_sovits_reference_inspect` | 检查参考 WAV 格式、时长、音量和哈希 |
| `gpt_sovits_synthesize` | 合成并验证非静音 WAV，返回文件绝对路径和参数 |

工具参数使用下划线，例如 CLI `--ref-audio` 对应 MCP `ref_audio`。
类型、必填项、默认值和数值范围直接从 CLI 生成；变更工具均提供 `dry_run`。
成功结果在 `structuredContent` 和 JSON 文本中返回同一 `{ok, command, data, warnings, error}`。
CLI 执行失败设置 MCP `isError=true` 并保留结构化错误；协议参数校验错误由 SDK 返回。

推荐调用顺序：`doctor` → `serve_status` → 必要时 `serve_start` →
`reference_inspect` → `synthesize`。合成示例参数：

```json
{
  "text": "欢迎回来，今天我们继续讲这个故事。",
  "text_lang": "zh",
  "ref_audio": "<reference WAV absolute path>",
  "prompt_lang": "zh",
  "prompt_text": "参考音频中实际说出的完整文字",
  "output": "<output WAV absolute path>",
  "text_split_method": "cut2",
  "speed_factor": 1.0,
  "temperature": 1.0,
  "seed": 42,
  "dry_run": true
}
```

核对参数后将 `dry_run` 改成 `false` 生成音频。输出父目录必须已存在，默认拒绝覆盖；
仅显式设置 `overwrite=true` 才覆盖。`text` 和 `text_file` 必须且只能提供一个。
上述 `cut2` 是示例显式选择，未提供时仍沿用 CLI 默认 `cut5`，不会改写默认音色或参数。
参考音频、模型和输出都属于运行 MCP 的设备；MCP 返回路径，不会自动传输音频至另一台设备。

## 生命周期与范围

- MCP 启动不自动加载模型、训练或切换音色。本次只封装推理工具，数据准备/训练仍使用原 CLI。
- 使用 CLI 创建的隔离配置副本，显式模型切换不修改上游默认配置。
- 单个 MCP 进程内工具调用串行执行，防止合成过程中切换模型。
  多个 MCP 进程或外部 CLI 不共享此锁，请勿同时操作同一后端的模型。
- 退出 MCP 不自动停止独立后端；释放 GPU 请调用 `gpt_sovits_serve_stop`。
- 超时/取消会回收当前 CLI 子进程，但已经发给后端的推理未必能取消；
  已启动的后台服务也可能继续运行。重试前先查服务状态与输出文件。
- 这是供受信任本机客户端使用的 stdio 服务，没有远程 HTTP MCP 或认证层。

## 测试

```powershell
py -3.13 -m pip install -e ".[mcp,test]"
$env:CLI_ANYTHING_FORCE_INSTALLED = "1"
py -3.13 -m pytest cli_anything/gpt_sovits/tests/test_mcp.py -v
```

真实推理测试还需要本机 GPT-SoVITS、可运行模型的隔离环境、模型权重、GPU 和 Windows
`Microsoft Huihui Desktop` 系统语音。系统语音只生成测试参考，最终 WAV 必须由真实 GPT-SoVITS 生成。
设置 `GPT_SOVITS_CHECKOUT` 和 `GPT_SOVITS_E2E_RUNTIME` 后运行：

```powershell
py -3.13 -m pytest cli_anything/gpt_sovits/tests/test_mcp_e2e.py -v -s
```

缺少后端时测试失败，不会伪造或跳过。测试使用临时独立端口与状态目录，不切换已有服务。
真实 MCP 验收覆盖中文合成；模型切换工具在 MCP 验收中只执行 dry-run，真实切换由既有 CLI E2E 覆盖。
英文及中英混合文本依赖上游英文资源（例如 NLTK `cmudict`）；当前本机发现该资源缺失，
未下载或验证英文合成。MCP 会如实返回后端错误，不会自动补装模型或语言资源。
