from dotenv import load_dotenv
import os
import json
import asyncio
from uuid import uuid4
from collections.abc import Awaitable, Callable
from langchain.chat_models import init_chat_model
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, AIMessage, AIMessageChunk, SystemMessage
from tools import (
    get_current_weather,
    search_product_catalog,
    search_knowledge_base,
    get_last_rag_context,
    reset_tool_call_guards,
    set_rag_step_queue,
    query_logistics,
    extract_logistics_request,
)
from cache import cache
from database import SessionLocal
from models import User, ChatSession, ChatMessage
from product_service import search_product_answer
from time_utils import utc_now

load_dotenv()

API_KEY = os.getenv("ARK_API_KEY")
MODEL = os.getenv("MODEL")
BASE_URL = os.getenv("BASE_URL")

class ConversationStorage:
    """对话存储（PostgreSQL + Redis）。"""

    @staticmethod
    def _messages_cache_key(user_id: str, session_id: str) -> str:
        return f"chat_messages:{user_id}:{session_id}"

    @staticmethod
    def _sessions_cache_key(user_id: str) -> str:
        return f"chat_sessions:{user_id}"

    @staticmethod
    def _to_langchain_messages(records: list[dict]) -> list:
        messages = []
        for msg_data in records:
            msg_type = msg_data.get("type")
            sender_type = msg_data.get("sender_type")
            content = msg_data.get("content", "")
            if msg_type == "human" and sender_type != "admin":
                messages.append(HumanMessage(content=content))
            elif msg_type == "ai":
                messages.append(AIMessage(content=content))
            elif msg_type == "system":
                messages.append(SystemMessage(content=content))
        return messages

    def save(self, user_id: str, session_id: str, messages: list, metadata: dict = None, extra_message_data: list = None):
        """保存对话"""
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return

            session = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
                .first()
            )
            if not session:
                session = ChatSession(user_id=user.id, session_id=session_id, metadata_json=metadata or {})
                db.add(session)
                db.flush()
            else:
                session.metadata_json = metadata or {}

            serialized = []
            now = utc_now()
            existing_rows = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_ref_id == session.id)
                .order_by(ChatMessage.id.asc())
                .all()
            )
            existing_signature = [(row.message_type, row.content) for row in existing_rows]
            incoming_signature = [(msg.type, str(msg.content)) for msg in messages]
            shared_count = 0
            for existing, incoming in zip(existing_signature, incoming_signature):
                if existing != incoming:
                    break
                shared_count += 1

            if shared_count < len(existing_rows):
                stale_ids = [row.id for row in existing_rows[shared_count:]]
                if stale_ids:
                    db.query(ChatMessage).filter(ChatMessage.id.in_(stale_ids)).delete(synchronize_session=False)

            for idx, msg in enumerate(messages[shared_count:], start=shared_count):
                rag_trace = None
                if extra_message_data and idx < len(extra_message_data):
                    extra = extra_message_data[idx] or {}
                    rag_trace = extra.get("rag_trace")
                sender_type = "user" if msg.type == "human" else ("ai" if msg.type == "ai" else "system")

                db.add(
                    ChatMessage(
                        session_ref_id=session.id,
                        message_type=msg.type,
                        sender_type=sender_type,
                        sender_id=user.id if sender_type == "user" else None,
                        content=str(msg.content),
                        timestamp=now,
                        rag_trace=rag_trace,
                        metadata_json={},
                    )
                )

            for idx, msg in enumerate(messages):
                rag_trace = None
                if extra_message_data and idx < len(extra_message_data):
                    extra = extra_message_data[idx] or {}
                    rag_trace = extra.get("rag_trace")
                sender_type = "user" if msg.type == "human" else ("ai" if msg.type == "ai" else "system")
                serialized.append(
                    {
                        "type": msg.type,
                        "sender_type": sender_type,
                        "content": str(msg.content),
                        "timestamp": now.isoformat(),
                        "rag_trace": rag_trace,
                    }
                )

            session.updated_at = now
            session.last_message_preview = str(messages[-1].content)[:255] if messages else ""
            db.commit()

            cache.set_json(self._messages_cache_key(user_id, session_id), serialized)
            cache.delete(self._sessions_cache_key(user_id))
        finally:
            db.close()

    def load(self, user_id: str, session_id: str) -> list:
        """加载对话"""
        cached = cache.get_json(self._messages_cache_key(user_id, session_id))
        if cached is not None:
            return self._to_langchain_messages(cached)

        records = self.get_session_messages(user_id, session_id)
        cache.set_json(self._messages_cache_key(user_id, session_id), records)
        return self._to_langchain_messages(records)

    def list_sessions(self, user_id: str) -> list:
        """列出用户的所有会话"""
        return [item["session_id"] for item in self.list_session_infos(user_id)]

    def list_session_infos(self, user_id: str) -> list[dict]:
        cached = cache.get_json(self._sessions_cache_key(user_id))
        if cached is not None:
            return cached

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return []

            sessions = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id)
                .order_by(ChatSession.updated_at.desc())
                .all()
            )
            result = []
            for s in sessions:
                count = db.query(ChatMessage).filter(ChatMessage.session_ref_id == s.id).count()
                result.append(
                    {
                        "session_id": s.session_id,
                        "updated_at": s.updated_at.isoformat(),
                        "message_count": count,
                        "status": s.status,
                        "last_message_preview": s.last_message_preview,
                    }
                )
            cache.set_json(self._sessions_cache_key(user_id), result)
            return result
        finally:
            db.close()

    def get_session_messages(self, user_id: str, session_id: str) -> list[dict]:
        cached = cache.get_json(self._messages_cache_key(user_id, session_id))
        if cached is not None:
            return cached

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return []
            session = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
                .first()
            )
            if not session:
                return []

            rows = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_ref_id == session.id)
                .order_by(ChatMessage.id.asc())
                .all()
            )
            result = [
                {
                    "type": row.message_type,
                    "sender_type": row.sender_type,
                    "content": row.content,
                    "timestamp": row.timestamp.isoformat(),
                    "rag_trace": row.rag_trace,
                }
                for row in rows
            ]
            cache.set_json(self._messages_cache_key(user_id, session_id), result)
            return result
        finally:
            db.close()

    def delete_session(self, user_id: str, session_id: str) -> bool:
        """删除指定用户的会话，返回是否删除成功"""
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return False
            session = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
                .first()
            )
            if not session:
                return False

            db.delete(session)
            db.commit()
            cache.delete(self._messages_cache_key(user_id, session_id))
            cache.delete(self._sessions_cache_key(user_id))
            return True
        finally:
            db.close()



def create_agent_instance():
    missing = [
        name
        for name, value in {
            "ARK_API_KEY": API_KEY,
            "MODEL": MODEL,
            "BASE_URL": BASE_URL,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"缺少模型配置: {', '.join(missing)}")

    model = init_chat_model(
        model=MODEL,
        model_provider="openai",
        api_key=API_KEY,
        base_url=BASE_URL,
        temperature=0.3,
        stream_usage=True,
    )

    agent = create_agent(
        model=model,
        tools=[search_product_catalog, get_current_weather, search_knowledge_base, query_logistics],
        system_prompt=(
            "You are SuperService, an enterprise-grade AI Customer Service Assistant. "
            "Your persona is highly professional, polite, efficient, and deeply knowledgeable about the company's business. "
            "When users ask about product name, category, price, stock, material, specifications, product parameters, or product images, prioritize using search_product_catalog before the general knowledge base. "
            "If search_product_catalog finds a matching product and returns an image markdown link, include that product image in the final answer. "
            "When users ask about company policy, points rules, uploaded documents, business procedures, internal knowledge, or anything likely to depend on the knowledge base, prioritize using search_knowledge_base. "
            "If the question is completely unrelated to the company's business or general helpfulness, you must politely decline to answer (e.g., '抱歉，作为企业智能客服，我只能回答与我们业务和服务相关的问题。'). "
            "Always maintain a secure, compliant, and professional tone. Do not make up facts. "
            "For business rules, policy details, internal process descriptions, and document-based facts, prefer the knowledge base over your own general knowledge. "
            "When answering from the knowledge base, present the result in a natural, helpful, and professional way, explicitly mentioning that the information comes from the company knowledge base. "
            "If retrieved knowledge-base results include Image URL, Related Image, Image Caption, or Visual Description fields, treat those as available document images. "
            "For questions asking what an uploaded PDF image or product looks like, answer from the Visual Description when available instead of saying the knowledge base has no image. "
            "If an Image URL is available but Visual Description is empty, say that the matching image has been found and is shown in the answer, then avoid inventing unseen visual details. "
            "If the user asks about weather, temperature, rain, forecast, tomorrow weather, county-level weather, district weather, or city weather conditions, use get_current_weather. "
            "For forecast or tomorrow/next-day questions, use get_current_weather with forecast behavior rather than realtime-only weather. "
            "If the user asks about package status, delivery, or logistics tracking, use query_logistics with the tracking number. "
            "If the user sends only a plausible logistics tracking number, treat it as a logistics lookup request and use query_logistics. "
            "When the carrier is mentioned, pass the carrier name or code; when a phone tail number is provided, pass it as phone. "
            "If the user is chatting casually or greeting you, greet them politely as a professional customer service representative. "
            "If a question contains both knowledge-base content and weather content, you may use both tools when needed and then provide one coherent combined answer. "
            "Avoid repeated calls to the same tool within one turn unless truly necessary. "
            "At most one knowledge-base search call per turn unless the first result is clearly malformed or unusable. "
            "If the retrieved knowledge-base context is insufficient, apologize politely and clarify that the knowledge base currently does not contain this information. "
            "If the weather tool fails or returns insufficient information, explain that the weather query could not be completed, and do not fabricate weather details. "
            "If tool results include a Step-back Question/Answer, you may use that principle to improve the final answer, but do not reveal chain-of-thought. "
            "Keep answers clear, highly structured, and easy to read, using bullet points when explaining complex processes. "
            "If you do not know the answer, admit it honestly and kindly, and optionally suggest they contact a human agent."
        ),
    )
    return agent, model


_agent = None
_model = None


def get_agent_instance():
    global _agent, _model
    if _agent is None or _model is None:
        _agent, _model = create_agent_instance()
    return _agent, _model

storage = ConversationStorage()

StreamObserver = Callable[[dict], Awaitable[None]]


def _message_content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
        return "".join(parts)
    return str(content) if content is not None else ""


def _extract_response_content(result) -> str:
    if isinstance(result, dict):
        if "output" in result:
            return _message_content_to_text(result["output"])
        messages = result.get("messages") or []
        for msg in reversed(messages):
            msg_type = getattr(msg, "type", "")
            if msg_type == "ai":
                text = _message_content_to_text(getattr(msg, "content", ""))
                if text:
                    return text
        if messages:
            return _message_content_to_text(getattr(messages[-1], "content", messages[-1]))
        return str(result)
    if hasattr(result, "content"):
        return _message_content_to_text(result.content)
    return str(result)


def _last_human_message_matches(messages: list, user_text: str) -> bool:
    if not messages:
        return False
    last = messages[-1]
    return getattr(last, "type", None) == "human" and str(getattr(last, "content", "")) == user_text


def _chunk_content_to_text(message_chunk) -> str:
    if isinstance(message_chunk, AIMessageChunk) and getattr(message_chunk, "tool_call_chunks", None):
        return ""
    if getattr(message_chunk, "type", "") not in ("AIMessageChunk", "ai", "AIMessage"):
        return ""
    return _message_content_to_text(getattr(message_chunk, "content", ""))


async def _notify_stream_observer(observer: StreamObserver | None, event: dict) -> None:
    if not observer:
        return
    try:
        await observer(event)
    except Exception:
        pass


def _direct_logistics_lookup(user_text: str) -> dict | None:
    return extract_logistics_request(user_text)


def _direct_product_lookup(user_text: str) -> str | None:
    return search_product_answer(user_text)


def _build_direct_logistics_response(lookup: dict) -> str:
    result = query_logistics.invoke(
        {
            "tracking_number": lookup.get("tracking_number", ""),
            "company": lookup.get("company"),
            "phone": lookup.get("phone"),
        }
    )
    result = str(result)
    if result.startswith("【"):
        return f"我帮你查询到以下物流信息：\n\n{result}"
    return result


def summarize_old_messages(model, messages: list) -> str:
    """将旧消息总结为摘要"""
    # 提取旧对话
    old_conversation = "\n".join([
        f"{'用户' if msg.type == 'human' else 'AI'}: {msg.content}"
        for msg in messages
    ])

    # 生成摘要
    summary_prompt = f"""请总结以下对话的关键信息：

{old_conversation}
总结（包含用户信息、重要事实、待办事项）："""

    summary = model.invoke(summary_prompt).content
    return summary


def chat_with_agent(
    user_text: str,
    user_id: str = "default_user",
    session_id: str = "default_session",
    user_message_already_saved: bool = False,
):
    """使用 Agent 处理用户消息并返回响应"""
    messages = storage.load(user_id, session_id)

    # 清理可能残留的 RAG 上下文，避免跨请求污染
    get_last_rag_context(clear=True)
    reset_tool_call_guards()

    if not (user_message_already_saved and _last_human_message_matches(messages, user_text)):
        messages.append(HumanMessage(content=user_text))

    product_answer = _direct_product_lookup(user_text)
    if product_answer:
        messages.append(AIMessage(content=product_answer))
        storage.save(user_id, session_id, messages, extra_message_data=[None] * len(messages))
        return {
            "response": product_answer,
            "rag_trace": None,
        }

    logistics_lookup = _direct_logistics_lookup(user_text)
    if logistics_lookup:
        response_content = _build_direct_logistics_response(logistics_lookup)
        messages.append(AIMessage(content=response_content))
        storage.save(user_id, session_id, messages, extra_message_data=[None] * len(messages))
        return {
            "response": response_content,
            "rag_trace": None,
        }

    agent_instance, model_instance = get_agent_instance()
    if len(messages) > 50:
        summary = summarize_old_messages(model_instance, messages[:40])

        messages = [
            SystemMessage(content=f"之前的对话摘要：\n{summary}")
        ] + messages[40:]

    result = agent_instance.invoke(
        {"messages": messages},
        config={"recursion_limit": 8},
    )

    response_content = _extract_response_content(result)
    
    messages.append(AIMessage(content=response_content))

    rag_context = get_last_rag_context(clear=True)
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]
    storage.save(user_id, session_id, messages, extra_message_data=extra_message_data)

    return {
        "response": response_content,
        "rag_trace": rag_trace,
    }


async def chat_with_agent_stream(
    user_text: str,
    user_id: str = "default_user",
    session_id: str = "default_session",
    user_message_already_saved: bool = False,
    observer: StreamObserver | None = None,
):
    """使用 Agent 处理用户消息并流式返回响应。
    
    架构：使用统一输出队列 + 后台任务，确保 RAG 检索步骤在工具执行期间实时推送，
    而非等待工具完成后才显示。
    """
    messages = storage.load(user_id, session_id)

    # 清理可能残留的 RAG 上下文
    get_last_rag_context(clear=True)
    reset_tool_call_guards()

    # 统一输出队列：所有事件（content / rag_step）都汇入这里
    output_queue = asyncio.Queue()
    stream_id = uuid4().hex
    stream_seq = 0
    last_progress_label = "正在处理"

    def _float_env(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    heartbeat_interval = max(1.0, _float_env("AGENT_STREAM_HEARTBEAT_SECONDS", 4.0))
    max_wait_seconds = _float_env("AGENT_STREAM_MAX_SECONDS", 300.0)

    def _with_stream_meta(event: dict) -> dict:
        nonlocal stream_seq
        stream_seq += 1
        return {
            **event,
            "_stream_id": stream_id,
            "_stream_seq": stream_seq,
        }

    def _remember_progress(event: dict) -> None:
        nonlocal last_progress_label
        event_type = event.get("type")
        if event_type == "rag_step":
            label = (event.get("step") or {}).get("label")
            if label:
                last_progress_label = label
        elif event_type == "content":
            last_progress_label = "正在生成回答"
        elif event_type == "answer_done":
            last_progress_label = "正在整理检索结果"
        elif event_type == "status" and event.get("content"):
            last_progress_label = str(event["content"]).rstrip("。. ")

    class _RagStepProxy:
        """代理对象：将 emit_rag_step 的原始 step dict 包装后放入统一输出队列。"""
        def put_nowait(self, step):
            output_queue.put_nowait({"type": "rag_step", "step": step})

    set_rag_step_queue(_RagStepProxy())

    if not (user_message_already_saved and _last_human_message_matches(messages, user_text)):
        messages.append(HumanMessage(content=user_text))

    product_answer = await asyncio.to_thread(_direct_product_lookup, user_text)
    if product_answer:
        try:
            status_event = _with_stream_meta({"type": "status", "content": "正在查询商品库..."})
            await _notify_stream_observer(observer, status_event)
            yield f"data: {json.dumps(status_event, ensure_ascii=False)}\n\n"

            content_event = _with_stream_meta({"type": "content", "content": product_answer})
            await _notify_stream_observer(observer, content_event)
            yield f"data: {json.dumps(content_event, ensure_ascii=False)}\n\n"

            done_event = _with_stream_meta({"type": "answer_done", "content_length": len(product_answer)})
            await _notify_stream_observer(observer, done_event)
            yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"

            messages.append(AIMessage(content=product_answer))
            storage.save(user_id, session_id, messages, extra_message_data=[None] * len(messages))
            yield "data: [DONE]\n\n"
            return
        finally:
            set_rag_step_queue(None)

    logistics_lookup = _direct_logistics_lookup(user_text)
    if logistics_lookup:
        try:
            status_event = _with_stream_meta({"type": "status", "content": "正在查询物流信息..."})
            await _notify_stream_observer(observer, status_event)
            yield f"data: {json.dumps(status_event, ensure_ascii=False)}\n\n"

            try:
                response_content = await asyncio.to_thread(_build_direct_logistics_response, logistics_lookup)
            except Exception as e:
                response_content = f"物流查询失败：{e}"

            content_event = _with_stream_meta({"type": "content", "content": response_content})
            await _notify_stream_observer(observer, content_event)
            yield f"data: {json.dumps(content_event, ensure_ascii=False)}\n\n"

            done_event = _with_stream_meta({"type": "answer_done", "content_length": len(response_content)})
            await _notify_stream_observer(observer, done_event)
            yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"

            messages.append(AIMessage(content=response_content))
            storage.save(user_id, session_id, messages, extra_message_data=[None] * len(messages))
            yield "data: [DONE]\n\n"
            return
        finally:
            set_rag_step_queue(None)

    agent_instance, model_instance = get_agent_instance()
    if len(messages) > 50:
        summary = summarize_old_messages(model_instance, messages[:40])
        messages = [
            SystemMessage(content=f"之前的对话摘要：\n{summary}")
        ] + messages[40:]

    full_response = ""
    output_queue.put_nowait({"type": "status", "content": "正在理解问题..."})

    async def _agent_worker():
        """后台任务：运行 agent 并将内容 chunk 推入输出队列。"""
        nonlocal full_response
        try:
            streamed_response = ""
            final_state = None
            async for item in agent_instance.astream(
                {"messages": messages},
                config={"recursion_limit": 8},
                stream_mode=["messages", "values"],
            ):
                if isinstance(item, tuple) and len(item) == 2:
                    mode, data = item
                else:
                    mode, data = "values", item

                if mode == "messages":
                    message_chunk, _metadata = data
                    text = _chunk_content_to_text(message_chunk)
                    if text:
                        streamed_response += text
                        await output_queue.put({"type": "content", "content": text})
                elif mode == "values":
                    final_state = data

            final_response = _extract_response_content(final_state) if final_state else ""
            full_response = final_response or streamed_response

            if full_response and not streamed_response:
                await output_queue.put({"type": "content", "content": full_response})
            if full_response:
                await output_queue.put({"type": "answer_done", "content_length": len(full_response)})
        except Exception as e:
            await output_queue.put({"type": "error", "content": str(e)})
        finally:
            # 哨兵：通知主循环 agent 已完成
            await output_queue.put(None)

    # 启动后台任务
    agent_task = asyncio.create_task(_agent_worker())

    try:
        # 主循环：持续从统一队列取事件并 yield SSE
        # RAG 步骤在工具执行期间通过 call_soon_threadsafe 实时入队，不需要等 agent 产出 chunk
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        while True:
            timeout = heartbeat_interval
            if max_wait_seconds > 0:
                remaining = max_wait_seconds - (loop.time() - started_at)
                if remaining <= 0:
                    timeout_event = _with_stream_meta({"type": "error", "content": "AI 响应超时，请稍后重试。"})
                    await _notify_stream_observer(observer, timeout_event)
                    yield f"data: {json.dumps(timeout_event, ensure_ascii=False)}\n\n"
                    break
                timeout = min(timeout, remaining)
            try:
                event = await asyncio.wait_for(output_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                heartbeat_event = _with_stream_meta({"type": "status", "content": f"{last_progress_label}，请稍候..."})
                await _notify_stream_observer(observer, heartbeat_event)
                yield f"data: {json.dumps(heartbeat_event, ensure_ascii=False)}\n\n"
                continue
            if event is None:
                break
            _remember_progress(event)
            event = _with_stream_meta(event)
            await _notify_stream_observer(observer, event)
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except GeneratorExit:
        # 客户端断开连接（AbortController）时，FastAPI 会向此生成器抛出 GeneratorExit
        # 我们必须在此处取消后台任务
        agent_task.cancel()
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass  # 任务已成功取消
        raise  # 重新抛出 GeneratorExit 以便 FastAPI 正确处理关闭
    finally:
        # 正常结束或异常退出时清理
        set_rag_step_queue(None)
        if not agent_task.done():
            agent_task.cancel()
            try:
                await asyncio.wait_for(agent_task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    # 获取 RAG trace
    rag_context = get_last_rag_context(clear=True)
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    extra_message_data = [None] * len(messages)
    if full_response:
        messages.append(AIMessage(content=full_response))
        extra_message_data.append({"rag_trace": rag_trace})
    storage.save(user_id, session_id, messages, extra_message_data=extra_message_data)

    # 发送 trace 信息

    if rag_trace:
        trace_event = _with_stream_meta({"type": "trace", "rag_trace": rag_trace})
        await _notify_stream_observer(observer, trace_event)
        yield f"data: {json.dumps(trace_event, ensure_ascii=False)}\n\n"

    # 发送结束信号
    yield "data: [DONE]\n\n"
    return
