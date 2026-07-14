import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("JWT_SECRET_KEY", "test-integration-secret")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_SOCKET_TIMEOUT_SECONDS", "0.01")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import api as api_module  # noqa: E402
import app as app_module  # noqa: E402
import database  # noqa: E402
from auth import get_password_hash  # noqa: E402
from customer_service import append_message  # noqa: E402
from database import Base, SessionLocal  # noqa: E402
from models import ChatMessage, ChatSession, Product, User  # noqa: E402


class _NoopCache:
    def get_json(self, *_args, **_kwargs):
        return None

    def set_json(self, *_args, **_kwargs):
        return None

    def delete(self, *_args, **_kwargs):
        return None

    def delete_pattern(self, *_args, **_kwargs):
        return None


class _FakeMilvusManager:
    def __init__(self):
        self.deleted = []

    def init_collection(self):
        return None

    def delete(self, expr):
        self.deleted.append(expr)
        return {"delete_count": 1}

    def query(self, **_kwargs):
        return []


class _FakeLoader:
    def load_document(self, _path, filename):
        return [
            {
                "chunk_id": f"{filename}:parent",
                "text": "parent chunk",
                "filename": filename,
                "file_type": "pdf",
                "chunk_level": 1,
            },
            {
                "chunk_id": f"{filename}:leaf",
                "text": "leaf chunk",
                "filename": filename,
                "file_type": "pdf",
                "chunk_level": 3,
            },
        ]


class _FakeParentChunkStore:
    def __init__(self):
        self.deleted = []
        self.upserted = []

    def delete_by_filename(self, filename):
        self.deleted.append(filename)

    def upsert_documents(self, docs):
        self.upserted.extend(docs)


class _FakeMilvusWriter:
    def __init__(self):
        self.written_batches = []

    def write_documents(self, docs):
        self.written_batches.append(list(docs))


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        database.engine = cls.engine
        SessionLocal.configure(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)
        cls.client = TestClient(app_module.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="superservice-api-test-"))
        self._patch_globals()
        self._clear_database()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _patch_globals(self):
        noop_cache = _NoopCache()
        api_module.cache = noop_cache
        api_module.loader = _FakeLoader()
        api_module.milvus_manager = _FakeMilvusManager()
        api_module.parent_chunk_store = _FakeParentChunkStore()
        api_module.milvus_writer = _FakeMilvusWriter()
        api_module.UPLOAD_DIR = self.temp_dir / "documents"
        api_module.PRODUCT_IMAGE_DIR = self.temp_dir / "product_images"
        api_module.extract_pdf_images = lambda _path, filename: [
            {"filename": filename, "page_number": 0, "image_index": 1}
        ]
        api_module.build_image_documents = lambda images: [
            {
                "chunk_id": f"{item['filename']}:image:{item['image_index']}",
                "text": "image description",
                "filename": item["filename"],
                "file_type": "pdf_image",
                "chunk_level": 3,
            }
            for item in images
        ]

    def _clear_database(self):
        db = SessionLocal()
        try:
            db.query(ChatMessage).delete()
            db.query(ChatSession).delete()
            db.query(Product).delete()
            db.query(User).delete()
            db.commit()
        finally:
            db.close()

    def _create_user(self, username="customer", password="secret123", role="user"):
        db = SessionLocal()
        try:
            user = User(username=username, password_hash=get_password_hash(password), role=role)
            db.add(user)
            db.commit()
            db.refresh(user)
            return user.id
        finally:
            db.close()

    def _login(self, username, password="secret123"):
        response = self.client.post("/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    @staticmethod
    def _auth(token):
        return {"Authorization": f"Bearer {token}"}

    def test_login_flow_register_login_and_me(self):
        response = self.client.post(
            "/auth/register",
            json={"username": "alice", "password": "secret123"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["username"], "alice")
        self.assertEqual(payload["role"], "user")
        self.assertTrue(payload["access_token"])

        bad_login = self.client.post(
            "/auth/login",
            json={"username": "alice", "password": "wrong-password"},
        )
        self.assertEqual(bad_login.status_code, 401)

        token = self._login("alice")
        me = self.client.get("/auth/me", headers=self._auth(token))
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json(), {"username": "alice", "role": "user"})

    def test_admin_upload_document_indexes_leaf_and_image_chunks(self):
        self._create_user("admin", role="admin")
        token = self._login("admin")

        response = self.client.post(
            "/documents/upload",
            files={"file": ("FAQ.pdf", b"%PDF-1.4 fake content", "application/pdf")},
            headers=self._auth(token),
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["filename"], "FAQ.pdf")
        self.assertEqual(payload["chunks_processed"], 2)
        self.assertTrue((api_module.UPLOAD_DIR / "FAQ.pdf").exists())
        self.assertEqual(len(api_module.parent_chunk_store.upserted), 1)
        self.assertEqual(len(api_module.milvus_writer.written_batches), 2)

    def test_chat_stream_persists_user_and_ai_messages(self):
        self._create_user("customer", role="user")
        token = self._login("customer")

        async def fake_chat_stream(message, username, session_id, user_message_already_saved=False, observer=None):
            self.assertTrue(user_message_already_saved)
            self.assertEqual(message, "hello")
            self.assertEqual(username, "customer")
            if observer:
                await observer({"type": "status", "content": "thinking"})
            db = SessionLocal()
            try:
                session = (
                    db.query(ChatSession)
                    .join(User, User.id == ChatSession.user_id)
                    .filter(User.username == username, ChatSession.session_id == session_id)
                    .first()
                )
                append_message(db, session, sender_type="ai", content="Hello from test")
                db.commit()
            finally:
                db.close()
            yield 'data: {"type":"content","content":"Hello from test"}\n\n'
            yield "data: [DONE]\n\n"

        api_module.chat_with_agent_stream = fake_chat_stream

        with self.client.stream(
            "POST",
            "/chat/stream",
            json={"message": "hello", "session_id": "stream-session"},
            headers=self._auth(token),
        ) as response:
            body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200, body)
        self.assertIn("Hello from test", body)
        self.assertIn("[DONE]", body)

        db = SessionLocal()
        try:
            session = db.query(ChatSession).filter(ChatSession.session_id == "stream-session").one()
            messages = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_ref_id == session.id)
                .order_by(ChatMessage.id.asc())
                .all()
            )
            self.assertEqual([message.sender_type for message in messages], ["user", "ai"])
            self.assertEqual(messages[-1].content, "Hello from test")
        finally:
            db.close()

    def test_handoff_flow_allows_admin_takeover_and_reply(self):
        self._create_user("customer", role="user")
        self._create_user("admin", role="admin")
        customer_token = self._login("customer")
        admin_token = self._login("admin")

        handoff = self.client.post(
            "/chat/stream",
            json={"message": "我要人工客服", "session_id": "handoff-session"},
            headers=self._auth(customer_token),
        )
        self.assertEqual(handoff.status_code, 200, handoff.text)
        self.assertIn("handoff_requested", handoff.text)

        conversations = self.client.get("/admin/conversations", headers=self._auth(admin_token))
        self.assertEqual(conversations.status_code, 200, conversations.text)
        self.assertEqual(conversations.json()["conversations"][0]["status"], "handoff_requested")

        takeover = self.client.post(
            "/admin/conversations/customer/handoff-session/takeover",
            headers=self._auth(admin_token),
        )
        self.assertEqual(takeover.status_code, 200, takeover.text)
        self.assertEqual(takeover.json()["conversation"]["status"], "human_active")

        reply = self.client.post(
            "/admin/conversations/customer/handoff-session/messages",
            json={"message": "人工客服已接入", "session_id": "handoff-session"},
            headers=self._auth(admin_token),
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["message"]["sender_type"], "admin")

    def test_admin_product_image_upload_creates_product_and_file(self):
        self._create_user("admin", role="admin")
        token = self._login("admin")

        response = self.client.post(
            "/admin/products",
            data={
                "name": "SuperService Mug",
                "category": "merch",
                "price": "19.90",
                "stock": "12",
                "material": "ceramic",
                "specifications": "350ml",
            },
            files={"image": ("mug.png", b"fake image bytes", "image/png")},
            headers=self._auth(token),
        )

        self.assertEqual(response.status_code, 200, response.text)
        product = response.json()["product"]
        self.assertEqual(product["name"], "SuperService Mug")
        self.assertEqual(product["stock"], 12)
        self.assertTrue(product["image_url"].startswith("/assets/product_images/"))

        stored_name = product["image_url"].split("/")[-1]
        self.assertTrue((api_module.PRODUCT_IMAGE_DIR / stored_name).exists())

        listing = self.client.get("/admin/products", headers=self._auth(token))
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["products"][0]["name"], "SuperService Mug")

    def test_admin_product_image_upload_rejects_unsupported_extension(self):
        self._create_user("admin", role="admin")
        token = self._login("admin")

        response = self.client.post(
            "/admin/products",
            data={"name": "Unsafe", "stock": "1"},
            files={"image": ("payload.exe", b"not an image", "application/octet-stream")},
            headers=self._auth(token),
        )

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
