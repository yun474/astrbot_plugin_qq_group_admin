from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class PluginStorage:
    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self.path = path
        self.retention_days = max(1, retention_days)
        self.data: dict[str, Any] = {
            "group_admins": {},
            "group_feature_overrides": {},
            "pending": {},
        }
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data.update(raw)
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            # Keep the plugin usable if a manually edited data file is malformed.
            pass
        self.data.setdefault("group_admins", {})
        self.data.setdefault("group_feature_overrides", {})
        self.data.setdefault("pending", {})
        self.data.pop("pending_counters", None)
        self.prune(save=False)

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
        admins = self.group_admins(group_openid)
        if member_openid in admins:
            return False
        admins.append(member_openid)
        self.data["group_admins"][group_openid] = admins
        self.save()
        return True

    def remove_group_admin(self, group_openid: str, member_openid: str) -> bool:
        admins = self.group_admins(group_openid)
        if member_openid not in admins:
            return False
        admins.remove(member_openid)
        self.data["group_admins"][group_openid] = admins
        self.save()
        return True

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
        """Invalidate all notifications/buttons after any approval entry succeeds."""
        keys = [
            key
            for key, item in self.data["pending"].items()
            if item.get("platform_id") == platform_id
            and item.get("group_openid") == group_openid
            and str(item.get("join_request_id")) == join_request_id
        ]
        if keys:
            for key in keys:
                del self.data["pending"][key]
            self.save()

    def remove_pending(self, notification_message_id: str) -> None:
        if self.data["pending"].pop(notification_message_id, None) is not None:
            self.save()

    def prune(self, *, save: bool = True) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        removed = False
        for message_id, item in list(self.data["pending"].items()):
            try:
                stored_at = datetime.fromisoformat(str(item.get("stored_at", "")))
                if stored_at.tzinfo is None:
                    stored_at = stored_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                stored_at = datetime.min.replace(tzinfo=timezone.utc)
            if stored_at < cutoff:
                del self.data["pending"][message_id]
                removed = True
        if removed and save:
            self.save()
