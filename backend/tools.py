from typing import Optional
import os
import requests
from dotenv import load_dotenv
try:
    from langchain_core.tools import tool
except ImportError:
    from langchain_core.tools import tool

load_dotenv()

AMAP_WEATHER_API = os.getenv("AMAP_WEATHER_API")
AMAP_API_KEY = os.getenv("AMAP_API_KEY")
AMAP_DISTRICT_API = os.getenv("AMAP_DISTRICT_API", "https://restapi.amap.com/v3/config/district")

_LAST_RAG_CONTEXT = None
_KNOWLEDGE_TOOL_CALLS_THIS_TURN = 0
_RAG_STEP_QUEUE = None  # asyncio.Queue, set by agent before streaming
_RAG_STEP_LOOP = None   # asyncio loop, captured when setting queue


def _set_last_rag_context(context: dict):
    global _LAST_RAG_CONTEXT
    _LAST_RAG_CONTEXT = context


def get_last_rag_context(clear: bool = True) -> Optional[dict]:
    """获取最近一次 RAG 检索上下文，默认读取后清空。"""
    global _LAST_RAG_CONTEXT
    context = _LAST_RAG_CONTEXT
    if clear:
        _LAST_RAG_CONTEXT = None
    return context


def reset_tool_call_guards():
    """每轮对话开始时重置工具调用计数。"""
    global _KNOWLEDGE_TOOL_CALLS_THIS_TURN
    _KNOWLEDGE_TOOL_CALLS_THIS_TURN = 0


def set_rag_step_queue(queue):
    """设置 RAG 步骤队列，并捕获当前事件循环以便跨线程调度。"""
    global _RAG_STEP_QUEUE, _RAG_STEP_LOOP
    _RAG_STEP_QUEUE = queue
    if queue:
        import asyncio
        try:
            _RAG_STEP_LOOP = asyncio.get_running_loop()
        except RuntimeError:
            _RAG_STEP_LOOP = asyncio.get_event_loop()
    else:
        _RAG_STEP_LOOP = None


def emit_rag_step(icon: str, label: str, detail: str = ""):
    """向队列发送一个 RAG 检索步骤。支持跨线程安全调用。"""
    global _RAG_STEP_QUEUE, _RAG_STEP_LOOP
    if _RAG_STEP_QUEUE is not None and _RAG_STEP_LOOP is not None:
        step = {"icon": icon, "label": label, "detail": detail}
        try:
            if not _RAG_STEP_LOOP.is_closed():
                _RAG_STEP_LOOP.call_soon_threadsafe(_RAG_STEP_QUEUE.put_nowait, step)
        except Exception:
            pass


def _resolve_location_to_adcode(location: str) -> str:
    """将自然语言地名解析为高德天气接口更稳定支持的 adcode。"""
    if not location:
        return location

    # 已经是纯数字 adcode 时直接返回
    if location.isdigit():
        return location

    if not AMAP_DISTRICT_API or not AMAP_API_KEY:
        return location

    candidates = [location]

    # 复合地名优先尝试最细一级行政区，如“泰州市靖江市” -> “靖江市”
    suffix_tokens = ["自治区", "特别行政区", "自治州", "地区", "盟", "省", "市", "区", "县", "旗"]
    for token in suffix_tokens:
        idx = location.rfind(token)
        if idx != -1 and idx + len(token) < len(location):
            tail = location[idx + len(token):].strip()
            if tail:
                candidates.append(tail)

    # 再尝试从后往前截取，提升“杭州市余杭区”“北京市海淀区”命中率
    for i in range(1, len(location)):
        tail = location[i:].strip()
        if len(tail) >= 2:
            candidates.append(tail)

    # 去重并保序
    deduped = []
    seen = set()
    for item in candidates:
        if item not in seen:
            seen.add(item)
            deduped.append(item)

    for candidate in deduped:
        try:
            resp = requests.get(
                AMAP_DISTRICT_API,
                params={
                    "key": AMAP_API_KEY,
                    "keywords": candidate,
                    "subdistrict": 0,
                    "extensions": "base",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            districts = data.get("districts") or []
            if districts and districts[0].get("adcode"):
                return districts[0]["adcode"]
        except Exception:
            pass

    return location


@tool("get_current_weather")
def get_current_weather(location: str, extensions: Optional[str] = "all") -> str:
    """获取指定城市的天气信息或天气预报。location 支持中文城市名，如杭州、北京；默认返回预报。"""
    if not location:
        return "location参数不能为空"
    if extensions not in ("base", "all"):
        return "extensions参数错误，请输入base或all"

    if not AMAP_WEATHER_API or not AMAP_API_KEY:
        return "天气服务未配置（缺少 AMAP_WEATHER_API 或 AMAP_API_KEY）"

    resolved_location = _resolve_location_to_adcode(location)

    params = {
        "key": AMAP_API_KEY,
        "city": resolved_location,
        "extensions": extensions,
        "output": "json",
    }

    try:
        resp = requests.get(AMAP_WEATHER_API, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "1":
            return f"查询失败：{data.get('info', '未知错误')}"

        if extensions == "base":
            lives = data.get("lives", [])
            if not lives:
                return f"未查询到 {location} 的天气数据"
            w = lives[0]
            return (
                f"【{w.get('city', location)} 实时天气】\n"
                f"天气状况：{w.get('weather', '未知')}\n"
                f"温度：{w.get('temperature', '未知')}℃\n"
                f"湿度：{w.get('humidity', '未知')}%\n"
                f"风向：{w.get('winddirection', '未知')}\n"
                f"风力：{w.get('windpower', '未知')}级\n"
                f"更新时间：{w.get('reporttime', '未知')}"
            )

        forecasts = data.get("forecasts", [])
        if not forecasts:
            return f"未查询到 {location} 的天气预报数据"
        f0 = forecasts[0]
        casts = f0.get("casts") or []
        if not casts:
            return f"未查询到 {location} 的天气预报数据"

        out = [f"【{f0.get('city', location)} 天气预报】", f"更新时间：{f0.get('reporttime', '未知')}", ""]

        labels = ["今天", "明天", "后天", "第4天"]
        for idx, cast in enumerate(casts[:4]):
            label = labels[idx] if idx < len(labels) else cast.get("date", f"第{idx + 1}天")
            out += [
                f"{label}：",
                f"  日期：{cast.get('date', '未知')}",
                f"  白天：{cast.get('dayweather', '未知')}",
                f"  夜间：{cast.get('nightweather', '未知')}",
                f"  气温：{cast.get('nighttemp', '未知')}~{cast.get('daytemp', '未知')}℃",
                f"  风向：{cast.get('daywind', '未知')} / {cast.get('nightwind', '未知')}",
                f"  风力：{cast.get('daypower', '未知')} / {cast.get('nightpower', '未知')}",
                "",
            ]
        return "\n".join(out)

    except requests.exceptions.Timeout:
        return "错误：请求天气服务超时"
    except requests.exceptions.RequestException as e:
        return f"错误：天气服务请求失败 - {e}"
    except Exception as e:
        return f"错误：解析天气数据失败 - {e}"


@tool("search_knowledge_base")
def search_knowledge_base(query: str) -> str:
    """Search for information in the knowledge base using hybrid retrieval (dense + sparse vectors)."""
    # ... guards omitted ...
    global _KNOWLEDGE_TOOL_CALLS_THIS_TURN
    if _KNOWLEDGE_TOOL_CALLS_THIS_TURN >= 1:
        return (
            "TOOL_CALL_LIMIT_REACHED: search_knowledge_base has already been called once in this turn. "
            "Use the existing retrieval result and provide the final answer directly."
        )
    _KNOWLEDGE_TOOL_CALLS_THIS_TURN += 1

    from rag_pipeline import run_rag_graph

    # 在同步工具中获取当前的 Loop 可能不可靠，但我们之前是通过 call_soon_threadsafe 调度的。
    # 这里 _RAG_STEP_QUEUE 是在主线程/Loop 设置的全局变量。
    # 如果工具运行在线程池中，它是可以访问到全局变量 _RAG_STEP_QUEUE 的。
    # emit_rag_step 内部做了 try-except 和 get_event_loop()。

    # 问题可能出在 asyncio.get_event_loop() 在子线程中调用会报错或者拿不到主线程的loop。
    # 我们应该在 set_rag_step_queue 时也保存 loop 引用，或者在 emit_rag_step 中更健壮地获取 loop。

    rag_result = run_rag_graph(query)

    docs = rag_result.get("docs", []) if isinstance(rag_result, dict) else []
    rag_trace = rag_result.get("rag_trace", {}) if isinstance(rag_result, dict) else {}
    if rag_trace:
        _set_last_rag_context({"rag_trace": rag_trace})

    if not docs:
        return "No relevant documents found in the knowledge base."

    formatted = []
    for i, result in enumerate(docs, 1):
        source = result.get("filename", "Unknown")
        page = result.get("page_number", "N/A")
        text = result.get("text", "")
        formatted.append(f"[{i}] {source} (Page {page}):\n{text}")

    return "Retrieved Chunks:\n" + "\n\n---\n\n".join(formatted)
