"""Vision model helpers for describing extracted PDF images."""
import base64
import mimetypes
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()


class VisionService:
    """Call an OpenAI-compatible vision chat model to describe images."""

    def __init__(self):
        self.base_url = (os.getenv("VISION_BASE_URL") or "").rstrip("/")
        self.api_key = os.getenv("VISION_API_KEY") or ""
        self.model = os.getenv("VISION_MODEL") or ""
        self.timeout = int(os.getenv("VISION_TIMEOUT", "45"))

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @staticmethod
    def _data_url(image_path: str) -> str:
        path = Path(image_path)
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{payload}"

    def describe_image(self, image_path: str, hint: str = "") -> str:
        if not self.enabled:
            return ""

        prompt = (
            "请直接描述这张商品图片的可见外观，用中文回答。"
            "包含主体、颜色、形状、图案、材质感、文字或角色元素。"
            "不要编造看不见的信息，不要输出编号。"
        )
        if hint:
            prompt += f"\n已知商品名/线索：{hint}"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": self._data_url(image_path)},
                        },
                    ],
                }
            ],
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
