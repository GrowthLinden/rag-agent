from __future__ import annotations

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from cache import cache
from models import ChatMessage, ChatSession, User
from time_utils import utc_now


AI_ACTIVE = "ai_active"
HANDOFF_REQUESTED = "handoff_requested"
HUMAN_ACTIVE = "human_active"
CLOSED = "closed"

USER_HANDOFF_KEYWORDS = (
    "转人工",
    "人工客服",
    "真人客服",
    "人工处理",
    "找人工",
    "找客服",
    "客服人员",
    "不要机器人",
    "投诉",
)


def wants_handoff(text: str) -> bool:
    normalized = (text or "").replace(" ", "").lower()
    return any(keyword in normalized for keyword in USER_HANDOFF_KEYWORDS)


def get_or_create_session(db: Session, user: User, session_id: str) -> ChatSession:
    session = (
        db.query(ChatSession)
        .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
        .first()
    )
    if session:
        return session

    now = utc_now()
    session = ChatSession(
        user_id=user.id,
        session_id=session_id,
        metadata_json={},
        status=AI_ACTIVE,
        updated_at=now,
        created_at=now,
    )
    db.add(session)
    db.flush()
    return session


def append_message(
    db: Session,
    session: ChatSession,
    sender_type: str,
    content: str,
    sender_id: int | None = None,
    message_type: str | None = None,
    rag_trace: dict | None = None,
    metadata: dict | None = None,
) -> ChatMessage:
    now = utc_now()
    normalized_message_type = message_type or {
        "user": "human",
        "admin": "human",
        "ai": "ai",
        "system": "system",
    }.get(sender_type, sender_type)
    message = ChatMessage(
        session_ref_id=session.id,
        message_type=normalized_message_type,
        sender_type=sender_type,
        sender_id=sender_id,
        content=content,
        timestamp=now,
        rag_trace=rag_trace,
        metadata_json=metadata or {},
    )
    db.add(message)
    session.last_message_preview = content[:255]
    session.updated_at = now
    if sender_type == "user":
        session.unread_user_count += 1
    elif sender_type == "admin":
        session.unread_admin_count += 1
    db.flush()
    if session.user:
        cache.delete(f"chat_messages:{session.user.username}:{session.session_id}")
        cache.delete(f"chat_sessions:{session.user.username}")
    return message


def request_handoff(db: Session, session: ChatSession, reason: str) -> None:
    now = utc_now()
    session.status = HANDOFF_REQUESTED
    session.handoff_reason = reason[:255]
    session.handoff_at = session.handoff_at or now
    session.assigned_admin_id = None
    session.updated_at = now
    append_message(
        db,
        session,
        sender_type="system",
        content="正在为您呼叫人工服务....",
        metadata={"event": "handoff_requested", "reason": reason},
    )


def takeover_session(db: Session, session: ChatSession, admin: User) -> ChatMessage:
    session.status = HUMAN_ACTIVE
    session.assigned_admin_id = admin.id
    session.unread_user_count = 0
    session.updated_at = utc_now()
    return append_message(
        db,
        session,
        sender_type="system",
        content=f"人工客服 {admin.username} 已接入本次会话。",
        sender_id=admin.id,
        metadata={"event": "human_takeover", "admin": admin.username},
    )


def close_session(db: Session, session: ChatSession, admin: User) -> None:
    now = utc_now()
    session.status = CLOSED
    session.closed_at = now
    session.updated_at = now
    append_message(
        db,
        session,
        sender_type="system",
        content="本次人工客服会话已结束。如需继续咨询，请新建会话。",
        sender_id=admin.id,
        metadata={"event": "conversation_closed", "admin": admin.username},
    )


def close_session_by_user(db: Session, session: ChatSession) -> ChatMessage:
    now = utc_now()
    session.status = CLOSED
    session.closed_at = now
    session.updated_at = now
    return append_message(
        db,
        session,
        sender_type="system",
        content="本次会话已结束。如需继续咨询，请新建会话，祝您生活愉快",
        metadata={"event": "conversation_closed", "actor": "user"},
    )


def list_admin_sessions(db: Session, status: str | None = None) -> list[ChatSession]:
    query = db.query(ChatSession).join(User, User.id == ChatSession.user_id)
    if status == "processing":
        query = query.filter(ChatSession.status.in_([AI_ACTIVE, HUMAN_ACTIVE]))
    elif status == "completed":
        query = query.filter(ChatSession.status == CLOSED)
    elif status:
        query = query.filter(ChatSession.status == status)
    return query.order_by(ChatSession.updated_at.desc()).all()


def count_admin_sessions_by_status(db: Session) -> dict[str, int]:
    rows = (
        db.query(ChatSession.status, func.count(ChatSession.id))
        .group_by(ChatSession.status)
        .all()
    )
    counts = {status: count for status, count in rows}
    counts["all"] = sum(counts.values())
    counts["completed"] = counts.get(CLOSED, 0)
    return counts


def list_users(db: Session, keyword: str | None = None) -> list[User]:
    query = db.query(User)
    if keyword:
        normalized = keyword.strip()
        like = f"%{normalized}%"
        role_terms = []
        if normalized in ("管理员", "管理", "admin"):
            role_terms.append("admin")
        if normalized in ("普通用户", "用户", "user"):
            role_terms.append("user")
        role_filters = [User.role == role for role in role_terms]
        query = query.filter(or_(User.username.ilike(like), User.role.ilike(like), *role_filters))
    return query.order_by(User.created_at.desc()).all()


def count_user_sessions(db: Session, user_id: int) -> int:
    return db.query(func.count(ChatSession.id)).filter(ChatSession.user_id == user_id).scalar() or 0


def serialize_message(message: ChatMessage) -> dict:
    return {
        "id": message.id,
        "type": message.message_type,
        "sender_type": message.sender_type,
        "sender_id": message.sender_id,
        "content": message.content,
        "timestamp": message.timestamp.isoformat(),
        "rag_trace": message.rag_trace,
        "metadata": message.metadata_json or {},
    }


def serialize_session(session: ChatSession) -> dict:
    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "username": session.user.username if session.user else "",
        "status": session.status,
        "assigned_admin_id": session.assigned_admin_id,
        "assigned_admin": session.assigned_admin.username if session.assigned_admin else None,
        "handoff_reason": session.handoff_reason,
        "handoff_at": session.handoff_at.isoformat() if session.handoff_at else None,
        "closed_at": session.closed_at.isoformat() if session.closed_at else None,
        "last_message_preview": session.last_message_preview,
        "unread_user_count": session.unread_user_count,
        "unread_admin_count": session.unread_admin_count,
        "updated_at": session.updated_at.isoformat(),
        "created_at": session.created_at.isoformat(),
    }
