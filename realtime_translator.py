"""
회의 음성 → 실시간 번역 자막 (macOS / Windows)
시스템 오디오(mac: BlackHole, win: WASAPI 루프백) → OpenAI STT → Claude 번역 → 항상 위 자막창
설정은 같은 폴더의 .env 파일에서 읽음 (.env.example 참고)
"""
import io
import os, re
import queue
import sys
import threading
import time
import wave
from collections import deque
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=os.path.join(HERE, ".env")):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

import numpy as np
import tkinter as tk
from anthropic import Anthropic
from openai import OpenAI

INPUT_DEVICE = os.getenv("CAPTION_DEVICE", "BlackHole")  # 입력 장치 이름 일부
SAMPLE_RATE = 16000
# MODE=quality(기본): 문장 끝까지 기다렸다 상위 모델로 번역 / MODE=fast: 빠르지만 문장이 잘게 끊김
MODE = os.getenv("MODE", "quality").strip().lower()
_Q = MODE != "fast"
MAX_CHUNK_SEC = float(os.getenv("CHUNK_SEC", "8" if _Q else "4"))      # 이 길이를 넘으면 무조건 전송
MIN_SPEECH_SEC = 0.6    # 이보다 짧은 소리는 버림
PAUSE_SEC = float(os.getenv("PAUSE_SEC", "0.6" if _Q else "0.35"))    # 이만큼 조용하면 문장 끝으로 보고 전송
CONTEXT_N = int(os.getenv("CONTEXT_N", "4" if _Q else "2"))           # 번역할 때 참고할 직전 발화 수
EXTRA_SAVE_LANGS = [x.strip() for x in os.getenv("EXTRA_SAVE_LANGS", "").split(",") if x.strip()]  # 종료 시 추가 저장할 언어. 예: 日本語,English
# IT·통신 업계에서 외래어로 쓰는 용어는 그대로 (예: license → 허가 X, 라이센스 O). 사용자 GLOSSARY 가 우선
TECH_GLOSSARY = ("license=라이센스, licensing=라이센싱, subscription=구독, server=서버, cloud=클라우드, "
                 "on-premise=온프레미스, platform=플랫폼, solution=솔루션, deployment=구축, "
                 "integration=연동, migration=마이그레이션, contact center=컨택센터, seat=시트, "
                 "SIP trunk=SIP 트렁크, pipeline=파이프라인, partner=파트너, renewal=갱신")
GLOSSARY = ", ".join(x for x in (os.getenv("GLOSSARY", "").strip(), TECH_GLOSSARY) if x)
# 음성 인식이 제품명을 일반 단어로 잘못 듣는 경우 교정 (my collab → MiCollab 등)
TERM_FIXES = [(re.compile(r"\b(?:my|mi|me)[\s-]?collab\b", re.I), "MiCollab"),
              (re.compile(r"\bmy[\s-]?voice (business|connect|mx-?one)\b", re.I), lambda m: "MiVoice " + m.group(1).title().replace("Mx-One", "MX-ONE").replace("Mxone", "MX-ONE")),
              (re.compile(r"\b(?:my|mi)[\s-]?contact center\b", re.I), "MiContact Center"),
              (re.compile(r"\bmi[\s-]?cloud\b", re.I), "MiCloud"),
              (re.compile(r"\bmy[\s-]tel\b", re.I), "Mitel")]
def fix_terms(t):
    for pat, rep in TERM_FIXES:
        t = pat.sub(rep, t)
    return t

# 번역하지 않고 영어 그대로 둘 제품·회사명. 음성 인식에도 힌트로 전달해 철자를 맞춤
PRODUCT_TERMS = ", ".join(x for x in (os.getenv("PRODUCT_TERMS", "").strip(),
    "Mitel, MiCollab, MiVoice Business, MiVoice Connect, MiVoice MX-ONE, MX-ONE, MiContact Center, "
    "MiCloud, CloudLink, OpenScape, Unify, Microsoft Teams, Zoom, Webex, Avaya, Genesys, Five9") if x)
BLOCK_SEC = 0.05        # 소리 크기 판정 단위
SILENCE_RMS = float(os.getenv("SILENCE_RMS", "0.005"))       # 스피커: 이보다 조용하면 무음
MIC_SILENCE_RMS = float(os.getenv("MIC_SILENCE_RMS", "0.0012"))  # 마이크: 내장 마이크는 소리가 작아 기준을 낮춤
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-mini-transcribe")
# 앞에서부터 시도해 계정에서 쓸 수 있는 첫 모델을 사용
MODEL_CANDIDATES = ([os.getenv("TRANSLATE_MODEL")] if os.getenv("TRANSLATE_MODEL") else []) + (
    ["claude-sonnet-5-5", "claude-sonnet-5"] if _Q else []) + ["claude-haiku-4-5-20251001", "claude-haiku-4-5"]
TRANSLATE_MODEL = MODEL_CANDIDATES[0]
SOURCE_LANG = os.getenv("SOURCE_LANG", "").strip()    # STT 언어 코드. 빈값 = 자동 감지(영어·일본어 등)
TARGET_LANG = os.getenv("TARGET_LANG", "한국어")       # 번역 목표 언어 (자연어 이름)
REVERSE_LANG = os.getenv("REVERSE_LANG", "English")   # 화자가 이미 목표 언어로 말하면 이 언어로 번역 (빈값 = 그대로 표시)
MAX_LINES = 4           # 화면에 유지할 번역 항목 수
FONT = "Malgun Gothic" if sys.platform == "win32" else "Apple SD Gothic Neo"
KO_SIZE = int(os.getenv("KO_SIZE", "15"))   # 번역문 글자 크기
EN_SIZE = int(os.getenv("EN_SIZE", "11"))   # 원문 글자 크기
SAVE_DIR = os.getenv("CAPTION_SAVE_DIR", HERE)
CAPTURE_MIC = os.getenv("CAPTURE_MIC", "1") != "0"   # 내 마이크도 같이 인식 (0 이면 끔)

def setup_dialog():
    """API 키가 없으면 입력창을 띄워 .env 를 만든다. 취소하면 종료."""
    import webbrowser
    win = tk.Tk()
    win.title("실시간번역기 처음 설정")
    win.resizable(False, False)
    pad = {"padx": 12, "pady": 4}
    tk.Label(win, text="처음 한 번만 입력합니다. 키는 이 폴더의 .env 파일에만 저장됩니다.",
             justify="left").grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 8))

    fields = [
        ("OpenAI API 키 (음성 인식)", "OPENAI_API_KEY", "", "https://platform.openai.com/api-keys"),
        ("Anthropic API 키 (번역)", "ANTHROPIC_API_KEY", "", "https://console.anthropic.com/settings/keys"),
        ("화자 언어 (비우면 영어·일본어 등 자동 감지)", "SOURCE_LANG", "", None),
        ("자막 언어", "TARGET_LANG", "한국어", None),
    ]
    def paste_into(e):
        try:
            e.delete(0, "end")
            e.insert(0, win.clipboard_get().strip())
        except tk.TclError:
            msg.config(text="클립보드가 비어 있습니다")
        return "break"

    entries = {}
    for i, (label, key, default, url) in enumerate(fields, start=1):
        tk.Label(win, text=label).grid(row=i, column=0, sticky="w", **pad)
        e = tk.Entry(win, width=52)
        e.insert(0, default)
        e.grid(row=i, column=1, **pad)
        for seq in ("<Command-v>", "<Control-v>", "<Button-2>", "<Button-3>"):
            e.bind(seq, lambda ev, e=e: paste_into(e))
        entries[key] = e
        if url:
            f = tk.Frame(win)
            f.grid(row=i, column=2, **pad)
            tk.Button(f, text="붙여넣기", command=lambda e=e: paste_into(e)).pack(side="left")
            tk.Button(f, text="키 발급 페이지", command=lambda u=url: webbrowser.open(u)
                      ).pack(side="left")

    msg = tk.Label(win, text="", fg="red")
    msg.grid(row=6, column=0, columnspan=3)
    done = {"ok": False}

    def save():
        vals = {k: e.get().strip() for k, e in entries.items()}
        if not vals["OPENAI_API_KEY"].startswith("sk-"):
            msg.config(text="OpenAI 키는 sk- 로 시작합니다"); return
        if not vals["ANTHROPIC_API_KEY"].startswith("sk-ant-"):
            msg.config(text="Anthropic 키는 sk-ant- 로 시작합니다"); return
        if not vals["TARGET_LANG"]:
            msg.config(text="자막 언어를 입력하세요"); return
        with open(os.path.join(HERE, ".env"), "w", encoding="utf-8") as f:
            for k, v in vals.items():
                f.write(f"{k}={v}\n")
        for k, v in vals.items():
            os.environ[k] = v
        done["ok"] = True
        win.destroy()

    tk.Button(win, text="저장하고 시작", command=save, width=16
              ).grid(row=7, column=0, columnspan=3, pady=(6, 14))
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.mainloop()
    if not done["ok"]:
        raise SystemExit("설정이 취소되었습니다.")


if not (os.getenv("OPENAI_API_KEY") and os.getenv("ANTHROPIC_API_KEY")):
    setup_dialog()
    SOURCE_LANG = os.getenv("SOURCE_LANG", "").strip()
    TARGET_LANG = os.getenv("TARGET_LANG", "한국어")
    REVERSE_LANG = os.getenv("REVERSE_LANG", "English")

LANG_CHOICES = ["한국어", "日本語", "English", "中文", "Español", "Français", "Deutsch"]


def set_env(key: str, val: str):
    """.env 의 한 항목만 바꾼다 (다른 줄·API 키는 그대로)."""
    path = os.path.join(HERE, ".env")
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={val}"
            break
    else:
        lines.append(f"{key}={val}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def choose_language():
    """시작할 때 자막 언어를 고르는 창. Enter = 지난번 설정으로 바로 시작."""
    global TARGET_LANG, REVERSE_LANG
    from tkinter import ttk
    win = tk.Tk()
    win.title("실시간번역기 - 언어 선택")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    pad = {"padx": 14, "pady": 6}
    tk.Label(win, text="자막 언어 (상대방 말을 이 언어로 보여줌)").grid(row=0, column=0, sticky="w", **pad)
    tgt = ttk.Combobox(win, values=LANG_CHOICES, width=18)
    tgt.set(TARGET_LANG)
    tgt.grid(row=0, column=1, **pad)
    tk.Label(win, text="자막 언어로 말할 때는 이 언어로").grid(row=1, column=0, sticky="w", **pad)
    rev = ttk.Combobox(win, values=["(번역 안 함)"] + LANG_CHOICES, width=18)
    rev.set(REVERSE_LANG or "(번역 안 함)")
    rev.grid(row=1, column=1, **pad)
    tk.Label(win, text="목록에 없으면 직접 입력 가능 (예: Tiếng Việt)", fg="gray"
             ).grid(row=2, column=0, columnspan=2, sticky="w", padx=14)
    started = {"ok": False}

    def start(_=None):
        global TARGET_LANG, REVERSE_LANG
        t = tgt.get().strip() or TARGET_LANG
        r = rev.get().strip()
        r = "" if r in ("", "(번역 안 함)") else r
        TARGET_LANG, REVERSE_LANG = t, r
        set_env("TARGET_LANG", t)          # 다음 실행 때 기본값으로
        set_env("REVERSE_LANG", r)
        started["ok"] = True
        win.destroy()

    tk.Button(win, text="시작 (Enter)", command=start, width=16
              ).grid(row=3, column=0, columnspan=2, pady=(10, 14))
    win.bind("<Return>", start)
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.after(100, lambda: (win.lift(), win.focus_force()))
    win.mainloop()
    if not started["ok"]:
        raise SystemExit("언어 선택이 취소되었습니다.")
    print(f"[언어] 자막={TARGET_LANG}  역방향={REVERSE_LANG or '없음'}", flush=True)


if os.getenv("ASK_LANG", "1") != "0":
    choose_language()

openai_client = OpenAI()      # OPENAI_API_KEY
claude = Anthropic()          # ANTHROPIC_API_KEY
audio_q: queue.Queue[np.ndarray] = queue.Queue()
stt_q: queue.Queue[tuple[int, str]] = queue.Queue()      # (id, 영어)
text_q: queue.Queue[tuple[int, str]] = queue.Queue()     # (id, 표시문)


def make_vad(tag="", threshold=None):
    """16kHz 모노 블록을 받아, 말이 끊기는 지점에서 발화 단위로 audio_q에 넣는 함수 반환. tag 는 화자 표시."""
    th = SILENCE_RMS if threshold is None else threshold
    buf = []            # 현재 발화 (말 시작 후 누적)
    speech_sec = 0.0
    silence_sec = 0.0

    def flush():
        nonlocal buf, speech_sec, silence_sec
        if speech_sec >= MIN_SPEECH_SEC:
            audio_q.put((np.concatenate(buf), tag))
        buf, speech_sec, silence_sec = [], 0.0, 0.0

    def feed(block: np.ndarray):
        nonlocal speech_sec, silence_sec
        loud = float(np.sqrt(np.mean(block ** 2))) >= th
        if loud:
            buf.append(block)
            speech_sec += BLOCK_SEC
            silence_sec = 0.0
        elif buf:                       # 말하다가 멈춘 구간
            buf.append(block)           # 끝소리 잘림 방지
            silence_sec += BLOCK_SEC
            if silence_sec >= PAUSE_SEC:
                flush()
        if speech_sec >= MAX_CHUNK_SEC:
            flush()

    return feed


def capture_mac():
    """BlackHole 가상 장치에서 입력."""
    import sounddevice as sd
    dev = None
    for i, d in enumerate(sd.query_devices()):
        if INPUT_DEVICE.lower() in d["name"].lower() and d["max_input_channels"] > 0:
            dev = i
            break
    if dev is None:
        raise RuntimeError(f"입력 장치 '{INPUT_DEVICE}' 없음. BlackHole 설치·설정 확인")
    print(f"[capture] device={dev} {sd.query_devices(dev)['name']}", flush=True)
    feed_spk = make_vad()
    feed_mic = make_vad("(나) ", MIC_SILENCE_RMS)
    bs = int(SAMPLE_RATE * BLOCK_SEC)
    streams = [sd.InputStream(device=dev, samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                              blocksize=bs, callback=lambda d, n, t, s: feed_spk(d[:, 0].copy()))]
    if CAPTURE_MIC:
        try:
            mic = sd.default.device[0]
            print(f"[capture] mic={mic} {sd.query_devices(mic)['name']}", flush=True)
            streams.append(sd.InputStream(device=mic, samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                          blocksize=bs, callback=lambda d, n, t, s: feed_mic(d[:, 0].copy())))
        except Exception as e:
            print(f"[capture] 마이크 사용 불가: {e}", flush=True)
    for st in streams:
        st.start()
    threading.Event().wait()


def capture_windows():
    """WASAPI 루프백으로 기본 스피커 출력을 직접 캡처 (가상 장치 불필요)."""
    import pyaudiowpatch as pyaudio
    p = pyaudio.PyAudio()
    dev = p.get_default_wasapi_loopback()
    rate = int(dev["defaultSampleRate"])
    ch = int(dev["maxInputChannels"])
    print(f"[capture] loopback {dev['name']} {rate}Hz {ch}ch", flush=True)
    n_out = int(SAMPLE_RATE * BLOCK_SEC)

    def open_stream(d, feed):
        r, c = int(d["defaultSampleRate"]), int(d["maxInputChannels"])

        def cb(data, frame_count, time_info, status):
            x = np.frombuffer(data, dtype=np.float32).reshape(-1, c).mean(axis=1)
            if r != SAMPLE_RATE:      # 16kHz로 리샘플
                x = np.interp(np.linspace(0, len(x), n_out, endpoint=False),
                              np.arange(len(x)), x).astype(np.float32)
            feed(x)
            return (None, pyaudio.paContinue)

        return p.open(format=pyaudio.paFloat32, channels=c, rate=r, input=True,
                      input_device_index=d["index"], frames_per_buffer=int(r * BLOCK_SEC),
                      stream_callback=cb)

    open_stream(dev, make_vad())
    if CAPTURE_MIC:
        try:
            mic = p.get_default_input_device_info()
            print(f"[capture] mic {mic['name']}", flush=True)
            open_stream(mic, make_vad("(나) ", MIC_SILENCE_RMS))
        except Exception as e:
            print(f"[capture] 마이크 사용 불가: {e}", flush=True)
    threading.Event().wait()


capture = capture_windows if sys.platform == "win32" else capture_mac


def to_wav_bytes(samples: np.ndarray) -> bytes:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    bio.seek(0)
    bio.name = "chunk.wav"
    return bio


STT_HINT = f"Business meeting about enterprise communications. Terms: {PRODUCT_TERMS}."


def transcribe(samples: np.ndarray) -> str:
    kwargs = {"language": SOURCE_LANG} if SOURCE_LANG else {}
    kwargs["prompt"] = STT_HINT
    r = openai_client.audio.transcriptions.create(
        model=STT_MODEL, file=to_wav_bytes(samples), **kwargs)
    t = r.text.strip()
    # 소리가 불분명하면 STT 가 힌트 문장을 그대로 돌려줌 → 버림
    if "Business meeting about enterprise" in t or "Terms: Mitel" in t or "MiVoice Connect, MiVoice MX-ONE" in t:
        return ""
    return fix_terms(t)


def relevant(items: str, text: str) -> str:
    """용어집·제품명 중 이번 문장(과 직전 문장)에 실제로 나온 것만 남겨 토큰 절약."""
    t = text.lower()
    return ", ".join(x.strip() for x in items.split(",")
                     if x.strip() and x.split("=")[0].strip().lower() in t)


# 맞장구·추임새는 번역 API 를 부르지 않고 그대로 표시
FILLER = re.compile(r"^(?:(?:ok(?:ay)?|yeah|yes|yep|right|sure|uh-?huh|mm+|hmm+|um+|uh+|ah+|oh|"
                    r"네|예|음+|아+|어+|그렇죠|맞아요|좋아요|はい|ええ|うん)[\s.,!?…]*)+$", re.I)


def build_system(history, en="") -> str:
    seen = en + " " + " ".join(src for src, _ in history)
    gloss, prods = relevant(GLOSSARY, seen), relevant(PRODUCT_TERMS, seen)
    rev = (f"If the new line is already in {TARGET_LANG}, translate it into {REVERSE_LANG} instead. "
           if REVERSE_LANG else f"If the new line is already in {TARGET_LANG}, output it unchanged. ")
    ctx = "\n".join(f"- {src}  =>  {tgt}" for src, tgt in history) or "(none)"
    gl = f"\nGlossary (always use these renderings): {gloss}" if gloss else ""
    return ("You are a professional simultaneous interpreter subtitling a live business meeting into "
            f"{TARGET_LANG}. " + rev +
            "The new line comes from speech recognition and may be a fragment of a longer sentence: "
            "use the recent conversation to resolve meaning, pronouns and terminology, and keep wording "
            "consistent with earlier translations. Translate meaning, not word-for-word; use natural spoken "
            f"business {TARGET_LANG}. Keep names, product names, numbers and units exact. "
            "For IT/telecom/enterprise-software terms, use the loanword professionals actually say "
            "(e.g. in Korean: license → 라이센스, not 허가), not a literal dictionary word. " +
            (f"Never translate or transliterate these product/company names, write them exactly in English: {prods}. " if prods else "") +
            "Output only the translation of the new line, no notes or quotes."
            f"\n\nRecent conversation (source => translation):\n{ctx}{gl}")


def translate_stream(en: str, history):
    """번역문을 글자가 나오는 대로 yield (누적 문자열)."""
    global TRANSLATE_MODEL
    acc = ""
    try:
        with claude.messages.stream(
                model=TRANSLATE_MODEL, max_tokens=400, system=build_system(history, en),
                messages=[{"role": "user", "content": en}]) as s:
            for piece in s.text_stream:
                acc += piece
                yield acc
    except Exception as e:
        # 계정에서 이 모델을 못 쓰면 다음 후보로 자동 전환
        i = MODEL_CANDIDATES.index(TRANSLATE_MODEL)
        if not acc and i + 1 < len(MODEL_CANDIDATES) and getattr(e, "status_code", None) in (400, 404):
            print(f"[번역] {TRANSLATE_MODEL} 사용 불가 → {MODEL_CANDIDATES[i + 1]} 로 전환", flush=True)
            TRANSLATE_MODEL = MODEL_CANDIDATES[i + 1]
            yield from translate_stream(en, history)
        else:
            raise


def stt_worker():
    seq = 0
    while True:
        samples, tag = audio_q.get()
        t0 = time.time()
        try:
            en = transcribe(samples)
            if not en:
                continue
            seq += 1
            print(f"[stt {seq}] {time.time()-t0:.2f}s  {tag}{en}", flush=True)
            text_q.put((seq, "…", tag + en))      # 원문 먼저 표시
            stt_q.put((seq, en, tag, time.time()))
        except Exception as e:
            print(f"[STT 오류] {e!r}", flush=True)
            text_q.put((0, f"[STT 오류] {e}", ""))


def translate_worker():
    history = deque(maxlen=CONTEXT_N)   # (원문, 번역) 최근 발화
    while True:
        seq, en, tag, t0 = stt_q.get()
        try:
            ko = ""
            first = None
            if FILLER.match(en.strip()):            # 맞장구는 번역 호출 없이 그대로
                text_q.put((seq, tag + en, tag + en))
                continue
            for ko in translate_stream(en, list(history)):
                if first is None:
                    first = time.time() - t0
                text_q.put((seq, tag + ko, tag + en))   # 부분 번역을 계속 갱신
            print(f"[ko {seq}] 첫글자 {first or 0:.2f}s 완료 {time.time()-t0:.2f}s  {ko}",
                  flush=True)
            history.append((en, ko))
        except Exception as e:
            print(f"[번역 오류] {e!r}", flush=True)
            text_q.put((seq, f"[번역 오류] {e}", tag + en))


def ui():
    root = tk.Tk()
    root.title("실시간번역기")
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.85)
    root.geometry("900x250+100+60")
    root.configure(bg="black")
    box = tk.Text(root, bg="black", fg="white", bd=0, highlightthickness=0,
                  wrap="word", cursor="arrow", padx=10, pady=8)
    box.tag_configure("ko", font=(FONT, KO_SIZE), foreground="white",
                      spacing1=4)
    box.tag_configure("en", font=(FONT, EN_SIZE), foreground="#9a9a9a",
                      lmargin1=14, lmargin2=14, spacing3=6)
    box.insert("end", "대기 중…", "ko")
    box.config(state="disabled")
    box.pack(fill="both", expand=True)
    lines: dict[int, tuple[str, str]] = {}   # seq → (번역, 원문), 삽입 순서 유지
    history: dict[int, str] = {}
    sources: dict[int, tuple[str, str]] = {}   # seq → (시각, 원문)  추가 언어 저장용
    err_seq = 0
    save_path = os.path.join(SAVE_DIR, f"실시간번역기_{datetime.now():%Y%m%d_%H%M}.txt")
    dirty = False

    def save():
        nonlocal dirty
        if not history:
            return
        with open(save_path, "w", encoding="utf-8") as f:
            f.write("\n".join(history[k] for k in sorted(history)) + "\n")
        dirty = False

    def autosave():           # 10초마다 저장 → 강제 종료돼도 자막 보존
        if dirty:
            save()
        root.after(10_000, autosave)

    def render():
        box.config(state="normal")
        box.delete("1.0", "end")
        for ko, en in lines.values():
            box.insert("end", ko + "\n", "ko")
            if en:
                box.insert("end", en + "\n", "en")
        box.config(state="disabled")
        box.see("end")

    def poll():
        nonlocal err_seq, dirty
        while not text_q.empty():
            seq, ko, en = text_q.get()
            if seq == 0:
                err_seq -= 1
                seq = err_seq
            lines[seq] = (ko, en)
            ts = sources[seq][0] if seq in sources else f"{datetime.now():%H:%M:%S}"
            if seq > 0 and en:
                sources[seq] = (ts, en)
            history[seq] = f"[{ts}] {ko}\n  ({en})"
            dirty = True
            for k in list(lines)[:-MAX_LINES]:
                del lines[k]
            render()
        root.after(150, poll)

    def save_extra_langs():
        """회의 종료 후 전체 원문을 다른 언어로 한 번 더 번역해 별도 파일로 저장."""
        items = [sources[k] for k in sorted(sources)]
        if not items or not EXTRA_SAVE_LANGS:
            return
        for lang in EXTRA_SAVE_LANGS:
            box.config(state="normal"); box.delete("1.0", "end")
            box.insert("end", f"{lang} 번역본 저장 중… ({len(items)}문장)", "ko")
            box.config(state="disabled"); root.update()
            out = []
            try:
                for i in range(0, len(items), 40):           # 40문장씩 나눠 번역
                    batch = items[i:i + 40]
                    numbered = "\n".join(f"{n}. {src}" for n, (_, src) in enumerate(batch, 1))
                    gl = f" Glossary: {GLOSSARY}." if GLOSSARY else ""
                    msg = claude.messages.create(
                        model=TRANSLATE_MODEL, max_tokens=8000,
                        system=(f"Translate this numbered business-meeting transcript into natural {lang}. "
                                "Lines are consecutive speech, use the surrounding lines for context. "
                                "Keep the leading '(나)' speaker tag untranslated. Keep names and numbers exact."
                                f"{gl} Output exactly one line per input line, as 'N. translation', nothing else."),
                        messages=[{"role": "user", "content": numbered}])
                    got = {}
                    for line in msg.content[0].text.splitlines():
                        num, _, txt = line.partition(". ")
                        if num.strip().isdigit():
                            got[int(num)] = txt.strip()
                    for n, (ts, src) in enumerate(batch, 1):
                        out.append(f"[{ts}] {got.get(n, '(번역 누락)')}\n  ({src})")
                path = save_path[:-4] + f"_{lang}.txt"
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(out) + "\n")
                print(f"[saved] {path}", flush=True)
            except Exception as e:
                print(f"[{lang} 저장 오류] {e!r}", flush=True)

    def on_close():
        save()
        if history:
            print(f"[saved] {save_path}", flush=True)
        save_extra_langs()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    poll()
    autosave()
    root.mainloop()


if __name__ == "__main__":
    threading.Thread(target=capture, daemon=True).start()
    threading.Thread(target=stt_worker, daemon=True).start()
    threading.Thread(target=translate_worker, daemon=True).start()
    ui()
