"""Dashboard-backed group profiles; group identity never uses a user's session ID."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

GROUP_SECTIONS = (
    "permission_settings",
    "command_settings",
    "join_request_settings",
    "member_notice_settings",
    "llm_tool_settings",
    "limit_settings",
    "keyword_settings",
)
SCHEMA = json.loads(
    Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8")
)


def defaults(items: dict) -> dict:
    return {
        key: defaults(meta["items"])
        if meta["type"] == "object"
        else deepcopy(meta.get("default"))
        for key, meta in items.items()
    }


def valid_group_umo(umo: str) -> bool:
    platform, separator, group = umo.partition(":GroupMessage:")
    return bool(separator and platform and group and ":" not in group)


class GroupConfig:
    def __init__(self, config: dict):
        self.config = config

    @property
    def data(self) -> dict:
        return self.config["group_management"]

    def profile(self, umo: str) -> dict | None:
        matches = [
            p
            for p in self.data["groups"]
            if p.get("umo", "").strip() == umo and p.get("confirmed", True)
        ]
        if len(matches) > 1:
            raise ValueError(f"群配置 UMO 重复，请在面板合并：{umo}")
        return matches[0] if matches else None

    def snapshot(self, umo: str) -> dict:
        if not valid_group_umo(umo):
            raise ValueError("群配置必须使用 平台ID:GroupMessage:群ID 格式的 UMO")
        result = {
            "__template_key": "group",
            "umo": umo,
            "confirmed": True,
            "migration_note": "",
        }
        result.update(deepcopy(self.data["global_settings"]))
        result["join_request_settings"].pop("pending_retention_days", None)
        return result

    def set_value(self, umo: str, section: str, key: str, value: Any) -> None:
        # Persist one source of truth. A failed disk write must not change live behavior.
        before = deepcopy(dict(self.config))
        try:
            profile = self.profile(umo)
            if profile is None:
                profile = self.snapshot(umo)
                self.data["groups"].append(profile)
            profile.setdefault(section, {})[key] = deepcopy(value)
            self.save()
        except Exception:
            self.config.clear()
            self.config.update(before)
            raise

    def save(self) -> None:
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()

    def migrate(self, storage: Any, context: Any) -> list[str]:
        if int(self.config.get("config_layout_version", 0)) >= 2:
            self._complete_profiles()
            return []
        before = deepcopy(dict(self.config))
        old_groups = storage.data.get("group_feature_overrides", {})
        backup = storage.path.with_name("group-config-before-v2.json")
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            temporary = backup.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"config": before, "group_feature_overrides": old_groups},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(backup)
        unresolved = []
        try:
            global_settings = {
                section: defaults(SCHEMA[section]["items"])
                for section in GROUP_SECTIONS
            }
            for section in GROUP_SECTIONS:
                global_settings[section].update(deepcopy(self.config.get(section, {})))
            self.config["group_management"] = {
                "enabled": bool(
                    self.config.get("scope_settings", {}).get(
                        "enable_per_group_feature_settings", False
                    )
                ),
                "global_settings": global_settings,
                "groups": [],
            }
            known_groups = {
                f"{item.get('platform_id')}:GroupMessage:{item.get('group_openid')}"
                for item in storage.data.get("pending", {}).values()
                if item.get("platform_id") and item.get("group_openid")
            }
            for umo, values in old_groups.items():
                valid = valid_group_umo(umo)
                # Old versions did not record whether this ID was a group or a user.
                try:
                    unique_session = (
                        context.get_config(umo)
                        .get("platform_settings", {})
                        .get("unique_session", False)
                    )
                except Exception:
                    unique_session = True
                profile = (
                    self.snapshot(umo)
                    if valid
                    else self.snapshot("unknown:GroupMessage:unknown")
                )
                profile["umo"] = umo
                for section in GROUP_SECTIONS:
                    for key in profile[section]:
                        if key in values:
                            profile[section][key] = values[key]
                if not valid or (unique_session and umo not in known_groups):
                    profile["confirmed"] = False
                    profile["migration_note"] = (
                        "旧记录无法确认群归属；请填写真实群 UMO，再开启确认开关。原记录及迁移前配置已备份。"
                    )
                    unresolved.append(umo)
                self.data["groups"].append(profile)
            self.config["config_layout_version"] = 2
            self.save()
        except Exception:
            self.config.clear()
            self.config.update(before)
            raise
        return unresolved

    def _complete_profiles(self) -> None:
        """AstrBot does not fill new fields inside existing template_list entries."""
        before = deepcopy(dict(self.config))
        template = SCHEMA["group_management"]["items"]["groups"]["templates"]["group"]
        changed = False
        try:
            for profile in self.data["groups"]:
                for section in GROUP_SECTIONS:
                    values = profile.setdefault(section, {})
                    for key, value in defaults(
                        template["items"][section]["items"]
                    ).items():
                        if key not in values:
                            values[key] = value
                            changed = True
            if changed:
                self.save()
        except Exception:
            self.config.clear()
            self.config.update(before)
            raise
