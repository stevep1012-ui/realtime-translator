#!/bin/bash
# Finder에서 더블클릭. 필요한 프로그램을 순서대로 자동 설치하고 실행합니다.
cd "$(dirname "$0")"
export TK_SILENCE_DEPRECATION=1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

step() { echo; echo "▶ $1"; }
fail() { echo; echo "✖ $1"; read -n 1 -s -r -p "아무 키나 누르면 닫힙니다."; echo; exit 1; }

# 1. Homebrew
if ! command -v brew >/dev/null; then
  step "Homebrew 설치 (Mac 암호를 물으면 입력, Enter 를 누르라고 하면 Enter)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    || fail "Homebrew 설치 실패. 인터넷 연결을 확인하세요."
  eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
fi

# 2. Python + Tk
if ! brew list python-tk >/dev/null 2>&1; then
  step "Python 설치 (1~3분)"
  brew install python python-tk || fail "Python 설치 실패"
fi
PY="$(brew --prefix)/bin/python3"

# 3. BlackHole (시스템 소리 캐처용 가상 장치)
if ! [ -e /Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver ]; then
  step "BlackHole 설치 (Mac 암호 입력 필요)"
  brew install blackhole-2ch || fail "BlackHole 설치 실패"
  echo "설치 완료. Mac 을 재시작한 뒤 이 파일을 다시 실행하세요."
  read -n 1 -s -r -p "아무 키나 누르면 닫힙니다."; echo; exit 0
fi

# 4. 파이썬 패키지
if [ ! -d .venv ]; then
  step "필요한 패키지 설치 (최초 1회)"
  "$PY" -m venv .venv || fail "venv 생성 실패"
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q sounddevice numpy openai anthropic || fail "패키지 설치 실패"
fi

# 5. 다중 출력 장치 확인 (스피커 + BlackHole 동시 출력)
if ! .venv/bin/python - <<'EOF'
import sounddevice as sd, sys
names = [d["name"].lower() for d in sd.query_devices()]
sys.exit(0 if any("다중 출력" in n or "multi-output" in n for n in names) else 1)
EOF
then
  step "마지막 수동 단계: 다중 출력 장치 만들기 (한 번만)"
  cat <<'EOF'
  지금 열리는 [오디오 MIDI 설정] 창에서:
   1) 왼쪽 아래  +  버튼 → "다중 출력 장치 생성"
   2) 오른쪽 목록에서  BlackHole 2ch  와  실제 쓰는 스피커/헤드셋  체크
   3) 창 닫기
  그다음 [시스템 설정 → 사운드 → 출력] 에서 "다중 출력 장치" 선택
  (Teams/Zoom 은 앱 안의 스피커 설정도 "다중 출력 장치"로)
EOF
  open -a "Audio MIDI Setup"
  read -n 1 -s -r -p "위 설정을 마쳤으면 아무 키나 누르세요."; echo
fi

# 6. 실행 (첫 실행이면 API 키 입력창이 뜨)
step "실시간번역기 시작. 자막은 이 폴더에 자동 저장됩니다."
.venv/bin/python realtime_translator.py
