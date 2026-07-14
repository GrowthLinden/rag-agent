import os
import logging
import dashscope
from dashscope.audio.asr import Recognition
from dashscope.audio.tts import SpeechSynthesizer


logger = logging.getLogger(__name__)

def get_api_key():
    return os.getenv("ALIYUN_API_KEY") or os.getenv("EMBEDDING_API_KEY", "")

def speech_to_text(audio_file_path: str) -> str:
    """
    使用阿里云 DashScope 语音识别（Paraformer）将音频转为文本
    """
    api_key = get_api_key()
    if not api_key:
        raise ValueError("缺少 ALIYUN_API_KEY 环境变量，请在 .env 文件中配置以使用语音服务。")
        
    dashscope.api_key = api_key
    
    try:
        recognition = Recognition(model='paraformer-realtime-v1',
                                  format='wav',
                                  sample_rate=16000,
                                  callback=None)
        
        # 阿里云 SDK 中 call 接受本地文件路径
        result = recognition.call(audio_file_path)
        
        if result.status_code == 200:
            sentences = result.get_sentence()
            if sentences:
                # sentence format is [{'text': 'hello', ...}]
                return "".join([s.get('text', '') for s in sentences])
        else:
            logger.warning("ASR error: %s - %s", result.code, result.message)
            raise RuntimeError(f"语音识别失败: {result.message}")
    except Exception:
        logger.exception("DashScope ASR exception")
        raise
        
    return ""

def text_to_speech(text: str, output_file_path: str) -> bool:
    """
    使用阿里云 DashScope 语音合成（Sambert）将文本转为语音文件
    """
    api_key = get_api_key()
    if not api_key:
        raise ValueError("缺少 ALIYUN_API_KEY 环境变量，请在 .env 文件中配置以使用语音服务。")
        
    dashscope.api_key = api_key
    
    try:
        result = SpeechSynthesizer.call(model='sambert-zhichu-v1',
                                        text=text,
                                        sample_rate=16000,
                                        format='wav')
        if result.get_audio_data() is not None:
            with open(output_file_path, 'wb') as f:
                f.write(result.get_audio_data())
            return True
        else:
            logger.warning("TTS error: %s", result.get_response().message)
            return False
    except Exception:
        logger.exception("DashScope TTS exception")
        raise
