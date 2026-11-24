# Legend — 하이라이트 엔진 프로토타입

긴 영상(팟캐스트·인터뷰·강의)에서 숏츠로 쓸 만한 구간을 찾아 **점수순 클립 후보 JSON**으로 내보내는 엔진입니다.
이어서 **9:16 세로 전환(얼굴 추적)과 애니메이션 자막을 입힌 숏츠 MP4**까지 렌더링합니다.

## 파이프라인

```
영상 ─▶ STT(faster-whisper, 단어 타임스탬프)
     ─▶ 문장 분할 (문장부호 / 화자 전환 / 긴 무음 / 20초 상한)
     ─▶ 신호 분석 (음량 z-score, 말 속도 z-score)
     ─▶ 후보 선정 (Claude: 문장 ID 범위로 응답 · 또는 오프라인 휴리스틱)
     ─▶ 경계 보정 (완결된 문장에서 끝나도록 확장/축소, 길이 범위 맞춤, 무음 구간 패딩)
     ─▶ 점수 = LLM 70% (훅 30 · 완결성 25 · 가치 20 ·감정 15 ·트렌드 10) + 신호 30%
     ─▶ 중복 제거(IoU) ─▶ 상위 N개
```

설계 포인트
- **LLM은 타임스탬프가 아니라 문장 ID를 반환** → 클립 경계는 항상 문장 경계에 맞춰집니다.
- `postprocess.fit_bounds`가 "문장 중간에 끊김"을 LLM 결과와 무관하게 강제로 막습니다.
- "그래서/그리고/근데"처럼 앞 맥락에 의존하는 시작, 미완결 끝은 완결성 점수를 깎습니다.
- 긴 영상은 20분 단위로 겹치게(90초) 나눠 병렬 호출하고, 겹친 후보는 IoU 기준으로 제거합니다.
- Claude 호출은 JSON 스키마 구조화 출력을 쓰고, 서버 측 폴백(`fallbacks: "default"`)을 켜 두었습니다. 모델이 요청을 거절하면 자동으로 다른 모델이 이어받습니다.

## 세로 숏츠 렌더링 (`highlight_engine/render/`)

```
클립 구간 ─▶ 장면 전환 감지 (썸네일 차이, 최근 움직임 대비 적응형 임계값)
         ─▶ 얼굴 검출 (초당 5프레임, 640px 축소본)
         ─▶ 장면(shot)마다 레이아웃 자동 선택
              track : 9:16 창이 주 화자 얼굴을 따라감 (작은 흔들림은 고정, 큰 이동만 부드럽게 추적, 컷에서 즉시 전환)
              split : 두 사람이 멀리 떨어진 2인 화면 → 위/아래 분할
              fit   : 얼굴 없음(슬라이드·화면녹화) → 블러 배경 위에 원본 전체
         ─▶ 프레임 합성(OpenCV) → ffmpeg로 인코딩하면서 ASS 자막을 입힘 + 원본 오디오
```

- 자막 위치는 기본적으로 레이아웃을 따라갑니다(track 70% 높이 / split 경계선 / fit 영상 바로 아래). 스타일에서 top/middle/bottom으로 고정할 수도 있습니다.
- 각 숏츠 옆에 `short_XX.json`(레이아웃, 장면별 크롭 키프레임)이 생성됩니다. 편집기에서 크롭 위치를 보여주고 수정하는 데이터입니다.
- 얼굴 검출 기본값은 OpenCV 내장 Haar(오프라인 동작). 운영에서는 `--yunet face_detection_yunet_2023mar.onnx`(OpenCV model zoo) 사용을 권장합니다.
- 한글 자막에는 한글 폰트가 필요합니다: 기본 `Noto Sans CJK KR` (`apt install fonts-noto-cjk`), 다른 폰트는 `--fonts-dir`.

```bash
# 엔진 실행과 동시에 렌더링
python -m highlight_engine video.mp4 --out clips.json --render-dir shorts/ --preset bold

# 이미 뽑은 클립 JSON으로 렌더링만 (특정 순위만)
python -m highlight_engine.render clips.json video.mp4 --out-dir shorts/ --ranks 1,2 --preset emphasis --layout auto
```

### 자막 스타일 고르기

| 프리셋 | 이름 | 효과 |
|---|---|---|
| **`legend`** | **레전드 마커 (시그니처, 기본값)** | 말하는 단어 뒤로 브랜드 핑크 형광펜이 손으로 긋듯 0.1초 만에 칠해짐. 단어마다 살짝 다른 기울기, 키워드는 골드, 자막이 아래에서 떠오르며 등장, 제목은 핑크 띠가 있는 둥근 카드 |
| `basic` | 기본 | 흰 글씨 + 검은 외곽선, 말하는 단어는 노란색 |
| `bold` | 굵은 노랑 | 크고 굵은 노란 글씨, 말하는 단어는 흰색으로 커짐 |
| `box` | 반투명 박스 | 반투명 검은 배경 박스, 말하는 단어는 민트색 |
| `highlight_box` | 단어 박스 | 말하는 단어 뒤에 색 박스가 따라다님 |
| `one_word` | 한 단어씩 | 한 단어씩 화면에 크게 팝 |
| `reveal` | 타자기 | 말하는 대로 단어가 하나씩 나타남 |
| `karaoke` | 노래방 | 말하는 동안 왼쪽부터 색이 채워짐 |
| `emphasis` | 키워드 강조 | 핵심 키워드는 핑크, 말하는 단어는 초록색으로 팝 |
| `neon` | 네온 | 하늘색 네온 글로우 |
| `news` | 방송 자막 | 화면 아래 반투명 바, 차분한 스타일 |
| `minimal` | 미니멀 | 작은 흰 글씨, 효과 없음 |

```bash
python -m highlight_engine.render --list-presets            # 목록 (--json: 웹 UI 스타일 선택기용 카탈로그)
python -m highlight_engine.render --preview styles.png --preview-video video.mp4   # 모든 스타일 미리보기 갤러리

# 프리셋 + 개별 수정 (폰트/크기/색/위치)
python -m highlight_engine.render clips.json video.mp4 --preset neon \
    --font "Pretendard" --caption-size 90 --highlight-color FF4FA3 --caption-position bottom

# 내 브랜드 스타일을 파일로 저장해 재사용
echo '{"base": "highlight_box", "font": "Pretendard", "word_box_color": "6C5CE7", "size": 88}' > brand.json
python -m highlight_engine.render clips.json video.mp4 --style-file brand.json
```

시그니처 스타일은 자막 글자 폭을 libass와 같은 폰트 파일로 측정해(`render/textmetrics.py`) 형광펜 도형을
단어 바로 뒤에 그립니다(`render/signature.py`). 형광펜 색은 `word_box_color`, 키워드 색은 `keyword_color`,
제목 띠 색은 `title_box_color`로 바꿀 수 있어 브랜드 색에 맞출 수 있습니다.

스타일 파일에는 `CaptionStyle`의 모든 필드를 쓸 수 있습니다(`mode`, `color`, `highlight`, `highlight_scale`,
`keyword_color`, `outline`, `blur`, `box`, `max_chars`, `max_words`, `position`, `uppercase`, `title_*` …).
잘못된 필드명이나 색상 값은 오류로 알려줍니다. 렌더링된 `short_XX.json`에 사용한 스타일 전체가 기록됩니다.

데모 영상 만들기(테스트용 합성 영상: 단독 화면·2인 화면·슬라이드가 번갈아 나옴):

```bash
python examples/make_demo_video.py demo.mp4 145
python -m highlight_engine examples/sample_podcast.json --audio demo.mp4 --selector heuristic \
    --length 15-45 --top 3 --out clips.json --render-dir shorts/
```

## 웹 편집기 (`webapp/`)

```bash
python -m webapp serve                      # http://127.0.0.1:8000
python -m webapp import video.mp4 --transcript t.json --selector heuristic   # 로컬 영상으로 프로젝트 만들기
```

- **업로드 → 자동 분석**: 영상(+선택: 자막 json)을 올리면 백그라운드에서 전사·하이라이트 분석, 진행률 표시
- **클립 목록**: 점수순 썸네일, 렌더링 여부 표시
- **9:16 실시간 미리보기**: 서버와 같은 규칙으로 캔버스에 세로 화면을 그리고 자막을 겹쳐 보여줌
  (libass 글꼴 크기·줄바꿈 방식을 보정해 최종 렌더와 거의 같게 보임)
- **구간·텍스트**: 타임라인 손잡이로 시작·끝 조절(단어 경계에 자동으로 맞춤), 단어 클릭→이동/시작점/끝점/오타 수정,
  제목·강조 키워드 수정, "AI 추천 구간으로" 되돌리기. 단축키 Space 재생, I 시작점, O 끝점
- **자막 스타일**: 11개 프리셋(이 영상 장면으로 만든 썸네일) + 글꼴·크기·위치·색상 세부 조정
- **화면 구성**: 전체 레이아웃(자동/얼굴 추적/2인 분할/전체 화면), 장면별 레이아웃 지정,
  얼굴 추적 장면은 미리보기를 좌우로 드래그해 화면 위치 조정
- **내보내기**: 렌더링 진행률 → 결과 재생·다운로드 (클립당 최신 결과만 보관)

구조: FastAPI(`webapp/server.py`) + 파일 기반 저장소(`webapp/store.py`, `data/projects/<id>/`) + 프로세스 내 작업 큐(`webapp/jobs.py`) +
빌드 없는 ES 모듈 프론트엔드(`webapp/static/`). 운영 단계에서는 설계 문서대로 Postgres·S3·Redis 큐·GPU 워커, Next.js로 옮기면 되도록
API를 분리해 두었습니다. 인증·결제는 아직 없으므로 **로컬/내부망에서만** 실행하세요.

## 설치

```bash
pip install -r requirements.txt     # ffmpeg 필요 (apt install ffmpeg)
export ANTHROPIC_API_KEY=...         # Claude 선택기 사용 시
```

## 사용법

```bash
# 영상 → 클립 후보 (전사 결과도 저장)
python -m highlight_engine video.mp4 --length medium --top 10 \
    --save-transcript video.transcript.json --out clips.json --export-dir clips/

# 이미 전사된 JSON으로 빠르게 반복 실험 (+ 음량 신호용 원본 지정)
python -m highlight_engine video.transcript.json --audio video.mp4 --out clips.json

# 프롬프트 클리핑
python -m highlight_engine video.transcript.json --prompt "가격 얘기만"

# API 키 없이 오프라인 베이스라인
python -m highlight_engine examples/sample_podcast.json --selector heuristic --length 15-45
```

| 옵션 | 설명 |
|---|---|
| `--length` | `short`(15-30) / `medium`(30-60) / `long`(60-90) / `MIN-MAX` |
| `--selector` | `claude`(기본) / `heuristic` |
| `--model`, `--effort` | Claude 모델(기본 `claude-opus-5-5`)과 effort(기본 `high`) |
| `--whisper-model` | `large-v3`(기본), CPU 테스트는 `small` 권장 |
| `--export-dir` | 클립 미리보기 MP4를 잘라 저장 (가로 원본 그대로) |

## 출력 예시

```json
{
  "duration": 3605.2,
  "clips": [{
    "rank": 1, "score": 87, "start": 412.3, "end": 459.8, "duration": 47.5,
    "title": "부업 월 100만 원의 진실", "hook": "진짜 가능한가요?",
    "reason": "질문 훅 + 구체적 숫자 + 명확한 결론",
    "keywords": ["100만 원", "포기"],
    "score_detail": {"hook": 9, "completeness": 8, "emotion": 6, "value": 9, "trend": 8, "signal": 61.2},
    "text": "..."
  }]
}
```

## 테스트

```bash
python -m pytest -q
```

Claude 호출은 테스트에서 가짜 클라이언트로 대체하므로 API 키 없이 실행됩니다.

## 다음 단계
1. **평가셋 구축**: 실제 영상 30개에 사람이 "좋은 구간" 라벨을 달아 휴리스틱과 Claude, 프롬프트 버전별로 비교
2. 화자 분리(pyannote) + 입 움직임으로 "말하는 사람"을 추적 (현재는 가장 큰 얼굴 기준)
3. GPU 인코딩(NVENC)과 YuNet 기본화로 처리 속도·검출 품질 개선
4. 웃음·박수 감지 등 오디오 이벤트 신호 추가
