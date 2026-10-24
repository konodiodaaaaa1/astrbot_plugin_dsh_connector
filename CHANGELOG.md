# Changelog

## 2.2.0 - 2026-10-24

- 新增 `app` 连接入口：直接连接本机正在运行的 DeepSeek Harness 桌面应用，与 `http` 的 Web Host 入口平级。桌面应用的 Web Host 绑定 `127.0.0.1` 的随机端口且不落盘，插件通过本机进程表（`dsh-desktop-host` 命令行特征）自动发现端口。
- 新增 `app_base_url` 配置：自动发现失败、应用不在默认位置或需要手动指定时使用；`app` 入口复用现有的浏览器会话凭据、Typert RPC、图片与 Markdown 卡片链路。
- `auto` 模式改为依次尝试 `http`、`app`，都不可用时回退 `headless`；执行前先做一次轻量入口探测，会话中途断线不会换入口重跑同一指令。`/dsh status` 与 `/dsh settings` 会显示当前入口地址。
- 授权事件桥支持按入口重建，并在每次重连前刷新 base URL：桌面应用重启换了端口后，授权转发无需重启 AstrBot 即可自愈。
- 新增 App 入口的单元测试、桌面应用 live 测试（`DSH_CONNECTOR_LIVE_APP=1` 时运行）与 README 说明。

## 2.1.1 - 2026-10-18

- 新增 DSH 授权请求桥：通过 `/api/remote.mux` 的 `$events` 流接收 `approval/request`，在绑定的聊天里提示，并用 `/dsh approve`、`/dsh deny` 经 `$events/result` 回送结论。
- 新增会话到聊天的反向索引（AstrBot KV），授权请求能路由回原聊天；DSH 界面取消的请求会同步提示。
- 修复长任务完成不唤醒的问题：`command_timeout` 到期后不再放弃，后台继续监视 turn，完成时主动把结果发到聊天；`completion_watch_timeout` 控制监视上限。
- 新增 `/dsh approvals` 与状态里的「事件桥 / 待授权」信息。
- 事件桥断线自动重连；插件重载后 DSH 会重新投递未回答的授权请求。

## 2.1.0 - 2026-10-18

- 适配 DSH 0.2.0-rc.2：RPC 端点从点号方法切换到 Typert 的 `<namespace>/<method>`，payload 统一为 `{"args": {...}}`。
- 新增 DSH 浏览器会话认证：从 `$DSH_HOME/.credentials.yaml` 的 `client-connection/browser-session` 密钥本地签发 `dsh-auth-*` cookie，无需手动复制启动 token，DSH 重启后仍然可用。
- `session/history` 改为 `session/projections` 取游标 + `session/page` 翻页，回复轮询不再依赖已删除的历史接口。
- `session/prompt` 补充客户端 `requestId`；`commands/execute` 补充 `submittedAttachments`。
- 新增 `dsh_home`、`default_cwd` 配置项；`host.describe` 删除后由 settings 与本机 DSH 状态合成主机描述。
- 模型目录改用 `session/modelCatalog`，服务商改用 `llm/listConfigurableProviders`，权限预设改用 `permissionPresets/catalog`。
- Presets、Skills、Goals、Subagents、Workspaces 迁移到新命名空间；子代理列表改由父会话的 `session/list` 推导，工作区列表读取 DSH 的 `storages/workspace.json`。
- 补齐 401/403/404 的可读错误提示，便于定位凭据、Host 校验与版本不匹配问题。

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
