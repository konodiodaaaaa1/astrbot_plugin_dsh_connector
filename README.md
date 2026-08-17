# AstrBot DSH Connector

面向 DeepSeek Harness Web Host 的 AstrBot 连接器。每个 AstrBot 聊天窗口拥有独立的 DSH 会话绑定和创建选项，任务结果以文本、图片及 DSH 原生 attachment 形式回传。

仓库地址：<https://github.com/konodiodaaaaa1/astrbot_plugin_dsh_connector>

## 启动

先启动本机 DSH Web Host：

```powershell
dsh web --port 3080
```

插件默认连接 `http://127.0.0.1:3080`。AstrBot 插件配置页负责连接地址、超时、图片限额和 LLM 工具开关。

## 聊天窗口配置

发送以下命令进入交互向导：

```text
/dsh setup
```

向导依次选择：

1. 工作目录
2. Agent Preset
3. Provider 与模型
4. 推理强度
5. 权限预设
6. IANA 客户端时区
7. 确认创建

选项按聊天窗口保存在 AstrBot KV 中。完成向导后，连接器会创建并绑定新 DSH 会话。其他聊天窗口使用各自的选项和绑定。

也可以直接管理当前窗口的选项：

```text
/dsh config
/dsh config set cwd D:\AI\workspace
/dsh config set preset standard
/dsh config set model deepseek-official/deepseek-v4-pro
/dsh config set effort high
/dsh config set permission workspace-write
/dsh config set timezone Asia/Shanghai
/dsh config clear effort
/dsh config reset
/dsh session new
```

`/dsh session new` 使用当前聊天保存的选项创建会话。未配置的字段沿用 DSH 主机或模型默认值。

## 会话与任务

```text
/dsh <任务>
/dsh steer <补充指令>
/dsh stop
/dsh sessions
/dsh session switch <sessionId>
/dsh session search <关键词>
/dsh session rename <标题>
/dsh session fork [seq]
/dsh history [条数]
/dsh reset
```

模型、Preset 和权限也支持在当前会话直接调整：

```text
/dsh model
/dsh model <provider/model> [reasoning_effort]
/dsh preset list
/dsh preset select <preset>
/dsh permission <preset>
```

## DSH 控制面

连接器直接调用 DSH RPC，可查询和控制以下域：

- Settings namespace、JSON Schema、路径级 `set`/`unset`
- LLM providers 与模型目录
- Agent Presets、Skills、Subagents、Goals、Workspaces
- Session search、fork、queue、cancel 和 attachment

示例：

```text
/dsh settings
/dsh setting schema llm-deepseek
/dsh setting get llm-deepseek models
/dsh setting set llm-deepseek maxTokens 128000
/dsh providers
/dsh global-models
/dsh skills
/dsh subagents
/dsh workspaces
/dsh goal
```

## 图片输出

以下图片来源会转为 AstrBot `Image` 消息组件：

- DSH `session.attachment` 引用
- DSH 图片内容块
- Markdown 图片链接
- `data:image/...` URL
- 本地文件路径及 HTTP(S) 图片

下载大小、数量和超时由插件配置页中的图片参数控制。连接器卸载时会清理其创建的临时图片。

## Markdown 卡片

插件配置中的 `reply_render_mode` 控制 DSH 文本输出：

- `text` 保持普通文本回复
- `auto` 在代码块、表格、标题、引用、公式、图片 Markdown 或较长内容时输出图片卡
- `card` 将所有 DSH 文本经 AstrBot t2i 渲染为图片卡

卡片使用 AstrBot 当前配置的 t2i 模板和端点，因此复用已有的 Markdown、Shiki 代码高亮与 KaTeX 公式渲染能力。`card_max_chars` 会对长回复分卡，保留每段代码围栏；t2i 不可用时自动发送原始文本。DSH 原生图片和 attachment 保持独立图片消息输出。

## LLM 工具

`enable_llm_tools` 开启后注册 `dsh_connector_*` 工具，覆盖状态、会话、模型、配置发现、Skills、Subagents、Goals 和 Workspaces。

`enable_llm_mutation_tools` 单独控制 LLM 对 DSH Settings 的写入能力。

## 许可证

[MIT](LICENSE)
