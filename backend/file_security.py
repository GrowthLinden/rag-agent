"""Helpers for safe file names, upload paths, and Milvus string literals."""
from __future__ import annotations

import os
import re
import unicodedata
from hashlib import sha1
from pathlib import Path


ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def sanitize_upload_filename(filename: str, allowed_extensions: set[str] | None = None) -> str:
    """Return a storage-safe filename while preserving readable names where possible."""
    allowed = allowed_extensions or ALLOWED_DOCUMENT_EXTENSIONS
    raw_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    raw_name = unicodedata.normalize("NFKC", raw_name)
    if not raw_name:
        raise ValueError("文件名不能为空")

    stem, suffix = os.path.splitext(raw_name)
    suffix = suffix.lower()
    if suffix not in allowed:
        raise ValueError("仅支持 PDF、Word 和 Excel 文档")

    safe_stem = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", stem, flags=re.UNICODE).strip("._")
    if not safe_stem:
        safe_stem = "document"

    safe_name = f"{safe_stem[:120]}{suffix}"
    if safe_name != raw_name:
        digest = sha1(raw_name.encode("utf-8")).hexdigest()[:8]
        safe_name = f"{safe_stem[:100]}_{digest}{suffix}"
    return safe_name


def sanitize_asset_filename(
    filename: str,
    allowed_extensions: set[str],
    *,
    default_stem: str = "asset",
    error_message: str = "文件类型不支持",
) -> str:
    raw_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    raw_name = unicodedata.normalize("NFKC", raw_name)
    if not raw_name:
        raise ValueError("文件名不能为空")

    stem, suffix = os.path.splitext(raw_name)
    suffix = suffix.lower()
    if suffix not in allowed_extensions:
        raise ValueError(error_message)

    safe_stem = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "_", stem, flags=re.UNICODE).strip("._")
    return f"{(safe_stem or default_stem)[:80]}{suffix}"


def resolve_upload_target(upload_dir: Path, filename: str) -> tuple[str, Path]:
    """Return a sanitized filename and an absolute target path inside upload_dir."""
    safe_filename = sanitize_upload_filename(filename)
    root = upload_dir.resolve()
    target = (root / safe_filename).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("文件路径不合法") from exc
    return safe_filename, target


def validate_upload_size(content: bytes, max_bytes: int) -> None:
    if len(content) > max_bytes:
        max_mb = max_bytes / 1024 / 1024
        raise ValueError(f"文件过大，最大允许 {max_mb:.0f}MB")


def milvus_string_literal(value: str) -> str:
    """Escape a Python string for use as a Milvus double-quoted string literal."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
