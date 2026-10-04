from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from astrbot.api import logger


class PluginStorage:
    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self.path = path
        self.retention_days = max(1, retention_days)
        self.data: dict[str, Any] = {
            "group_admins": {},
            "group_feature_overrides": {},
            "pending": {},
            "reviewed": {},
        }
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._validate(raw)
        except FileNotFoundError:
            return
        except (ValueError, UnicodeError) as exc:
            backup = self.path.with_name(f"{self.path.stem}.corrupt-{uuid4().hex}.json")
            # Both paths stay in the same data directory. If preserving the file
            # fails, abort loading rather than overwrite the only recovery copy.
            self.path.replace(backup)
            logger.error("群管存储格式损坏，原文件已保留至 %s：%s", backup, exc)
            return
        except OSError as exc:
            raise OSError(f"无法读取群管存储 {self.path}，原文件未修改") from exc
        self.data.update(raw)
        self.data.pop("pending_counters", None)
        self.prune(save=False)

    @staticmethod
    def _validate(raw: Any) -> None:
        if not isinstance(raw, dict):
            raise ValueError("存储根节点必须是对象")
        for name in ("group_admins", "group_feature_overrides", "pending", "reviewed"):
            if not isinstance(raw.get(name, {}), dict):
                raise ValueError(f"{name} 必须是对象")
        if any(
            not isinstance(value, str) for value in raw.get("reviewed", {}).values()
        ):
            raise ValueError("已审批记录必须包含时间字符串")
        for admins in raw.get("group_admins", {}).values():
            if not isinstance(admins, list) or any(
                not isinstance(member, str) for member in admins
            ):
                raise ValueError("群管名单必须是 OpenID 字符串列表")
        for overrides in raw.get("group_feature_overrides", {}).values():
            if not isinstance(overrides, dict) or any(
                not isinstance(value, bool) for value in overrides.values()
            ):
                raise ValueError("分群开关必须是布尔值对象")
        for item in raw.get("pending", {}).values():
            if not isinstance(item, dict):
                raise ValueError("待审记录必须是对象")
            for key in (
                "platform_id",
                "group_openid",
                "member_openid",
                "join_request_id",
                "stored_at",
                "ref_idx",
                "callback_token",
            ):
                if key in item and not isinstance(item[key], str):
                    raise ValueError(f"待审记录 {key} 必须是字符串")
            callbacks = item.get("review_callbacks", {})
            if not isinstance(callbacks, dict) or any(
                not isinstance(binding, dict) for binding in callbacks.values()
            ):
                raise ValueError("审批按钮映射必须是对象")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.path)

    def group_admins(self, group_openid: str) -> list[str]:
        admins = self.data["group_admins"].get(group_openid, [])
        return [str(item) for item in admins]

    def add_group_admin(self, group_openid: str, member_openid: str) -> bool:
        return bool(self.update_group_admins(group_openid, [member_openid], add=True))

    def remove_group_admin(self, group_openid: str, member_openid: str) -> bool:
        return bool(self.update_group_admins(group_openid, [member_openid], add=False))

    def update_group_admins(
        self, group_openid: str, members: list[str], *, add: bool
    ) -> int:
        """Commit a whole command together; failed writes cannot change permissions."""
        groups = self.data["group_admins"]
        previous = groups.get(group_openid)
        admins = self.group_admins(group_openid)
        changed = 0
        for member in members:
            if add and member not in admins:
                admins.append(member)
                changed += 1
            elif not add and member in admins:
                admins.remove(member)
                changed += 1
        if not changed:
            return 0
        groups[group_openid] = admins
        try:
            self.save()
        except Exception:
            if previous is None:
                groups.pop(group_openid, None)
            else:
                groups[group_openid] = previous
            raise
        return changed

    def group_feature_override(self, group_umo: str, key: str) -> bool | None:
        overrides = self.data["group_feature_overrides"].get(group_umo, {})
        if not isinstance(overrides, dict):
            return None
        value = overrides.get(key)
        return value if isinstance(value, bool) else None

    def set_group_feature_override(
        self,
        group_umo: str,
        key: str,
        value: bool,
    ) -> None:
        groups = self.data["group_feature_overrides"]
        overrides = groups.get(group_umo)
        if not isinstance(overrides, dict):
            overrides = {}
        overrides[key] = bool(value)
        groups[group_umo] = overrides
        self.save()

    def put_pending(self, notification_message_id: str, item: dict[str, Any]) -> None:
        item = dict(item)
        item["stored_at"] = datetime.now(timezone.utc).isoformat()
        self.data["pending"][notification_message_id] = item
        self.prune(save=False)
        self.save()

    def reserve_pending(self, item: dict[str, Any]) -> str:
        """Persist an application until its notification message ID is available."""
        item = dict(item)
        group_openid = str(item.get("group_openid") or "")
        item["stored_at"] = datetime.now(timezone.utc).isoformat()
        join_request_id = str(item["join_request_id"])
        key = f"request:{item.get('platform_id', '')}:{group_openid}:{join_request_id}"
        self.data["pending"][key] = item
        self.prune(save=False)
        self.save()
        return key

    def bind_pending_message(
        self, pending_key: str, message_id: str, ref_idx: str = ""
    ) -> str:
        """Save QQ's reference index even if the send response has no message ID."""
        item = self.data["pending"].pop(pending_key, None)
        if not isinstance(item, dict):
            return pending_key
        if ref_idx:
            item["ref_idx"] = ref_idx
        key = message_id or pending_key
        self.data["pending"][key] = item
        self.save()
        return key

    def find_pending_by_quote(
        self, references: set[str], platform_id: str, group_openid: str
    ) -> tuple[str, dict[str, Any]] | None:
        self.prune()
        matched = None
        for key, item in self.data["pending"].items():
            if not isinstance(item, dict):
                continue
            if (
                item.get("platform_id") != platform_id
                or item.get("group_openid") != group_openid
            ):
                continue
            if key not in references and item.get("ref_idx") not in references:
                continue
            if matched is not None:
                return None  # Conflicting quote identifiers must never pick a request.
            matched = str(key), dict(item)
        return matched

    def get_pending(self, notification_message_id: str) -> dict[str, Any] | None:
        item = self.data["pending"].get(notification_message_id)
        return dict(item) if isinstance(item, dict) else None

    def find_pending_by_join_request_id(
        self,
        join_request_id: str,
        group_openid: str = "",
        platform_id: str = "",
    ) -> tuple[str, dict[str, Any]] | None:
        for message_id, item in self.data["pending"].items():
            if not isinstance(item, dict):
                continue
            if str(item.get("join_request_id") or "") != join_request_id:
                continue
            if group_openid and str(item.get("group_openid") or "") != group_openid:
                continue
            if platform_id and str(item.get("platform_id") or "") != platform_id:
                continue
            return str(message_id), dict(item)
        return None

    def find_pending_by_token(self, token: str) -> tuple[str, dict[str, Any]] | None:
        self.prune()
        for message_id, item in self.data["pending"].items():
            if not isinstance(item, dict):
                continue
            if item.get("callback_token") == token or token in item.get(
                "review_callbacks", {}
            ):
                return str(message_id), dict(item)
        return None

    def remove_reviewed_request(
        self, platform_id: str, group_openid: str, join_request_id: str
    ) -> None:
        """Keep a completion marker and invalidate every card for the application."""
        key = json.dumps([platform_id, group_openid, join_request_id])
        self.data["reviewed"][key] = datetime.now(timezone.utc).isoformat()
        keys = [
            key
            for key, item in self.data["pending"].items()
            if item.get("platform_id") == platform_id
            and item.get("group_openid") == group_openid
            and str(item.get("join_request_id")) == join_request_id
        ]
        for key in keys:
            del self.data["pending"][key]
        self.prune(save=False)
        # Keep the live completion marker even if persistence fails: QQ has
        # already executed the operation, so a retry must not execute it again.
        self.save()

    def is_reviewed(
        self, platform_id: str, group_openid: str, join_request_id: str
    ) -> bool:
        self.prune(save=False)
        return (
            json.dumps([platform_id, group_openid, join_request_id])
            in self.data["reviewed"]
        )

    def remove_pending(self, notification_message_id: str) -> None:
        if self.data["pending"].pop(notification_message_id, None) is not None:
            self.save()

    def prune(self, *, save: bool = True) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        removed = False
        for section in ("pending", "reviewed"):
            for key, item in list(self.data[section].items()):
                timestamp = item.get("stored_at", "") if section == "pending" else item
                try:
                    stored_at = datetime.fromisoformat(str(timestamp))
                    if stored_at.tzinfo is None:
                        stored_at = stored_at.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    stored_at = datetime.min.replace(tzinfo=timezone.utc)
                if stored_at < cutoff:
                    del self.data[section][key]
                    removed = True
        if removed and save:
            self.save()
