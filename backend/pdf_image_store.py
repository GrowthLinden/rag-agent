"""Extract and look up images embedded in uploaded PDF files."""
import json
import re
import shutil
from hashlib import sha1
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from vision_service import VisionService


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"
IMAGE_ROOT = DATA_DIR / "pdf_images"
INDEX_PATH = IMAGE_ROOT / "index.json"


def _safe_name(filename: str) -> str:
    stem = Path(filename).stem or "document"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._")
    digest = sha1(filename.encode("utf-8")).hexdigest()[:10]
    return f"{safe or 'document'}_{digest}"


def _load_index() -> dict[str, list[dict[str, Any]]]:
    if not INDEX_PATH.exists():
        return {}
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_index(index: dict[str, list[dict[str, Any]]]) -> None:
    IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def _extension(image_name: str) -> str:
    suffix = Path(image_name or "").suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"}:
        return "jpg" if suffix == "jpeg" else suffix
    return "png"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _page_item_labels(text: str) -> list[str]:
    labels: list[str] = []
    for line in (text or "").splitlines():
        match = re.match(r"^\s*\d+\s*[.．、]\s*(.+?)\s*$", line)
        if match:
            labels.append(match.group(1).strip())
    return labels


def _image_score(image: dict[str, Any], query: str) -> float:
    query_norm = _normalize_text(query)
    if not query_norm:
        return 0.0

    best = 0.0
    primary_fields = [
        image.get("caption", ""),
        " ".join(image.get("keywords", []) or []),
    ]
    for field in primary_fields:
        field_norm = _normalize_text(field)
        if not field_norm:
            continue
        if field_norm in query_norm or query_norm in field_norm:
            best = max(best, 100.0)
            continue
        common_run = _max_common_substring_len(field_norm, query_norm)
        if common_run >= 3:
            best = max(best, 0.9)
        elif common_run >= 2:
            best = max(best, 0.75)
        field_chars = set(field_norm)
        query_chars = set(query_norm)
        if field_chars:
            best = max(best, len(field_chars & query_chars) / len(field_chars))

    visual_fields = [
        image.get("visual_description", ""),
        image.get("text", ""),
    ]
    for field in visual_fields:
        field_norm = _normalize_text(field)
        if not field_norm:
            continue
        if query_norm in field_norm:
            best = max(best, 0.95)
            continue
        common_run = _max_common_substring_len(field_norm, query_norm)
        if common_run >= 3:
            best = max(best, 0.85)
        elif common_run >= 2:
            best = max(best, 0.65)
    return best


def _max_common_substring_len(left: str, right: str) -> int:
    best = 0
    previous = [0] * (len(right) + 1)
    for left_char in left:
        current = [0] * (len(right) + 1)
        for index, right_char in enumerate(right, 1):
            if left_char == right_char:
                current[index] = previous[index - 1] + 1
                best = max(best, current[index])
        previous = current
    return best


def _filter_images_by_query(images: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    if not query or not images:
        return images

    scored = [(image, _image_score(image, query)) for image in images]
    exact_matches = [image for image, score in scored if score >= 100.0]
    if exact_matches:
        return exact_matches

    best_score = max((score for _, score in scored), default=0.0)
    if best_score >= 0.6:
        return [image for image, score in scored if score == best_score]
    return []


def images_from_retrieved_docs(
    docs: list[dict[str, Any]],
    query: str = "",
    max_images: int = 3,
) -> list[dict[str, Any]]:
    """Return only images that are directly represented by relevant retrieved image docs."""
    candidates: list[tuple[dict[str, Any], float]] = []
    seen = set()
    for index, doc in enumerate(docs):
        image_url = doc.get("image_url") or ""
        if not image_url or image_url in seen:
            continue
        seen.add(image_url)
        image = {
            "filename": doc.get("filename", ""),
            "page_number": doc.get("page_number", 0),
            "image_index": doc.get("chunk_idx", 0),
            "url": image_url,
            "caption": doc.get("image_caption", ""),
            "visual_description": doc.get("visual_description", ""),
            "text": doc.get("text", ""),
        }
        score = _image_score(image, query)
        rank_bonus = max(0.0, 0.1 - index * 0.01)
        candidates.append((image, score + rank_bonus))

    if not candidates:
        return []

    exact = [image for image, score in candidates if score >= 100.0]
    if exact:
        return exact[:max_images]

    best_score = max(score for _, score in candidates)
    if best_score < 0.6:
        return []

    selected = [
        image
        for image, score in sorted(candidates, key=lambda item: item[1], reverse=True)
        if score >= best_score - 0.05
    ]
    return selected[:max_images]


def delete_pdf_images(filename: str) -> None:
    index = _load_index()
    index.pop(filename, None)
    doc_dir = IMAGE_ROOT / _safe_name(filename)
    if doc_dir.exists():
        shutil.rmtree(doc_dir, ignore_errors=True)
    _save_index(index)


def extract_pdf_images(file_path: str, filename: str) -> list[dict[str, Any]]:
    """Extract embedded PDF images and return browser-visible metadata."""
    delete_pdf_images(filename)

    images: list[dict[str, Any]] = []
    reader = PdfReader(file_path)
    doc_dir = IMAGE_ROOT / _safe_name(filename)
    doc_dir.mkdir(parents=True, exist_ok=True)
    vision = VisionService()

    for page_index, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        page_labels = _page_item_labels(page_text)
        for image_index, image in enumerate(getattr(page, "images", []) or []):
            ext = _extension(getattr(image, "name", ""))
            image_name = f"page_{page_index + 1}_image_{image_index + 1}.{ext}"
            out_path = doc_dir / image_name
            try:
                out_path.write_bytes(image.data)
            except OSError:
                continue
            rel_path = out_path.relative_to(DATA_DIR).as_posix()
            caption = page_labels[image_index] if image_index < len(page_labels) else ""
            visual_description = ""
            try:
                visual_description = vision.describe_image(str(out_path), hint=caption)
            except Exception:
                visual_description = ""
            images.append(
                {
                    "filename": filename,
                    "page_number": page_index,
                    "image_index": image_index + 1,
                    "url": f"/assets/{rel_path}",
                    "name": image_name,
                    "caption": caption,
                    "keywords": [caption] if caption else [],
                    "page_text": page_text,
                    "visual_description": visual_description,
                }
            )

    index = _load_index()
    index[filename] = images
    _save_index(index)
    return images


def build_image_documents(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for image in images:
        caption = image.get("caption") or ""
        visual_description = image.get("visual_description") or ""
        text_parts = [
            "PDF图片资料",
            f"商品名称：{caption}" if caption else "",
            f"图片外观描述：{visual_description}" if visual_description else "",
            f"来源文件：{image.get('filename', '')}",
            f"页码：{int(image.get('page_number', 0) or 0) + 1}",
        ]
        text = "\n".join(part for part in text_parts if part).strip()
        if not text:
            continue

        filename = image.get("filename", "")
        page_number = int(image.get("page_number", 0) or 0)
        image_index = int(image.get("image_index", 0) or 0)
        chunk_id = f"{filename}::p{page_number}::image::{image_index}"
        docs.append(
            {
                "text": text,
                "filename": filename,
                "file_path": "",
                "file_type": "PDF_IMAGE",
                "page_number": page_number,
                "chunk_id": chunk_id,
                "parent_chunk_id": "",
                "root_chunk_id": chunk_id,
                "chunk_level": 3,
                "chunk_idx": image_index,
                "content_kind": "image",
                "image_url": image.get("url", ""),
                "image_caption": caption,
                "visual_description": visual_description,
            }
        )
    return docs


def images_for_pages(docs: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
    index = _load_index()
    wanted = {
        (doc.get("filename"), int(doc.get("page_number", 0) or 0))
        for doc in docs
        if doc.get("filename")
    }
    results: list[dict[str, Any]] = []
    seen = set()
    for filename, page_number in wanted:
        for image in index.get(filename, []):
            if int(image.get("page_number", -1)) != page_number:
                continue
            key = image.get("url")
            if key in seen:
                continue
            seen.add(key)
            results.append(image)
    return _filter_images_by_query(results, query)


def attach_images_to_docs(docs: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
    all_images = images_for_pages(docs, query=query)
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for image in all_images:
        key = (image.get("filename", ""), int(image.get("page_number", 0) or 0))
        grouped.setdefault(key, []).append(image)

    enriched = []
    for doc in docs:
        item = dict(doc)
        key = (item.get("filename", ""), int(item.get("page_number", 0) or 0))
        item["images"] = grouped.get(key, [])
        enriched.append(item)
    return enriched
