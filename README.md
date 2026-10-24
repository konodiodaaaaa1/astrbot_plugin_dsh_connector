# AstrBot DSH Connector

面向 DeepSeek Harness（DSH 0.2+）的 AstrBot 连接器，支持两种平级入口：`dsh web` 起的 Web Host，以及本机正在运行的 DeepSeek Harness 桌面应用。每个 AstrBot 聊天窗口拥有独立的 DSH 会话绑定和创建选项，任务结果以文本、图片及 DSH 原生 attachment 形式回传。

仓库地址：<https://github.com/konodiodaaaaa1/astrbot_plugin_dsh_connector>

## 启动

连接器在插件配置的 `mode` 里选择入口：

- `http`（默认）：连接 `dsh web` 起的 Web Host。先启动：

```powershell
dsh web --port 3080
```

插件默认连接 `http://127.0.0.1:3080`。

- `app`：连接本机 DeepSeek Harness 桌面应用。不需要额外启动 `dsh web`；只要桌面应用在运行，插件会从本机进程表找到它的 Web Host 进程（`dsh-desktop-host`），自动使用其随机回环端口，并复用同一份 `$DSH_HOME/.credentials.yaml` 签发 cookie。自动发现失败时，在 `app_base_url` 填入应用 Host 地址（例如 `http://127.0.0.1:10404`）。

- `auto`：依次尝试 `http`、`app` 两个入口，都连接不上时回退 `headless`。

AstrBot 插件配置页负责连接方式、两个入口地址、DSH 主目录、默认工作目录、超时、图片限额和 LLM 工具开关。

## 兼容性与认证

插件调用 DSH 0.2 起的 Typert RPC：端点为 `/api/<namespace>/<method>`，请求体是 `{"args": {...}}`。DSH 0.2 同时给 `/api` 加了浏览器会话认证——只有启动时打印的 `?token=` URL 能换取 `dsh-auth-*` 签名 cookie，且不接受 `Authorization` 头。插件不要求复制那个一次性 token，而是读取 DSH 持久化的签名密钥（`$DSH_HOME/.credentials.yaml` 里的 `client-connection/browser-session` 记录）在本地现签 cookie，所以 DSH 重启后无需重新配置。

桌面应用的 `app` 入口共用同一套认证：桌面应用内部同样以 `runProfile()` 拉起 Web 应用，只是绑定 `127.0.0.1` 的随机端口。插件自动发现该端口后，用同一份凭据现签 cookie，因此应用重启换了端口也能重新连上，无需重新配置。

`dsh_home` 留空时按环境变量 `DSH_HOME`、再退回 `~/.dsh` 定位；DSH 装在别处时把它指过去即可。工作区列表也从同一目录的 `storages/workspace.json` 读取，因为 DSH 0.2 不再提供 `workspace.list`。`http` 与 `app` 两个入口都只接受回环地址（`app` 由插件自动发现或由 `app_base_url` 指定），DSH 的 Host 校验会拒绝其它 authority；新建会话未指定目录时，依次使用配置 `default_cwd`、DSH 已登记的第一个工作区路径、用户主目录。

`headless` 模式仍可单独使用（`dsh --profile headless "<指令>"`），但它每条消息都会起一个新进程，也无法复用 HTTP 模式按聊天绑定的会话，仅建议作为应急回退；`auto` 在两个 HTTP 入口都不可用时才会走到它。

## 聊天窗口配置

发送以下命令进入交互向导：

```text
/dsh setup
```

向导依次选择：

1. DSH 工作空间（可选）
2. 工作目录
3. Agent Preset
4. Provider 与模型
5. 推理强度
6. 权限预设
7. IANA 客户端时区
8. 确认创建

工作空间、Agent Preset、模型和权限预设均从当前 DSH 主机实时读取。工作空间与工作目录二选一；选择工作空间会使用 DSH `workspaceId` 创建会话，输入目录会改用该目录。它们按聊天窗口保存在 AstrBot KV 中，不进入插件固定配置。完成向导后，连接器会创建并绑定新 DSH 会话。其他聊天窗口使用各自的选项和绑定。

也可以直接管理当前窗口的选项：

```text
/dsh config
/dsh config set workspace <workspaceId>
/dsh config set cwd D:\AI\workspace
/dsh config set preset standard
/dsh config set model deepseek-official/deepseek-v4-pro
/dsh config set effort high
/dsh config set permission <DSH 返回的权限预设>
/dsh config set timezone Asia/Shanghai
/dsh config clear effort
/dsh config reset
/dsh session new
```

`/dsh session new` 使用当前聊天保存的选项创建会话。未配置的字段沿用 DSH 主机或模型默认值。

`/dsh permission` 会读取并显示当前 DSH 实际提供的权限预设；`/dsh setup` 的权限步骤使用同一份动态列表。

`/dsh session delete` 会要求输入 `delete` 再确认，并调用 DSH 的 `workspace.archiveSession` 从 DSH 会话列表移除目标会话；DSH 保留归档会话的历史日志。

## 会话与任务

```text
/dsh <任务>
/dsh steer <补充指令>
/dsh stop
/dsh approve
/dsh deny
/dsh approvals
/dsh sessions
/dsh session switch <sessionId>
/dsh session search <关键词>
/dsh session rename <标题>
/dsh session fork [seq]
/dsh session delete [sessionId]
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
- LLM providers 与模型目录（`session/modelCatalog`、`llm/listConfigurableProviders`）
- Agent Presets、Skills、Subagents、Goals、Workspaces
- Session search、fork、queue、cancel 和 attachment

会话历史与回复轮询走 `session/projections`（取当前 `asOfSeq` 游标）加 `session/page`（按游标翻页），不再依赖 DSH 0.2 已删除的 `session.history`。

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

## 实时回复

HTTP/auto 模式等待 DSH turn 完成后，将最终文本、卡片和图片组件组成消息链发送。普通长度回复保持一条消息；超过 QQ 单条消息限制时按换行边界有序切片。卡片、DSH attachment 与其他图片仍会在任务完成后继续发送。

单个 turn 超过 `command_timeout`（默认 600 秒）时，插件不再直接放弃：它会把「仍在后台执行」的提示发给聊天，并继续在后台监视该会话，turn 结束（或失败）后把最终结果主动发到原聊天。监视上限由 `completion_watch_timeout` 控制（默认 6 小时），可以用 `/dsh stop` 取消 turn。

## 授权请求

DSH 的 `approval/request` 不是 HTTP RPC，而是经 `/api/remote.mux` WebSocket 下发的 waterfall。插件用 `route_approvals` 开关（默认开启）在后台维持这条事件桥；收到属于某个聊天所绑定会话的授权请求时，会把工具名、原因和会话 ID 发到该聊天，等待 `/dsh approve`（允许一次）或 `/dsh deny`（拒绝），再通过 `$events/result` 把结论送回 DSH。等待期间 turn 保持挂起，授权完成后继续执行；DSH 界面里取消请求时聊天也会收到失效提示。从 DSH Web 界面直接创建的会话没有聊天绑定，仍由界面自己的授权弹窗处理。

## 长任务与进程重启

插件在 `initialize()` 时建立事件桥，DSH 侧断线会退避重连；DSH 会把尚未回答的授权请求重新投递给新连接，因此插件重载后仍能补齐待授权提示。会话到聊天的反向索引保存在 AstrBot KV 中，重启 AstrBot 不会丢失授权路由。

## LLM 工具

`enable_llm_tools` 开启后注册 `dsh_connector_*` 工具，覆盖状态、会话、模型、配置发现、Skills、Subagents、Goals 和 Workspaces。

`enable_llm_mutation_tools` 单独控制 LLM 对 DSH Settings 的写入能力。

## 许可证

[MIT](LICENSE)
