from collections import defaultdict
from collections import Counter
from typing import List, Tuple, Dict, Any
import os
import json
import math
import requests
from dotenv import load_dotenv

from milvus_client import MilvusManager
from embedding import EmbeddingService
from parent_chunk_store import ParentChunkStore
from pdf_image_store import attach_images_to_docs, images_from_retrieved_docs
from langchain.chat_models import init_chat_model

load_dotenv()

ARK_API_KEY = os.getenv("ARK_API_KEY")
MODEL = os.getenv("MODEL")
BASE_URL = os.getenv("BASE_URL")
RERANK_MODEL = os.getenv("RERANK_MODEL")
RERANK_BINDING_HOST = os.getenv("RERANK_BINDING_HOST")
RERANK_API_KEY = os.getenv("RERANK_API_KEY")
AUTO_MERGE_ENABLED = os.getenv("AUTO_MERGE_ENABLED", "true").lower() != "false"
AUTO_MERGE_THRESHOLD = int(os.getenv("AUTO_MERGE_THRESHOLD", "2"))
LEAF_RETRIEVE_LEVEL = int(os.getenv("LEAF_RETRIEVE_LEVEL", "3"))
KEYWORD_FALLBACK_SCAN_LIMIT = int(os.getenv("KEYWORD_FALLBACK_SCAN_LIMIT", "10000"))
CANDIDATE_MULTIPLIER = max(1, int(os.getenv("RETRIEVAL_CANDIDATE_MULTIPLIER", "2")))

# 全局初始化检索依赖，避免反复构造
_embedding_service = EmbeddingService()
_milvus_manager = MilvusManager()
_parent_chunk_store = ParentChunkStore()

_stepback_model = None


def _get_rerank_endpoint() -> str:
    if not RERANK_BINDING_HOST:
        return ""
    host = RERANK_BINDING_HOST.strip().rstrip("/")
    return host if host.endswith("/v1/rerank") else f"{host}/v1/rerank"


def _retrieval_output_fields() -> list[str]:
    return [
        "text",
        "filename",
        "file_type",
        "page_number",
        "chunk_id",
        "parent_chunk_id",
        "root_chunk_id",
        "chunk_level",
        "chunk_idx",
        "content_kind",
        "image_url",
        "image_caption",
        "visual_description",
    ]


def _score_keyword_candidates(query: str, docs: List[dict], top_n: int) -> List[dict]:
    query_tokens = _embedding_service.tokenize(query)
    if not query_tokens or not docs:
        return []

    doc_tokens = []
    doc_freq = Counter()
    total_len = 0
    for doc in docs:
        tokens = _embedding_service.tokenize(doc.get("text", ""))
        doc_tokens.append(tokens)
        total_len += len(tokens)
        for token in set(tokens):
            doc_freq[token] += 1

    total_docs = len(docs)
    avg_len = total_len / total_docs if total_docs else 1
    query_counter = Counter(query_tokens)
    query_phrase = "".join(query_tokens)

    scored = []
    for doc, tokens in zip(docs, doc_tokens):
        if not tokens:
            continue
        tf = Counter(tokens)
        doc_len = len(tokens)
        score = 0.0
        matched_terms = 0
        for token, query_weight in query_counter.items():
            freq = tf.get(token, 0)
            if not freq:
                continue
            matched_terms += 1
            idf = math.log((total_docs - doc_freq[token] + 0.5) / (doc_freq[token] + 0.5) + 1)
            numerator = freq * (_embedding_service.k1 + 1)
            denominator = freq + _embedding_service.k1 * (
                1 - _embedding_service.b + _embedding_service.b * doc_len / max(avg_len, 1)
            )
            score += query_weight * idf * numerator / denominator

        text = doc.get("text", "")
        compact_text = "".join(_embedding_service.tokenize(text))
        if query_phrase and query_phrase in compact_text:
            score += 8.0
        if matched_terms >= max(1, min(3, len(query_counter))):
            score += matched_terms * 0.25

        if score <= 0:
            continue
        item = dict(doc)
        item["score"] = float(score)
        item["keyword_score"] = float(score)
        scored.append(item)

    scored.sort(key=lambda item: item.get("keyword_score", 0.0), reverse=True)
    return scored[:top_n]


def _keyword_fallback_retrieve(query: str, top_k: int, candidate_k: int, filter_expr: str, reason: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "rerank_enabled": bool(RERANK_MODEL and RERANK_API_KEY and RERANK_BINDING_HOST),
        "rerank_applied": False,
        "rerank_model": RERANK_MODEL,
        "rerank_endpoint": _get_rerank_endpoint(),
        "rerank_error": None,
        "retrieval_mode": "keyword_fallback",
        "retrieval_error": reason,
        "candidate_k": candidate_k,
        "leaf_retrieve_level": LEAF_RETRIEVE_LEVEL,
        "candidate_count": 0,
    }
    try:
        docs = _milvus_manager.query(
            filter_expr=filter_expr,
            output_fields=_retrieval_output_fields(),
            limit=KEYWORD_FALLBACK_SCAN_LIMIT,
        )
        candidates = _score_keyword_candidates(query, list(docs), candidate_k)
        meta["candidate_count"] = len(candidates)
        reranked, rerank_meta = _rerank_documents(query=query, docs=candidates, top_k=top_k)
        merged_docs, merge_meta = _auto_merge_documents(docs=reranked, top_k=top_k)
        merged_docs = attach_images_to_docs(merged_docs, query=query)
        meta.update(rerank_meta)
        meta["retrieval_mode"] = "keyword_fallback"
        meta["retrieval_error"] = reason
        meta["candidate_k"] = candidate_k
        meta["leaf_retrieve_level"] = LEAF_RETRIEVE_LEVEL
        meta.update(merge_meta)
        meta["retrieved_images"] = images_from_retrieved_docs(merged_docs, query=query)
        return {"docs": merged_docs, "meta": meta}
    except Exception as fallback_error:
        meta.update({
            "rerank_applied": False,
            "rerank_error": "keyword_fallback_failed",
            "retrieval_mode": "failed",
            "retrieval_error": f"{reason}; keyword_fallback_error={fallback_error}",
            "auto_merge_enabled": AUTO_MERGE_ENABLED,
            "auto_merge_applied": False,
            "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
            "auto_merge_replaced_chunks": 0,
            "auto_merge_steps": 0,
            "candidate_count": 0,
            "retrieved_images": [],
        })
        return {"docs": [], "meta": meta}


def _merge_to_parent_level(docs: List[dict], threshold: int = 2) -> Tuple[List[dict], int]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for doc in docs:
        parent_id = (doc.get("parent_chunk_id") or "").strip()
        if parent_id:
            groups[parent_id].append(doc)

    merge_parent_ids = [parent_id for parent_id, children in groups.items() if len(children) >= threshold]
    if not merge_parent_ids:
        return docs, 0

    parent_docs = _parent_chunk_store.get_documents_by_ids(merge_parent_ids)
    parent_map = {item.get("chunk_id", ""): item for item in parent_docs if item.get("chunk_id")}

    merged_docs: List[dict] = []
    merged_count = 0
    for doc in docs:
        parent_id = (doc.get("parent_chunk_id") or "").strip()
        if not parent_id or parent_id not in parent_map:
            merged_docs.append(doc)
            continue
        parent_doc = dict(parent_map[parent_id])
        score = doc.get("score")
        if score is not None:
            parent_doc["score"] = max(float(parent_doc.get("score", score)), float(score))
        parent_doc["merged_from_children"] = True
        parent_doc["merged_child_count"] = len(groups[parent_id])
        merged_docs.append(parent_doc)
        merged_count += 1

    deduped: List[dict] = []
    seen = set()
    for item in merged_docs:
        key = item.get("chunk_id") or (item.get("filename"), item.get("page_number"), item.get("text"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped, merged_count


def _auto_merge_documents(docs: List[dict], top_k: int) -> Tuple[List[dict], Dict[str, Any]]:
    if not AUTO_MERGE_ENABLED or not docs:
        return docs[:top_k], {
            "auto_merge_enabled": AUTO_MERGE_ENABLED,
            "auto_merge_applied": False,
            "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
            "auto_merge_replaced_chunks": 0,
            "auto_merge_steps": 0,
        }

    # 两段自动合并：L3->L2，再 L2->L1。
    merged_docs, merged_count_l3_l2 = _merge_to_parent_level(docs, threshold=AUTO_MERGE_THRESHOLD)
    merged_docs, merged_count_l2_l1 = _merge_to_parent_level(merged_docs, threshold=AUTO_MERGE_THRESHOLD)

    merged_docs.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    merged_docs = merged_docs[:top_k]

    replaced_count = merged_count_l3_l2 + merged_count_l2_l1
    return merged_docs, {
        "auto_merge_enabled": AUTO_MERGE_ENABLED,
        "auto_merge_applied": replaced_count > 0,
        "auto_merge_threshold": AUTO_MERGE_THRESHOLD,
        "auto_merge_replaced_chunks": replaced_count,
        "auto_merge_steps": int(merged_count_l3_l2 > 0) + int(merged_count_l2_l1 > 0),
    }


def _rerank_documents(query: str, docs: List[dict], top_k: int) -> Tuple[List[dict], Dict[str, Any]]:
    docs_with_rank = [{**doc, "rrf_rank": i} for i, doc in enumerate(docs, 1)]
    meta: Dict[str, Any] = {
        "rerank_enabled": bool(RERANK_MODEL and RERANK_API_KEY and RERANK_BINDING_HOST),
        "rerank_applied": False,
        "rerank_model": RERANK_MODEL,
        "rerank_endpoint": _get_rerank_endpoint(),
        "rerank_error": None,
        "candidate_count": len(docs_with_rank),
    }
    if not docs_with_rank or not meta["rerank_enabled"]:
        return docs_with_rank[:top_k], meta

    payload = {
        "model": RERANK_MODEL,
        "query": query,
        "documents": [doc.get("text", "") for doc in docs_with_rank],
        "top_n": min(top_k, len(docs_with_rank)),
        "return_documents": False,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {RERANK_API_KEY}",
    }
    try:
        meta["rerank_applied"] = True
        response = requests.post(
            meta["rerank_endpoint"],
            headers=headers,
            json=payload,
            timeout=15,
        )
        if response.status_code >= 400:
            meta["rerank_error"] = f"HTTP {response.status_code}: {response.text}"
            return docs_with_rank[:top_k], meta

        items = response.json().get("results", [])
        reranked = []
        for item in items:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(docs_with_rank):
                doc = dict(docs_with_rank[idx])
                score = item.get("relevance_score")
                if score is not None:
                    doc["rerank_score"] = score
                reranked.append(doc)

        if reranked:
            return reranked[:top_k], meta

        meta["rerank_error"] = "empty_rerank_results"
        return docs_with_rank[:top_k], meta
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        meta["rerank_error"] = str(e)
        return docs_with_rank[:top_k], meta


def _get_stepback_model():
    global _stepback_model
    if not ARK_API_KEY or not MODEL:
        return None
    if _stepback_model is None:
        _stepback_model = init_chat_model(
            model=MODEL,
            model_provider="openai",
            api_key=ARK_API_KEY,
            base_url=BASE_URL,
            temperature=0.2,
        )
    return _stepback_model


def _generate_step_back_question(query: str) -> str:
    model = _get_stepback_model()
    if not model:
        return ""
    prompt = (
        "请将用户的具体问题抽象成更高层次、更概括的‘退步问题’，"
        "用于探寻背后的通用原理或核心概念。只输出退步问题一句话，不要解释。\n"
        f"用户问题：{query}"
    )
    try:
        return (model.invoke(prompt).content or "").strip()
    except Exception:
        return ""


def _answer_step_back_question(step_back_question: str) -> str:
    model = _get_stepback_model()
    if not model or not step_back_question:
        return ""
    prompt = (
        "请简要回答以下退步问题，提供通用原理/背景知识，"
        "控制在120字以内。只输出答案，不要列出推理过程。\n"
        f"退步问题：{step_back_question}"
    )
    try:
        return (model.invoke(prompt).content or "").strip()
    except Exception:
        return ""


def generate_hypothetical_document(query: str) -> str:
    model = _get_stepback_model()
    if not model:
        return ""
    prompt = (
        "请基于用户问题生成一段‘假设性文档’，内容应像真实资料片段，"
        "用于帮助检索相关信息。文档可以包含合理推测，但需与问题语义相关。"
        "只输出文档正文，不要标题或解释。\n"
        f"用户问题：{query}"
    )
    try:
        return (model.invoke(prompt).content or "").strip()
    except Exception:
        return ""


def step_back_expand(query: str) -> dict:
    step_back_question = _generate_step_back_question(query)
    step_back_answer = _answer_step_back_question(step_back_question)
    if step_back_question or step_back_answer:
        expanded_query = (
            f"{query}\n\n"
            f"退步问题：{step_back_question}\n"
            f"退步问题答案：{step_back_answer}"
        )
    else:
        expanded_query = query
    return {
        "step_back_question": step_back_question,
        "step_back_answer": step_back_answer,
        "expanded_query": expanded_query,
    }


def retrieve_documents(query: str, top_k: int = 5) -> Dict[str, Any]:
    candidate_k = max(top_k * CANDIDATE_MULTIPLIER, top_k)
    filter_expr = f"chunk_level == {LEAF_RETRIEVE_LEVEL}"
    dense_embedding = None
    try:
        dense_embeddings = _embedding_service.get_embeddings([query])
        dense_embedding = dense_embeddings[0]
        sparse_embedding = _embedding_service.get_sparse_embedding(query)

        retrieved = _milvus_manager.hybrid_retrieve(
            dense_embedding=dense_embedding,
            sparse_embedding=sparse_embedding,
            top_k=candidate_k,
            filter_expr=filter_expr,
        )
        if not retrieved:
            return _keyword_fallback_retrieve(
                query=query,
                top_k=top_k,
                candidate_k=candidate_k,
                filter_expr=filter_expr,
                reason="hybrid_empty",
            )
        reranked, rerank_meta = _rerank_documents(query=query, docs=retrieved, top_k=top_k)
        merged_docs, merge_meta = _auto_merge_documents(docs=reranked, top_k=top_k)
        merged_docs = attach_images_to_docs(merged_docs, query=query)
        rerank_meta["retrieval_mode"] = "hybrid"
        rerank_meta["retrieval_error"] = None
        rerank_meta["candidate_k"] = candidate_k
        rerank_meta["leaf_retrieve_level"] = LEAF_RETRIEVE_LEVEL
        rerank_meta.update(merge_meta)
        rerank_meta["retrieved_images"] = images_from_retrieved_docs(merged_docs, query=query)
        return {"docs": merged_docs, "meta": rerank_meta}
    except Exception as hybrid_error:
        try:
            if dense_embedding is None:
                dense_embeddings = _embedding_service.get_embeddings([query])
                dense_embedding = dense_embeddings[0]
            retrieved = _milvus_manager.dense_retrieve(
                dense_embedding=dense_embedding,
                top_k=candidate_k,
                filter_expr=filter_expr,
            )
            if not retrieved:
                return _keyword_fallback_retrieve(
                    query=query,
                    top_k=top_k,
                    candidate_k=candidate_k,
                    filter_expr=filter_expr,
                    reason=f"hybrid_error={hybrid_error}; dense_empty",
                )
            reranked, rerank_meta = _rerank_documents(query=query, docs=retrieved, top_k=top_k)
            merged_docs, merge_meta = _auto_merge_documents(docs=reranked, top_k=top_k)
            merged_docs = attach_images_to_docs(merged_docs, query=query)
            rerank_meta["retrieval_mode"] = "dense_fallback"
            rerank_meta["retrieval_error"] = str(hybrid_error)
            rerank_meta["candidate_k"] = candidate_k
            rerank_meta["leaf_retrieve_level"] = LEAF_RETRIEVE_LEVEL
            rerank_meta.update(merge_meta)
            rerank_meta["retrieved_images"] = images_from_retrieved_docs(merged_docs, query=query)
            return {"docs": merged_docs, "meta": rerank_meta}
        except Exception as dense_error:
            return _keyword_fallback_retrieve(
                query=query,
                top_k=top_k,
                candidate_k=candidate_k,
                filter_expr=filter_expr,
                reason=f"hybrid_error={hybrid_error}; dense_error={dense_error}",
            )
