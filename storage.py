from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class PluginStorage:
    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self.path = path
        self.retention_days = max(1, retention_days)
        self.data: dict[str, Any] = {"group_admins": {}, "pending": {}}
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

    def get_pending(self, notification_message_id: str) -> dict[str, Any] | None:
        item = self.data["pending"].get(notification_message_id)
        return dict(item) if isinstance(item, dict) else None

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


