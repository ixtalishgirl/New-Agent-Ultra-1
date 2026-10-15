"""Backup — snapshot and restore agent state.

Backs up:
- Memory (project memory, conversations)
- Configuration
- Task history
- Notifications

No personality. No rules. Direct execution.
"""
from __future__ import annotations

import json
import shutil
import time
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class BackupInfo:
    id: str
    timestamp: float
    path: str
    size_bytes: int
    components: List[str]
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BackupManager:
    """Create and restore backups of agent state."""

    def __init__(self, backup_dir: str = "backups",
                 data_dir: str = "."):
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(data_dir)
        self._index_file = self.backup_dir / "index.json"
        self._index: List[Dict[str, Any]] = self._load_index()

    def _load_index(self) -> List[Dict[str, Any]]:
        if self._index_file.exists():
            try:
                return json.loads(self._index_file.read_text())
            except Exception:
                pass
        return []

    def _save_index(self) -> None:
        try:
            self._index_file.write_text(
                json.dumps(self._index, indent=2))
        except Exception:
            pass

    def create(self, description: str = "",
               components: Optional[List[str]] = None
               ) -> BackupInfo:
        """Create a backup. Returns BackupInfo."""
        import uuid
        backup_id = f"backup_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        backup_path = self.backup_dir / f"{backup_id}.zip"

        # Default components
        if components is None:
            components = ["memory", "config", "logs"]

        backed_up = []
        with zipfile.ZipFile(backup_path, "w",
                             zipfile.ZIP_DEFLATED) as zf:
            # Memory
            if "memory" in components:
                for mem_dir in ["memory", "agent/memory/data",
                                ".agent_memory"]:
                    p = self.data_dir / mem_dir
                    if p.exists():
                        self._add_to_zip(zf, p, f"memory/{p.name}")
                        backed_up.append(f"memory/{p.name}")
            # Config
            if "config" in components:
                p = self.data_dir / "agent" / "configs"
                if p.exists():
                    self._add_to_zip(zf, p, "configs")
                    backed_up.append("configs")
            # Logs
            if "logs" in components:
                for log in ["notifications.log", "agent.log",
                            "audit.log"]:
                    p = self.data_dir / log
                    if p.exists():
                        zf.write(p, f"logs/{p.name}")
                        backed_up.append(f"logs/{p.name}")
            # Metadata
            meta = {
                "id": backup_id,
                "timestamp": time.time(),
                "description": description,
                "components": backed_up,
            }
            zf.writestr("metadata.json", json.dumps(meta, indent=2))

        info = BackupInfo(
            id=backup_id,
            timestamp=time.time(),
            path=str(backup_path),
            size_bytes=backup_path.stat().st_size,
            components=backed_up,
            description=description,
        )
        self._index.append(info.to_dict())
        self._save_index()
        return info

    def _add_to_zip(self, zf: zipfile.ZipFile, path: Path,
                    arc_prefix: str) -> None:
        if path.is_file():
            zf.write(path, f"{arc_prefix}/{path.name}")
        elif path.is_dir():
            for f in path.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(path)
                    zf.write(f, f"{arc_prefix}/{rel}")

    def list(self) -> List[BackupInfo]:
        """List all backups."""
        return [BackupInfo(**b) for b in self._index]

    def restore(self, backup_id: str,
                dest_dir: Optional[str] = None) -> Dict[str, Any]:
        """Restore a backup. Returns status."""
        info = next((b for b in self._index
                     if b["id"] == backup_id), None)
        if not info:
            return {"ok": False,
                    "error": f"Backup not found: {backup_id}"}

        backup_path = Path(info["path"])
        if not backup_path.exists():
            return {"ok": False, "error": "Backup file missing"}

        dest = Path(dest_dir) if dest_dir else self.data_dir
        try:
            with zipfile.ZipFile(backup_path, "r") as zf:
                # Security check
                for member in zf.namelist():
                    member_path = (dest / member).resolve()
                    if not str(member_path).startswith(
                            str(dest.resolve())):
                        return {"ok": False,
                                "error": "Path traversal blocked"}
                zf.extractall(dest)
            return {"ok": True, "backup_id": backup_id,
                    "restored_to": str(dest)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete(self, backup_id: str) -> bool:
        """Delete a backup."""
        info = next((b for b in self._index
                     if b["id"] == backup_id), None)
        if not info:
            return False
        try:
            Path(info["path"]).unlink(missing_ok=True)
        except Exception:
            pass
        self._index = [b for b in self._index
                       if b["id"] != backup_id]
        self._save_index()
        return True

    def cleanup(self, keep: int = 10) -> int:
        """Delete old backups, keep newest N. Returns deleted count."""
        if len(self._index) <= keep:
            return 0
        # Sort by timestamp, keep newest
        sorted_backups = sorted(self._index,
                                key=lambda b: b["timestamp"])
        to_delete = sorted_backups[:-keep]
        count = 0
        for b in to_delete:
            if self.delete(b["id"]):
                count += 1
        return count

    def verify(self, backup_id: str) -> Dict[str, Any]:
        """Verify backup integrity."""
        info = next((b for b in self._index
                     if b["id"] == backup_id), None)
        if not info:
            return {"ok": False, "error": "Not found"}
        path = Path(info["path"])
        if not path.exists():
            return {"ok": False, "error": "File missing"}
        try:
            with zipfile.ZipFile(path, "r") as zf:
                bad = zf.testzip()
                if bad:
                    return {"ok": False,
                            "error": f"Corrupt file: {bad}"}
                return {"ok": True,
                        "files": len(zf.namelist()),
                        "size": path.stat().st_size}
        except Exception as e:
            return {"ok": False, "error": str(e)}
