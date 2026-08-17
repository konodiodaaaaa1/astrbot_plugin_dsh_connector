# AstrBot DeepSeek Harness Connector

将 AstrBot 连接到本机 [DeepSeek Harness](https://github.com/deepseek-ai/DeepSeek-Harness) Web 主机。插件为每个聊天窗口维护独立 DSH 会话，并把 DSH 的文本与图片输出呈现为 AstrBot 消息链。

## 功能

- `/dsh <任务>` 发送任务，支持独立多轮会话。
- 读取和切换 DSH provider、模型、推理强度、权限预设与 Agent 预设。
- 查看会话、历史、运行状态，取消任务，向进行中的任务发送 `steer` 指令。
- 解析 DSH 内容块、Markdown 图片、data URL、本地文件和 HTTP(S) 图片，发送为 AstrBot `Image` 消息组件。
- 提供 `dsh_bridge_*` LLM 工具：状态、会话、模型、创建会话、发送任务、模型/权限设置和停止任务。

## 配置

启动 DSH Web 主机，例如：

```powershell
dsh web --port 3080
```

插件默认地址为 `http://127.0.0.1:3080`。可在 AstrBot 的插件配置页设置：默认工作目录、Agent 预设、provider/model、推理强度、权限预设、会话持久化和图片限额。

`enable_llm_tools` 默认关闭。开启后，AstrBot 的 LLM 能调用名称以 `dsh_bridge_` 开头的会话控制工具。

## 聊天命令

```text
/dsh status
/dsh sessions
/dsh model
/dsh model deepseek-official/deepseek-v4-pro high
/dsh effort max
/dsh permission workspace-write
/dsh preset standard
/dsh steer 先停止当前步骤，改为只检查测试结果
/dsh stop
/dsh history 30
/dsh settings
/dsh reset
```

`/dsh preset` 保存为当前聊天的新会话预设；执行 `/dsh reset` 后生效。`/dsh model` 显示当前 DSH 实例实际提供的模型和推理档位。

## 图片渲染

远程图片和 data URL 会下载到临时文件后作为 AstrBot `Image` 组件发送。`max_images_per_reply`、`max_image_bytes`、`image_download_timeout` 与 `allow_remote_images` 控制该过程。插件卸载时会清理它创建的临时图片。

## 发布

源码仓库：[konodiodaaaaa1/astrbot_plugin_dsh_connector](https://github.com/konodiodaaaaa1/astrbot_plugin_dsh_connector)。可通过 AstrBot 插件市场提交入口提交该仓库地址。

## 许可证

[MIT](LICENSE)
