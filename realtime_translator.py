"""
회의 음성 → 실시간 번역 자막 (macOS / Windows)
시스템 오디오(mac: BlackHole, win: WASAPI 루프백) → OpenAI STT → Claude 번역 → 항상 위 자막창
설정은 같은 폴더의 .env 파일에서 읽음 (.env.example 참고)
"""
import io
import os
import queue
import sys
import threading
import time
import wave
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
MAX_CHUNK_SEC = float(os.getenv("CHUNK_SEC", "4"))   # 이 길이를 넘으면 무조건 전송
MIN_SPEECH_SEC = 0.6    # 이보다 짧은 소리는 버림
PAUSE_SEC = float(os.getenv("PAUSE_SEC", "0.35"))    # 이만큼 조용하면 문장 끝으로 보고 전송
BLOCK_SEC = 0.05        # 소리 크기 판정 단위
SILENCE_RMS = 0.005     # 이보다 조용하면 무음
STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-mini-transcribe")
TRANSLATE_MODEL = os.getenv("TRANSLATE_MODEL", "claude-haiku-4-5")
SOURCE_LANG = os.getenv("SOURCE_LANG", "").strip()    # STT 언어 코드. 빈값 = 자동 감지(영어·일본어 등)
TARGET_LANG = os.getenv("TARGET_LANG", "한국어")       # 번역 목표 언어 (자연어 이름)
MAX_LINES = 4           # 화면에 유지할 번역 항목 수
FONT = "Malgun Gothic" if sys.platform == "win32" else "Apple SD Gothic Neo"
KO_SIZE = int(os.getenv("KO_SIZE", "15"))   # 번역문 글자 크기
EN_SIZE = int(os.getenv("EN_SIZE", "11"))   # 원문 글자 크기
SAVE_DIR = os.getenv("CAPTION_SAVE_DIR", HERE)

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

openai_client = OpenAI()      # OPENAI_API_KEY
claude = Anthropic()          # ANTHROPIC_API_KEY
audio_q: queue.Queue[np.ndarray] = queue.Queue()
stt_q: queue.Queue[tuple[int, str]] = queue.Queue()      # (id, 영어)
text_q: queue.Queue[tuple[int, str]] = queue.Queue()     # (id, 표시문)


def make_vad():
    """16kHz 모노 블록을 받아, 말이 끊기는 지점에서 발화 단위로 audio_q에 넣는 함수 반환."""
    buf = []            # 현재 발화 (말 시작 후 누적)
    speech_sec = 0.0
    silence_sec = 0.0

    def flush():
        nonlocal buf, speech_sec, silence_sec
        if speech_sec >= MIN_SPEECH_SEC:
            audio_q.put(np.concatenate(buf))
        buf, speech_sec, silence_sec = [], 0.0, 0.0

    def feed(block: np.ndarray):
        nonlocal speech_sec, silence_sec
        loud = float(np.sqrt(np.mean(block ** 2))) >= SILENCE_RMS
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
    feed = make_vad()

    def cb(indata, n, t, status):
        feed(indata[:, 0].copy())

    with sd.InputStream(device=dev, samplerate=SAMPLE_RATE, channels=1,
                        dtype="float32", blocksize=int(SAMPLE_RATE * BLOCK_SEC),
                        callback=cb):
        threading.Event().wait()


def capture_windows():
    """WASAPI 루프백으로 기본 스피커 출력을 직접 캐처 (가상 장치 불필요)."""
    import pyaudiowpatch as pyaudio
    p = pyaudio.PyAudio()
    dev = p.get_default_wasapi_loopback()
    rate = int(dev["defaultSampleRate"])
    ch = int(dev["maxInputChannels"])
    print(f"[capture] loopback {dev['name']} {rate}Hz {ch}ch", flush=True)
    feed = make_vad()
    n_in = int(rate * BLOCK_SEC)
    n_out = int(SAMPLE_RATE * BLOCK_SEC)

    def cb(data, frame_count, time_info, status):
        x = np.frombuffer(data, dtype=np.float32).reshape(-1, ch).mean(axis=1)
        if rate != SAMPLE_RATE:      # 16kHz로 리샘플
            x = np.interp(np.linspace(0, len(x), n_out, endpoint=False),
                          np.arange(len(x)), x).astype(np.float32)
        feed(x)
        return (None, pyaudio.paContinue)

    p.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
           input_device_index=dev["index"], frames_per_buffer=n_in,
           stream_callback=cb)
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


def transcribe(samples: np.ndarray) -> str:
    kwargs = {"language": SOURCE_LANG} if SOURCE_LANG else {}
    r = openai_client.audio.transcriptions.create(
        model=STT_MODEL, file=to_wav_bytes(samples), **kwargs)
    return r.text.strip()


def translate_stream(en: str, prev: str):
    """번역문을 글자가 나오는 대로 yield (누적 문자열)."""
    acc = ""
    with claude.messages.stream(
            model=TRANSLATE_MODEL, max_tokens=300,
            system=f"Translate meeting speech (any language) into natural {TARGET_LANG} subtitles. "
                   f"If the text is already {TARGET_LANG}, return it unchanged. "
                   f"Output only the translation. Previous context: {prev}",
            messages=[{"role": "user", "content": en}]) as s:
        for piece in s.text_stream:
            acc += piece
            yield acc


def stt_worker():
    seq = 0
    while True:
        samples = audio_q.get()
        t0 = time.time()
        try:
            en = transcribe(samples)
            if not en:
                continue
            seq += 1
            print(f"[stt {seq}] {time.time()-t0:.2f}s  {en}", flush=True)
            text_q.put((seq, "…", en))            # 영어 먼저 표시
            stt_q.put((seq, en, time.time()))
        except Exception as e:
            print(f"[STT 오류] {e!r}", flush=True)
            text_q.put((0, f"[STT 오류] {e}", ""))


def translate_worker():
    prev = ""
    while True:
        seq, en, t0 = stt_q.get()
        try:
            ko = ""
            first = None
            for ko in translate_stream(en, prev):
                if first is None:
                    first = time.time() - t0
                text_q.put((seq, ko, en))          # 부분 번역을 계속 갱신
            print(f"[ko {seq}] 첫글자 {first:.2f}s 완료 {time.time()-t0:.2f}s  {ko}",
                  flush=True)
            prev = en
        except Exception as e:
            print(f"[번역 오류] {e!r}", flush=True)
            text_q.put((seq, f"[번역 오류] {e}", en))


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
            history[seq] = f"[{datetime.now():%H:%M:%S}] {ko}\n  ({en})"
            dirty = True
            for k in list(lines)[:-MAX_LINES]:
                del lines[k]
            render()
        root.after(150, poll)

    def on_close():
        save()
        if history:
            print(f"[saved] {save_path}", flush=True)
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
