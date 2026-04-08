"""Sign task service with CRUD and execution helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.config import get_settings
from backend.utils.account_locks import get_account_lock
from backend.utils.persisted_state import (
    CATEGORY_CHAT_CACHE,
    CATEGORY_SIGN_TASK,
    CATEGORY_SIGN_TASK_HISTORY,
    delete_state,
    list_state_items,
    load_state_json,
    save_state_json,
)
from backend.utils.proxy import build_proxy_dict
from backend.utils.tg_session import (
    get_account_proxy,
    get_account_session_string,
    get_global_semaphore,
    get_session_mode,
    load_session_string_file,
)
from tg_signer.core import UserSigner, get_client

settings = get_settings()


class TaskLogHandler(logging.Handler):
    """Custom log handler that stores live task logs in memory."""

    def __init__(self, log_list: List[str]):
        super().__init__()
        self.log_list = log_list

    def emit(self, record):
        try:
            msg = self.format(record)
            self.log_list.append(msg)
            if len(self.log_list) > 1000:
                self.log_list.pop(0)
        except Exception:
            self.handleError(record)


class BackendUserSigner(UserSigner):
    """Backend signer wrapper with non-interactive behavior."""

    @property
    def task_dir(self):
        # Backend layout: signs/account_name/task_name
        return self.tasks_dir / self._account / self.task_name

    def ask_for_config(self):
        raise ValueError(
            f"Task config file not found: {self.config_file}. Interactive input is disabled in backend mode."
        )

    def reconfig(self):
        raise ValueError(
            f"Task config file not found: {self.config_file}. Interactive input is disabled in backend mode."
        )

    def ask_one(self):
        raise ValueError("Interactive input is disabled in backend mode")


class SignTaskService:
    """Sign task service."""

    @staticmethod
    def _read_positive_int_env(name: str, default: int, minimum: int = 1) -> int:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return max(int(raw), minimum)
        except (TypeError, ValueError):
            return default

    def __init__(self):
        from backend.core.config import get_settings

        settings = get_settings()
        self.workdir = settings.resolve_workdir()
        self.signs_dir = self.workdir / "signs"
        self.run_history_dir = self.workdir / "history"
        self.signs_dir.mkdir(parents=True, exist_ok=True)
        self.run_history_dir.mkdir(parents=True, exist_ok=True)
        print(
            f"DEBUG: SignTaskService initialized, signs_dir={self.signs_dir}, exists={self.signs_dir.exists()}"
        )
        self._active_logs: Dict[tuple[str, str], List[str]] = {}  # (account, task) -> logs
        self._active_tasks: Dict[tuple[str, str], bool] = {}  # (account, task) -> running
        self._cleanup_tasks: Dict[tuple[str, str], asyncio.Task] = {}
        self._tasks_cache = None  # in-memory task cache
        self._account_locks: Dict[str, asyncio.Lock] = {}
        self._account_last_run_end: Dict[str, float] = {}
        self._account_cooldown_seconds = int(
            os.getenv("SIGN_TASK_ACCOUNT_COOLDOWN", "5")
        )
        self._history_max_entries = self._read_positive_int_env(
            "SIGN_TASK_HISTORY_MAX_ENTRIES", 100, 10
        )
        self._history_max_flow_lines = self._read_positive_int_env(
            "SIGN_TASK_HISTORY_MAX_FLOW_LINES", 200, 20
        )
        self._history_max_line_chars = self._read_positive_int_env(
            "SIGN_TASK_HISTORY_MAX_LINE_CHARS", 500, 80
        )
        self._cleanup_old_logs()

    @staticmethod
    def _task_requires_updates(task_config: Optional[Dict[str, Any]]) -> bool:
        """Return whether the task depends on Telegram update handlers."""
        if not isinstance(task_config, dict):
            return True
        chats = task_config.get("chats")
        if not isinstance(chats, list):
            return True
        response_actions = {3, 4, 5, 6, 7}
        for chat in chats:
            if not isinstance(chat, dict):
                continue
            actions = chat.get("actions")
            if not isinstance(actions, list):
                continue
            for action in actions:
                if not isinstance(action, dict):
                    continue
                try:
                    action_id = int(action.get("action"))
                except (TypeError, ValueError):
                    continue
                if action_id in response_actions:
                    return True
        return False

    def _cleanup_old_logs(self):
        """Remove log files older than 3 days."""
        from datetime import datetime, timedelta

        if not self.run_history_dir.exists():
            return

        limit = datetime.now() - timedelta(days=3)
        for log_file in self.run_history_dir.glob("*.json"):
            if log_file.stat().st_mtime < limit.timestamp():
                try:
                    log_file.unlink()
                except Exception:
                    continue

    def _safe_history_key(self, name: str) -> str:
        return name.replace("/", "_").replace("\\", "_")

    def _history_file_path(self, task_name: str, account_name: str = "") -> Path:
        if account_name:
            safe_account = self._safe_history_key(account_name)
            safe_task = self._safe_history_key(task_name)
            return self.run_history_dir / f"{safe_account}__{safe_task}.json"
        return self.run_history_dir / f"{self._safe_history_key(task_name)}.json"

    def _normalize_flow_logs(
        self, flow_logs: Optional[List[str]]
    ) -> tuple[List[str], bool, int]:
        if not isinstance(flow_logs, list):
            return [], False, 0

        total = len(flow_logs)
        trimmed: List[str] = []
        for line in flow_logs[: self._history_max_flow_lines]:
            text = str(line).replace("\r", "").rstrip("\n")
            if len(text) > self._history_max_line_chars:
                text = text[: self._history_max_line_chars] + "..."
            trimmed.append(text)
        return trimmed, total > len(trimmed), total

    def _load_history_entries(
        self, task_name: str, account_name: str = ""
    ) -> List[Dict[str, Any]]:
        history_file = self._history_file_path(task_name, account_name)
        legacy_file = self.run_history_dir / f"{self._safe_history_key(task_name)}.json"

        selected_file = None
        if history_file.exists():
            selected_file = history_file
        elif legacy_file.exists():
            selected_file = legacy_file

        if selected_file is not None:
            try:
                with open(selected_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = None

            if isinstance(data, dict):
                data_list = [data]
            elif isinstance(data, list):
                data_list = data
            else:
                data_list = []

            entries: List[Dict[str, Any]] = []
            for item in data_list:
                if not isinstance(item, dict):
                    continue
                if account_name:
                    item_account = item.get("account_name")
                    if item_account and item_account != account_name:
                        continue
                entries.append(item)

            entries.sort(key=lambda x: x.get("time", ""), reverse=True)
            if entries:
                save_state_json(
                    CATEGORY_SIGN_TASK_HISTORY,
                    task_name,
                    entries,
                    scope=account_name,
                )
                return entries

        db_entries = load_state_json(
            CATEGORY_SIGN_TASK_HISTORY,
            task_name,
            scope=account_name,
            default=None,
        )
        if isinstance(db_entries, list):
            entries = [item for item in db_entries if isinstance(item, dict)]
            entries.sort(key=lambda x: x.get("time", ""), reverse=True)
            if entries:
                try:
                    history_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(history_file, "w", encoding="utf-8") as f:
                        json.dump(entries, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            return entries
        return []

    def get_task_history_logs(
        self, task_name: str, account_name: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        if limit < 1:
            limit = 1
        if limit > 200:
            limit = 200

        history = self._load_history_entries(task_name, account_name=account_name)
        result: List[Dict[str, Any]] = []
        for item in history[:limit]:
            flow_logs = item.get("flow_logs")
            if not isinstance(flow_logs, list):
                flow_logs = []

            result.append(
                {
                    "time": item.get("time", ""),
                    "success": bool(item.get("success", False)),
                    "message": item.get("message", "") or "",
                    "flow_logs": [str(line) for line in flow_logs],
                    "flow_truncated": bool(item.get("flow_truncated", False)),
                    "flow_line_count": int(item.get("flow_line_count", len(flow_logs))),
                }
            )
        return result

    def get_account_history_logs(self, account_name: str) -> List[Dict[str, Any]]:
        """获取某账号下所有任务的最近历史日志"""
        all_history = []
        tasks = self.list_tasks(account_name=account_name)

        for task in tasks:
            task_name = task["name"]
            for data in self._load_history_entries(task_name, account_name=account_name):
                entry = dict(data)
                entry["task_name"] = task_name
                all_history.append(entry)

        all_history.sort(key=lambda x: x.get("time", ""), reverse=True)
        return all_history
    def clear_account_history_logs(self, account_name: str) -> Dict[str, int]:
        """清除某账号下所有任务的历史日志"""
        removed_files = 0
        removed_entries = 0

        def _count_entries(data: Any) -> int:
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                return 1
            return 0

        from backend.services.config import get_config_service

        config_service = get_config_service()
        tasks = self.list_tasks(account_name=account_name)
        for task in tasks:
            task_name = task.get("name") or ""
            if not task_name:
                continue

            db_history = self._load_history_entries(task_name, account_name=account_name)
            if db_history:
                removed_entries += len(db_history)
            delete_state(CATEGORY_SIGN_TASK_HISTORY, task_name, scope=account_name)

            config = config_service.get_sign_config(task_name, account_name=account_name)
            if isinstance(config, dict) and "last_run" in config:
                updated_config = dict(config)
                updated_config.pop("last_run", None)
                config_service.save_sign_config(task_name, updated_config)

            if self._tasks_cache is not None:
                for t in self._tasks_cache:
                    if t["name"] == task_name and t.get("account_name") == account_name:
                        t.pop("last_run", None)
                        break

            history_file = self._history_file_path(task_name, account_name)
            if history_file.exists():
                try:
                    with open(history_file, "r", encoding="utf-8") as f:
                        removed_entries += _count_entries(json.load(f))
                except Exception:
                    pass
                try:
                    history_file.unlink()
                    removed_files += 1
                except Exception:
                    pass
                continue

            legacy_file = self.run_history_dir / f"{self._safe_history_key(task_name)}.json"
            if not legacy_file.exists():
                continue

            try:
                with open(legacy_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data_list = [data]
                elif isinstance(data, list):
                    data_list = data
                else:
                    data_list = []
            except Exception:
                continue

            if not data_list:
                try:
                    legacy_file.unlink()
                    removed_files += 1
                except Exception:
                    pass
                continue

            has_account_field = any(
                isinstance(item, dict) and "account_name" in item for item in data_list
            )
            if not has_account_field:
                removed_entries += len(data_list)
                try:
                    legacy_file.unlink()
                    removed_files += 1
                except Exception:
                    pass
                continue

            kept: List[Dict[str, Any]] = []
            for item in data_list:
                if not isinstance(item, dict):
                    continue
                if item.get("account_name") == account_name:
                    removed_entries += 1
                else:
                    kept.append(item)

            if not kept:
                try:
                    legacy_file.unlink()
                    removed_files += 1
                except Exception:
                    pass
            else:
                try:
                    with open(legacy_file, "w", encoding="utf-8") as f:
                        json.dump(kept, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

        return {"removed_files": removed_files, "removed_entries": removed_entries}
    def _get_last_run_info(
        self, task_name: str, account_name: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Return the latest execution record for the task."""
        entries = self._load_history_entries(task_name, account_name)
        if entries:
            return entries[0]
        return None

    def _save_run_info(
        self,
        task_name: str,
        success: bool,
        message: str = "",
        account_name: str = "",
        flow_logs: Optional[List[str]] = None,
    ):
        """Persist task execution history as an append-only list."""
        from datetime import datetime

        history_file = self._history_file_path(task_name, account_name)
        normalized_logs, flow_truncated, flow_line_count = self._normalize_flow_logs(
            flow_logs
        )

        new_entry = {
            "time": datetime.now().isoformat(),
            "success": success,
            "message": message,
            "account_name": account_name,
            "flow_logs": normalized_logs,
            "flow_truncated": flow_truncated,
            "flow_line_count": flow_line_count,
        }

        history = []
        if history_file.exists():
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        history = data
                    else:
                        history = [data]
            except Exception:
                history = []

        history.insert(0, new_entry)
        history = history[: self._history_max_entries]
        save_state_json(
            CATEGORY_SIGN_TASK_HISTORY,
            task_name,
            history,
            scope=account_name,
        )

        try:
            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)

            # Update last_run in the local config mirror.
            task = self.get_task(task_name, account_name)
            if task:
                task_dir = self.signs_dir / account_name / task_name
                if not task_dir.exists():
                    task_dir = self.signs_dir / task_name

                config_file = task_dir / "config.json"
                if config_file.exists():
                    try:
                        with open(config_file, "r", encoding="utf-8") as f:
                            config = json.load(f)
                        config["last_run"] = new_entry
                        with open(config_file, "w", encoding="utf-8") as f:
                            json.dump(config, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        print(f"DEBUG: failed to update task config last_run: {e}")

            # Update the in-memory task cache.
            if self._tasks_cache is not None:
                for t in self._tasks_cache:
                    if t["name"] == task_name and t.get("account_name") == account_name:
                        t["last_run"] = new_entry
                        break

        except Exception as e:
            print(f"DEBUG: failed to persist run info: {str(e)}")

    def _append_scheduler_log(self, filename: str, message: str) -> None:
        try:
            logs_dir = settings.resolve_logs_dir()
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / filename
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f'{message}\n')
        except Exception as e:
            logging.getLogger('backend.sign_tasks').warning(
                'Failed to write scheduler log %s: %s', filename, e
            )

    def _task_record_to_output(
        self, task_name: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        account_name = config.get("account_name", "")
        self._ensure_task_config_file(task_name, account_name, config)
        last_run = config.get("last_run")
        if not last_run:
            last_run = self._get_last_run_info(task_name, account_name=account_name)
        return {
            "name": task_name,
            "account_name": account_name,
            "sign_at": config.get("sign_at", ""),
            "random_seconds": config.get("random_seconds", 0),
            "sign_interval": config.get("sign_interval", 1),
            "chats": config.get("chats", []),
            "enabled": True,
            "last_run": last_run,
            "execution_mode": config.get("execution_mode", "fixed"),
            "range_start": config.get("range_start", ""),
            "range_end": config.get("range_end", ""),
        }

    def _ensure_task_config_file(
        self, task_name: str, account_name: str, config: Dict[str, Any]
    ) -> None:
        if not account_name:
            return
        task_dir = self.signs_dir / account_name / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        config_file = task_dir / "config.json"
        try:
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_chat_cache(self, account_name: str) -> List[Dict[str, Any]]:
        cache_file = self.signs_dir / account_name / "chats_cache.json"
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, list):
                    save_state_json(CATEGORY_CHAT_CACHE, account_name, payload, scope="")
                    return payload
            except Exception:
                pass
        data = load_state_json(CATEGORY_CHAT_CACHE, account_name, scope="")
        if isinstance(data, list):
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            return data
        return []

    def _save_chat_cache(self, account_name: str, chats: List[Dict[str, Any]]) -> None:
        save_state_json(CATEGORY_CHAT_CACHE, account_name, chats, scope="")
        account_dir = self.signs_dir / account_name
        account_dir.mkdir(parents=True, exist_ok=True)
        cache_file = account_dir / "chats_cache.json"
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(chats, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _delete_chat_cache(self, account_name: str) -> None:
        delete_state(CATEGORY_CHAT_CACHE, account_name, scope="")
        cache_file = self.signs_dir / account_name / "chats_cache.json"
        if cache_file.exists():
            try:
                cache_file.unlink()
            except Exception:
                pass

    def list_tasks(
        self, account_name: Optional[str] = None, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """Return all sign tasks, with in-memory caching."""
        if self._tasks_cache is not None and not force_refresh:
            if account_name:
                return [
                    t
                    for t in self._tasks_cache
                    if t.get("account_name") == account_name
                ]
            return self._tasks_cache

        tasks = []
        base_dir = self.signs_dir

        print(f"DEBUG: scanning task directory: {base_dir}")
        try:
            # Scan all subdirectories under signs/.
            for account_path in base_dir.iterdir():
                if not account_path.is_dir():
                    # Compatibility with the old flat layout: signs/task_name
                    if (account_path / "config.json").exists():
                        task_info = self._load_task_config(account_path)
                        if task_info:
                            tasks.append(task_info)
                    continue

                # Scan tasks inside account directories.
                for task_dir in account_path.iterdir():
                    if not task_dir.is_dir():
                        continue

                    task_info = self._load_task_config(task_dir)
                    if task_info:
                        tasks.append(task_info)

            self._tasks_cache = sorted(
                tasks, key=lambda x: (x["account_name"], x["name"])
            )

            if not self._tasks_cache:
                db_tasks = []
                for task_name, _scope, config in list_state_items(CATEGORY_SIGN_TASK):
                    if not isinstance(config, dict):
                        continue
                    db_tasks.append(self._task_record_to_output(task_name, config))
                self._tasks_cache = sorted(
                    db_tasks, key=lambda x: (x["account_name"], x["name"])
                )

            if account_name:
                return [
                    t
                    for t in self._tasks_cache
                    if t.get("account_name") == account_name
                ]
            return self._tasks_cache

        except Exception as e:
            print(f"DEBUG: failed to scan task directory: {str(e)}")
            return []

    def _load_task_config(self, task_dir: Path) -> Optional[Dict[str, Any]]:
        """Load one task config and prefer last_run from config.json."""
        config_file = task_dir / "config.json"
        if not config_file.exists():
            return None

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            from backend.services.config import get_config_service
            if isinstance(config, dict):
                get_config_service().save_sign_config(task_dir.name, config)

            # Prefer last_run embedded in config.json.
            last_run = config.get("last_run")
            if not last_run:
                last_run = self._get_last_run_info(
                    task_dir.name, account_name=config.get("account_name", "")
                )

            return {
                "name": task_dir.name,
                "account_name": config.get("account_name", ""),
                "sign_at": config.get("sign_at", ""),
                "random_seconds": config.get("random_seconds", 0),
                "sign_interval": config.get("sign_interval", 1),
                "chats": config.get("chats", []),
                "enabled": True,
                "last_run": last_run,
                "execution_mode": config.get("execution_mode", "fixed"),
                "range_start": config.get("range_start", ""),
                "range_end": config.get("range_end", ""),
            }
        except Exception:
            return None

    def get_task(
        self, task_name: str, account_name: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Return a single task definition."""
        from backend.services.config import get_config_service

        db_config = get_config_service().get_sign_config(task_name, account_name)
        if isinstance(db_config, dict):
            return self._task_record_to_output(task_name, db_config)

        if account_name:
            task_dir = self.signs_dir / account_name / task_name
        else:
            # Search mode for compatibility with the old layout.
            task_dir = self.signs_dir / task_name
            if not (task_dir / "config.json").exists():
                for acc_dir in self.signs_dir.iterdir():
                    if (
                        acc_dir.is_dir()
                        and (acc_dir / task_name / "config.json").exists()
                    ):
                        task_dir = acc_dir / task_name
                        break

        config_file = task_dir / "config.json"

        if not config_file.exists():
            return None

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            return {
                "name": task_name,
                "account_name": config.get("account_name", ""),
                "sign_at": config.get("sign_at", ""),
                "random_seconds": config.get("random_seconds", 0),
                "sign_interval": config.get("sign_interval", 1),
                "chats": config.get("chats", []),
                "enabled": True,
                "execution_mode": config.get("execution_mode", "fixed"),
                "range_start": config.get("range_start", ""),
                "range_end": config.get("range_end", ""),
            }
        except Exception:
            return None

    def create_task(
        self,
        task_name: str,
        sign_at: str,
        chats: List[Dict[str, Any]],
        random_seconds: int = 0,
        sign_interval: Optional[int] = None,
        account_name: str = "",
        execution_mode: str = "fixed",
        range_start: str = "",
        range_end: str = "",
    ) -> Dict[str, Any]:
        """Create a new sign task."""
        import random

        from backend.services.config import get_config_service

        if not account_name:
            raise ValueError("account_name is required")

        account_dir = self.signs_dir / account_name
        account_dir.mkdir(parents=True, exist_ok=True)

        task_dir = account_dir / task_name
        task_dir.mkdir(parents=True, exist_ok=True)

        # Resolve sign_interval.
        if sign_interval is None:
            config_service = get_config_service()
            global_settings = config_service.get_global_settings()
            sign_interval = global_settings.get("sign_interval")

        if sign_interval is None:
            sign_interval = random.randint(1, 120)

        config = {
            "_version": 3,
            "account_name": account_name,
            "sign_at": sign_at,
            "random_seconds": random_seconds,
            "sign_interval": sign_interval,
            "chats": chats,
            "execution_mode": execution_mode,
            "range_start": range_start,
            "range_end": range_end,
        }

        get_config_service().save_sign_config(task_name, config)

        # Invalidate cache
        self._tasks_cache = None

        try:
            from backend.scheduler import add_or_update_sign_task_job

            add_or_update_sign_task_job(
                account_name,
                task_name,
                range_start if execution_mode == "range" else sign_at,
                enabled=True,
            )
        except Exception as e:
            print(f"DEBUG: failed to update scheduler task: {e}")

        return {
            "name": task_name,
            "account_name": account_name,
            "sign_at": sign_at,
            "random_seconds": random_seconds,
            "sign_interval": sign_interval,
            "chats": chats,
            "enabled": True,
            "execution_mode": execution_mode,
            "range_start": range_start,
            "range_end": range_end,
        }

    def update_task(
        self,
        task_name: str,
        sign_at: Optional[str] = None,
        chats: Optional[List[Dict[str, Any]]] = None,
        random_seconds: Optional[int] = None,
        sign_interval: Optional[int] = None,
        account_name: Optional[str] = None,
        execution_mode: Optional[str] = None,
        range_start: Optional[str] = None,
        range_end: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an existing sign task."""
        # Load the current task config.
        existing = self.get_task(task_name, account_name)
        if not existing:
            raise ValueError(f"Task not found: {task_name}")

        # Determine the account name for the update.
        # If a new account_name is provided, use it. Otherwise, use the existing one.
        acc_name = (
            account_name
            if account_name is not None
            else existing.get("account_name", "")
        )

        # Build the updated config.
        config = {
            "_version": 3,
            "account_name": acc_name,
            "sign_at": sign_at if sign_at is not None else existing["sign_at"],
            "random_seconds": random_seconds
            if random_seconds is not None
            else existing["random_seconds"],
            "sign_interval": sign_interval
            if sign_interval is not None
            else existing["sign_interval"],
            "chats": chats if chats is not None else existing["chats"],
            "execution_mode": execution_mode
            if execution_mode is not None
            else existing.get("execution_mode", "fixed"),
            "range_start": range_start
            if range_start is not None
            else existing.get("range_start", ""),
            "range_end": range_end
            if range_end is not None
            else existing.get("range_end", ""),
        }

        # Persist the updated config.
        task_dir = self.signs_dir / acc_name / task_name
        if not task_dir.exists():
            task_dir = self.signs_dir / task_name

        from backend.services.config import get_config_service

        get_config_service().save_sign_config(task_name, config)

        # Invalidate cache
        self._tasks_cache = None

        try:
            from backend.scheduler import add_or_update_sign_task_job

            add_or_update_sign_task_job(
                config["account_name"],
                task_name,
                config.get("range_start")
                if config.get("execution_mode") == "range"
                else config["sign_at"],
                enabled=True,
            )
        except Exception as e:
            msg = f"DEBUG: failed to update scheduler task: {e}"
            print(msg)
            self._append_scheduler_log(
                "scheduler_error.log", f"{datetime.now()}: {msg}"
            )
        else:
            self._append_scheduler_log(
                "scheduler_update.log",
                f"{datetime.now()}: Updated task {task_name} with cron {config.get('range_start') if config.get('execution_mode') == 'range' else config['sign_at']}",
            )

        return {
            "name": task_name,
            "account_name": config["account_name"],
            "sign_at": config["sign_at"],
            "random_seconds": config["random_seconds"],
            "sign_interval": config["sign_interval"],
            "chats": config["chats"],
            "enabled": True,
            "execution_mode": config.get("execution_mode", "fixed"),
            "range_start": config.get("range_start", ""),
            "range_end": config.get("range_end", ""),
        }

    def delete_task(self, task_name: str, account_name: Optional[str] = None) -> bool:
        """
        删除签到任务
        """
        from backend.services.config import get_config_service

        deleted_from_config = get_config_service().delete_sign_config(
            task_name, account_name=account_name
        )

        task_dir = None
        if account_name:
            task_dir = self.signs_dir / account_name / task_name
            if not task_dir.exists():
                delete_state(CATEGORY_SIGN_TASK_HISTORY, task_name, scope=account_name)
                return deleted_from_config
        else:
            task_dir = self.signs_dir / task_name
            if not task_dir.exists():
                for acc_dir in self.signs_dir.iterdir():
                    if acc_dir.is_dir() and (acc_dir / task_name).exists():
                        task_dir = acc_dir / task_name
                        break

        if not task_dir or not task_dir.exists():
            delete_state(CATEGORY_SIGN_TASK_HISTORY, task_name, scope=account_name)
            return deleted_from_config

        real_account_name = account_name
        if not real_account_name:
            if task_dir.parent.parent == self.signs_dir:
                real_account_name = task_dir.parent.name
            else:
                try:
                    with open(task_dir / "config.json", "r", encoding="utf-8") as f:
                        real_account_name = json.load(f).get("account_name")
                except Exception:
                    pass

        delete_state(CATEGORY_SIGN_TASK_HISTORY, task_name, scope=real_account_name)

        try:
            import shutil

            shutil.rmtree(task_dir)
            self._tasks_cache = None

            if real_account_name:
                try:
                    from backend.scheduler import remove_sign_task_job

                    remove_sign_task_job(real_account_name, task_name)
                except Exception as e:
                    print(f"DEBUG: failed to remove scheduler task: {e}")

            return True
        except Exception:
            return deleted_from_config
    async def get_account_chats(
        self, account_name: str, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """Return the account chat list with cache support."""
        if not force_refresh:
            cached = self._load_chat_cache(account_name)
            if cached:
                return cached

        # If cache is missing or force_refresh is set, refresh from Telegram.
        return await self.refresh_account_chats(account_name)

    def search_account_chats(
        self,
        account_name: str,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Search the cached chat list without calling get_dialogs again."""
        if limit < 1:
            limit = 1
        if limit > 200:
            limit = 200
        if offset < 0:
            offset = 0

        data = self._load_chat_cache(account_name)
        if not data:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}

        q = (query or "").strip()
        if not q:
            total = len(data)
            return {
                "items": data[offset : offset + limit],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

        is_numeric = q.lstrip("-").isdigit()
        if is_numeric or q.startswith("-100"):
            def match(chat: Dict[str, Any]) -> bool:
                chat_id = chat.get("id")
                if chat_id is None:
                    return False
                return q in str(chat_id)
        else:
            q_lower = q.lower()

            def match(chat: Dict[str, Any]) -> bool:
                title = (chat.get("title") or "").lower()
                username = (chat.get("username") or "").lower()
                return q_lower in title or q_lower in username

        filtered = [c for c in data if match(c)]
        total = len(filtered)
        return {
            "items": filtered[offset : offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _is_invalid_session_error(err: Exception) -> bool:
        msg = str(err)
        if not msg:
            return False
        upper = msg.upper()
        return (
            "AUTH_KEY_UNREGISTERED" in upper
            or "AUTH_KEY_INVALID" in upper
            or "SESSION_REVOKED" in upper
            or "SESSION_EXPIRED" in upper
            or "USER_DEACTIVATED" in upper
        )

    async def _cleanup_invalid_session(self, account_name: str) -> None:
        try:
            from backend.services.telegram import get_telegram_service

            await get_telegram_service().delete_account(account_name)
        except Exception as e:
            print(f"DEBUG: failed to clean up invalid session: {e}")

        # Clear stale chat cache after an invalid session.
        self._delete_chat_cache(account_name)

    async def refresh_account_chats(self, account_name: str) -> List[Dict[str, Any]]:
        """Connect to Telegram and refresh the chat list."""
        from pyrogram.enums import ChatType

        # Resolve Telegram session data.
        from backend.core.config import get_settings
        from backend.services.config import get_config_service

        settings = get_settings()
        session_dir = settings.resolve_session_dir()
        session_mode = get_session_mode()
        session_string = None
        fallback_session_string = None
        used_fallback_session = False
        session_file = session_dir / f"{account_name}.session"

        if session_mode == "string":
            session_string = (
                get_account_session_string(account_name)
                or load_session_string_file(session_dir, account_name)
            )
            if not session_string:
                raise ValueError(
                    f"Account {account_name} login is invalid, please login again"
                )
        else:
            fallback_session_string = (
                get_account_session_string(account_name)
                or load_session_string_file(session_dir, account_name)
            )
            if not session_file.exists():
                if fallback_session_string:
                    session_string = fallback_session_string
                    used_fallback_session = True
                else:
                    raise ValueError(
                        f"Account {account_name} login is invalid, please login again"
                    )

        config_service = get_config_service()
        tg_config = config_service.get_telegram_config()
        api_id = os.getenv("TG_API_ID") or tg_config.get("api_id")
        api_hash = os.getenv("TG_API_HASH") or tg_config.get("api_hash")

        try:
            api_id = int(api_id) if api_id is not None else None
        except (TypeError, ValueError):
            api_id = None

        if isinstance(api_hash, str):
            api_hash = api_hash.strip()

        if not api_id or not api_hash:
            raise ValueError("Telegram API ID or API Hash is not configured")

        # Reuse the shared Telegram client helper.
        proxy_dict = None
        proxy_value = get_account_proxy(account_name)
        if proxy_value:
            proxy_dict = build_proxy_dict(proxy_value)
        client_kwargs = {
            "name": account_name,
            "workdir": session_dir,
            "api_id": api_id,
            "api_hash": api_hash,
            "session_string": session_string,
            "in_memory": session_mode == "string",
            "proxy": proxy_dict,
            "no_updates": True,
        }
        client = get_client(**client_kwargs)

        chats: List[Dict[str, Any]] = []
        logger = logging.getLogger("backend")
        try:
            # Initialize the per-account lock shared across services.
            if account_name not in self._account_locks:
                self._account_locks[account_name] = get_account_lock(account_name)

            account_lock = self._account_locks[account_name]

            async def _fetch_chats(active_client) -> List[Dict[str, Any]]:
                local_chats: List[Dict[str, Any]] = []
                # Keep lifecycle and account locking scoped to this refresh.
                async with account_lock:
                    async with get_global_semaphore():
                        async with active_client:
                            # Probe account identity first to detect invalid sessions.
                            await active_client.get_me()

                            try:
                                async for dialog in active_client.get_dialogs():
                                    try:
                                        chat = getattr(dialog, "chat", None)
                                        if chat is None:
                                            logger.warning(
                                                "get_dialogs returned an empty chat, skipped"
                                            )
                                            continue
                                        chat_id = getattr(chat, "id", None)
                                        if chat_id is None:
                                            logger.warning(
                                                "get_dialogs returned a chat without id, skipped"
                                            )
                                            continue

                                        chat_info = {
                                            "id": chat_id,
                                            "title": chat.title
                                            or chat.first_name
                                            or chat.username
                                            or str(chat_id),
                                            "username": chat.username,
                                            "type": chat.type.name.lower(),
                                        }

                                        # Add a lightweight prefix for bot dialogs.
                                        if chat.type == ChatType.BOT:
                                            chat_info["title"] = f"BOT {chat_info['title']}"

                                        local_chats.append(chat_info)
                                    except Exception as e:
                                        logger.warning(
                                            f"Failed to process dialog, skipped: {type(e).__name__}: {e}"
                                        )
                                        continue
                            except Exception as e:
                                # Return partial results if get_dialogs is interrupted.
                                logger.warning(
                                    f"get_dialogs interrupted, returning partial result: {type(e).__name__}: {e}"
                                )
                return local_chats

            try:
                chats = await _fetch_chats(client)
            except Exception as e:
                if self._is_invalid_session_error(e):
                    if fallback_session_string and not used_fallback_session:
                        logger.warning(
                            "Session invalid for %s, retry with session_string: %s",
                            account_name,
                            e,
                        )
                        try:
                            from tg_signer.core import close_client_by_name

                            await close_client_by_name(account_name, workdir=session_dir)
                        except Exception:
                            pass
                        used_fallback_session = True
                        retry_kwargs = dict(client_kwargs)
                        retry_kwargs["session_string"] = fallback_session_string
                        retry_kwargs["in_memory"] = True
                        retry_kwargs["no_updates"] = True
                        client = get_client(**retry_kwargs)
                        chats = await _fetch_chats(client)
                    else:
                        logger.warning(
                            "Session invalid for %s: %s",
                            account_name,
                            e,
                        )
                        await self._cleanup_invalid_session(account_name)
                        raise ValueError(
                            f"Account {account_name} login is invalid, please login again"
                        )
                else:
                    raise

            # Persist the refreshed chat cache.
            self._save_chat_cache(account_name, chats)

            return chats

        except Exception as e:
            raise e
    async def run_task(self, account_name: str, task_name: str) -> Dict[str, Any]:
        """Compatibility wrapper around run_task_with_logs."""
        return await self.run_task_with_logs(account_name, task_name)

    def _task_key(self, account_name: str, task_name: str) -> tuple[str, str]:
        return account_name, task_name

    def _find_task_keys(self, task_name: str) -> List[tuple[str, str]]:
        return [key for key in self._active_logs.keys() if key[1] == task_name]

    def get_active_logs(
        self, task_name: str, account_name: Optional[str] = None
    ) -> List[str]:
        """Return active logs for a task."""
        if account_name:
            return self._active_logs.get(self._task_key(account_name, task_name), [])
        for key in self._find_task_keys(task_name):
            return self._active_logs.get(key, [])
        return []

    def is_task_running(self, task_name: str, account_name: Optional[str] = None) -> bool:
        """Return whether a task is currently running."""
        if account_name:
            return self._active_tasks.get(self._task_key(account_name, task_name), False)
        return any(key[1] == task_name for key, running in self._active_tasks.items() if running)

    async def run_task_with_logs(
        self, account_name: str, task_name: str
    ) -> Dict[str, Any]:
        """Run a task in-process and capture live logs."""

        if self.is_task_running(task_name, account_name):
            return {"success": False, "error": "Task is already running", "output": ""}

        if account_name not in self._account_locks:
            self._account_locks[account_name] = get_account_lock(account_name)

        account_lock = self._account_locks[account_name]
        print(f"DEBUG: waiting for account lock: {account_name}...")

        task_key = self._task_key(account_name, task_name)
        self._active_tasks[task_key] = True
        self._active_logs[task_key] = []

        tg_logger = logging.getLogger("tg-signer")
        log_handler = TaskLogHandler(self._active_logs[task_key])
        log_handler.setLevel(logging.INFO)
        log_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
        tg_logger.addHandler(log_handler)

        success = False
        error_msg = ""
        output_str = ""

        try:
            async with account_lock:
                last_end = self._account_last_run_end.get(account_name)
                if last_end:
                    gap = time.time() - last_end
                    wait_seconds = self._account_cooldown_seconds - gap
                    if wait_seconds > 0:
                        self._active_logs[task_key].append(
                            f"Waiting for account cooldown: {int(wait_seconds)}s"
                        )
                        await asyncio.sleep(wait_seconds)

                print(f"DEBUG: acquired account lock {account_name}, starting task {task_name}")
                self._active_logs[task_key].append(
                    f"Starting task {task_name} (account: {account_name})"
                )

                from backend.services.config import get_config_service

                config_service = get_config_service()
                tg_config = config_service.get_telegram_config()
                api_id = os.getenv("TG_API_ID") or tg_config.get("api_id")
                api_hash = os.getenv("TG_API_HASH") or tg_config.get("api_hash")

                try:
                    api_id = int(api_id) if api_id is not None else None
                except (TypeError, ValueError):
                    api_id = None

                if isinstance(api_hash, str):
                    api_hash = api_hash.strip()

                if not api_id or not api_hash:
                    raise ValueError("Telegram API ID or API Hash is not configured")

                session_dir = settings.resolve_session_dir()
                session_mode = get_session_mode()
                session_string = None
                use_in_memory = False
                proxy_dict = None
                proxy_value = get_account_proxy(account_name)
                if proxy_value:
                    proxy_dict = build_proxy_dict(proxy_value)

                if session_mode == "string":
                    session_string = (
                        get_account_session_string(account_name)
                        or load_session_string_file(session_dir, account_name)
                    )
                    if not session_string:
                        raise ValueError(
                            f"Account {account_name} has no valid session_string"
                        )
                    use_in_memory = True
                else:
                    if os.getenv("SIGN_TASK_FORCE_IN_MEMORY") == "1":
                        session_string = load_session_string_file(
                            session_dir, account_name
                        )
                        use_in_memory = bool(session_string)

                task_cfg = self.get_task(task_name, account_name=account_name)
                requires_updates = self._task_requires_updates(task_cfg)
                signer_no_updates = not requires_updates
                self._active_logs[task_key].append(
                    f"Update listener: {'enabled' if requires_updates else 'disabled'}"
                )

                signer = BackendUserSigner(
                    task_name=task_name,
                    session_dir=str(session_dir),
                    account=account_name,
                    workdir=self.workdir,
                    proxy=proxy_dict,
                    session_string=session_string,
                    in_memory=use_in_memory,
                    api_id=api_id,
                    api_hash=api_hash,
                    no_updates=signer_no_updates,
                )

                async with get_global_semaphore():
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            await signer.run_once(num_of_dialogs=20)
                            break
                        except Exception as e:
                            if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                                delay = (attempt + 1) * 3
                                self._active_logs[task_key].append(
                                    f"Session database locked, retrying in {delay}s..."
                                )
                                await asyncio.sleep(delay)
                                continue
                            raise

                success = True
                self._active_logs[task_key].append("Task completed")
                await asyncio.sleep(2)

        except Exception as e:
            error_msg = f"Task execution failed: {str(e)}"
            self._active_logs[task_key].append(error_msg)
            traceback.print_exc()
            logger = logging.getLogger("backend")
            logger.error(error_msg)
        finally:
            self._account_last_run_end[account_name] = time.time()
            self._active_tasks[task_key] = False
            tg_logger.removeHandler(log_handler)

            final_logs = list(self._active_logs.get(task_key, []))
            output_str = "\n".join(final_logs)

            last_reply = ""
            if success:
                for line in reversed(final_logs):
                    normalized = line.replace("\n", " ").strip()
                    if not normalized:
                        continue
                    if len(normalized) > 200:
                        normalized = normalized[:197] + "..."
                    last_reply = normalized
                    break

            msg = error_msg if not success else last_reply
            self._save_run_info(
                task_name,
                success,
                msg,
                account_name,
                flow_logs=final_logs,
            )

            if task_key in self._cleanup_tasks:
                cleanup_task = self._cleanup_tasks.get(task_key)
                if cleanup_task and not cleanup_task.done():
                    cleanup_task.cancel()

            async def cleanup():
                try:
                    await asyncio.sleep(60)
                    if not self._active_tasks.get(task_key):
                        self._active_logs.pop(task_key, None)
                finally:
                    self._cleanup_tasks.pop(task_key, None)

            self._cleanup_tasks[task_key] = asyncio.create_task(cleanup())

        return {
            "success": success,
            "output": output_str,
            "error": error_msg,
        }


# Create the shared service instance
_sign_task_service: Optional[SignTaskService] = None


def get_sign_task_service() -> SignTaskService:
    global _sign_task_service
    if _sign_task_service is None:
        _sign_task_service = SignTaskService()
    return _sign_task_service

