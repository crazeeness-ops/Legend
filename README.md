# Legend — 숏츠 자동 제작 서비스

## 🚀 내 컴퓨터에서 바로 실행해 보기 (체험 모드)

API 키 없이 모든 기능을 눌러볼 수 있어요. YouTube 연결과 결제는 **가짜 서버**로 동작합니다(실제 업로드·실제 돈 없음).

**준비물 (처음 한 번)**
- Python 3.11 이상 — Windows: https://www.python.org/downloads/ (설치할 때 **Add python.exe to PATH** 체크) / Mac: `brew install python`
- ffmpeg — Windows: 명령 프롬프트에서 `winget install Gyan.FFmpeg` / Mac: `brew install ffmpeg`

**실행**
- Windows: `start-windows.bat` 더블클릭
- Mac: `start-mac.command` 더블클릭 (처음엔 우클릭 → 열기)
- 직접: `pip install -r requirements.txt` 후 `python -m webapp demo`

브라우저가 열리면 체험 계정으로 로그인하세요 (비밀번호 `demo1234`):
- `creator@demo.kr` — 크리에이터 (체험 프로젝트가 미리 만들어져 있어요)
- `buyer@demo.kr` — 구매자·클리퍼

처음 실행은 프로그램 설치 때문에 몇 분 걸려요. 새 영상을 자막 파일 없이 올리면 음성 인식 모델(약 500MB)을 한 번 내려받아요.
`ANTHROPIC_API_KEY`를 설정하면 Claude 하이라이트 추천을 쓸 수 있어요.

---

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
| **`legend_sticker`** | **레전드 스티커 (시그니처 2)** | 말하는 단어가 ±3° 기울어진 흰 스티커 위에 진한 글씨로 "톡" 튀어나옴(55%→114%→100% 탄성 등장), 뒤에 핑크 그림자. 나머지 글자는 흰 글씨+굵은 외곽선, 키워드는 핑크, 제목도 기울어진 스티커 카드 |
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

### YouTube Shorts 자동 업로드

- **채널 연결**: 상단 "YouTube 연결" → Google 로그인(OAuth 2.0, `youtube.upload` + `youtube.readonly`).
  토큰은 `data/youtube/account.json`(권한 600)에 저장되고 만료 시 자동 갱신, 해제 시 Google에서도 철회
- **게시 탭**: Claude가 제목·설명·해시태그(#Shorts 포함)·검색 태그 작성(키가 없으면 기본 문구) → 수정 →
  공개/일부 공개/비공개, **예약 게시**(YouTube `publishAt` 사용: 서버가 꺼져 있어도 정한 시간에 공개), 아동용 여부
- **자동 렌더링**: 편집 내용이 마지막 렌더링과 다르면(구간·자막·스타일·화면 해시 비교) 다시 렌더링 후 업로드
- **이어 올리기**: 8MB 단위 resumable upload, 5xx·연결 끊김·응답 유실 시 서버에 받은 위치를 물어 이어서 전송, 토큰 만료 시 갱신 후 계속
- **일괄 예약 게시**: 클립 여러 개를 첫 공개 시간 + 간격(6시간~일주일)으로 한 번에 예약. 실패한 클립이 있어도 나머지는 계속
- 오류는 한국어로 안내(할당량 초과, 권한 없음, 예약 시간 오류 등)

설정 (한 번만):
1. Google Cloud 콘솔 → 프로젝트 생성 → **YouTube Data API v3** 사용 설정
2. OAuth 동의 화면(외부, 테스트 사용자에 본인 계정 추가)
3. 사용자 인증 정보 → OAuth 클라이언트 ID(**웹 애플리케이션**), 리디렉션 URI `http://127.0.0.1:8000/oauth/google/callback` → JSON 다운로드
4. `python -m webapp serve --google-client-secret client_secret.json` (또는 `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`)

알아둘 제한:
- **API 감사(audit)를 통과하지 않은 Google 프로젝트로 올린 영상은 비공개로 고정**됩니다. 공개·예약 게시를 서비스로 쓰려면 YouTube API 서비스 감사를 신청해야 합니다.
- 기본 할당량 10,000 unit/일, 업로드 1회 약 1,600 unit → 하루 약 6개. 서비스 규모에 맞게 할당량 증설 신청 필요
- 세로 3분 이하 영상은 YouTube가 자동으로 Shorts로 분류합니다.
- 연결된 채널로 누구나 게시할 수 있으므로 편집기는 **로컬(127.0.0.1)에서만** 실행하세요(로그인 기능 전까지).

오프라인 검증: `tests/fake_google.py`는 OAuth와 YouTube 업로드 API를 흉내 내는 가짜 서버입니다(오류 주입 가능).
`python tests/fake_google.py --port 9999` 후 `LEGEND_GOOGLE_BASE=http://127.0.0.1:9999 GOOGLE_CLIENT_ID=x GOOGLE_CLIENT_SECRET=y python -m webapp serve`로
실제 Google 없이 전체 흐름을 눌러볼 수 있습니다.

구조: FastAPI(`webapp/server.py`) + 파일 기반 저장소(`webapp/store.py`, `data/projects/<id>/`) + 프로세스 내 작업 큐(`webapp/jobs.py`) +
빌드 없는 ES 모듈 프론트엔드(`webapp/static/`). 운영 단계에서는 설계 문서대로 Postgres·S3·Redis 큐·GPU 워커, Next.js로 옮기면 되도록
API를 분리해 두었습니다. 인증·결제는 아직 없으므로 **로컬/내부망에서만** 실행하세요.

## 계정 · 내 채널 · 클리핑 마켓

**계정**: 이메일+비밀번호 가입(크리에이터/클리퍼 선택), 쿠키 세션(HttpOnly, SameSite=Lax), 쓰기 요청은 CSRF 헤더 필수.
프로젝트·작업·YouTube 연결은 사용자별로 분리되고, 남의 프로젝트는 존재 여부도 드러나지 않게 404로 응답합니다.
요금제별 월 분석 한도(Free 60분 / Creator 300분 / Pro 1,200분, 원본 길이 기준) — 분석 시작 시 차감, 실패하면 자동 환불.
결제 연동 전까지는 `python -m webapp set-plan 이메일 pro`로 관리자가 변경합니다.

**내 채널 (크리에이터)**: 연결된 채널의 긴 영상을 조회수·좋아요·좋아요율·최신 순으로 보여 주고, YouTube Analytics의
**시청 유지율 그래프**(빨간 점 = 몰입 구간)를 함께 표시합니다. "클립 만들기"로 원본 파일을 올리면(길이로 같은 영상인지 확인)
유지율을 하이라이트 점수에 반영합니다 — 시간이 갈수록 떨어지는 자연 감소를 빼고 남는 "더 오래 본/다시 본 구간"을 찾아
신호 점수에 더하고, 이때는 실제 시청자 데이터 비중을 높입니다(LLM 70% → 60%).

**클리핑 마켓**
- 크리에이터: 내 채널 영상으로 캠페인 개설(채널 소유 확인 + 원본 파일 직접 제공 → 클리퍼는 YouTube에서 내려받지 않음),
  규칙·출처 문구·1,000회당 보상·예산·최대 길이·마감일 설정, 제출물 승인/반려, 조회수 새로고침, 마감/재개
- 클리퍼: 열린 캠페인 둘러보기 → 참여(내 월 사용량에서 차감) → 편집기에서 다듬기 → 내 채널에 게시하면 **자동 제출**
  (출처 문구는 지워도 업로드 직전에 다시 들어가고, 최대 길이·마감은 서버에서 강제) → 승인되면 조회수만큼 정산
- 정산 = 승인된 클립 조회수 ÷ 1,000 × 단가, 먼저 제출된 순서로 예산 한도까지. **실제 송금은 아직 수동**입니다.

**클립 판매 (이용 허락 판매)**
- 판매자: '내 채널'에서 소유 확인을 거친 영상의 클립만 판매 가능. 편집기 "판매" 탭에서 가격(원본 조회수·클립 점수·길이 기반
  추천가 제공), 비독점/독점(독점은 1명 한정, 추천가 ×5), 라이선스 기간(30일/90일/1년/무기한), 판매 마감일, 최대 판매 수,
  출처 표기 문구, 판매자 정보(전자상거래법상 구매자에게 공개)를 정하고 권리 보유를 확인·동의해야 등록됩니다.
  등록 시점의 영상을 고정 사본으로 저장하므로 이후 편집해도 산 사람의 영상은 바뀌지 않습니다.
- 구매자: 워터마크 저화질 미리보기 → 이용 조건·환불 규정 확인 → 결제 → 원본(1080×1920) 다운로드, **이용 허락서**(라이선스 번호,
  기간, 출처 의무, 허용/금지 범위, 판매자 보증, 중개자 고지), "내 채널에 올리기"(설명란에 출처 + 라이선스 번호 자동 삽입)
- 환불: 결제 후 7일 이내, 다운로드·게시 전에만. 판매 수량은 결제 확정 시점에 다시 확인해 초과 판매를 막습니다.
- 수수료 20%, 판매/구매 내역과 정산 예정 금액 표시, 권리 침해 신고 접수
- 결제는 토스페이먼츠로 처리합니다(아래 "결제" 참고). 키가 없으면 개발용 테스트 결제로 동작합니다.
  판매자 지급은 지금은 관리자가 정산 목록으로 처리하며, 실제 운영에서는 토스페이먼츠 지급대행(별도 계약) 등으로
  Legend가 돈을 직접 보관하지 않게 해야 합니다. 출시 전 변호사·세무사 검토 필수.

## 결제 (토스페이먼츠)

| 무엇 | 어떻게 |
|---|---|
| 클립 구매 | 결제창(SDK v2 `requestPayment`) → `/pay/success`에서 **서버가 주문 금액과 비교**하고 품절 여부를 확인한 뒤 승인(`/v1/payments/confirm`). 승인 후 품절이 확인되면 자동 취소 |
| 환불 | 다운로드·게시 전 7일 이내 → 결제 취소 API |
| 요금제 구독 | 카드 등록(`requestBillingAuth`) → 빌링키 발급 → 첫 달 즉시 결제. 매달 자동 갱신(내장 스케줄러 또는 `python -m webapp run-billing`), 실패 시 하루 간격 3회 재시도 후 Free로 변경, 해지는 기간 끝까지 유지, 요금제 변경은 다음 결제일부터, 카드 변경 시 밀린 결제 즉시 재시도 |
| 결제 알림(웹훅) | `POST /api/webhooks/toss`. 알림 내용은 믿지 않고 **토스에 결제를 다시 조회**해서 반영(놓친 승인 복구, 관리자 취소 반영) |
| 판매자 정산 | 판매 후 7일이 지나면 정산 대기. `python -m webapp payouts --csv 정산.csv` → 지급 → `--mark-paid` |
| 안전장치 | 시크릿 키는 서버에만, 승인·취소·자동결제에 중복 방지 키(Idempotency-Key), 자동결제 주문번호를 회차별로 고정해 재시작해도 이중 결제 없음, `--production`이면 테스트 결제로 대체하지 않음 |

설정:
```bash
export TOSS_CLIENT_KEY=test_ck_...   # 개발자센터 > API 키 (API 개별 연동 키)
export TOSS_SECRET_KEY=test_sk_...
python -m webapp serve --public-url https://내도메인 --production
```
토스페이먼츠 개발자센터에 웹훅 주소 `https://내도메인/api/webhooks/toss`(결제 상태 변경)를 등록하세요.
자동결제(빌링)는 토스페이먼츠와 **별도 계약**이 필요합니다. 실제 키로 바꾸기 전에 공식 문서와 필드명을 한 번 더 대조하세요.

오프라인 검증: `python tests/fake_toss.py --port 9998`(가짜 결제창·승인·취소·빌링) 후
`LEGEND_TOSS_BASE=http://127.0.0.1:9998 LEGEND_TOSS_SDK=http://127.0.0.1:9998/v2/standard TOSS_CLIENT_KEY=test_ck_fake TOSS_SECRET_KEY=test_sk_fake`로 실행.

`tests/fake_google.py --demo-video-seconds 200`은 크리에이터/클리퍼 두 채널, 영상 통계, 시청 유지율까지 흉내 내므로
실제 Google 없이 두 계정으로 전체 흐름을 눌러볼 수 있습니다(`POST /_fake/identity`로 다음에 로그인할 채널 선택,
`POST /_fake/views`로 조회수 설정). OAuth 리디렉트는 `--public-url`과 같은 주소로 접속해야 로그인 쿠키가 유지되며,
다른 주소로 들어오면 올바른 주소로 안내합니다.

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
