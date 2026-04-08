from __future__ import annotations

import json
from typing import Any, Optional

from backend.core.database import get_session_local
from backend.models.persisted_state import PersistedState

CATEGORY_SIGN_TASK = "sign_task"
CATEGORY_MONITOR_TASK = "monitor_task"
CATEGORY_SIGN_TASK_HISTORY = "sign_task_history"
CATEGORY_CHAT_CACHE = "chat_cache"
CATEGORY_AI_CONFIG = "ai_config"
CATEGORY_GLOBAL_SETTINGS = "global_settings"
CATEGORY_TELEGRAM_CONFIG = "telegram_config"


def _normalize_scope(scope: Optional[str]) -> str:
    return (scope or "").strip()


def load_state_json(
    category: str,
    item_key: str,
    *,
    scope: Optional[str] = None,
    default: Any = None,
) -> Any:
    session_local = get_session_local()
    with session_local() as db:
        row = (
            db.query(PersistedState)
            .filter(PersistedState.category == category)
            .filter(PersistedState.item_key == item_key)
            .filter(PersistedState.scope == _normalize_scope(scope))
            .first()
        )
    if row is None:
        return default
    try:
        return json.loads(row.payload)
    except Exception:
        return default


def save_state_json(
    category: str,
    item_key: str,
    payload: Any,
    *,
    scope: Optional[str] = None,
) -> None:
    session_local = get_session_local()
    normalized_scope = _normalize_scope(scope)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    with session_local() as db:
        row = (
            db.query(PersistedState)
            .filter(PersistedState.category == category)
            .filter(PersistedState.item_key == item_key)
            .filter(PersistedState.scope == normalized_scope)
            .first()
        )
        if row is None:
            row = PersistedState(
                category=category,
                item_key=item_key,
                scope=normalized_scope,
                payload=serialized,
            )
            db.add(row)
        else:
            row.payload = serialized
        db.commit()


def delete_state(category: str, item_key: str, *, scope: Optional[str] = None) -> bool:
    session_local = get_session_local()
    normalized_scope = _normalize_scope(scope)
    with session_local() as db:
        row = (
            db.query(PersistedState)
            .filter(PersistedState.category == category)
            .filter(PersistedState.item_key == item_key)
            .filter(PersistedState.scope == normalized_scope)
            .first()
        )
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True


def list_state_rows(category: str, *, scope: Optional[str] = None) -> list[PersistedState]:
    session_local = get_session_local()
    with session_local() as db:
        query = db.query(PersistedState).filter(PersistedState.category == category)
        if scope is not None:
            query = query.filter(PersistedState.scope == _normalize_scope(scope))
        rows = query.order_by(PersistedState.updated_at.desc()).all()
    return rows


def list_state_items(category: str, *, scope: Optional[str] = None) -> list[tuple[str, str, Any]]:
    items: list[tuple[str, str, Any]] = []
    for row in list_state_rows(category, scope=scope):
        try:
            payload = json.loads(row.payload)
        except Exception:
            continue
        items.append((row.item_key, row.scope, payload))
    return items
