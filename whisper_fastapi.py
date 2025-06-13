import asyncio
from functools import partial
from contextlib import asynccontextmanager
import aiohttp
import os
import sys
import dataclasses
import faster_whisper
import json
from fastapi.responses import PlainTextResponse, StreamingResponse
import wave
from faster_whisper.vad import VadOptions
import pydub
import io
import hashlib
import argparse
import uvicorn
from typing import (
    Annotated,
    Any,
    BinaryIO,
    Literal,
    Generator,
    Optional,
    Tuple,
    Iterable,
    Union,
)
from fastapi import (
    File,
    HTTPException,
    Query,
    UploadFile,
    Form,
    FastAPI,
    Request,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from src.whisper_ctranslate2.writers import format_timestamp
from faster_whisper.transcribe import Segment, TranscriptionInfo
from faster_whisper.tokenizer import _LANGUAGE_CODES
import opencc
from prometheus_fastapi_instrumentator import Instrumentator
from wyoming.server import AsyncEventHandler, AsyncServer, partial
from wyoming.event import Event
from wyoming.audio import AudioChunk, AudioStop
from wyoming.asr import Transcribe, Transcript
from wyoming.info import Describe, Info
from wyoming.info import AsrModel, AsrProgram, Attribution, Info

# redirect print to stderr
_print = print


def print(*args, **kwargs):
    _print(*args, file=sys.stderr, **kwargs)


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="0.0.0.0", type=str)
parser.add_argument("--wyoming-uri", default="tcp://0.0.0.0:3001", type=str)
parser.add_argument("--port", default=5000, type=int)
parser.add_argument("--model", default="large-v3", type=str)
parser.add_argument("--device", default="auto", type=str)
parser.add_argument("--cache_dir", default=None, type=str)
parser.add_argument("--local_files_only", default=False, type=bool)
parser.add_argument("--threads", default=4, type=int)
args = parser.parse_args()


# for home assistant wyoming server
@asynccontextmanager
async def lifespan(_: FastAPI):
    server = AsyncServer.from_uri(args.wyoming_uri)
    print(f"Running wyoming server at {args.wyoming_uri}")
    asyncio.create_task(server.run(partial(Handler)))
    yield


app = FastAPI(lifespan=lifespan)

# Instrument your app with default metrics and expose the metrics
Instrumentator().instrument(app).expose(app, endpoint="/konele/metrics")
ccc = opencc.OpenCC("t2s.json")

print(f"Loading model to device {args.device}...")
model = faster_whisper.WhisperModel(
    model_size_or_path=args.model,
    device=args.device,
    cpu_threads=args.threads,
    local_files_only=args.local_files_only,
)
print(f"Model loaded to device {model.model.device}")


# allow all cors
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def gpt_refine_text(
    ge: Generator[Segment, None, None], info: TranscriptionInfo, context: str
) -> str:
    text = build_json_result(ge, info).text.strip()
    model = os.environ.get("OPENAI_LLM_MODEL", "gpt-4o-mini")
    if not text:
        return ""
    body: dict = {
        "model": model,
        "temperature": 0.1,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": f"""
You are a audio transcription text refiner. You may refer to the context to correct the transcription text. 
Your task is to correct the transcribed text by removing redundant and repetitive words, resolving any contradictions, and fixing punctuation errors.
Keep my spoken language as it is, and do not change my speaking style. Only fix the text.
Response directly with the text.
                    """.strip(),
            },
            {
                "role": "user",
                "content": f"""
context: {context}
---
transcription: {text}
                     """.strip(),
            },
        ],
    }
    print(f"Refining text length: {len(text)} with {model}")
    print(body)
    async with aiohttp.ClientSession() as session:
        async with session.post(
            os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            + "/chat/completions",
            json=body,
            headers={
                "Authorization": f'Bearer {os.environ["OPENAI_API_KEY"]}',
            },
        ) as response:
            return (await response.json())["choices"][0]["message"]["content"]


def stream_writer(generator: Generator[Segment, Any, None]):
    for segment in generator:
        yield "data: " + json.dumps(segment, ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"


def text_writer(generator: Generator[Segment, Any, None]):
    for segment in generator:
        yield segment.text.strip() + "\n"


def tsv_writer(generator: Generator[Segment, Any, None]):
    yield "start\tend\ttext\n"
    for i, segment in enumerate(generator):
        start_time = str(round(1000 * segment.start))
        end_time = str(round(1000 * segment.end))
        text = segment.text.strip()
        yield f"{start_time}\t{end_time}\t{text}\n"


def srt_writer(generator: Generator[Segment, Any, None]):
    for i, segment in enumerate(generator):
        start_time = format_timestamp(
            segment.start, decimal_marker=",", always_include_hours=True
        )
        end_time = format_timestamp(
            segment.end, decimal_marker=",", always_include_hours=True
        )
        text = segment.text.strip()
        yield f"{i}\n{start_time} --> {end_time}\n{text}\n\n"


def vtt_writer(generator: Generator[Segment, Any, None]):
    yield "WEBVTT\n\n"
    for _, segment in enumerate(generator):
        start_time = format_timestamp(segment.start)
        end_time = format_timestamp(segment.end)
        text = segment.text.strip()
        yield f"{start_time} --> {end_time}\n{text}\n\n"


@dataclasses.dataclass
class JsonResult(TranscriptionInfo):
    segments: list[Segment]
    text: str


def build_json_result(
    generator: Iterable[Segment],
    info: TranscriptionInfo,
) -> JsonResult:
    segments = [i for i in generator]
    return JsonResult(
        text="\n".join(i.text for i in segments),
        segments=segments,
        **dataclasses.asdict(info),
    )


def stream_builder(
    audio: BinaryIO,
    task: str,
    vad_filter: bool,
    language: str | None,
    initial_prompt: str = "",
    repetition_penalty: float = 1.0,
    vad_options: Optional[VadOptions] = None,
) -> Tuple[Generator[Segment, None, None], TranscriptionInfo]:
    segments, info = model.transcribe(
        audio=audio,
        language=language,
        task=task,
        vad_filter=vad_filter,
        initial_prompt=initial_prompt if initial_prompt else None,
        word_timestamps=True,
        repetition_penalty=repetition_penalty,
        vad_parameters=vad_options,
    )
    print(
        "Detected language '%s' with probability %f"
        % (info.language, info.language_probability)
    )

    def wrap():
        for segment in segments:
            if info.language == "zh":
                segment.text = ccc.convert(segment.text)
            yield segment

    return wrap(), info


@app.websocket("/k6nele/status")
@app.websocket("/konele/status")
@app.websocket("/v1/k6nele/status")
@app.websocket("/v1/konele/status")
async def konele_status(
    websocket: WebSocket,
):
    await websocket.accept()
    await websocket.send_json(dict(num_workers_available=1))
    await websocket.close()


@app.websocket("/k6nele/ws")
@app.websocket("/konele/ws")
@app.websocket("/konele/ws/gpt_refine")
@app.websocket("/k6nele/ws/gpt_refine")
@app.websocket("/v1/k6nele/ws")
@app.websocket("/v1/konele/ws")
@app.websocket("/v1/konele/ws/gpt_refine")
@app.websocket("/v1/k6nele/ws/gpt_refine")
async def konele_ws(
    websocket: WebSocket,
    task: Literal["transcribe", "translate"] = "transcribe",
    lang: str = "und",
    initial_prompt: str = "",
    vad_filter: bool = False,
    content_type: Annotated[str, Query(alias="content-type")] = "audio/x-raw",
):
    await websocket.accept()

    # convert lang code format (eg. en-US to en)
    lang = lang.split("-")[0]

    data = b""
    while True:
        try:
            data += await websocket.receive_bytes()
            if data[-3:] == b"EOS":
                break
        except:
            break

    md5 = hashlib.md5(data).hexdigest()

    # create fake file for wave.open
    file_obj = io.BytesIO()

    if content_type.startswith("audio/x-flac"):
        pydub.AudioSegment.from_file(io.BytesIO(data), format="flac").export(
            file_obj, format="wav"
        )
    else:
        buffer = wave.open(file_obj, "wb")
        buffer.setnchannels(1)
        buffer.setsampwidth(2)
        buffer.setframerate(16000)
        buffer.writeframes(data)

    file_obj.seek(0)

    generator, info = stream_builder(
        audio=file_obj,
        task=task,
        vad_filter=vad_filter,
        language=None if lang == "und" else lang,
        initial_prompt=initial_prompt,
    )

    if websocket.url.path.endswith("gpt_refine"):
        result = await gpt_refine_text(generator, info, initial_prompt)
    else:
        result = build_json_result(generator, info).text

    await websocket.send_json(
        {
            "status": 0,
            "segment": 0,
            "result": {"hypotheses": [{"transcript": result}], "final": True},
            "id": md5,
        }
    )
    await websocket.close()


@app.post("/k6nele/post")
@app.post("/konele/post")
@app.post("/k6nele/post/gpt_refine")
@app.post("/konele/post/gpt_refine")
@app.post("/v1/k6nele/post")
@app.post("/v1/konele/post")
@app.post("/v1/k6nele/post/gpt_refine")
@app.post("/v1/konele/post/gpt_refine")
async def translateapi(
    request: Request,
    task: Literal["transcribe", "translate"] = "transcribe",
    lang: str = "und",
    initial_prompt: str = "",
    vad_filter: bool = False,
):
    content_type = request.headers.get("Content-Type", "")

    # convert lang code format (eg. en-US to en)
    lang = lang.split("-")[0]

    splited = [i.strip() for i in content_type.split(",") if "=" in i]
    info = {k: v for k, v in (i.split("=") for i in splited)}

    channels = int(info.get("channels", "1"))
    rate = int(info.get("rate", "16000"))

    body = await request.body()
    md5 = hashlib.md5(body).hexdigest()

    # create fake file for wave.open
    file_obj = io.BytesIO()

    if content_type.startswith("audio/x-flac"):
        pydub.AudioSegment.from_file(io.BytesIO(body), format="flac").export(
            file_obj, format="wav"
        )
    else:
        buffer = wave.open(file_obj, "wb")
        buffer.setnchannels(channels)
        buffer.setsampwidth(2)
        buffer.setframerate(rate)
        buffer.writeframes(body)

    file_obj.seek(0)

    generator, info = stream_builder(
        audio=file_obj,
        task=task,
        vad_filter=vad_filter,
        language=None if lang == "und" else lang,
        initial_prompt=initial_prompt,
    )

    if request.url.path.endswith("gpt_refine"):
        result = await gpt_refine_text(generator, info, initial_prompt)
    else:
        result = build_json_result(generator, info).text

    return {
        "status": 0,
        "hypotheses": [{"utterance": result}],
        "id": md5,
    }


@app.post("/v1/audio/transcriptions", response_model=Union[JsonResult, str])
@app.post("/v1/audio/translations", response_model=Union[JsonResult, str])
async def transcription(
    request: Request,
    file: UploadFile = File(...),
    prompt: str = Form(""),
    response_format: str = Form("json"),
    task: str = Form(""),
    language: str = Form("und"),
    vad_filter: bool = Form(False),
    vad_threshold: Optional[float] = Form(None),
    vad_neg_threshold: Optional[float] = Form(None),
    vad_min_speech_duration_ms: Optional[int] = Form(None),
    vad_max_speech_duration_s: Optional[float] = Form(None),
    vad_min_silence_duration_ms: Optional[int] = Form(None),
    vad_speech_pad_ms: Optional[int] = Form(None),
    repetition_penalty: float = Form(1.0),
    gpt_refine: bool = Form(False),
):
    """Transcription endpoint

    User upload audio file in multipart/form-data format and receive transcription in response
    """

    if not task:
        if request.url.path == "/v1/audio/transcriptions":
            task = "transcribe"
        elif request.url.path == "/v1/audio/translations":
            task = "translate"
        else:
            raise HTTPException(400, "task parameter is required")

    vad_options = VadOptions()
    if vad_threshold is not None:
        vad_options.threshold = vad_threshold
    if vad_neg_threshold is not None:
        vad_options.neg_threshold = vad_neg_threshold
    if vad_min_speech_duration_ms is not None:
        vad_options.min_speech_duration_ms = vad_min_speech_duration_ms
    if vad_max_speech_duration_s is not None:
        vad_options.max_speech_duration_s = vad_max_speech_duration_s
    if vad_min_silence_duration_ms is not None:
        vad_options.min_silence_duration_ms = vad_min_silence_duration_ms
    if vad_speech_pad_ms is not None:
        vad_options.speech_pad_ms = vad_speech_pad_ms

    # timestamp as filename, keep original extension
    generator, info = stream_builder(
        audio=io.BytesIO(file.file.read()),
        task=task,
        vad_filter=vad_filter,
        initial_prompt=prompt,
        language=None if language == "und" else language,
        repetition_penalty=repetition_penalty,
        vad_options=vad_options,
    )

    # special function for streaming response (OpenAI API does not have this)
    if response_format == "stream":
        return StreamingResponse(
            stream_writer(generator),
            media_type="text/event-stream",
        )
    elif response_format == "json":
        return build_json_result(generator, info)
    elif response_format == "text":
        if gpt_refine:
            return PlainTextResponse(await gpt_refine_text(generator, info, prompt))
        return StreamingResponse(text_writer(generator), media_type="text/plain")
    elif response_format == "tsv":
        return StreamingResponse(tsv_writer(generator), media_type="text/plain")
    elif response_format == "srt":
        return StreamingResponse(srt_writer(generator), media_type="text/plain")
    elif response_format == "vtt":
        return StreamingResponse(vtt_writer(generator), media_type="text/plain")

    raise HTTPException(400, "Invailed response_format")


# for home assitant
# code from https://github.com/rhasspy/wyoming-faster-whisper
class Handler(AsyncEventHandler):
    file_obj: io.BytesIO | None = None
    wav_file: wave.Wave_write | None = None
    lang: str | None = None

    async def handle_event(self, event: Event) -> bool:
        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)

            if self.wav_file is None:
                print("AudioChunk begin")
                self.file_obj = io.BytesIO()
                self.wav_file = wave.open(self.file_obj, "wb")
                self.wav_file.setframerate(chunk.rate)
                self.wav_file.setsampwidth(chunk.width)
                self.wav_file.setnchannels(chunk.channels)

            self.wav_file.writeframes(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            print("AudioStop")
            assert self.wav_file is not None
            assert self.file_obj is not None
            self.wav_file.close()
            self.wav_file = None
            self.file_obj.seek(0)

            generator, info = stream_builder(
                audio=self.file_obj,
                task="transcribe",
                vad_filter=False,
                language=self.lang,
            )
            text = build_json_result(generator, info).text
            print(text)
            await self.write_event(Transcript(text=text).event())

            self.lang = None
            return False

        if Transcribe.is_type(event.type):
            print("Transcribe")
            transcribe = Transcribe.from_event(event)
            if transcribe.language:
                self.lang = transcribe.language

            return True

        if Describe.is_type(event.type):
            print("Describe")
            await self.write_event(
                Info(
                    asr=[
                        AsrProgram(
                            name="whisper-forward",
                            description="Whisper forward to OpenAI API endpoint",
                            attribution=Attribution(
                                name="heimoshuiyu",
                                url="https://github.com/heimoshuiyu/whisper-fastapi",
                            ),
                            installed=True,
                            version="0.1",
                            models=[
                                AsrModel(
                                    name="whisper-1",
                                    description="whisper-1",
                                    attribution=Attribution(
                                        name="Systran",
                                        url="https://huggingface.co/Systran",
                                    ),
                                    installed=True,
                                    languages=list(
                                        _LANGUAGE_CODES
                                    ),  # pylint: disable=protected-access
                                    version="0.1",
                                )
                            ],
                        )
                    ],
                ).event()
            )
            return True

        return True


uvicorn.run(app, host=args.host, port=args.port)
