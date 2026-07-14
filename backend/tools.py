from typing import Optional
import base64
import hashlib
import json
import os
import re
import requests
from dotenv import load_dotenv
from langchain_core.tools import tool
from product_service import search_product_answer

load_dotenv()


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


AMAP_WEATHER_API = os.getenv("AMAP_WEATHER_API")
AMAP_API_KEY = os.getenv("AMAP_API_KEY")
AMAP_DISTRICT_API = os.getenv("AMAP_DISTRICT_API", "https://restapi.amap.com/v3/config/district")
OPEN_METEO_GEOCODING_API = os.getenv("OPEN_METEO_GEOCODING_API", "https://geocoding-api.open-meteo.com/v1/search")
OPEN_METEO_FORECAST_API = os.getenv("OPEN_METEO_FORECAST_API", "https://api.open-meteo.com/v1/forecast")
ALIYUN_LOGISTICS_API = os.getenv("ALIYUN_LOGISTICS_API", "https://wuliu.market.alicloudapi.com/kdi")
KDNIAO_LOGISTICS_API = os.getenv("KDNIAO_LOGISTICS_API", "https://api.kdniao.com/Ebusiness/EbusinessOrderHandle.aspx")
KUAIDI100_QUERY_API = os.getenv("KUAIDI100_QUERY_API", "https://poll.kuaidi100.com/poll/query.do")
KUAIDI100_AUTO_API = os.getenv("KUAIDI100_AUTO_API", "https://www.kuaidi100.com/autonumber/auto")
LOGISTICS_TIMEOUT_SECONDS = _float_env("LOGISTICS_TIMEOUT_SECONDS", 10.0)
WEATHER_TIMEOUT_SECONDS = _float_env("WEATHER_TIMEOUT_SECONDS", 10.0)

_LAST_RAG_CONTEXT = None
_KNOWLEDGE_TOOL_CALLS_THIS_TURN = 0
_RAG_STEP_QUEUE = None  # asyncio.Queue, set by agent before streaming
_RAG_STEP_LOOP = None   # asyncio loop, captured when setting queue

_LOGISTICS_KEYWORDS = (
    "物流", "快递", "包裹", "运单", "单号", "货运", "配送", "派送", "签收", "到哪", "到哪里",
    "发货", "查件", "查一下", "追踪", "tracking", "shipment", "delivery", "express", "package",
)

_TRACKING_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9][A-Za-z0-9\-\s]{5,40}[A-Za-z0-9])(?![A-Za-z0-9])")
_PHONE_HINT_RE = re.compile(r"(?:手机|电话|手机号|收件人|寄件人|尾号|后四位|phone)\D*(\d{4,11})", re.IGNORECASE)

_CARRIER_ALIASES = {
    "sf": "shunfeng",
    "顺丰": "shunfeng",
    "顺丰速运": "shunfeng",
    "顺丰快递": "shunfeng",
    "ems": "ems",
    "邮政": "youzhengguonei",
    "邮政快递": "youzhengguonei",
    "邮政包裹": "youzhengguonei",
    "中国邮政": "youzhengguonei",
    "圆通": "yuantong",
    "圆通速递": "yuantong",
    "yt": "yuantong",
    "中通": "zhongtong",
    "中通快递": "zhongtong",
    "zt": "zhongtong",
    "申通": "shentong",
    "申通快递": "shentong",
    "韵达": "yunda",
    "韵达快递": "yunda",
    "yd": "yunda",
    "百世": "huitongkuaidi",
    "百世快递": "huitongkuaidi",
    "百世汇通": "huitongkuaidi",
    "德邦": "debangwuliu",
    "德邦物流": "debangwuliu",
    "京东": "jd",
    "京东物流": "jd",
    "jd": "jd",
    "极兔": "jtexpress",
    "极兔速递": "jtexpress",
    "j&t": "jtexpress",
    "jt": "jtexpress",
    "宅急送": "zhaijisong",
    "天天": "tiantian",
    "天天快递": "tiantian",
    "安能": "annengwuliu",
    "安能物流": "annengwuliu",
    "优速": "youshuwuliu",
    "优速快递": "youshuwuliu",
    "跨越": "kuayue",
    "跨越速运": "kuayue",
}

_CARRIER_NAMES = {
    "shunfeng": "顺丰速运",
    "ems": "EMS",
    "youzhengguonei": "邮政包裹",
    "yuantong": "圆通速递",
    "zhongtong": "中通快递",
    "shentong": "申通快递",
    "yunda": "韵达快递",
    "huitongkuaidi": "百世快递",
    "debangwuliu": "德邦物流",
    "jd": "京东物流",
    "jtexpress": "极兔速递",
    "zhaijisong": "宅急送",
    "tiantian": "天天快递",
    "annengwuliu": "安能物流",
    "youshuwuliu": "优速快递",
    "kuayue": "跨越速运",
}

_KDNIAO_CARRIER_CODES = {
    "shunfeng": "SF",
    "ems": "EMS",
    "youzhengguonei": "YZPY",
    "yuantong": "YTO",
    "zhongtong": "ZTO",
    "shentong": "STO",
    "yunda": "YD",
    "huitongkuaidi": "HTKY",
    "debangwuliu": "DBL",
    "jd": "JD",
    "jtexpress": "JTSD",
    "zhaijisong": "ZJS",
    "tiantian": "HHTT",
    "annengwuliu": "ANE",
    "youshuwuliu": "UC",
    "kuayue": "KYSY",
}

_KDNIAO_CODE_TO_CARRIER = {value: key for key, value in _KDNIAO_CARRIER_CODES.items()}

_ALIYUN_STATUS_MAP = {
    "1": "在途中",
    "2": "正在派件",
    "3": "已签收",
    "4": "派送失败",
    "5": "疑难件",
    "6": "退件签收",
}

_KUAIDI100_STATUS_MAP = {
    "0": "在途",
    "1": "揽收",
    "2": "疑难",
    "3": "已签收",
    "4": "退签",
    "5": "派件",
    "6": "退回",
    "7": "转投",
    "8": "清关",
    "10": "待清关",
    "11": "清关中",
    "12": "已清关",
    "13": "清关异常",
    "14": "拒签",
}

_KDNIAO_STATUS_MAP = {
    "0": "暂无轨迹",
    "1": "已揽收",
    "2": "在途中",
    "3": "已签收",
    "4": "问题件",
    "5": "转寄",
    "6": "清关",
    "7": "拒签",
    "8": "派件中",
    "10": "待清关",
    "11": "清关中",
    "12": "已清关",
    "13": "清关异常",
    "14": "收件人拒签",
}

_WMO_WEATHER_MAP = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    56: "冻毛毛雨",
    57: "较强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "较强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "较强阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴强冰雹",
}


def _provider_result(ok: bool, message: str, *, missing_config: bool = False) -> dict:
    return {"ok": ok, "message": message, "missing_config": missing_config}


def _request_error_summary(error: requests.exceptions.RequestException) -> str:
    if isinstance(error, requests.exceptions.Timeout):
        return "请求超时"
    if isinstance(error, requests.exceptions.SSLError):
        return "网络/TLS 连接失败"
    if isinstance(error, requests.exceptions.ConnectionError):
        return "网络连接失败"
    response = getattr(error, "response", None)
    if response is not None:
        return f"HTTP {response.status_code}"
    return "请求失败"


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


def _weather_provider_order() -> list[str]:
    configured = os.getenv("WEATHER_PROVIDER", "openmeteo,amap")
    providers = []
    for item in configured.split(","):
        provider = item.strip().lower().replace("-", "")
        if provider in ("amap", "openmeteo") and provider not in providers:
            providers.append(provider)
    return providers or ["amap", "openmeteo"]


def _weather_code_text(code) -> str:
    try:
        return _WMO_WEATHER_MAP.get(int(code), f"天气代码 {code}")
    except (TypeError, ValueError):
        return "未知"


def _query_amap_weather(location: str, extensions: str) -> dict:
    if not AMAP_WEATHER_API or not AMAP_API_KEY:
        return _provider_result(False, "未配置 AMAP_WEATHER_API 或 AMAP_API_KEY", missing_config=True)

    resolved_location = _resolve_location_to_adcode(location)
    params = {
        "key": AMAP_API_KEY,
        "city": resolved_location,
        "extensions": extensions,
        "output": "json",
    }

    try:
        resp = requests.get(AMAP_WEATHER_API, params=params, timeout=WEATHER_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "1":
            return _provider_result(False, f"查询失败：{data.get('info', '未知错误')}")

        if extensions == "base":
            lives = data.get("lives", [])
            if not lives:
                return _provider_result(False, f"未查询到 {location} 的实时天气数据")
            w = lives[0]
            return _provider_result(
                True,
                (
                    f"【{w.get('city', location)} 实时天气】\n"
                    f"天气状况：{w.get('weather', '未知')}\n"
                    f"温度：{w.get('temperature', '未知')}℃\n"
                    f"湿度：{w.get('humidity', '未知')}%\n"
                    f"风向：{w.get('winddirection', '未知')}\n"
                    f"风力：{w.get('windpower', '未知')}级\n"
                    f"更新时间：{w.get('reporttime', '未知')}\n"
                    f"数据来源：高德天气"
                ),
            )

        forecasts = data.get("forecasts", [])
        if not forecasts:
            return _provider_result(False, f"未查询到 {location} 的天气预报数据")
        f0 = forecasts[0]
        casts = f0.get("casts") or []
        if not casts:
            return _provider_result(False, f"未查询到 {location} 的天气预报数据")

        out = [
            f"【{f0.get('city', location)} 天气预报】",
            f"更新时间：{f0.get('reporttime', '未知')}",
            "数据来源：高德天气",
            "",
        ]
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
        return _provider_result(True, "\n".join(out))
    except requests.exceptions.RequestException as e:
        return _provider_result(False, _request_error_summary(e))
    except Exception as e:
        return _provider_result(False, f"解析天气数据失败：{e}")


def _query_open_meteo_weather(location: str, extensions: str) -> dict:
    try:
        geo_resp = requests.get(
            OPEN_METEO_GEOCODING_API,
            params={"name": location, "count": 1, "language": "zh", "format": "json"},
            headers={"User-Agent": "SuperService/1.0"},
            timeout=WEATHER_TIMEOUT_SECONDS,
        )
        geo_resp.raise_for_status()
        geo_data = geo_resp.json()
        results = geo_data.get("results") or []
        if not results:
            return _provider_result(False, f"未识别到地点：{location}")

        place = results[0]
        place_name = place.get("name") or location
        admin1 = place.get("admin1") or ""
        country = place.get("country") or ""
        display_name = "，".join(item for item in [country, admin1, place_name] if item)

        weather_resp = requests.get(
            OPEN_METEO_FORECAST_API,
            params={
                "latitude": place.get("latitude"),
                "longitude": place.get("longitude"),
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": place.get("timezone") or "Asia/Shanghai",
                "forecast_days": 4,
            },
            headers={"User-Agent": "SuperService/1.0"},
            timeout=WEATHER_TIMEOUT_SECONDS,
        )
        weather_resp.raise_for_status()
        data = weather_resp.json()

        current = data.get("current") or {}
        if extensions == "base":
            return _provider_result(
                True,
                (
                    f"【{display_name} 实时天气】\n"
                    f"天气状况：{_weather_code_text(current.get('weather_code'))}\n"
                    f"温度：{current.get('temperature_2m', '未知')}℃\n"
                    f"湿度：{current.get('relative_humidity_2m', '未知')}%\n"
                    f"风速：{current.get('wind_speed_10m', '未知')} km/h\n"
                    f"风向：{current.get('wind_direction_10m', '未知')}°\n"
                    f"更新时间：{current.get('time', '未知')}\n"
                    f"数据来源：Open-Meteo"
                ),
            )

        daily = data.get("daily") or {}
        dates = daily.get("time") or []
        codes = daily.get("weather_code") or []
        max_temps = daily.get("temperature_2m_max") or []
        min_temps = daily.get("temperature_2m_min") or []
        rain_probs = daily.get("precipitation_probability_max") or []
        if not dates:
            return _provider_result(False, f"未查询到 {location} 的天气预报数据")

        out = [
            f"【{display_name} 天气预报】",
            f"更新时间：{current.get('time', '未知')}",
            "数据来源：Open-Meteo",
            "",
        ]
        labels = ["今天", "明天", "后天", "第4天"]
        for idx, date in enumerate(dates[:4]):
            label = labels[idx] if idx < len(labels) else f"第{idx + 1}天"
            out += [
                f"{label}：",
                f"  日期：{date}",
                f"  天气：{_weather_code_text(codes[idx] if idx < len(codes) else None)}",
                f"  气温：{min_temps[idx] if idx < len(min_temps) else '未知'}~{max_temps[idx] if idx < len(max_temps) else '未知'}℃",
                f"  降水概率：{rain_probs[idx] if idx < len(rain_probs) else '未知'}%",
                "",
            ]
        return _provider_result(True, "\n".join(out))
    except requests.exceptions.RequestException as e:
        return _provider_result(False, _request_error_summary(e))
    except Exception as e:
        return _provider_result(False, f"解析天气数据失败：{e}")


@tool("get_current_weather")
def get_current_weather(location: str, extensions: Optional[str] = "all") -> str:
    """获取指定城市的天气信息或天气预报。location 支持中文城市名，如杭州、北京；默认返回预报。"""
    if not location:
        return "location参数不能为空"
    if extensions not in ("base", "all"):
        return "extensions参数错误，请输入base或all"

    errors = []
    for provider in _weather_provider_order():
        if provider == "amap":
            result = _query_amap_weather(location, extensions)
            provider_name = "高德天气"
        else:
            result = _query_open_meteo_weather(location, extensions)
            provider_name = "Open-Meteo"

        if result["ok"]:
            return result["message"]
        if not result.get("missing_config"):
            errors.append(f"{provider_name}: {result['message']}")

    if errors:
        return "天气查询暂时不可用，已尝试以下服务：\n" + "\n".join(f"- {item}" for item in errors)
    return "天气查询服务未配置，也无法使用备用天气服务"


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
        image_url = result.get("image_url") or ""
        image_caption = result.get("image_caption") or ""
        visual_description = result.get("visual_description") or ""
        images = result.get("images") or []

        extra = []
        if image_url:
            extra.append(f"Image URL: {image_url}")
        if image_caption:
            extra.append(f"Image Caption: {image_caption}")
        if visual_description:
            extra.append(f"Visual Description: {visual_description}")
        for image in images:
            extra.append(
                "Related Image: "
                f"{image.get('url', '')} "
                f"caption={image.get('caption', '')} "
                f"description={image.get('visual_description', '')}"
            )

        extra_text = "\n" + "\n".join(extra) if extra else ""
        formatted.append(f"[{i}] {source} (Page {page}):\n{text}{extra_text}")

    return "Retrieved Chunks:\n" + "\n\n---\n\n".join(formatted)


@tool("search_product_catalog")
def search_product_catalog(query: str) -> str:
    """查询商家商品库。当用户询问商品名称、类别、价格、库存、材质、规格或商品图片时优先使用。"""
    answer = search_product_answer(query)
    if answer:
        return answer
    return "商品库中没有找到匹配的商品。"

def normalize_tracking_number(value: str) -> str:
    """Normalize a user-entered tracking number while keeping carrier prefixes."""
    if not value:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()


def _is_plausible_tracking_number(value: str) -> bool:
    if not value or not 6 <= len(value) <= 32:
        return False
    if not re.fullmatch(r"[A-Z0-9]+", value):
        return False
    return any(char.isdigit() for char in value)


def _looks_like_standalone_tracking_text(text: str, tracking_number: str) -> bool:
    compact_text = normalize_tracking_number(text)
    if compact_text != tracking_number:
        return False
    if re.fullmatch(r"1[3-9]\d{9}", tracking_number):
        return False
    if not tracking_number.isdigit():
        return True
    return len(tracking_number) >= 10


def _normalize_company_code(company: Optional[str]) -> Optional[str]:
    if not company:
        return None
    cleaned = re.sub(r"\s+", "", str(company).strip()).lower()
    if not cleaned:
        return None
    if cleaned in _CARRIER_ALIASES:
        return _CARRIER_ALIASES[cleaned]
    if cleaned in _CARRIER_NAMES:
        return cleaned
    return cleaned


def _detect_company_code(text: str) -> Optional[str]:
    compact_text = re.sub(r"\s+", "", text or "").lower()
    for alias, code in sorted(_CARRIER_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias and alias.lower() in compact_text:
            return code
    return None


def _detect_phone_hint(text: str) -> Optional[str]:
    match = _PHONE_HINT_RE.search(text or "")
    if not match:
        return None
    return match.group(1)


def extract_logistics_request(text: str) -> Optional[dict]:
    """Extract a likely logistics lookup from free-form user text."""
    if not text:
        return None

    has_keyword = any(keyword.lower() in text.lower() for keyword in _LOGISTICS_KEYWORDS)
    candidates = []
    seen = set()
    for match in _TRACKING_TOKEN_RE.finditer(text):
        tracking_number = normalize_tracking_number(match.group(1))
        if _is_plausible_tracking_number(tracking_number) and tracking_number not in seen:
            seen.add(tracking_number)
            candidates.append(tracking_number)

    if not candidates:
        return None

    tracking_number = candidates[0]
    company_code = _detect_company_code(text)
    if not has_keyword and not company_code and not _looks_like_standalone_tracking_text(text, tracking_number):
        return None

    return {
        "tracking_number": tracking_number,
        "company": company_code,
        "phone": _detect_phone_hint(text),
    }


def _logistics_provider_order() -> list[str]:
    configured = os.getenv("LOGISTICS_PROVIDER", "kdniao,aliyun,kuaidi100")
    providers = []
    for item in configured.split(","):
        provider = item.strip().lower()
        if provider in ("kdniao", "aliyun", "kuaidi100") and provider not in providers:
            providers.append(provider)
    return providers or ["kdniao", "aliyun", "kuaidi100"]


def _format_trace_line(trace: dict) -> str:
    time_text = trace.get("time") or trace.get("ftime") or trace.get("date") or trace.get("AcceptTime") or ""
    status_text = (
        trace.get("status")
        or trace.get("context")
        or trace.get("desc")
        or trace.get("AcceptStation")
        or trace.get("Remark")
        or ""
    )
    location = trace.get("location") or trace.get("areaName") or trace.get("Location") or ""
    if location and location not in status_text:
        status_text = f"{status_text}（{location}）" if status_text else location
    return f"- {time_text}: {status_text}".rstrip(": ")


def _format_logistics_result(
    *,
    provider_name: str,
    tracking_number: str,
    company_code: Optional[str],
    company_name: Optional[str],
    status_desc: str,
    traces: list[dict],
    extra_lines: Optional[list[str]] = None,
) -> str:
    display_company = company_name or _CARRIER_NAMES.get(company_code or "", company_code or "未知物流")
    out = [
        f"【{display_company} 物流查询结果】",
        f"单号: {tracking_number}",
        f"当前状态: {status_desc}",
        f"数据来源: {provider_name}",
        "",
        "轨迹追踪:",
    ]

    if not traces:
        out.append("- 暂无轨迹信息")
    else:
        for trace in traces[:20]:
            line = _format_trace_line(trace)
            if line:
                out.append(line)

    if extra_lines:
        out.append("")
        out.extend(extra_lines)

    return "\n".join(out)


def _kdniao_sign(request_data: str, app_key: str) -> str:
    digest = hashlib.md5((request_data + app_key).encode("utf-8")).hexdigest()
    return base64.b64encode(digest.encode("utf-8")).decode("utf-8")


def _kdniao_request(request_type: str, request_data: dict, ebusiness_id: str, app_key: str) -> dict:
    request_text = json.dumps(request_data, ensure_ascii=False, separators=(",", ":"))
    resp = requests.post(
        KDNIAO_LOGISTICS_API,
        data={
            "RequestData": request_text,
            "EBusinessID": ebusiness_id,
            "RequestType": request_type,
            "DataSign": _kdniao_sign(request_text, app_key),
            "DataType": "2",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        timeout=LOGISTICS_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()


def _kdniao_detect_company(tracking_number: str, ebusiness_id: str, app_key: str) -> tuple[Optional[str], Optional[str]]:
    try:
        data = _kdniao_request("2002", {"LogisticCode": tracking_number}, ebusiness_id, app_key)
        shippers = data.get("Shippers") or data.get("Shipper") or []
        if isinstance(shippers, list) and shippers:
            first = shippers[0]
            return first.get("ShipperCode"), first.get("ShipperName")
    except Exception:
        return None, None
    return None, None


def _query_kdniao_logistics(tracking_number: str, company_code: Optional[str], phone: Optional[str]) -> dict:
    ebusiness_id = (os.getenv("KDNIAO_EBUSINESS_ID") or "").strip()
    app_key = (os.getenv("KDNIAO_API_KEY") or "").strip()
    request_type = (os.getenv("KDNIAO_REQUEST_TYPE") or "8002").strip() or "8002"
    if not ebusiness_id or not app_key:
        return _provider_result(False, "未配置 KDNIAO_EBUSINESS_ID 或 KDNIAO_API_KEY", missing_config=True)

    shipper_code = _KDNIAO_CARRIER_CODES.get(company_code or "")
    shipper_name = _CARRIER_NAMES.get(company_code or "")
    if request_type == "1002" and not shipper_code:
        shipper_code, shipper_name = _kdniao_detect_company(tracking_number, ebusiness_id, app_key)
    if request_type == "1002" and not shipper_code:
        return _provider_result(False, "无法识别快递公司，请补充快递公司名称")

    request_data = {
        "LogisticCode": tracking_number,
    }
    if shipper_code:
        request_data["OrderCode"] = ""
        request_data["ShipperCode"] = shipper_code
    if phone:
        request_data["CustomerName"] = phone

    try:
        data = _kdniao_request(request_type, request_data, ebusiness_id, app_key)
        if not data.get("Success"):
            reason = data.get("Reason") or data.get("ReasonDesc") or "未知错误"
            if (shipper_code or data.get("ShipperCode")) == "SF" and not phone:
                reason += "。顺丰查询通常需要收/寄件人手机号或后四位"
            return _provider_result(False, f"查询失败：{reason}")

        state = str(data.get("State", ""))
        status_desc = _KDNIAO_STATUS_MAP.get(state, f"未知状态({state})")
        traces = data.get("Traces") or []
        response_shipper_code = data.get("ShipperCode") or shipper_code
        response_company_code = company_code or _KDNIAO_CODE_TO_CARRIER.get(response_shipper_code or "")
        response_shipper_name = shipper_name or _CARRIER_NAMES.get(response_company_code or "") or response_shipper_code
        return _provider_result(
            True,
            _format_logistics_result(
                provider_name="快递鸟",
                tracking_number=tracking_number,
                company_code=response_company_code,
                company_name=response_shipper_name,
                status_desc=status_desc,
                traces=traces,
            ),
        )
    except requests.exceptions.RequestException as e:
        return _provider_result(False, _request_error_summary(e))
    except Exception as e:
        return _provider_result(False, f"解析物流数据失败：{e}")


def _query_aliyun_logistics(tracking_number: str, company_code: Optional[str]) -> dict:
    appcode = os.getenv("ALIYUN_LOGISTICS_APPCODE")
    if not appcode:
        return _provider_result(False, "未配置 ALIYUN_LOGISTICS_APPCODE", missing_config=True)

    params = {"no": tracking_number}
    if company_code:
        params["type"] = company_code

    headers = {"Authorization": f"APPCODE {appcode}"}

    try:
        resp = requests.get(ALIYUN_LOGISTICS_API, params=params, headers=headers, timeout=LOGISTICS_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()

        if str(data.get("status")) != "0":
            message = data.get("msg") or data.get("message") or data.get("desc") or "未知错误"
            return _provider_result(False, f"查询失败：{message}")

        result = data.get("result") or {}
        deliverystatus = str(result.get("deliverystatus", ""))
        status_desc = _ALIYUN_STATUS_MAP.get(deliverystatus, f"未知状态({deliverystatus})")
        company_name = result.get("expName") or _CARRIER_NAMES.get(company_code or "")
        traces = result.get("list") or []

        return _provider_result(
            True,
            _format_logistics_result(
                provider_name="阿里云云市场",
                tracking_number=tracking_number,
                company_code=company_code,
                company_name=company_name,
                status_desc=status_desc,
                traces=traces,
            ),
        )
    except requests.exceptions.RequestException as e:
        return _provider_result(False, _request_error_summary(e))
    except Exception as e:
        return _provider_result(False, f"解析物流数据失败：{e}")


def _kuaidi100_detect_company(tracking_number: str) -> Optional[str]:
    key = os.getenv("KUAIDI100_KEY")
    if not key:
        return None
    try:
        resp = requests.get(KUAIDI100_AUTO_API, params={"num": tracking_number, "key": key}, timeout=LOGISTICS_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0].get("comCode")
    except Exception:
        return None
    return None


def _query_kuaidi100_logistics(tracking_number: str, company_code: Optional[str], phone: Optional[str]) -> dict:
    key = os.getenv("KUAIDI100_KEY")
    customer = os.getenv("KUAIDI100_CUSTOMER")
    if not key or not customer:
        return _provider_result(False, "未配置 KUAIDI100_KEY 或 KUAIDI100_CUSTOMER", missing_config=True)

    company_code = company_code or _kuaidi100_detect_company(tracking_number)
    if not company_code:
        return _provider_result(False, "无法识别快递公司，请补充快递公司名称")

    param = {
        "com": company_code,
        "num": tracking_number,
        "resultv2": "4",
        "show": "0",
        "order": "desc",
        "lang": "zh",
    }
    if phone:
        param["phone"] = phone

    param_text = json.dumps(param, ensure_ascii=False, separators=(",", ":"))
    sign = hashlib.md5((param_text + key + customer).encode("utf-8")).hexdigest().upper()

    try:
        resp = requests.post(
            KUAIDI100_QUERY_API,
            data={"customer": customer, "sign": sign, "param": param_text},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=LOGISTICS_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()

        if str(data.get("status")) != "200":
            message = data.get("message") or data.get("returnCode") or "未知错误"
            if company_code == "shunfeng" and not phone:
                message += "。顺丰查询通常需要收/寄件人手机号或后四位"
            return _provider_result(False, f"查询失败：{message}")

        state = str(data.get("state", ""))
        status_desc = _KUAIDI100_STATUS_MAP.get(state, f"未知状态({state})")
        traces = data.get("data") or []
        extra_lines = []
        if data.get("arrivalTime"):
            extra_lines.append(f"预计到达: {data.get('arrivalTime')}")
        if data.get("routeInfo", {}).get("cur", {}).get("name"):
            extra_lines.append(f"当前位置: {data['routeInfo']['cur']['name']}")

        return _provider_result(
            True,
            _format_logistics_result(
                provider_name="快递100",
                tracking_number=tracking_number,
                company_code=company_code,
                company_name=_CARRIER_NAMES.get(company_code),
                status_desc=status_desc,
                traces=traces,
                extra_lines=extra_lines,
            ),
        )
    except requests.exceptions.RequestException as e:
        return _provider_result(False, _request_error_summary(e))
    except Exception as e:
        return _provider_result(False, f"解析物流数据失败：{e}")


def _query_logistics_impl(tracking_number: str, company: Optional[str] = None, phone: Optional[str] = None) -> str:
    tracking_number = normalize_tracking_number(tracking_number)
    if not tracking_number:
        return "错误：必须提供物流单号"
    if not _is_plausible_tracking_number(tracking_number):
        return "错误：物流单号格式不正确，请提供 6-32 位字母或数字组成的单号"

    company_code = _normalize_company_code(company)
    errors = []
    missing_configs = 0

    for provider in _logistics_provider_order():
        if provider == "kdniao":
            result = _query_kdniao_logistics(tracking_number, company_code, phone)
            provider_name = "快递鸟"
        elif provider == "aliyun":
            result = _query_aliyun_logistics(tracking_number, company_code)
            provider_name = "阿里云云市场"
        else:
            result = _query_kuaidi100_logistics(tracking_number, company_code, phone)
            provider_name = "快递100"

        if result["ok"]:
            return result["message"]

        if result.get("missing_config"):
            missing_configs += 1
        errors.append(f"{provider_name}: {result['message']}")

    if missing_configs == len(errors):
        return (
            "错误：物流查询服务未配置。请在 .env 中配置 KDNIAO_EBUSINESS_ID/KDNIAO_API_KEY，"
            "或配置 ALIYUN_LOGISTICS_APPCODE、KUAIDI100_KEY/KUAIDI100_CUSTOMER。"
        )

    return "物流查询暂时不可用，已尝试以下服务：\n" + "\n".join(f"- {item}" for item in errors)


@tool("query_logistics")
def query_logistics(tracking_number: str, company: Optional[str] = None, phone: Optional[str] = None) -> str:
    """查询企业物流轨迹状态。当用户询问包裹到哪里了，或者查询快递单号时使用。
    参数:
    - tracking_number: 快递单号或包裹跟踪号 (必填)
    - company: 物流公司名称或编码，如圆通、顺丰、yuantong、shunfeng (可选)
    - phone: 收/寄件人手机号或后四位；顺丰等快递可能必填 (可选)
    """
    return _query_logistics_impl(tracking_number, company=company, phone=phone)
