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
            "pending": {},
            "pending_counters": {},
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
        self.data.setdefault("pending", {})
        self.data.setdefault("pending_counters", {})
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

    def put_pending(self, notification_message_id: str, item: dict[str, Any]) -> None:
        item = dict(item)
        item["stored_at"] = datetime.now(timezone.utc).isoformat()
        self.data["pending"][notification_message_id] = item
        self.prune(save=False)
        self.save()

    def reserve_pending(self, item: dict[str, Any]) -> tuple[str, int]:
        """Persist an application and allocate its stable per-group review index."""
        item = dict(item)
        group_openid = str(item.get("group_openid") or "")
        counters = self.data["pending_counters"]
        index = int(counters.get(group_openid, 0)) + 1
        counters[group_openid] = index
        item["review_index"] = index
        item["stored_at"] = datetime.now(timezone.utc).isoformat()
        join_request_id = str(item.get("join_request_id") or index)
        key = f"request:{group_openid}:{join_request_id}"
        self.data["pending"][key] = item
        self.prune(save=False)
        self.save()
        return key, index

    def bind_pending_message(self, pending_key: str, message_id: str) -> str:
        """Re-key a reserved application with the notification message ID."""
        if not message_id or pending_key == message_id:
            return pending_key
        item = self.data["pending"].pop(pending_key, None)
        if not isinstance(item, dict):
            return pending_key
        self.data["pending"][message_id] = item
        self.save()
        return message_id

    def get_pending(self, notification_message_id: str) -> dict[str, Any] | None:
        item = self.data["pending"].get(notification_message_id)
        return dict(item) if isinstance(item, dict) else None

    def find_pending_by_join_request_id(
        self,
        join_request_id: str,
        group_openid: str = "",
    ) -> tuple[str, dict[str, Any]] | None:
        for message_id, item in self.data["pending"].items():
            if not isinstance(item, dict):
                continue
            if str(item.get("join_request_id") or "") != join_request_id:
                continue
            if group_openid and str(item.get("group_openid") or "") != group_openid:
                continue
            return str(message_id), dict(item)
        return None

    def find_pending_by_index(
        self,
        group_openid: str,
        review_index: int,
    ) -> tuple[str, dict[str, Any]] | None:
        for message_id, item in self.data["pending"].items():
            if not isinstance(item, dict):
                continue
            if str(item.get("group_openid") or "") != group_openid:
                continue
            if int(item.get("review_index") or 0) == review_index:
                return str(message_id), dict(item)
        return None

    def remove_pending(self, notification_message_id: str) -> None:
        if self.data["pending"].pop(notification_message_id, None) is not None:
            self.save()

    def reset_group_pending(self, group_openid: str) -> int:
        """Clear one group's pending mappings and restart its display index."""
        removed = 0
        for message_id, item in list(self.data["pending"].items()):
            if not isinstance(item, dict):
                continue
            if str(item.get("group_openid") or "") != group_openid:
                continue
            del self.data["pending"][message_id]
            removed += 1
        self.data["pending_counters"].pop(group_openid, None)
        self.save()
        return removed

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
