"""Per-chat DSH session options and interactive setup state machine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .dsh_client import normalize_client_time_zone


OPTION_FIELDS = (
    "workspace_id",
    "working_directory",
    "agent_preset",
    "provider",
    "model",
    "reasoning_effort",
    "permission_preset",
    "client_time_zone",
)

FIELD_ALIASES = {
    "workspace": "workspace_id",
    "workspace_id": "workspace_id",
    "cwd": "working_directory",
    "directory": "working_directory",
    "preset": "agent_preset",
    "provider": "provider",
    "model": "model",
    "effort": "reasoning_effort",
    "permission": "permission_preset",
    "timezone": "client_time_zone",
    "time_zone": "client_time_zone",
}


def default_session_options() -> dict[str, str]:
    return {
        "workspace_id": "",
        "working_directory": "",
        "agent_preset": "",
        "provider": "",
        "model": "",
        "reasoning_effort": "",
        "permission_preset": "",
        "client_time_zone": normalize_client_time_zone(),
    }


def normalize_session_options(raw: Any) -> dict[str, str]:
    options = default_session_options()
    if isinstance(raw, dict):
        for field in OPTION_FIELDS:
            if field in raw:
                options[field] = str(raw.get(field) or "").strip()
    options["client_time_zone"] = normalize_client_time_zone(options["client_time_zone"])
    if options["workspace_id"]:
        options["working_directory"] = ""
    if not options["provider"] or not options["model"]:
        options["provider"] = ""
        options["model"] = ""
        options["reasoning_effort"] = ""
    return options


def resolve_option_field(name: str) -> str | None:
    normalized = str(name or "").strip().lower()
    if normalized in OPTION_FIELDS:
        return normalized
    return FIELD_ALIASES.get(normalized)


def format_session_options(options: dict[str, str]) -> str:
    value = normalize_session_options(options)
    model = f"{value['provider']}/{value['model']}" if value["model"] else "DSH 默认"
    return "\n".join([
        "【当前聊天的 DSH 新会话选项】",
        f"工作空间：{value['workspace_id'] or '未指定（使用目录）'}",
        f"工作目录：{value['working_directory'] or 'DSH 主机默认'}",
        f"Agent Preset：{value['agent_preset'] or 'DSH 默认'}",
        f"模型：{model}",
        f"推理强度：{value['reasoning_effort'] or '模型默认'}",
        f"权限：{value['permission_preset'] or 'DSH 默认'}",
        f"时区：{value['client_time_zone']}",
        "使用 /dsh setup 交互配置，或 /dsh config set <字段> <值>。",
    ])


@dataclass
class SetupResult:
    prompt: str = ""
    confirmed: bool = False
    cancelled: bool = False


class SessionSetupWizard:
    """Pure setup wizard fed with catalogs returned by the connected DSH host."""

    def __init__(
        self,
        host_cwd: str,
        presets: list[dict[str, Any]],
        models: list[dict[str, Any]],
        initial: dict[str, str] | None = None,
        workspaces: list[dict[str, Any]] | None = None,
        permission_presets: list[str] | None = None,
    ) -> None:
        self.host_cwd = host_cwd
        self.presets = [row for row in presets if isinstance(row, dict) and row.get("id")]
        self.models = [row for row in models if isinstance(row, dict) and row.get("provider") and row.get("model")]
        self.workspaces = [
            row for row in workspaces or []
            if isinstance(row, dict) and row.get("workspaceId")
        ]
        self.permission_presets = [str(value) for value in permission_presets or [] if str(value).strip()]
        self.options = normalize_session_options(initial or {})
        self.step = "workspace" if self.workspaces else "directory"
        self._efforts: list[str] = []

    def initial_prompt(self) -> str:
        if self.workspaces:
            lines = [
                "步骤 1/8 - DSH 工作空间",
                "  [0] 不指定工作空间，下一步使用目录",
            ]
            lines.extend(
                f"  [{index}] {row.get('title') or row.get('path')} - {row.get('workspaceId')}"
                for index, row in enumerate(self.workspaces, 1)
            )
            lines.append("回复序号或直接输入 workspaceId。")
            return "\n".join(lines)
        return "\n".join([
            "步骤 1/7 - 工作目录",
            f"DSH 主机目录：{self.host_cwd}",
            "直接输入目录；回复 0 使用主机默认；回复 取消 退出。",
        ])

    @staticmethod
    def _choice(raw: str, count: int) -> int | None:
        return int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= count else None

    def _step_workspace(self, value: str) -> SetupResult:
        index = self._choice(value, len(self.workspaces))
        if value == "0":
            self.options["workspace_id"] = ""
        elif index is not None:
            self.options["workspace_id"] = str(self.workspaces[index]["workspaceId"])
        else:
            wanted = next(
                (str(row["workspaceId"]) for row in self.workspaces if str(row["workspaceId"]) == value),
                "",
            )
            if not wanted:
                return SetupResult("请输入工作空间序号、0，或有效 workspaceId。")
            self.options["workspace_id"] = wanted
        if self.options["workspace_id"]:
            self.options["working_directory"] = ""
        self.step = "directory"
        return SetupResult(
            "步骤 2/8 - 工作目录\n"
            "已选择工作空间时回复 0 保留该分配；输入目录会改用目录创建会话。\n"
            f"DSH 主机目录：{self.host_cwd}\n"
            "直接输入目录；回复 0 使用当前工作空间或主机默认。"
        )

    def process(self, raw: str) -> SetupResult:
        value = str(raw or "").strip()
        if value.lower() in {"cancel", "quit", "q", "取消", "退出"}:
            return SetupResult("已取消 DSH 会话配置。", cancelled=True)
        handler = getattr(self, f"_step_{self.step}")
        return handler(value)

    def _step_directory(self, value: str) -> SetupResult:
        if not value:
            return SetupResult("目录为空，请输入目录或回复 0。")
        if value == "0":
            self.options["working_directory"] = ""
        else:
            self.options["working_directory"] = value
            self.options["workspace_id"] = ""
        self.step = "preset"
        lines = [f"步骤 {'3/8' if self.workspaces else '2/7'} - Agent Preset", "  [0] DSH 默认"]
        lines.extend(f"  [{index}] {row['id']} - {row.get('name') or row.get('description') or ''}" for index, row in enumerate(self.presets, 1))
        lines.append("回复序号或直接输入 Preset ID。")
        return SetupResult("\n".join(lines))

    def _step_preset(self, value: str) -> SetupResult:
        index = self._choice(value, len(self.presets))
        self.options["agent_preset"] = "" if value == "0" else str(self.presets[index]["id"] if index is not None else value)
        self.step = "model"
        lines = [f"步骤 {'4/8' if self.workspaces else '3/7'} - 模型", "  [0] DSH 默认"]
        lines.extend(f"  [{index}] {row['provider']}/{row['model']}" for index, row in enumerate(self.models, 1))
        lines.append("回复序号或直接输入 provider/model。")
        return SetupResult("\n".join(lines))

    def _step_model(self, value: str) -> SetupResult:
        if value == "0":
            self.options.update(provider="", model="", reasoning_effort="")
            self._efforts = []
        else:
            index = self._choice(value, len(self.models))
            row = self.models[index] if index is not None else None
            if row is None:
                if "/" not in value:
                    return SetupResult("请输入列表序号、0，或 provider/model。")
                provider, model = value.split("/", 1)
                row = {"provider": provider, "model": model, "efforts": []}
            self.options["provider"] = str(row["provider"])
            self.options["model"] = str(row["model"])
            self._efforts = [str(item) for item in row.get("efforts") or []]
        self.step = "effort"
        lines = [f"步骤 {'5/8' if self.workspaces else '4/7'} - 推理强度", "  [0] 模型默认"]
        lines.extend(f"  [{index}] {effort}" for index, effort in enumerate(self._efforts, 1))
        lines.append("回复序号或直接输入推理强度。")
        return SetupResult("\n".join(lines))

    def _step_effort(self, value: str) -> SetupResult:
        index = self._choice(value, len(self._efforts))
        self.options["reasoning_effort"] = "" if value == "0" else str(self._efforts[index] if index is not None else value)
        self.step = "permission"
        lines = [
            f"步骤 {'6/8' if self.workspaces else '5/7'} - 权限",
            "  [0] DSH 默认",
        ]
        if self.permission_presets:
            lines.extend(f"  [{index}] {preset}" for index, preset in enumerate(self.permission_presets, 1))
        else:
            lines.append("DSH 未报告权限预设；可直接输入权限名称。")
        lines.append("回复序号、0，或直接输入权限预设。")
        return SetupResult("\n".join(lines))

    def _step_permission(self, value: str) -> SetupResult:
        index = self._choice(value, len(self.permission_presets))
        self.options["permission_preset"] = "" if value == "0" else str(
            self.permission_presets[index] if index is not None else value
        )
        self.step = "timezone"
        return SetupResult("\n".join([
            f"步骤 {'7/8' if self.workspaces else '6/7'} - 客户端时区",
            "  [1] Asia/Shanghai",
            "  [2] UTC",
            "也可直接输入有效 IANA Area/Location 名称。",
        ]))

    def _step_timezone(self, value: str) -> SetupResult:
        choices = {"1": "Asia/Shanghai", "2": "UTC"}
        normalized = normalize_client_time_zone(choices.get(value, value))
        if normalized == "UTC" and value not in {"2", "UTC", "utc"}:
            return SetupResult("时区无效，请输入 Asia/Shanghai、UTC 或有效 IANA Area/Location。")
        self.options["client_time_zone"] = normalized
        self.step = "confirm"
        final_step = "8/8" if self.workspaces else "7/7"
        return SetupResult(format_session_options(self.options) + f"\n\n步骤 {final_step} - 回复 y 创建并绑定新会话，其他输入取消。")

    def _step_confirm(self, value: str) -> SetupResult:
        if value.lower() != "y":
            return SetupResult("已取消 DSH 会话配置。", cancelled=True)
        return SetupResult("正在创建并绑定 DSH 会话...", confirmed=True)
