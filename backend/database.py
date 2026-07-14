import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/langchain_app",
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()


def init_db() -> None:
    # Delayed import to avoid circular dependency.
    import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_customer_service_columns()


def _ensure_customer_service_columns() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("chat_sessions"):
        return

    session_columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    message_columns = {column["name"] for column in inspector.get_columns("chat_messages")}

    session_alters = {
        "status": "ALTER TABLE chat_sessions ADD COLUMN status VARCHAR(40) NOT NULL DEFAULT 'ai_active'",
        "assigned_admin_id": "ALTER TABLE chat_sessions ADD COLUMN assigned_admin_id INTEGER NULL",
        "handoff_reason": "ALTER TABLE chat_sessions ADD COLUMN handoff_reason VARCHAR(255) NOT NULL DEFAULT ''",
        "handoff_at": "ALTER TABLE chat_sessions ADD COLUMN handoff_at TIMESTAMP WITH TIME ZONE NULL",
        "closed_at": "ALTER TABLE chat_sessions ADD COLUMN closed_at TIMESTAMP WITH TIME ZONE NULL",
        "last_message_preview": "ALTER TABLE chat_sessions ADD COLUMN last_message_preview VARCHAR(255) NOT NULL DEFAULT ''",
        "unread_user_count": "ALTER TABLE chat_sessions ADD COLUMN unread_user_count INTEGER NOT NULL DEFAULT 0",
        "unread_admin_count": "ALTER TABLE chat_sessions ADD COLUMN unread_admin_count INTEGER NOT NULL DEFAULT 0",
    }
    message_alters = {
        "sender_type": "ALTER TABLE chat_messages ADD COLUMN sender_type VARCHAR(20) NOT NULL DEFAULT 'user'",
        "sender_id": "ALTER TABLE chat_messages ADD COLUMN sender_id INTEGER NULL",
        "rag_trace": "ALTER TABLE chat_messages ADD COLUMN rag_trace JSON NULL",
        "metadata_json": "ALTER TABLE chat_messages ADD COLUMN metadata_json JSON NOT NULL DEFAULT '{}'",
    }

    with engine.begin() as conn:
        for name, statement in session_alters.items():
            if name not in session_columns:
                conn.execute(text(statement))
        for name, statement in message_alters.items():
            if name not in message_columns:
                conn.execute(text(statement))
