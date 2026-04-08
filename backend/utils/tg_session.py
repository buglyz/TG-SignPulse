from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from backend.core.config import get_settings
from backend.core.database import get_session_local
from backend.models.account import Account

_SESSION_MODE_ENV = "TG_SESSION_MODE"
_SESSION_MODE_FILE = "file"
_SESSION_MODE_STRING = "string"

_GLOBAL_SEMAPHORE: Optional[asyncio.Semaphore] = None


def get_session_mode() -> str:
    mode = os.getenv(_SESSION_MODE_ENV, _SESSION_MODE_FILE).strip().lower()
    return _SESSION_MODE_STRING if mode == _SESSION_MODE_STRING else _SESSION_MODE_FILE


def is_string_session_mode() -> bool:
    return get_session_mode() == _SESSION_MODE_STRING


def get_no_updates_flag() -> bool:
    raw = os.getenv("TG_SESSION_NO_UPDATES") or os.getenv("TG_NO_UPDATES") or ""
    raw = raw.strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_global_semaphore() -> asyncio.Semaphore:
    global _GLOBAL_SEMAPHORE
    if _GLOBAL_SEMAPHORE is None:
        raw = (os.getenv("TG_GLOBAL_CONCURRENCY") or "1").strip()
        try:
            limit = int(raw)
        except ValueError:
            limit = 1
        if limit < 1:
            limit = 1
        _GLOBAL_SEMAPHORE = asyncio.Semaphore(limit)
    return _GLOBAL_SEMAPHORE


def _account_store_path() -> Path:
    settings = get_settings()
    session_dir = settings.resolve_session_dir()
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / "accounts.json"


def _load_account_store() -> dict:
    path = _account_store_path()
    if not path.exists():
        return {"accounts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"accounts": {}}
    if not isinstance(data, dict):
        return {"accounts": {}}
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        data["accounts"] = {}
    return data


def _save_account_store(data: dict) -> None:
    path = _account_store_path()
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp_path.replace(path)


def _resolve_telegram_credentials() -> tuple[Optional[str], Optional[str]]:
    api_id = (os.getenv("TG_API_ID") or "").strip()
    api_hash = (os.getenv("TG_API_HASH") or "").strip()
    if api_id and api_hash:
        return api_id, api_hash
    try:
        from backend.services.config import get_config_service

        tg_config = get_config_service().get_telegram_config()
    except Exception:
        tg_config = {}
    api_id = api_id or str(tg_config.get("api_id") or "").strip()
    api_hash = api_hash or str(tg_config.get("api_hash") or "").strip()
    return (api_id or None, api_hash or None)


def _get_account_record(account_name: str) -> Optional[Account]:
    session_local = get_session_local()
    with session_local() as db:
        return (
            db.query(Account)
            .filter(Account.account_name == account_name)
            .first()
        )


def _list_db_account_names() -> list[str]:
    session_local = get_session_local()
    with session_local() as db:
        rows = db.query(Account.account_name).all()
    names = []
    for row in rows:
        value = row[0] if isinstance(row, tuple) else getattr(row, "account_name", None)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    return sorted(set(names))


def list_account_names() -> list[str]:
    names = set(_list_db_account_names())
    data = _load_account_store()
    accounts = data.get("accounts", {})
    if isinstance(accounts, dict):
        names.update(accounts.keys())
    return sorted(name for name in names if isinstance(name, str) and name.strip())


def get_account_session_string(account_name: str) -> Optional[str]:
    record = _get_account_record(account_name)
    if record and isinstance(record.session_string, str) and record.session_string.strip():
        return record.session_string.strip()
    data = _load_account_store()
    entry = data.get("accounts", {}).get(account_name)
    if not isinstance(entry, dict):
        return None
    session_string = entry.get("session_string")
    if isinstance(session_string, str) and session_string.strip():
        normalized = session_string.strip()
        try:
            set_account_session_string(account_name, normalized)
        except Exception:
            pass
        return normalized
    return None


def set_account_session_string(account_name: str, session_string: str) -> None:
    normalized = session_string.strip()
    api_id, api_hash = _resolve_telegram_credentials()
    session_local = get_session_local()
    with session_local() as db:
        record = (
            db.query(Account)
            .filter(Account.account_name == account_name)
            .first()
        )
        if record is None:
            if not api_id or not api_hash:
                raise ValueError("Telegram API credentials are required to persist session_string")
            record = Account(
                account_name=account_name,
                api_id=str(api_id),
                api_hash=str(api_hash),
                session_string=normalized,
            )
            db.add(record)
        else:
            if api_id and not record.api_id:
                record.api_id = str(api_id)
            if api_hash and not record.api_hash:
                record.api_hash = str(api_hash)
            record.session_string = normalized
            record.updated_at = datetime.utcnow()
        db.commit()

    data = _load_account_store()
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
        data["accounts"] = accounts
    entry = accounts.get(account_name)
    if not isinstance(entry, dict):
        entry = {}
    entry["session_string"] = normalized
    entry["updated_at"] = datetime.utcnow().isoformat()
    accounts[account_name] = entry
    _save_account_store(data)


def delete_account_session_string(account_name: str) -> None:
    session_local = get_session_local()
    with session_local() as db:
        record = (
            db.query(Account)
            .filter(Account.account_name == account_name)
            .first()
        )
        if record is not None:
            record.session_string = None
            record.updated_at = datetime.utcnow()
            db.commit()

    data = _load_account_store()
    accounts = data.get("accounts")
    if isinstance(accounts, dict) and account_name in accounts:
        entry = accounts.get(account_name)
        if isinstance(entry, dict):
            entry.pop("session_string", None)
            entry["updated_at"] = datetime.utcnow().isoformat()
            if any(entry.get(key) for key in ("remark", "proxy")):
                accounts[account_name] = entry
            else:
                accounts.pop(account_name, None)
        else:
            accounts.pop(account_name, None)
        _save_account_store(data)


def get_account_profile(account_name: str) -> dict[str, Any]:
    record = _get_account_record(account_name)
    if record is not None:
        profile = {
            "remark": record.remark.strip()
            if isinstance(record.remark, str) and record.remark.strip()
            else None,
            "proxy": record.proxy.strip()
            if isinstance(record.proxy, str) and record.proxy.strip()
            else None,
        }
        if profile["remark"] is not None or profile["proxy"] is not None:
            return profile

    data = _load_account_store()
    entry = data.get("accounts", {}).get(account_name)
    if not isinstance(entry, dict):
        return {}
    profile = {
        "remark": entry.get("remark"),
        "proxy": entry.get("proxy"),
    }
    if profile.get("remark") is not None or profile.get("proxy") is not None:
        try:
            set_account_profile(
                account_name,
                remark=profile.get("remark"),
                proxy=profile.get("proxy"),
            )
        except Exception:
            pass
    return profile


def get_account_proxy(account_name: str) -> Optional[str]:
    profile = get_account_profile(account_name)
    proxy = profile.get("proxy")
    if isinstance(proxy, str) and proxy.strip():
        return proxy.strip()
    return None


def get_account_remark(account_name: str) -> Optional[str]:
    profile = get_account_profile(account_name)
    remark = profile.get("remark")
    if isinstance(remark, str) and remark.strip():
        return remark.strip()
    return None


def set_account_profile(
    account_name: str, *, remark: Optional[str] = None, proxy: Optional[str] = None
) -> None:
    normalized_remark = remark.strip() if isinstance(remark, str) else remark
    normalized_proxy = proxy.strip() if isinstance(proxy, str) else proxy

    api_id, api_hash = _resolve_telegram_credentials()
    session_local = get_session_local()
    with session_local() as db:
        record = (
            db.query(Account)
            .filter(Account.account_name == account_name)
            .first()
        )
        if record is None and api_id and api_hash:
            record = Account(
                account_name=account_name,
                api_id=str(api_id),
                api_hash=str(api_hash),
            )
            db.add(record)
        if record is not None:
            if api_id and not record.api_id:
                record.api_id = str(api_id)
            if api_hash and not record.api_hash:
                record.api_hash = str(api_hash)
            if remark is not None:
                record.remark = normalized_remark
            if proxy is not None:
                record.proxy = normalized_proxy
            record.updated_at = datetime.utcnow()
            db.commit()

    data = _load_account_store()
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
        data["accounts"] = accounts
    entry = accounts.get(account_name)
    if not isinstance(entry, dict):
        entry = {}
    if remark is not None:
        entry["remark"] = normalized_remark
    if proxy is not None:
        entry["proxy"] = normalized_proxy
    entry["updated_at"] = datetime.utcnow().isoformat()
    accounts[account_name] = entry
    _save_account_store(data)


def session_string_file_path(session_dir: Path, account_name: str) -> Path:
    return session_dir / f"{account_name}.session_string"


def load_session_string_file(session_dir: Path, account_name: str) -> Optional[str]:
    path = session_string_file_path(session_dir, account_name)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return content or None


def save_session_string_file(
    session_dir: Path, account_name: str, session_string: str
) -> None:
    path = session_string_file_path(session_dir, account_name)
    path.write_text(session_string.strip(), encoding="utf-8")


def delete_session_string_file(session_dir: Path, account_name: str) -> None:
    path = session_string_file_path(session_dir, account_name)
    if path.exists():
        try:
            path.unlink()
        except Exception:
            pass
