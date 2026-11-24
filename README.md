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

- 자막: 단어 단위 카라오케 하이라이트, 키워드 강조색, 페이지 등장 팝 애니메이션, 상단 제목 바. 
  레이아웃에 따라 위치가 바뀝니다(track 70% 높이 / split 경계선 / fit 영상 바로 아래).
- 프리셋 5종: `basic`, `bold`, `box`, `emphasis`, `minimal`. 브랜드 폰트·색은 `with_brand()`로 덮어씁니다.
- 각 숏츠 옆에 `short_XX.json`(레이아웃, 장면별 크롭 키프레임)이 생성됩니다. 편집기에서 크롭 위치를 보여주고 수정하는 데이터입니다.
- 얼굴 검출 기본값은 OpenCV 내장 Haar(오프라인 동작). 운영에서는 `--yunet face_detection_yunet_2023mar.onnx`(OpenCV model zoo) 사용을 권장합니다.
- 한글 자막에는 한글 폰트가 필요합니다: 기본 `Noto Sans CJK KR` (`apt install fonts-noto-cjk`), 다른 폰트는 `--fonts-dir`.

```bash
# 엔진 실행과 동시에 렌더링
python -m highlight_engine video.mp4 --out clips.json --render-dir shorts/ --preset bold

# 이미 뽑은 클립 JSON으로 렌더링만 (특정 순위만)
python -m highlight_engine.render clips.json video.mp4 --out-dir shorts/ --ranks 1,2 --preset emphasis --layout auto
```

데모 영상 만들기(테스트용 합성 영상: 단독 화면·2인 화면·슬라이드가 번갈아 나옴):

```bash
python examples/make_demo_video.py demo.mp4 145
python -m highlight_engine examples/sample_podcast.json --audio demo.mp4 --selector heuristic \
    --length 15-45 --top 3 --out clips.json --render-dir shorts/
```

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
