"""Generate a synthetic Korean podcast transcript (word timestamps) for demos/tests.

Usage: python examples/generate_sample.py examples/sample_podcast.json
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from highlight_engine.models import Transcript, Word  # noqa: E402

DIALOGUE = [
    ("A", "안녕하세요 여러분, 오늘도 돈 이야기 팟캐스트에 오신 걸 환영합니다."),
    ("A", "오늘은 직장인 부업 전문가 김대표님을 모셨습니다."),
    ("B", "네 안녕하세요, 반갑습니다."),
    ("A", "요즘 날씨가 많이 추워졌죠."),
    ("B", "그러게요, 오늘 아침에 정말 추웠어요."),
    ("A", "자 그럼 본론으로 들어가 볼게요."),
    ("A", "대표님, 직장인이 부업으로 월 100만 원 버는 게 진짜 가능한가요?"),
    ("B", "솔직히 말씀드리면 가능합니다, 그런데 대부분이 처음 3개월 안에 포기해요."),
    ("B", "제가 300명 넘게 상담해 보니까 실패하는 이유는 딱 하나였어요."),
    ("B", "돈이 되는 일이 아니라 재미있어 보이는 일부터 시작한다는 거예요."),
    ("B", "예를 들어서 유튜브를 시작하면 첫 6개월은 수익이 거의 0원입니다."),
    ("B", "그래서 저는 무조건 이미 가진 기술로 첫 10만 원을 먼저 벌어 보라고 해요."),
    ("A", "아 그게 핵심이네요!"),
    ("B", "네, 첫 10만 원을 벌면 그다음 100만 원은 생각보다 빨리 옵니다."),
    ("A", "음 그렇군요."),
    ("A", "잠깐 광고 듣고 오겠습니다."),
    ("A", "이 방송은 여러분의 구독과 좋아요로 만들어집니다."),
    ("A", "자 다시 돌아왔습니다."),
    ("A", "그러면 대표님이 겪은 최악의 실수는 뭐였어요?"),
    ("B", "아 이건 정말 부끄러운 이야기인데요."),
    ("B", "제가 처음에 퇴사하고 모은 돈 2천만 원을 강의 사는 데 다 썼어요."),
    ("B", "강의를 열 개 넘게 들었는데 실제로 실행한 건 하나도 없었습니다."),
    ("B", "그때 깨달았죠, 배우는 건 일이 아니고 실행만이 돈이 된다는 걸요."),
    ("A", "와 2천만 원이면 정말 큰돈인데요!"),
    ("B", "네, 그래서 지금은 강의 하나 들으면 무조건 일주일 안에 하나는 실행해 보라고 합니다."),
    ("A", "그건 그렇고 다음 주 게스트 소개를 잠깐 드릴게요."),
    ("A", "다음 주에는 부동산 전문가 분이 나오십니다."),
    ("A", "마지막으로 부업을 시작하려는 분들께 한마디 부탁드려요."),
    ("B", "완벽하게 준비된 날은 절대 오지 않습니다."),
    ("B", "오늘 퇴근하고 딱 30분만, 내가 팔 수 있는 걸 종이에 적어 보세요."),
    ("B", "그 30분이 1년 뒤 여러분의 월급을 바꿀 겁니다."),
    ("A", "오늘 말씀 정말 감사합니다, 다음 주에 또 만나요."),
]


def build(dialogue=DIALOGUE, sec_per_char: float = 0.13, pause: float = 0.55) -> Transcript:
    words: list[Word] = []
    t = 0.4
    for i, (speaker, sentence) in enumerate(dialogue):
        for token in sentence.split():
            d = 0.08 + sec_per_char * len(token)
            words.append(Word(text=token, start=round(t, 3), end=round(t + d, 3), speaker=speaker))
            t += d + 0.06
        t += pause + (0.4 if i % 5 == 4 else 0.0)
    return Transcript(language="ko", duration=round(t + 0.5, 2), words=words)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "examples/sample_podcast.json"
    build().save(out)
    print(f"wrote {out}")
