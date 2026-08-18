# Changelog

## 2.0.1 - 2026-08-18

- 删除实时增量与文本分片发送，完整 DSH 回复统一作为一条消息链发送，避免 QQ 端按句子产生多条消息。
- card 模式关闭实时增量发送，避免流式 step 与最终 card 重复输出。
- 多 step 任务只使用最后一个已定稿的 `assistant/message` 作为最终回复。
- 增加 card 模式与多 step 回复回归测试。

## 2.0.0 - 2026-08-17

- 将权限预设切换到 DSH Typert `commands/execute` 接口，权限会真实写入目标会话。
- 新会话向导支持从 DSH 主机动态选择工作空间与权限预设。
- 支持 DSH `assistant/chunk` 实时文本回传，减少等待期间的空白。
- 完善 Markdown、代码块、表格、公式与图片内容的卡片渲染，并保留 DSH 原生 attachment 图片。
- 增加 `/dsh help` 帮助列表与 `/dsh session delete` 会话归档删除流程。
- 扩展会话级模型、推理强度、Agent Preset、时区和 DSH Settings 控制能力。
