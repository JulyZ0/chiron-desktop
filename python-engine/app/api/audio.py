"""本地语音转写（``POST /v1/audio/transcribe``）。

为什么需要这个端点：对话页此前的"语音输入"走浏览器的 ``webkitSpeechRecognition``。
它的构造器在 Chromium 里存在，但**识别后端是 Google 的在线语音服务**，而 Electron
的官方构建不含该服务的密钥 —— 于是桌面包里 ``start()`` 看似成功、随后 ``onerror``
报 ``network`` / ``service-not-allowed``，用户看到的是"进入录音态然后失败"。

本端点改成**引擎内离线转写**：渲染进程用 ``MediaRecorder`` 录一段音频，经网关上传
到这里，由 ``faster-whisper``（CTranslate2 后端）在本机解码并转写。音频**不出这台
机器**，也不产生第三方 API 费用。

三个刻意的取舍：

1. **依赖延迟导入**。``faster_whisper`` 不在引擎的必需依赖里（它会带入 ctranslate2 /
   onnxruntime 等较大的原生包）。未安装时本模块**仍然可以导入**，端点如实返回 503
   与原因，而不是让整个引擎在 import 期就崩掉。
2. **模型懒加载 + 单并发**。首次调用要下载/加载模型（数百 MB、数秒到数十秒），放在
   线程里做，避免阻塞事件循环；转写是 CPU 密集的，并发跑只会互相拖慢，所以用一个
   ``Semaphore(1)`` 排队。
3. **只接受一段完整音频**。本端点不做流式识别：一次请求 = 一段录音，返回最终文本。
   流式需要 VAD 分段与增量解码，复杂度与收益不成比例（对话输入框一次说一两句）。

认证不在这里实现：``AuthMiddleware``（``app/middleware/auth.py``）已把网关代理路径
（``X-Internal-Token`` + ``?user_id=&tenant_id=``）与（可选）JWT 直连收口。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import io
import logging
import threading
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audio"])

#: 单次音频体积上限。25 MB ≈ Whisper 常见的单文件上限，按 opus 32 kbps 也够录十几个
#: 分钟。超过即 413：截断转写比报错更糟 —— 用户会拿到"少了后半句"的文本却不知道。
MAX_AUDIO_BYTES = 25 * 1024 * 1024

#: 读上传体时的分块大小。**必须分块**：``await file.read()`` 会把任意大的 body 一次
#: 读进内存，攻击者一个请求就能把引擎的内存吃满。
_READ_CHUNK = 1 * 1024 * 1024

#: ``faster_whisper`` 的导入名。用 ``importlib`` 而不是 ``import`` 语句：
#: mypy 的 strict 门禁会对可选依赖报 ``import-not-found``，而这里的语义本来就是
#: "装了就用、没装就如实报不可用"。
_MODULE_NAME = "faster_whisper"

#: 进程内已加载的模型。``faster-whisper`` 的 ``WhisperModel`` 不是线程安全的封装对象，
#: 但它的 ``transcribe`` 返回惰性迭代器 —— 必须**在同一段同步代码里**把段全部取完，
#: 所以下面整个过程都放在线程里，并用 ``_MODEL_LOCK`` 串行化加载。
_model: Any = None
_model_name = ""
_model_lock = threading.Lock()

#: 转写排队：CPU 转写并发跑只会互相抢核，串行反而总耗时更短。
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """惰性构造转写信号量。

    刻意不在模块顶层构造：``asyncio.Semaphore`` 在 Python 3.10+ 虽不再强绑定事件循环，
    但模块级对象会在测试里跨 ``event_loop`` 复用（pytest-asyncio 每个用例一个新循环），
    惰性构造让每个循环拿到自己的实例。
    """
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(1)
    return _semaphore


def _module_missing_reason() -> str:
    """返回不可用原因；``""`` 表示依赖齐备。

    ``find_spec`` 只查"能不能 import"，不做真实导入 —— 真实导入在第一次转写时才发生，
    这样"引擎能起来"与"语音能用"是两件事，前者不该被后者拖死。
    """
    if not settings.speech_local_enabled:
        return "本地语音转写已被 SPEECH_LOCAL_ENABLED 关闭"
    if importlib.util.find_spec(_MODULE_NAME) is None:
        return (
            "引擎环境缺少 faster-whisper（本地语音转写依赖）。"
            "请在引擎 venv 中安装 requirements.txt 所声明的依赖后重启引擎。"
        )
    return ""


def _load_model() -> Any:
    """加载（并缓存）Whisper 模型。**必须在线程里调用**。

    模型目录：``SPEECH_LOCAL_MODEL_DIR`` 为空时交给 faster-whisper 自己的默认缓存
    （HuggingFace cache）。桌面壳会把它指到 ``<dataDir>/models`` —— 打包态的引擎源码
    目录是只读的，默认值落到那里会静默失败。
    """
    global _model, _model_name
    wanted = (settings.speech_local_model or "small").strip() or "small"
    with _model_lock:
        if _model is not None and _model_name == wanted:
            return _model
        module = importlib.import_module(_MODULE_NAME)
        model_dir = (settings.speech_local_model_dir or "").strip()
        kwargs: dict[str, Any] = {
            "device": (settings.speech_local_device or "cpu").strip() or "cpu",
            "compute_type": (settings.speech_local_compute_type or "int8").strip() or "int8",
        }
        if model_dir:
            kwargs["download_root"] = model_dir
        logger.info(
            "loading local speech model: model=%s device=%s compute_type=%s",
            wanted,
            kwargs["device"],
            kwargs["compute_type"],
        )
        _model = module.WhisperModel(wanted, **kwargs)
        _model_name = wanted
        return _model


def _transcribe_sync(data: bytes, language: str) -> dict[str, Any]:
    """在线程里跑的同步转写。返回 ``{text, language, duration}``。

    ``vad_filter=True``：录音前后常有一大段静音，没有 VAD 时 Whisper 会把静音也解码一遍
    （纯浪费），偶尔还会在静音上"幻听"出字幕组署名之类的文本。
    """
    model = _load_model()
    segments, info = model.transcribe(
        io.BytesIO(data),
        language=(language or "").strip() or None,
        vad_filter=True,
        beam_size=5,
    )
    # 惰性迭代器：必须在本次调用内取完（生成器持有模型状态）。
    text = "".join(seg.text for seg in segments).strip()
    return {
        "text": text,
        "language": getattr(info, "language", "") or "",
        "duration": float(getattr(info, "duration", 0.0) or 0.0),
    }


async def _read_limited(file: UploadFile) -> bytes:
    """分块读取上传音频并封顶。超限抛 413（读到这里已经收下的部分直接丢弃）。"""
    buf = bytearray()
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_AUDIO_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"音频过大（上限 {MAX_AUDIO_BYTES // (1024 * 1024)} MB），请缩短录音后重试",
            )
    return bytes(buf)


@router.get("/v1/audio/availability")
async def availability() -> dict[str, Any]:
    """语音转写是否可用（前端据此决定按钮是否可点、并显示原因）。

    只探测依赖与开关，**不加载模型** —— 这个端点会被界面打开时调用一次，加载模型
    要下载数百 MB，不该被一次"看看能不能用"触发。
    """
    reason = _module_missing_reason()
    return {
        "available": not reason,
        "reason": reason,
        "model": (settings.speech_local_model or "small").strip() or "small",
        "loaded": _model is not None,
    }


@router.post("/v1/audio/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form(""),
) -> dict[str, Any]:
    """把一段音频转写成文本。

    ``language`` 为空时由模型自动判定；前端在"跟随界面语言"时传 ``zh`` / ``en``
    之类的短码，可以避免短句被误判成别的语种。
    """
    reason = _module_missing_reason()
    if reason:
        # 503 而不是 500：这是"本机环境没装/关了这个能力"，可操作且与请求无关。
        raise HTTPException(status_code=503, detail=reason)

    data = await _read_limited(file)
    if not data:
        raise HTTPException(status_code=400, detail="音频为空")

    try:
        async with _get_semaphore():
            result = await asyncio.to_thread(_transcribe_sync, data, language)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — 解码/模型/磁盘错误统一转成可读的 500
        # 异常原文可能带本机路径，只进日志；响应体给固定说明。
        logger.error("local speech transcription failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=500,
            detail="音频解码或转写失败（详见引擎日志）",
        ) from exc

    # 只记元信息：音频长度、字节数、识别语言。**不记转写文本** —— 那是用户说的话。
    logger.info(
        "local speech transcribed: bytes=%d duration=%.1fs language=%s chars=%d",
        len(data),
        result["duration"],
        result["language"],
        len(result["text"]),
    )
    return result
