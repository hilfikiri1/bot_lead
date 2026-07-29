"""Agent users, roles, one-time invitations and lead access context."""

from __future__ import annotations

import hashlib
import secrets
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent_invite import AgentInvite
from app.models.agent_user import AgentUser

settings = get_settings()

ROLES = {"owner", "admin", "manager", "viewer"}
MANAGE_ROLES = {"owner", "admin"}
WRITE_ROLES = {"owner", "admin", "manager"}
_current_user: ContextVar[AgentUser | None] = ContextVar(
    "bbs_current_agent_user", default=None
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def set_current_user(user: AgentUser | None) -> None:
    _current_user.set(user)


def current_user() -> AgentUser | None:
    return _current_user.get()


def can_manage_users(user: AgentUser | None) -> bool:
    return bool(user and user.status == "active" and user.role in MANAGE_ROLES)


def can_write(user: AgentUser | None) -> bool:
    return bool(user and user.status == "active" and user.role in WRITE_ROLES)


def can_invite_role(user: AgentUser, role: str) -> bool:
    if role not in ROLES:
        return False
    if user.role == "owner":
        return role in {"admin", "manager", "viewer"}
    if user.role == "admin":
        return role in {"manager", "viewer"}
    return False


def current_user_can_write() -> bool:
    user = current_user()
    # System/background work has no Telegram actor and keeps its existing path.
    return True if user is None else can_write(user)


def user_can_access_responsible_id(
    user: AgentUser | None, responsible_user_id: int | None
) -> bool:
    if user is None:
        return True
    if user.status != "active":
        return False
    if user.role in {"owner", "admin"} or user.lead_access_scope == "all":
        return True
    if user.lead_access_scope != "assigned" or not user.kommo_user_id:
        return False
    return int(responsible_user_id or 0) == int(user.kommo_user_id)


def current_user_can_access_responsible_id(responsible_user_id: int | None) -> bool:
    return user_can_access_responsible_id(current_user(), responsible_user_id)


def assert_current_user_can_access_lead(lead: dict[str, Any]) -> None:
    user = current_user()
    if user_can_access_responsible_id(user, lead.get("responsible_user_id")):
        return
    if user and user.lead_access_scope == "assigned" and not user.kommo_user_id:
        raise PermissionError(
            "Для менеджера ещё не указан Kommo User ID. "
            "Владелец может привязать его командой /bind_kommo."
        )
    raise PermissionError("Эта сделка назначена другому сотруднику.")


async def get_user_by_telegram_id(
    db: AsyncSession, telegram_user_id: int
) -> AgentUser | None:
    result = await db.execute(
        select(AgentUser).where(AgentUser.telegram_user_id == int(telegram_user_id))
    )
    return result.scalar_one_or_none()


async def ensure_bootstrap_user(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    telegram_username: str | None = None,
    display_name: str | None = None,
) -> AgentUser | None:
    allowed = settings.get_allowed_user_ids()
    if int(telegram_user_id) not in allowed:
        return None

    existing = await get_user_by_telegram_id(db, telegram_user_id)
    now = _utcnow()
    if existing:
        existing.telegram_username = telegram_username or existing.telegram_username
        existing.display_name = display_name or existing.display_name
        existing.last_seen_at = now
        if existing.status != "active":
            existing.status = "active"
        await db.commit()
        return existing

    owner_telegram_id = settings.telegram_owner_user_id or (allowed[0] if allowed else None)
    is_owner = int(telegram_user_id) == int(owner_telegram_id or 0)
    user = AgentUser(
        telegram_user_id=int(telegram_user_id),
        telegram_username=(telegram_username or None),
        display_name=(display_name or None),
        role="owner" if is_owner else "manager",
        status="active",
        interface_language="ru",
        # Existing Railway allowlist keeps its historical access until it is
        # intentionally tightened from the team settings.
        lead_access_scope="all",
        joined_at=now,
        last_seen_at=now,
    )
    db.add(user)
    try:
        await db.commit()
        await db.refresh(user)
        return user
    except IntegrityError:
        await db.rollback()
        concurrent = await get_user_by_telegram_id(db, telegram_user_id)
        return concurrent


async def authorize_telegram_user(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    telegram_username: str | None = None,
    display_name: str | None = None,
) -> AgentUser | None:
    user = await ensure_bootstrap_user(
        db,
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        display_name=display_name,
    )
    if user is None:
        user = await get_user_by_telegram_id(db, telegram_user_id)
        if user and user.status == "active":
            user.telegram_username = telegram_username or user.telegram_username
            user.display_name = display_name or user.display_name
            user.last_seen_at = _utcnow()
            await db.commit()
    if user is None or user.status != "active":
        set_current_user(None)
        return None
    set_current_user(user)
    return user


async def create_invite(
    db: AsyncSession,
    *,
    invited_by: AgentUser,
    role: str,
    interface_language: str = "ru",
    kommo_user_id: int | None = None,
) -> tuple[AgentInvite, str]:
    role = role.strip().lower()
    if not can_invite_role(invited_by, role):
        raise PermissionError("У этой роли нет права создавать такое приглашение.")
    raw_token = "inv_" + secrets.token_urlsafe(24)
    scope = "assigned" if role == "manager" else "all"
    ttl_hours = max(1, min(int(settings.agent_invite_ttl_hours or 48), 7 * 24))
    invite = AgentInvite(
        token_hash=_token_hash(raw_token),
        role=role,
        interface_language=interface_language,
        lead_access_scope=scope,
        kommo_user_id=kommo_user_id,
        status="pending",
        invited_by_user_id=invited_by.id,
        expires_at=_utcnow() + timedelta(hours=ttl_hours),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    return invite, raw_token


async def accept_invite(
    db: AsyncSession,
    *,
    raw_token: str,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
) -> AgentUser:
    token = raw_token.strip()
    invite = (
        await db.execute(
            select(AgentInvite)
            .where(AgentInvite.token_hash == _token_hash(token))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if invite is None:
        raise ValueError("Приглашение не найдено.")
    if invite.status != "pending":
        raise ValueError("Приглашение уже использовано или отменено.")
    if _aware(invite.expires_at) < _utcnow():
        invite.status = "expired"
        await db.commit()
        raise ValueError("Срок действия приглашения истёк.")

    user = await get_user_by_telegram_id(db, telegram_user_id)
    now = _utcnow()
    if user is None:
        user = AgentUser(
            telegram_user_id=int(telegram_user_id),
            telegram_username=telegram_username,
            display_name=display_name,
            role=invite.role,
            status="active",
            interface_language=invite.interface_language,
            lead_access_scope=invite.lead_access_scope,
            kommo_user_id=invite.kommo_user_id,
            invited_by_user_id=invite.invited_by_user_id,
            joined_at=now,
            last_seen_at=now,
        )
        db.add(user)
        await db.flush()
    else:
        if user.status == "active":
            raise ValueError("Этот Telegram-пользователь уже подключён к боту.")
        user.status = "active"
        user.role = invite.role
        user.interface_language = invite.interface_language
        user.lead_access_scope = invite.lead_access_scope
        user.kommo_user_id = invite.kommo_user_id
        user.invited_by_user_id = invite.invited_by_user_id
        user.joined_at = now
        user.last_seen_at = now
        user.telegram_username = telegram_username or user.telegram_username
        user.display_name = display_name or user.display_name

    invite.status = "accepted"
    invite.accepted_by_user_id = user.id
    invite.accepted_at = now
    await db.commit()
    await db.refresh(user)
    set_current_user(user)
    return user


async def list_users(db: AsyncSession) -> list[AgentUser]:
    result = await db.execute(
        select(AgentUser).order_by(AgentUser.created_at.asc(), AgentUser.id.asc())
    )
    return list(result.scalars().all())


async def bind_kommo_user(
    db: AsyncSession,
    *,
    actor: AgentUser,
    target_telegram_user_id: int,
    kommo_user_id: int,
) -> AgentUser:
    if not can_manage_users(actor):
        raise PermissionError("У вас нет права управлять пользователями.")
    target = await get_user_by_telegram_id(db, target_telegram_user_id)
    if target is None:
        raise ValueError("Пользователь не найден.")
    if actor.role == "admin" and target.role in {"owner", "admin"}:
        raise PermissionError("Admin не может изменять Owner или другого Admin.")
    target.kommo_user_id = int(kommo_user_id)
    target.lead_access_scope = "assigned"
    await db.commit()
    await db.refresh(target)
    return target
