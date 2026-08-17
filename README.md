# AstrBot DeepSeek Harness Connector

将 AstrBot 连接到本机 [DeepSeek Harness](https://github.com/deepseek-ai/DeepSeek-Harness) Web 主机。插件为每个聊天窗口维护独立 DSH 会话，并把 DSH 的文本与图片输出呈现为 AstrBot 消息链。

## 功能

- `/dsh <任务>` 发送任务，支持独立多轮会话。
- 读取和切换 DSH provider、模型、推理强度、权限预设与 Agent 预设。
- 通过 DSH 原生 RPC 管理会话搜索、切换、重命名、分叉和任务队列。
- 读取 DSH 的完整设置命名空间，按路径以 `settings.mutate` 和 revision 保护写入配置。
- 查询 LLM 服务商和模型目录、Skills、子代理、Goals、工作区及会话投影数据。
- 解析 DSH 原生 attachment、内容块、Markdown 图片、data URL、本地文件和 HTTP(S) 图片，发送为 AstrBot `Image` 消息组件。
- 提供 19 个 `dsh_bridge_*` LLM 工具，覆盖状态、会话、模型、服务商、设置、Skills、子代理、Goals、工作区和任务控制。

## 配置

启动 DSH Web 主机，例如：

```powershell
dsh web --port 3080
```

插件默认地址为 `http://127.0.0.1:3080`。可在 AstrBot 的插件配置页设置：默认工作目录、Agent 预设、provider/model、推理强度、权限预设、会话持久化和图片限额。

`enable_llm_tools` 默认关闭。开启后，AstrBot 的 LLM 能调用名称以 `dsh_bridge_` 开头的状态、会话、模型、服务商、设置、Skills 和工作区工具。`enable_llm_mutation_tools` 默认关闭；它单独控制 LLM 经 `settings.mutate` 写入 DSH 设置。

## 聊天命令

```text
/dsh status
/dsh sessions
/dsh session search harness
/dsh session switch session-xxxxxxxx
/dsh session rename 新标题
/dsh session fork 120
/dsh model
/dsh model deepseek-official/deepseek-v4-pro high
/dsh providers
/dsh global-models
/dsh effort max
/dsh permission workspace-write
/dsh preset list
/dsh preset select standard
/dsh preset read standard
/dsh setting get llm-deepseek models
/dsh setting schema llm-deepseek
/dsh setting set llm-deepseek maxTokens 128000
/dsh skills
/dsh subagents
/dsh workspaces
/dsh goal create 完成当前连接器重构
/dsh queue steer message-id
/dsh steer 先停止当前步骤，改为只检查测试结果
/dsh stop
/dsh history 30
/dsh settings
/dsh reset
```

`/dsh preset list` 展示 DSH 已加载的预设，`select` 立即作用于当前会话；简写 `/dsh preset <名称>` 保存为当前聊天的新会话预设。`/dsh setting` 始终从 DSH 主机取得命名空间、当前 revision 和生效方式，`set`、`unset` 为用户显式调用的配置操作。

## 图片渲染

远程图片和 data URL 会下载到临时文件后作为 AstrBot `Image` 组件发送。`max_images_per_reply`、`max_image_bytes`、`image_download_timeout` 与 `allow_remote_images` 控制该过程。插件卸载时会清理它创建的临时图片。

## 发布

源码仓库：[konodiodaaaaa1/astrbot_plugin_dsh_connector](https://github.com/konodiodaaaaa1/astrbot_plugin_dsh_connector)。可通过 AstrBot 插件市场提交入口提交该仓库地址。

## 许可证

[MIT](LICENSE)
