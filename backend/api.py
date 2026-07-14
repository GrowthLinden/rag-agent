import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
import tempfile
import uuid
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from agent import chat_with_agent, chat_with_agent_stream, storage
from auth import (
    authenticate_user,
    create_access_token,
    get_current_user,
    get_db,
    get_password_hash,
    get_user_from_token,
    require_admin,
    require_user,
)
from cache import cache
from database import SessionLocal
from file_security import (
    ALLOWED_IMAGE_EXTENSIONS,
    milvus_string_literal,
    resolve_upload_target,
    sanitize_asset_filename,
    validate_upload_size,
)
from customer_service import (
    CLOSED,
    HANDOFF_REQUESTED,
    HUMAN_ACTIVE,
    append_message,
    close_session,
    close_session_by_user,
    count_admin_sessions_by_status,
    count_user_sessions,
    get_or_create_session,
    list_admin_sessions,
    list_users,
    request_handoff,
    serialize_message,
    serialize_session,
    takeover_session,
    wants_handoff,
)
from document_loader import DocumentLoader
from embedding import EmbeddingService
from milvus_client import MilvusManager
from milvus_writer import MilvusWriter
from models import ChatMessage, ChatSession, ParentChunk, Product, User
from realtime import realtime_manager
from parent_chunk_store import ParentChunkStore
from pdf_image_store import build_image_documents, delete_pdf_images, extract_pdf_images
from audio_service import speech_to_text, text_to_speech
from product_service import apply_product_update, list_products, serialize_product
from schemas import (
    AdminUserCreateRequest,
    AdminUserUpdateRequest,
    AuthResponse,
    ChatRequest,
    ChatResponse,
    CurrentUserResponse,
    DocumentDeleteResponse,
    DocumentInfo,
    DocumentListResponse,
    DocumentUploadResponse,
    LoginRequest,
    MessageInfo,
    ProductDeleteResponse,
    ProductInfo,
    ProductListResponse,
    ProductMutationResponse,
    RegisterRequest,
    SessionDeleteResponse,
    SessionInfo,
    SessionListResponse,
    SessionMessagesResponse,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"
UPLOAD_DIR = DATA_DIR / "documents"
AUDIO_DIR = DATA_DIR / "audio"
PRODUCT_IMAGE_DIR = DATA_DIR / "product_images"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
MAX_AUDIO_UPLOAD_BYTES = int(os.getenv("MAX_AUDIO_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_PRODUCT_IMAGE_BYTES = int(os.getenv("MAX_PRODUCT_IMAGE_BYTES", str(8 * 1024 * 1024)))
MAX_TTS_CHARS = int(os.getenv("MAX_TTS_CHARS", "2000"))
MIN_PASSWORD_LENGTH = int(os.getenv("MIN_PASSWORD_LENGTH", "6"))

loader = DocumentLoader()
parent_chunk_store = ParentChunkStore()
milvus_manager = MilvusManager()
embedding_service = EmbeddingService()
milvus_writer = MilvusWriter(embedding_service=embedding_service, milvus_manager=milvus_manager)

router = APIRouter()


def _sse_event(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _get_user_session_or_404(db: Session, username: str, session_id: str) -> ChatSession:
    session = (
        db.query(ChatSession)
        .join(User, User.id == ChatSession.user_id)
        .filter(User.username == username, ChatSession.session_id == session_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


def _get_current_user_session(db: Session, user: User, session_id: str) -> ChatSession | None:
    return (
        db.query(ChatSession)
        .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
        .first()
    )


async def _notify_admin_session_changed(session: ChatSession, event_type: str = "session_changed"):
    await realtime_manager.send_to_admins(
        {
            "type": event_type,
            "session": serialize_session(session),
        }
    )


async def _notify_customer_message(session: ChatSession, message: ChatMessage):
    await realtime_manager.send_to_customer(
        session.user.username,
        session.session_id,
        {
            "type": "message",
            "session": serialize_session(session),
            "message": serialize_message(message),
        },
    )


async def _notify_admin_message(session: ChatSession, message: ChatMessage, event_type: str = "message"):
    await realtime_manager.send_to_admins(
        {
            "type": event_type,
            "session": serialize_session(session),
            "message": serialize_message(message),
        }
    )


def _latest_ai_message_after(db: Session, session: ChatSession, after_id: int | None) -> ChatMessage | None:
    query = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_ref_id == session.id, ChatMessage.sender_type == "ai")
    )
    if after_id is not None:
        query = query.filter(ChatMessage.id > after_id)
    return query.order_by(ChatMessage.id.desc()).first()


def _normalize_admin_role(role: str | None) -> str:
    normalized = (role or "user").strip().lower()
    if normalized not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="角色只能是 user 或 admin")
    return normalized


def _serialize_admin_user(db: Session, user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "created_at": user.created_at.isoformat(),
        "session_count": count_user_sessions(db, user.id),
    }


def _admin_count(db: Session) -> int:
    return db.query(User).filter(User.role == "admin").count()


def _clear_user_conversation_cache(username: str) -> None:
    cache.delete(f"chat_sessions:{username}")
    cache.delete_pattern(f"chat_messages:{username}:*")


def _validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"密码长度不能少于 {MIN_PASSWORD_LENGTH} 位")


def _remove_file_safely(path: str | Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _parse_product_stock(value: str | int | None) -> int:
    try:
        stock = int(value or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="库存必须是整数")
    if stock < 0:
        raise HTTPException(status_code=400, detail="库存不能小于 0")
    return stock


def _asset_url_to_data_path(url: str) -> Path | None:
    if not url or not url.startswith("/assets/"):
        return None
    relative = url.removeprefix("/assets/").lstrip("/\\")
    target = (DATA_DIR / relative).resolve()
    try:
        target.relative_to(DATA_DIR.resolve())
    except ValueError:
        return None
    return target


async def _save_product_image(image: UploadFile | None) -> str | None:
    if not image or not image.filename:
        return None
    try:
        safe_name = sanitize_asset_filename(
            image.filename,
            ALLOWED_IMAGE_EXTENSIONS,
            default_stem="product",
            error_message="商品图仅支持 jpg、jpeg、png、webp、gif",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    content = await image.read(MAX_PRODUCT_IMAGE_BYTES + 1)
    try:
        validate_upload_size(content, MAX_PRODUCT_IMAGE_BYTES)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc))

    PRODUCT_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    target_name = f"{uuid.uuid4().hex}_{safe_name}"
    target = (PRODUCT_IMAGE_DIR / target_name).resolve()
    try:
        target.relative_to(PRODUCT_IMAGE_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="商品图路径不合法")
    target.write_bytes(content)
    return f"/assets/product_images/{target_name}"


def _delete_product_image_by_url(url: str) -> None:
    path = _asset_url_to_data_path(url)
    if path and path.exists():
        _remove_file_safely(path)


@router.post("/auth/register", response_model=AuthResponse)
async def register(request: RegisterRequest, db: Session = Depends(get_db)):
    username = (request.username or "").strip()
    password = (request.password or "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")
    _validate_password(password)

    exists = db.query(User).filter(User.username == username).first()
    if exists:
        raise HTTPException(status_code=409, detail="用户名已存在")

    role = "user"
    user = User(username=username, password_hash=get_password_hash(password), role=role)
    db.add(user)
    db.commit()

    token = create_access_token(username=username, role=role)
    return AuthResponse(access_token=token, username=username, role=role)


@router.post("/auth/login", response_model=AuthResponse)
async def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = authenticate_user(db, request.username, request.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(username=user.username, role=user.role)
    return AuthResponse(access_token=token, username=user.username, role=user.role)


@router.get("/auth/me", response_model=CurrentUserResponse)
async def me(current_user: User = Depends(get_current_user)):
    return CurrentUserResponse(username=current_user.username, role=current_user.role)


@router.get("/sessions/{session_id}", response_model=SessionMessagesResponse)
async def get_session_messages(session_id: str, current_user: User = Depends(require_user)):
    """获取指定会话的所有消息"""
    try:
        db = SessionLocal()
        try:
            session = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == current_user.id, ChatSession.session_id == session_id)
                .first()
            )
            if not session:
                return SessionMessagesResponse(messages=[], session_status="ai_active")

            rows = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_ref_id == session.id)
                .order_by(ChatMessage.id.asc())
                .all()
            )
            messages = [
                MessageInfo(
                    type=row.message_type,
                    sender_type=row.sender_type,
                    content=row.content,
                    timestamp=row.timestamp.isoformat(),
                    rag_trace=row.rag_trace,
                )
                for row in rows
            ]
            return SessionMessagesResponse(messages=messages, session_status=session.status)
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(current_user: User = Depends(require_user)):
    """获取当前用户的所有会话列表"""
    try:
        sessions = [SessionInfo(**item) for item in storage.list_session_infos(current_user.username)]
        sessions.sort(key=lambda x: x.updated_at, reverse=True)
        return SessionListResponse(sessions=sessions)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/sessions/{session_id}", response_model=SessionDeleteResponse)
async def delete_session(session_id: str, current_user: User = Depends(require_user)):
    """删除当前用户的指定会话"""
    try:
        deleted = storage.delete_session(current_user.username, session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="会话不存在")
        return SessionDeleteResponse(session_id=session_id, message="成功删除会话")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/close")
async def close_current_user_session(session_id: str, current_user: User = Depends(require_user), db: Session = Depends(get_db)):
    session = _get_current_user_session(db, current_user, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.status == CLOSED:
        messages = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_ref_id == session.id, ChatMessage.sender_type == "system")
            .order_by(ChatMessage.id.desc())
            .all()
        )
        close_message = next(
            (
                message
                for message in messages
                if (message.metadata_json or {}).get("event") == "conversation_closed"
            ),
            None,
        )
    else:
        close_message = close_session_by_user(db, session)
        db.commit()

    session_payload = serialize_session(session)
    await _notify_admin_session_changed(session, "session_changed")
    if close_message:
        await _notify_admin_message(session, close_message)
        await _notify_customer_message(session, close_message)
    return {
        "conversation": session_payload,
        "message": serialize_message(close_message) if close_message else None,
    }


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest, current_user: User = Depends(require_user), db: Session = Depends(get_db)):
    try:
        session_id = request.session_id or "default_session"
        session = _get_current_user_session(db, current_user, session_id)
        if session and session.status == CLOSED:
            raise HTTPException(status_code=400, detail="本次会话已结束，请新建会话后继续咨询。")
        if session and session.status in (HANDOFF_REQUESTED, HUMAN_ACTIVE):
            message = append_message(
                db,
                session,
                sender_type="user",
                sender_id=current_user.id,
                content=request.message,
            )
            db.commit()
            await realtime_manager.send_to_admins(
                {
                    "type": "message",
                    "session": serialize_session(session),
                    "message": serialize_message(message),
                }
            )
            return ChatResponse(response="")
        if wants_handoff(request.message):
            session = session or get_or_create_session(db, current_user, session_id)
            message = append_message(
                db,
                session,
                sender_type="user",
                sender_id=current_user.id,
                content=request.message,
            )
            request_handoff(db, session, request.message)
            db.commit()
            await realtime_manager.send_to_admins(
                {
                    "type": "handoff_requested",
                    "session": serialize_session(session),
                    "message": serialize_message(message),
                }
            )
            return ChatResponse(response="正在为您呼叫人工服务....")
        session = session or get_or_create_session(db, current_user, session_id)
        user_message = append_message(
            db,
            session,
            sender_type="user",
            sender_id=current_user.id,
            content=request.message,
        )
        db.commit()
        await _notify_admin_message(session, user_message)

        resp = chat_with_agent(
            request.message,
            current_user.username,
            session_id,
            user_message_already_saved=True,
        )
        db.expire_all()
        refreshed_session = _get_current_user_session(db, current_user, session_id)
        if refreshed_session:
            ai_message = _latest_ai_message_after(db, refreshed_session, user_message.id)
            if ai_message:
                await _notify_admin_message(refreshed_session, ai_message)
        if isinstance(resp, dict):
            return ChatResponse(**resp)
        return ChatResponse(response=resp)
    except HTTPException:
        raise
    except Exception as e:
        message = str(e)
        match = re.search(r"Error code:\s*(\d{3})", message)
        if match:
            code = int(match.group(1))
            if code == 429:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        "上游模型服务触发限流/额度限制（429）。请检查账号额度/模型状态。\n"
                        f"原始错误：{message}"
                    ),
                )
            if code in (401, 403):
                raise HTTPException(status_code=code, detail=message)
            raise HTTPException(status_code=code, detail=message)
        raise HTTPException(status_code=500, detail=message)


@router.post("/chat/stream")
async def chat_stream_endpoint(request: ChatRequest, current_user: User = Depends(require_user)):
    """跟 Agent 对话 (流式)"""

    async def event_generator():
        db = SessionLocal()
        try:
            session_id = request.session_id or "default_session"
            session = _get_current_user_session(db, current_user, session_id)

            if session and session.status == CLOSED:
                yield _sse_event({"type": "error", "content": "本次会话已结束，请新建会话后继续咨询。"})
                yield "data: [DONE]\n\n"
                return

            if session and session.status in (HANDOFF_REQUESTED, HUMAN_ACTIVE):
                message = append_message(
                    db,
                    session,
                    sender_type="user",
                    sender_id=current_user.id,
                    content=request.message,
                )
                db.commit()
                await _notify_admin_session_changed(session, "customer_message")
                await realtime_manager.send_to_admins(
                    {
                        "type": "message",
                        "session": serialize_session(session),
                        "message": serialize_message(message),
                    }
                )
                yield _sse_event(
                    {
                        "type": "handoff_status",
                        "status": session.status,
                    }
                )
                yield "data: [DONE]\n\n"
                return

            if wants_handoff(request.message):
                session = session or get_or_create_session(db, current_user, session_id)
                user_message = append_message(
                    db,
                    session,
                    sender_type="user",
                    sender_id=current_user.id,
                    content=request.message,
                )
                request_handoff(db, session, request.message)
                db.commit()
                await realtime_manager.send_to_admins(
                    {
                        "type": "handoff_requested",
                        "session": serialize_session(session),
                        "message": serialize_message(user_message),
                    }
                )
                yield _sse_event(
                    {
                        "type": "handoff_status",
                        "status": session.status,
                        "content": "正在为您呼叫人工服务....",
                    }
                )
                yield "data: [DONE]\n\n"
                return

            session = session or get_or_create_session(db, current_user, session_id)
            user_message = append_message(
                db,
                session,
                sender_type="user",
                sender_id=current_user.id,
                content=request.message,
            )
            db.commit()
            await _notify_admin_message(session, user_message)

            async def observe_agent_event(event: dict):
                payload = {
                    "type": "ai_stream",
                    "session": serialize_session(session),
                    "event": event,
                }
                await realtime_manager.send_to_admins(payload)
                await realtime_manager.send_to_customer(current_user.username, session_id, payload)

            async for chunk in chat_with_agent_stream(
                request.message,
                current_user.username,
                session_id,
                user_message_already_saved=True,
                observer=observe_agent_event,
            ):
                yield chunk
            db.expire_all()
            refreshed_session = _get_current_user_session(db, current_user, session_id)
            if refreshed_session:
                ai_message = _latest_ai_message_after(db, refreshed_session, user_message.id)
                if ai_message:
                    await _notify_admin_message(refreshed_session, ai_message)
        except Exception as e:
            error_data = {"type": "error", "content": str(e)}
            yield _sse_event(error_data)
        finally:
            db.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/admin/conversations")
async def admin_list_conversations(status: str | None = None, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    sessions = list_admin_sessions(db, status)
    return {
        "conversations": [serialize_session(session) for session in sessions],
        "counts": count_admin_sessions_by_status(db),
    }


@router.get("/admin/conversations/{username}/{session_id}")
async def admin_get_conversation(username: str, session_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    session = _get_user_session_or_404(db, username, session_id)
    session.unread_user_count = 0
    db.commit()
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_ref_id == session.id)
        .order_by(ChatMessage.id.asc())
        .all()
    )
    return {
        "conversation": serialize_session(session),
        "messages": [serialize_message(message) for message in messages],
    }


@router.post("/admin/conversations/{username}/{session_id}/takeover")
async def admin_takeover_conversation(username: str, session_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    session = _get_user_session_or_404(db, username, session_id)
    if session.status == CLOSED:
        raise HTTPException(status_code=400, detail="会话已关闭，不能接管")
    system_message = takeover_session(db, session, admin)
    db.commit()
    session_payload = serialize_session(session)
    await _notify_admin_session_changed(session, "session_changed")
    await realtime_manager.send_to_customer(
        username,
        session_id,
        {"type": "session_changed", "session": session_payload},
    )
    if system_message:
        await _notify_customer_message(session, system_message)
    return {"conversation": session_payload}


@router.post("/admin/conversations/{username}/{session_id}/messages")
async def admin_send_message(username: str, session_id: str, request: ChatRequest, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    session = _get_user_session_or_404(db, username, session_id)
    if session.status == CLOSED:
        raise HTTPException(status_code=400, detail="会话已关闭，不能回复")
    takeover_message = None
    if session.status == HANDOFF_REQUESTED:
        takeover_message = takeover_session(db, session, admin)
    elif session.status != HUMAN_ACTIVE:
        raise HTTPException(status_code=400, detail="只有转人工后的会话才能由管理员回复")
    elif session.assigned_admin_id and session.assigned_admin_id != admin.id:
        raise HTTPException(status_code=403, detail="该会话已由其他管理员接入")

    message = append_message(
        db,
        session,
        sender_type="admin",
        sender_id=admin.id,
        content=request.message,
    )
    session.unread_user_count = 0
    db.commit()
    if takeover_message:
        await _notify_customer_message(session, takeover_message)
    await _notify_customer_message(session, message)
    await _notify_admin_session_changed(session, "session_changed")
    return {
        "conversation": serialize_session(session),
        "message": serialize_message(message),
    }


@router.post("/admin/conversations/{username}/{session_id}/close")
async def admin_close_conversation(username: str, session_id: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    session = _get_user_session_or_404(db, username, session_id)
    if session.status == CLOSED:
        return {"conversation": serialize_session(session)}
    close_session(db, session, admin)
    db.commit()
    system_message = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_ref_id == session.id)
        .order_by(ChatMessage.id.desc())
        .first()
    )
    session_payload = serialize_session(session)
    await _notify_admin_session_changed(session, "session_changed")
    await realtime_manager.send_to_customer(
        username,
        session_id,
        {"type": "session_changed", "session": session_payload},
    )
    if system_message:
        await _notify_customer_message(session, system_message)
    return {"conversation": session_payload}


@router.delete("/admin/conversations/{username}/{session_id}")
async def admin_delete_conversation(username: str, session_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    session = _get_user_session_or_404(db, username, session_id)
    session_payload = serialize_session(session)
    db.delete(session)
    db.commit()
    cache.delete(f"chat_messages:{username}:{session_id}")
    cache.delete(f"chat_sessions:{username}")
    await realtime_manager.send_to_admins(
        {
            "type": "session_deleted",
            "session": session_payload,
        }
    )
    await realtime_manager.send_to_customer(
        username,
        session_id,
        {
            "type": "session_deleted",
            "session": session_payload,
            "content": "本次历史会话已由管理员删除。",
        },
    )
    return {"conversation": session_payload, "message": "会话已删除"}


@router.get("/admin/users")
async def admin_list_users(keyword: str | None = None, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = list_users(db, keyword)
    return {"users": [_serialize_admin_user(db, user) for user in users]}


@router.post("/admin/users")
async def admin_create_user(request: AdminUserCreateRequest, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    username = (request.username or "").strip()
    password = (request.password or "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")
    _validate_password(password)
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail="用户名已存在")

    user = User(
        username=username,
        password_hash=get_password_hash(password),
        role=_normalize_admin_role(request.role),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"user": _serialize_admin_user(db, user), "message": "用户已创建"}


@router.put("/admin/users/{user_id}")
async def admin_update_user(user_id: int, request: AdminUserUpdateRequest, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    new_username = request.username.strip() if request.username is not None else None
    new_role = _normalize_admin_role(request.role) if request.role is not None else None
    new_password = request.password.strip() if request.password is not None else None

    if new_username is not None:
        if not new_username:
            raise HTTPException(status_code=400, detail="用户名不能为空")
        exists = db.query(User).filter(User.username == new_username, User.id != user.id).first()
        if exists:
            raise HTTPException(status_code=409, detail="用户名已存在")

    if admin.id == user.id and ((new_username is not None and new_username != user.username) or (new_role is not None and new_role != user.role)):
        raise HTTPException(status_code=400, detail="不能修改当前登录管理员的用户名或角色")

    if user.role == "admin" and new_role == "user" and _admin_count(db) <= 1:
        raise HTTPException(status_code=400, detail="至少需要保留一个管理员账号")

    old_username = user.username
    if new_username is not None:
        user.username = new_username
    if new_role is not None:
        user.role = new_role
    if new_password:
        _validate_password(new_password)
        user.password_hash = get_password_hash(new_password)

    db.commit()
    db.refresh(user)
    if old_username != user.username:
        _clear_user_conversation_cache(old_username)
    _clear_user_conversation_cache(user.username)
    return {"user": _serialize_admin_user(db, user), "message": "用户已更新"}


@router.delete("/admin/users/{user_id}")
async def admin_delete_user(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="不能删除当前登录管理员账号")
    if user.role == "admin" and _admin_count(db) <= 1:
        raise HTTPException(status_code=400, detail="至少需要保留一个管理员账号")

    username = user.username
    db.delete(user)
    db.commit()
    _clear_user_conversation_cache(username)
    await realtime_manager.send_to_admins(
        {
            "type": "user_deleted",
            "user_id": user_id,
            "username": username,
        }
    )
    return {"user_id": user_id, "message": "用户已删除"}


@router.get("/admin/products", response_model=ProductListResponse)
async def admin_list_products(keyword: str | None = None, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    products = list_products(db, keyword)
    return ProductListResponse(products=[ProductInfo(**serialize_product(product)) for product in products])


@router.post("/admin/products", response_model=ProductMutationResponse)
async def admin_create_product(
    name: str = Form(...),
    category: str = Form(""),
    price: str = Form(""),
    stock: str = Form("0"),
    material: str = Form(""),
    specifications: str = Form(""),
    image: UploadFile | None = File(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product_name = (name or "").strip()
    if not product_name:
        raise HTTPException(status_code=400, detail="商品名不能为空")

    image_url = await _save_product_image(image)
    product = Product(
        name=product_name,
        category=(category or "").strip(),
        price=(price or "").strip(),
        stock=_parse_product_stock(stock),
        material=(material or "").strip(),
        specifications=(specifications or "").strip(),
        image_url=image_url or "",
        created_by_id=admin.id,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return ProductMutationResponse(product=ProductInfo(**serialize_product(product)), message="商品已创建")


@router.put("/admin/products/{product_id}", response_model=ProductMutationResponse)
async def admin_update_product(
    product_id: int,
    name: str = Form(...),
    category: str = Form(""),
    price: str = Form(""),
    stock: str = Form("0"),
    material: str = Form(""),
    specifications: str = Form(""),
    image: UploadFile | None = File(None),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")

    product_name = (name or "").strip()
    if not product_name:
        raise HTTPException(status_code=400, detail="商品名不能为空")

    old_image_url = product.image_url
    new_image_url = await _save_product_image(image)
    apply_product_update(
        product,
        name=product_name,
        category=(category or "").strip(),
        price=(price or "").strip(),
        stock=_parse_product_stock(stock),
        material=(material or "").strip(),
        specifications=(specifications or "").strip(),
        image_url=new_image_url,
    )
    db.commit()
    db.refresh(product)
    if new_image_url and old_image_url and old_image_url != new_image_url:
        _delete_product_image_by_url(old_image_url)
    return ProductMutationResponse(product=ProductInfo(**serialize_product(product)), message="商品已更新")


@router.delete("/admin/products/{product_id}", response_model=ProductDeleteResponse)
async def admin_delete_product(product_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")

    image_url = product.image_url
    db.delete(product)
    db.commit()
    _delete_product_image_by_url(image_url)
    return ProductDeleteResponse(product_id=product_id, message="商品已删除")


@router.get("/admin/rag/pipeline")
async def admin_rag_pipeline(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    from rag_utils import (
        AUTO_MERGE_ENABLED,
        AUTO_MERGE_THRESHOLD,
        CANDIDATE_MULTIPLIER,
        KEYWORD_FALLBACK_SCAN_LIMIT,
        LEAF_RETRIEVE_LEVEL,
        RERANK_BINDING_HOST,
        RERANK_MODEL,
    )
    from rag_pipeline import RAG_FAST_MODE, RAG_GRADING_ENABLED, RAG_REWRITE_ENABLED

    parent_counts = (
        db.query(ParentChunk.filename, ParentChunk.chunk_level, ParentChunk.file_type, ParentChunk.chunk_id)
        .order_by(ParentChunk.filename.asc(), ParentChunk.chunk_level.asc())
        .all()
    )
    parent_stats: dict[str, dict] = {}
    for filename, chunk_level, file_type, _chunk_id in parent_counts:
        item = parent_stats.setdefault(
            filename,
            {"filename": filename, "file_type": file_type, "level_1": 0, "level_2": 0, "level_3": 0},
        )
        item[f"level_{chunk_level}"] = item.get(f"level_{chunk_level}", 0) + 1

    leaf_stats = {}
    try:
        milvus_manager.init_collection()
        rows = milvus_manager.query(
            output_fields=["filename", "file_type", "chunk_level"],
            limit=10000,
        )
        for row in rows:
            filename = row.get("filename", "")
            if not filename:
                continue
            item = leaf_stats.setdefault(
                filename,
                {"filename": filename, "file_type": row.get("file_type", ""), "level_1": 0, "level_2": 0, "level_3": 0},
            )
            level = int(row.get("chunk_level", 3) or 3)
            item[f"level_{level}"] = item.get(f"level_{level}", 0) + 1
    except Exception as exc:
        leaf_stats = {"__error__": {"error": str(exc)}}

    merged_stats = {}
    for filename, item in parent_stats.items():
        merged_stats[filename] = dict(item)
    for filename, item in leaf_stats.items():
        if filename == "__error__":
            continue
        target = merged_stats.setdefault(filename, dict(item))
        target["file_type"] = target.get("file_type") or item.get("file_type", "")
        target["level_3"] = item.get("level_3", 0)

    return {
        "settings": {
            "collection": os.getenv("MILVUS_COLLECTION", "embeddings_collection"),
            "embedder": os.getenv("EMBEDDER", ""),
            "dense_index": "HNSW / IP",
            "sparse_index": "SPARSE_INVERTED_INDEX / IP",
            "sparse_method": "BM25 sparse vector",
            "fusion": "RRF(k=60)",
            "rerank_enabled": bool(RERANK_MODEL and RERANK_BINDING_HOST and os.getenv("RERANK_API_KEY")),
            "rerank_model": RERANK_MODEL,
            "rerank_endpoint": RERANK_BINDING_HOST,
            "leaf_retrieve_level": LEAF_RETRIEVE_LEVEL,
            "candidate_rule": f"candidate_k = top_k * {CANDIDATE_MULTIPLIER}",
            "rag_fast_mode": RAG_FAST_MODE,
            "rag_grading_enabled": RAG_GRADING_ENABLED,
            "rag_rewrite_enabled": RAG_REWRITE_ENABLED,
            "auto_merge_enabled": AUTO_MERGE_ENABLED,
            "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
            "keyword_fallback_scan_limit": KEYWORD_FALLBACK_SCAN_LIMIT,
        },
        "indexing_flow": [
            {"name": "文档解析", "detail": "PDF / Word / Excel 解析为页级文本"},
            {"name": "三级滑动窗口分块", "detail": "L1≈1200、L2≈600、L3≈300；检索默认命中 L3"},
            {"name": "父块入库", "detail": "L1/L2 写入 PostgreSQL，用于 Auto-merging 回收上下文"},
            {"name": "密集向量", "detail": "调用 EMBEDDER 生成 dense_embedding，写入 Milvus HNSW 索引"},
            {"name": "BM25 稀疏向量", "detail": "对文本分词并生成 sparse_embedding，写入 Milvus 稀疏倒排索引"},
            {"name": "图片语义块", "detail": "PDF 图片抽取后生成视觉描述并作为可检索块写入"},
        ],
        "retrieval_flow": [
            {"name": "查询向量化", "detail": "同一问题生成 dense query 与 BM25 sparse query"},
            {"name": "Milvus 混合检索", "detail": "dense_embedding + sparse_embedding 两路召回"},
            {"name": "RRF 融合", "detail": "用 Reciprocal Rank Fusion 合并两路排序"},
            {"name": "Rerank 精排", "detail": "若配置 RERANK_*，对候选片段做相关性精排"},
            {"name": "Auto-merging", "detail": "同父块命中达到阈值时回收 L2/L1 父块"},
            {"name": "上下文组装", "detail": "最终片段进入 Agent 作为知识库上下文"},
        ],
        "document_stats": list(merged_stats.values()),
        "warnings": [leaf_stats["__error__"]["error"]] if "__error__" in leaf_stats else [],
    }


@router.post("/admin/rag/diagnose")
async def admin_rag_diagnose(request: ChatRequest, _: User = Depends(require_admin)):
    query = (request.message or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="检索问题不能为空")

    from rag_utils import retrieve_documents

    result = retrieve_documents(query, top_k=5)
    chunks = []
    for idx, doc in enumerate(result.get("docs", []), 1):
        chunks.append(
            {
                "rank": idx,
                "filename": doc.get("filename", ""),
                "page_number": doc.get("page_number", 0),
                "chunk_level": doc.get("chunk_level", 0),
                "chunk_id": doc.get("chunk_id", ""),
                "score": doc.get("score"),
                "keyword_score": doc.get("keyword_score"),
                "rerank_score": doc.get("rerank_score"),
                "rrf_rank": doc.get("rrf_rank"),
                "merged_from_children": bool(doc.get("merged_from_children")),
                "merged_child_count": doc.get("merged_child_count", 0),
                "text": doc.get("text", ""),
            }
        )
    return {
        "query": query,
        "meta": result.get("meta", {}),
        "chunks": chunks,
    }


@router.websocket("/ws/customer/{session_id}")
async def customer_websocket(websocket: WebSocket, session_id: str):
    token = websocket.query_params.get("token", "")
    db = SessionLocal()
    user = get_user_from_token(token, db)
    if not user or user.role != "user":
        await websocket.close(code=1008)
        db.close()
        return
    await realtime_manager.connect_customer(user.username, session_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        realtime_manager.disconnect_customer(user.username, session_id, websocket)
    finally:
        db.close()


@router.websocket("/ws/admin")
async def admin_websocket(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    db = SessionLocal()
    user = get_user_from_token(token, db)
    if not user or user.role != "admin":
        await websocket.close(code=1008)
        db.close()
        return
    await realtime_manager.connect_admin(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        realtime_manager.disconnect_admin(websocket)
    finally:
        db.close()


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(_: User = Depends(require_admin)):
    """获取已上传的文档列表（管理员）"""
    try:
        milvus_manager.init_collection()

        results = milvus_manager.query(
            output_fields=["filename", "file_type"],
            limit=10000,
        )

        file_stats = {}
        for item in results:
            filename = item.get("filename", "")
            file_type = item.get("file_type", "")
            if filename not in file_stats:
                file_stats[filename] = {
                    "filename": filename,
                    "file_type": file_type,
                    "chunk_count": 0,
                }
            file_stats[filename]["chunk_count"] += 1

        documents = [DocumentInfo(**stats) for stats in file_stats.values()]
        return DocumentListResponse(documents=documents)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取文档列表失败: {str(e)}")


@router.post("/documents/upload", response_model=DocumentUploadResponse)
async def upload_document(file: UploadFile = File(...), _: User = Depends(require_admin)):
    """上传文档并进行 embedding（管理员）"""
    try:
        original_filename = file.filename or ""
        try:
            filename, file_path = resolve_upload_target(UPLOAD_DIR, original_filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        file_lower = filename.lower()

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        milvus_manager.init_collection()

        delete_expr = f"filename == {milvus_string_literal(filename)}"
        try:
            milvus_manager.delete(delete_expr)
        except Exception:
            pass
        try:
            parent_chunk_store.delete_by_filename(filename)
        except Exception:
            pass

        with open(file_path, "wb") as f:
            content = await file.read(MAX_UPLOAD_BYTES + 1)
            try:
                validate_upload_size(content, MAX_UPLOAD_BYTES)
            except ValueError as exc:
                raise HTTPException(status_code=413, detail=str(exc))
            f.write(content)

        try:
            new_docs = loader.load_document(str(file_path), filename)
        except Exception as doc_err:
            raise HTTPException(status_code=500, detail=f"文档处理失败: {doc_err}")

        extracted_images = []
        if file_lower.endswith(".pdf"):
            try:
                extracted_images = extract_pdf_images(str(file_path), filename)
            except Exception:
                extracted_images = []

        if not new_docs:
            raise HTTPException(status_code=500, detail="文档处理失败，未能提取内容")

        parent_docs = [doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) in (1, 2)]
        leaf_docs = [doc for doc in new_docs if int(doc.get("chunk_level", 0) or 0) == 3]
        if not leaf_docs:
            raise HTTPException(status_code=500, detail="文档处理失败，未生成可检索叶子分块")

        parent_chunk_store.upsert_documents(parent_docs)
        milvus_writer.write_documents(leaf_docs)
        image_docs = build_image_documents(extracted_images)
        if image_docs:
            milvus_writer.write_documents(image_docs)

        return DocumentUploadResponse(
            filename=filename,
            chunks_processed=len(leaf_docs) + len(image_docs),
            message=(
                f"成功上传并处理 {filename}，叶子分块 {len(leaf_docs)} 个，"
                f"父级分块 {len(parent_docs)} 个（存入 PostgreSQL），"
                f"PDF 图片 {len(extracted_images)} 张，图片语义分块 {len(image_docs)} 个"
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文档上传失败: {str(e)}")


@router.delete("/documents/{filename}", response_model=DocumentDeleteResponse)
async def delete_document(filename: str, _: User = Depends(require_admin)):
    """删除文档在 Milvus 中的向量（保留本地文件，管理员）"""
    try:
        milvus_manager.init_collection()

        delete_expr = f"filename == {milvus_string_literal(filename)}"
        result = milvus_manager.delete(delete_expr)
        parent_chunk_store.delete_by_filename(filename)
        delete_pdf_images(filename)

        return DocumentDeleteResponse(
            filename=filename,
            chunks_deleted=result.get("delete_count", 0) if isinstance(result, dict) else 0,
            message=f"成功删除文档 {filename} 的向量数据（本地文件已保留）",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除文档失败: {str(e)}")

@router.post("/audio/speech-to-text")
async def audio_speech_to_text(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    temp_path = ""
    try:
        content = await file.read(MAX_AUDIO_UPLOAD_BYTES + 1)
        try:
            validate_upload_size(content, MAX_AUDIO_UPLOAD_BYTES)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp_audio:
            temp_audio.write(content)
            temp_path = temp_audio.name

        text = speech_to_text(temp_path)

        return {"text": text}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if temp_path:
            _remove_file_safely(temp_path)

@router.post("/audio/text-to-speech")
async def audio_text_to_speech(request: ChatRequest, current_user: User = Depends(get_current_user)):
    try:
        text = (request.message or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Message is empty")
        if len(text) > MAX_TTS_CHARS:
            raise HTTPException(status_code=413, detail=f"TTS 文本过长，最大允许 {MAX_TTS_CHARS} 字")

        output_filename = f"{uuid.uuid4().hex}.wav"
        output_path = AUDIO_DIR / output_filename
        os.makedirs(AUDIO_DIR, exist_ok=True)

        success = text_to_speech(text, str(output_path))
        if not success:
            raise HTTPException(status_code=500, detail="TTS generation failed")

        def iterfile():
            with open(output_path, mode="rb") as file_like:
                yield from file_like

        return StreamingResponse(
            iterfile(),
            media_type="audio/wav",
            background=BackgroundTask(_remove_file_safely, output_path),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
