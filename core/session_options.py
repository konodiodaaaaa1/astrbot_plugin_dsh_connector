"""Per-chat DSH session options and interactive setup state machine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .dsh_client import normalize_client_time_zone


OPTION_FIELDS = (
    "working_directory",
    "agent_preset",
    "provider",
    "model",
    "reasoning_effort",
    "permission_preset",
    "client_time_zone",
)

FIELD_ALIASES = {
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
    ) -> None:
        self.host_cwd = host_cwd
        self.presets = [row for row in presets if isinstance(row, dict) and row.get("id")]
        self.models = [row for row in models if isinstance(row, dict) and row.get("provider") and row.get("model")]
        self.options = normalize_session_options(initial or {})
        self.step = "directory"
        self._efforts: list[str] = []

    def initial_prompt(self) -> str:
        return "\n".join([
            "步骤 1/7 - 工作目录",
            f"DSH 主机目录：{self.host_cwd}",
            "直接输入目录；回复 0 使用主机默认；回复 取消 退出。",
        ])

    @staticmethod
    def _choice(raw: str, count: int) -> int | None:
        return int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= count else None

    def process(self, raw: str) -> SetupResult:
        value = str(raw or "").strip()
        if value.lower() in {"cancel", "quit", "q", "取消", "退出"}:
            return SetupResult("已取消 DSH 会话配置。", cancelled=True)
        handler = getattr(self, f"_step_{self.step}")
        return handler(value)

    def _step_directory(self, value: str) -> SetupResult:
        if not value:
            return SetupResult("目录为空，请输入目录或回复 0。")
        self.options["working_directory"] = "" if value == "0" else value
        self.step = "preset"
        lines = ["步骤 2/7 - Agent Preset", "  [0] DSH 默认"]
        lines.extend(f"  [{index}] {row['id']} - {row.get('name') or row.get('description') or ''}" for index, row in enumerate(self.presets, 1))
        lines.append("回复序号或直接输入 Preset ID。")
        return SetupResult("\n".join(lines))

    def _step_preset(self, value: str) -> SetupResult:
        index = self._choice(value, len(self.presets))
        self.options["agent_preset"] = "" if value == "0" else str(self.presets[index]["id"] if index is not None else value)
        self.step = "model"
        lines = ["步骤 3/7 - 模型", "  [0] DSH 默认"]
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
        lines = ["步骤 4/7 - 推理强度", "  [0] 模型默认"]
        lines.extend(f"  [{index}] {effort}" for index, effort in enumerate(self._efforts, 1))
        lines.append("回复序号或直接输入推理强度。")
        return SetupResult("\n".join(lines))

    def _step_effort(self, value: str) -> SetupResult:
        index = self._choice(value, len(self._efforts))
        self.options["reasoning_effort"] = "" if value == "0" else str(self._efforts[index] if index is not None else value)
        self.step = "permission"
        return SetupResult("\n".join([
            "步骤 5/7 - 权限",
            "  [0] DSH 默认",
            "  [1] read-only",
            "  [2] workspace-write",
            "  [3] danger-full-access",
            "回复序号或直接输入权限预设。",
        ]))

    def _step_permission(self, value: str) -> SetupResult:
        presets = ["read-only", "workspace-write", "danger-full-access"]
        index = self._choice(value, len(presets))
        self.options["permission_preset"] = "" if value == "0" else str(presets[index] if index is not None else value)
        self.step = "timezone"
        return SetupResult("\n".join([
            "步骤 6/7 - 客户端时区",
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
        return SetupResult(format_session_options(self.options) + "\n\n步骤 7/7 - 回复 y 创建并绑定新会话，其他输入取消。")

    def _step_confirm(self, value: str) -> SetupResult:
        if value.lower() != "y":
            return SetupResult("已取消 DSH 会话配置。", cancelled=True)
        return SetupResult("正在创建并绑定 DSH 会话...", confirmed=True)
