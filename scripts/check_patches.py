"""
발로란트 / 배틀그라운드 패치노트를 감지해서 디스코드 채널로 자동 전송하는 스크립트.

v2 변경점:
- PUBG 목록에서 배지("PC"/"콘솔") 텍스트를 제목으로 잘못 인식하던 버그 수정
  → 목록 페이지에서는 게시글 ID만 뽑고, 실제 제목/내용은 개별 글 페이지에서
    다시 정확하게 가져오도록 변경.
- ID를 항상 "https://사이트/.../숫자" 형태로 정규화해서, 실행마다 다른 문자열이
  생성되어 같은 글이 계속 "새 글"로 오인되는 문제를 방지.
- 단순 텍스트 한 줄 대신, 패치노트 본문의 섹션(##) 제목과 항목을 그대로 살려
  디스코드 임베드(제목 + 대표 이미지 + 섹션별 bullet)로 전송.

동작 방식:
1. 각 게임의 공식 패치노트 목록 페이지에서 게시글 링크(ID)를 수집한다.
2. state/state.json 에 저장된 "이미 보낸 ID" 목록과 비교해 새 글만 추린다.
3. 새 글이 있으면 해당 글 페이지를 다시 가져와 제목/설명/이미지/섹션을 파싱하고,
   구조화된 임베드로 디스코드에 전송한다.
4. 보낸 글의 ID를 state.json에 기록한다 (GitHub Actions가 커밋/푸시).

최초 실행(state.json이 없을 때)은 기존 글을 전부 "새 글"로 오인해 한꺼번에
스팸 알림을 보내는 걸 막기 위해, 알림 없이 현재 목록을 기준선(baseline)으로만
저장하고 끝낸다.
"""

import os
import re
import sys
import json

import requests
from bs4 import BeautifulSoup

VALORANT_LIST_URL = "https://playvalorant.com/ko-kr/news/tags/patch-notes/"
PUBG_LIST_URL = "https://pubg.com/ko/news?category=patch_notes"
OVERWATCH_LIST_URL = "https://overwatch.nexon.com/news/patchnotes"

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "state.json")

WEBHOOK_VALORANT = os.environ.get("DISCORD_WEBHOOK_VALORANT")
WEBHOOK_PUBG = os.environ.get("DISCORD_WEBHOOK_PUBG")
WEBHOOK_OVERWATCH = os.environ.get("DISCORD_WEBHOOK_OVERWATCH")
WEBHOOK_VALORANT_ISSUES = os.environ.get("DISCORD_WEBHOOK_VALORANT_ISSUES")  # 나중에 사용

# 저장소 assets 폴더에 넣어둔 배너 이미지의 raw URL. 워크플로우에서
# REPO_RAW_BASE 환경변수로 "https://raw.githubusercontent.com/<owner>/<repo>/main/assets"를 넘겨준다.
_REPO_RAW_BASE = os.environ.get("REPO_RAW_BASE", "")
VALORANT_BANNER_URL = f"{_REPO_RAW_BASE}/valorant_banner.png" if _REPO_RAW_BASE else None
PUBG_BANNER_URL = f"{_REPO_RAW_BASE}/pubg_banner.png" if _REPO_RAW_BASE else None
OVERWATCH_BANNER_URL = f"{_REPO_RAW_BASE}/overwatch_banner.png" if _REPO_RAW_BASE else None

HEADERS = {"User-Agent": "Mozilla/5.0 (patch-notify-bot)"}

MAX_SECTIONS = 8       # 임베드에 담을 최대 섹션(필드) 수
MAX_BULLETS = 5        # 섹션당 최대 bullet 수
MAX_FIELD_CHARS = 950  # 디스코드 필드 value 최대 길이(1024) 여유 두고 자름


# ---------------------------------------------------------------------------
# 상태 저장/로드
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f), True
    return {"valorant": [], "pubg": [], "overwatch": []}, False


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 목록 페이지에서 글 ID(URL) 수집
# ---------------------------------------------------------------------------

def fetch_valorant_ids():
    resp = requests.get(VALORANT_LIST_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    ids = []
    for a in soup.select("a[href*='valorant-patch-notes-']"):
        href = a.get("href")
        # 실제 슬러그는 점(.)이 아니라 하이픈 형식입니다. 예: valorant-patch-notes-13-02
        m = re.search(r"(valorant-patch-notes-\d+-\d+[a-z]?)", href or "")
        if not m:
            continue
        slug = m.group(1).rstrip("/")
        canonical = f"https://playvalorant.com/ko-kr/news/game-updates/{slug}/"
        if canonical not in ids:
            ids.append(canonical)
    return ids


def fetch_pubg_ids():
    # 목록 페이지는 자바스크립트 렌더링이라 Playwright(headless 크롬)로 접근한다.
    # 여기서는 게시글 ID만 뽑고, 제목/본문은 개별 글 페이지(순수 HTML)에서 따로 가져온다.
    from playwright.sync_api import sync_playwright

    ids = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(PUBG_LIST_URL, wait_until="networkidle", timeout=30000)
        # state="attached": 화면에 "보이는" 링크가 아니라 DOM에 "존재하는" 링크가 하나라도
        # 생기면 통과. (숨겨진 네비게이션 링크 때문에 visible 대기 시 타임아웃 나던 문제 수정)
        page.wait_for_selector("a[href]", state="attached", timeout=15000)
        anchors = page.query_selector_all("a[href]")
        for a in anchors:
            href = a.get_attribute("href") or ""
            # 끝이 숫자 ID로 끝나는 게시글 링크만 (카테고리 필터/네비 링크 제외)
            m = re.search(r"/news/(\d+)$", href.split("?")[0])
            if not m:
                continue
            article_id = m.group(1)
            canonical = f"https://pubg.com/ko/news/{article_id}"
            if canonical not in ids:
                ids.append(canonical)
        browser.close()
    return ids


def fetch_overwatch_ids():
    # 목록 페이지가 자바스크립트 렌더링이라 Playwright로 접근.
    # URL 패턴: /news/patchnotes/<숫자id>/<슬러그>
    from playwright.sync_api import sync_playwright

    ids = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(OVERWATCH_LIST_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_selector("a[href]", state="attached", timeout=15000)
        anchors = page.query_selector_all("a[href]")
        for a in anchors:
            href = a.get_attribute("href") or ""
            m = re.search(r"/news/patchnotes/\d+/[^/?#]+", href)
            if not m:
                continue
            path = m.group(0)
            canonical = f"https://overwatch.nexon.com{path}"
            if canonical not in ids:
                ids.append(canonical)
        browser.close()
    return ids


# ---------------------------------------------------------------------------
# 개별 글 페이지에서 제목/설명/이미지/섹션 파싱 (두 사이트 공통 로직)
# ---------------------------------------------------------------------------

def fetch_article_details(url):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        return tag["content"].strip() if tag and tag.get("content") else None

    title = meta("og:title") or (soup.title.get_text(strip=True) if soup.title else url)
    description = meta("og:description") or ""
    image = meta("og:image")

    # 실제 본문 제목(예: "발로란트 13.02 패치 노트")과 일치하는 헤딩 태그를 찾아,
    # 그 지점부터 나오는 헤딩만 훑는다. 이렇게 해야 사이트 상단 메뉴/사이드바에 있는
    # 헤딩("펍지 더 알아보기" 같은 것)을 패치 섹션으로 잘못 인식하지 않는다.
    title_tag = None
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = tag.get_text(" ", strip=True)
        if text and (text in title or title in text):
            title_tag = tag
            break

    # 사이트마다 실제 "섹션 제목"으로 쓰는 헤딩 레벨이 다르다
    # (발로란트/배그는 h2, 오버워치는 h4). title_tag 이후에 가장 많이
    # 등장하는 레벨을 그 사이트의 섹션 헤딩으로 자동 판단한다.
    search_root = title_tag.find_all_next() if title_tag else soup.find_all()
    level_counts = {"h2": 0, "h3": 0, "h4": 0}
    for el in search_root:
        if el.name in level_counts:
            level_counts[el.name] += 1
    heading_level = max(level_counts, key=lambda lv: level_counts[lv]) if any(level_counts.values()) else None

    headings = (
        (title_tag.find_all_next(heading_level) if title_tag else soup.find_all(heading_level))
        if heading_level
        else []
    )

    sections = []
    for h in headings[:MAX_SECTIONS]:
        heading_text = h.get_text(" ", strip=True)
        if not heading_text:
            continue
        bullets = []
        for el in h.find_all_next():
            if el.name == heading_level:
                break
            # 하위 목록을 담고 있는 상위 <li>는 건너뛴다. 상위 li의 텍스트에는
            # 하위 li 내용이 그대로 포함돼 있어서, 둘 다 넣으면 문장이 중복된다.
            if el.name == "li" and not el.find("li"):
                text = el.get_text(" ", strip=True)
                if text and text not in bullets:
                    bullets.append(text)
        if bullets:
            sections.append({"title": heading_text, "bullets": bullets[:MAX_BULLETS]})

    raw_text = _gather_raw_text(title_tag, soup)

    return {
        "title": title,
        "url": url,
        "description": description,
        "image": image,
        "sections": sections,
        "raw_text": raw_text,
    }


def _gather_raw_text(title_tag, soup, max_chars=16000):
    """AI 요약용 원문 텍스트 모음. 정교하게 다듬지 않고 넉넉히 모아서
    AI가 알아서 정리/중복 제거하도록 한다. title_tag를 못 찾은 경우
    (예: 오버워치처럼 제목이 헤딩 태그가 아닌 경우) 문서 전체에서 모은다."""
    source = title_tag.find_all_next(["h2", "h3", "h4", "p", "li"]) if title_tag else soup.find_all(
        ["h2", "h3", "h4", "p", "li"]
    )
    parts = []
    total = 0
    for el in source:
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# AI 요약 (ANTHROPIC_API_KEY가 설정된 경우에만 동작, 실패 시 원문 섹션으로 대체)
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
AI_MODEL = "claude-haiku-4-5-20251001"

AI_SYSTEM_PROMPT = (
    "당신은 게임 패치노트를 한국어 디스코드 커뮤니티용으로 간결하게 정리하는 도우미입니다. "
    "아래 원문 패치노트 텍스트를 읽고, 다음 JSON 형식으로만 답하세요. "
    "다른 설명이나 코드블록 표시 없이 JSON 객체 하나만 출력하세요.\n"
    '{"intro": "패치 전체를 한두 문장으로 요약", '
    '"sections": [{"emoji": "섹션에 어울리는 이모지 1개", "title": "섹션 제목", '
    '"bullets": ["핵심만 담은 짧은 항목", "..."]}]}\n'
    "규칙: 섹션은 최대 6개, 섹션당 bullet은 최대 5개, 각 bullet은 40자 내외로 압축. "
    "오타 수정이나 사소한 UI 정렬처럼 플레이어에게 중요하지 않은 내용은 제외하고, "
    "실질적으로 게임플레이에 영향을 주는 변경사항 위주로 정리하세요. "
    "원문에 없는 내용을 추측해서 넣지 마세요."
)


def summarize_with_ai(raw_text):
    if not ANTHROPIC_API_KEY or not raw_text:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=AI_MODEL,
            max_tokens=1200,
            system=AI_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": raw_text}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()
        text = re.sub(r"^```(json)?\s*|\s*```$", "", text)
        return json.loads(text)
    except Exception as e:
        print(f"[경고] AI 요약 실패, 원문 섹션으로 대체합니다: {e}", file=sys.stderr)
        return None


def build_embed_from_ai(details, ai_data, color):
    embed = {
        "title": details["title"][:250],
        "url": details["url"],
        "description": (ai_data.get("intro") or "")[:400],
        "color": color,
    }
    if details.get("image"):
        embed["thumbnail"] = {"url": details["image"]}

    fields = []
    used_chars = 0
    for section in ai_data.get("sections", [])[:6]:
        bullets = section.get("bullets", [])[:5]
        value = "\n".join(f"• {b}" for b in bullets)
        if len(value) > MAX_FIELD_CHARS:
            value = value[:MAX_FIELD_CHARS] + "…"
        if used_chars + len(value) > 5000:
            break
        emoji = section.get("emoji") or "📌"
        name = f"{emoji} {section.get('title', '')}"[:250]
        fields.append({"name": name, "value": value or "-", "inline": False})
        used_chars += len(value)

    if fields:
        embed["fields"] = fields

    return embed


# ---------------------------------------------------------------------------
# 디스코드 전송
# ---------------------------------------------------------------------------

def build_embed(details, color):
    embed = {
        "title": details["title"][:250],
        "url": details["url"],
        "description": (details["description"] or "")[:400],
        "color": color,
    }
    if details.get("image"):
        embed["thumbnail"] = {"url": details["image"]}

    fields = []
    used_chars = 0
    for section in details["sections"]:
        value = "\n".join(f"• {b}" for b in section["bullets"])
        if len(value) > MAX_FIELD_CHARS:
            value = value[:MAX_FIELD_CHARS] + "…"
        if used_chars + len(value) > 5000:  # 임베드 전체 길이 여유 확보
            break
        fields.append({"name": f"📌 {section['title']}"[:250], "value": value or "-", "inline": False})
        used_chars += len(value)

    if fields:
        embed["fields"] = fields

    return embed


def send_discord(webhook_url, embed):
    if not webhook_url:
        print(f"[스킵] 웹훅이 설정되지 않아 전송 생략: {embed.get('title')}")
        return
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=20)
    resp.raise_for_status()
    print(f"[전송완료] {embed.get('title')}")


def send_banner(webhook_url, banner_url):
    """새 패치노트 카드보다 먼저, 꾸며둔 배너 이미지를 한 장 보낸다."""
    if not webhook_url or not banner_url:
        return
    try:
        resp = requests.post(webhook_url, json={"embeds": [{"image": {"url": banner_url}}]}, timeout=20)
        resp.raise_for_status()
        print(f"[배너 전송완료] {banner_url}")
    except Exception as e:
        print(f"[경고] 배너 전송 실패: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main():
    # 커맨드라인 인자로 "valorant", "pubg", "overwatch" 중 하나 이상을
    # 쉼표로 구분해서 주면 그 게임들만 확인한다 (예: "valorant,pubg").
    # 인자가 없으면(all) 전부 확인한다.
    game_filter = sys.argv[1] if len(sys.argv) > 1 else "all"
    games = [g.strip() for g in game_filter.split(",")]
    check_valorant = "all" in games or "valorant" in games
    check_pubg = "all" in games or "pubg" in games
    check_overwatch = "all" in games or "overwatch" in games

    state, existed = load_state()
    state.setdefault("overwatch", [])

    valorant_ids = []
    if check_valorant:
        try:
            valorant_ids = fetch_valorant_ids()
        except Exception as e:
            print(f"[오류] 발로란트 목록 조회 실패: {e}", file=sys.stderr)

    pubg_ids = []
    if check_pubg:
        try:
            pubg_ids = fetch_pubg_ids()
        except Exception as e:
            print(f"[오류] PUBG 목록 조회 실패: {e}", file=sys.stderr)

    overwatch_ids = []
    if check_overwatch:
        try:
            overwatch_ids = fetch_overwatch_ids()
        except Exception as e:
            print(f"[오류] 오버워치 목록 조회 실패: {e}", file=sys.stderr)

    if not existed:
        if check_valorant:
            state["valorant"] = valorant_ids
        if check_pubg:
            state["pubg"] = pubg_ids
        if check_overwatch:
            state["overwatch"] = overwatch_ids
        save_state(state)
        print("최초 실행: 기존 글을 기준선으로 저장했습니다 (알림 없음).")
        return

    def process(article_url, webhook, color):
        details = fetch_article_details(article_url)
        ai_data = summarize_with_ai(details.get("raw_text"))
        if ai_data:
            embed = build_embed_from_ai(details, ai_data, color)
        else:
            embed = build_embed(details, color)  # AI 미사용/실패 시 원문 섹션으로 대체
        send_discord(webhook, embed)

    # 발로란트: 새 글만 오래된 순으로 처리 (있으면 배너부터 한 번 전송)
    if check_valorant:
        new_valorant = [i for i in valorant_ids if i not in state["valorant"]]
        if new_valorant:
            send_banner(WEBHOOK_VALORANT, VALORANT_BANNER_URL)
        for article_url in reversed(new_valorant):
            try:
                process(article_url, WEBHOOK_VALORANT, color=0xFF4655)
            except Exception as e:
                print(f"[오류] 발로란트 글 처리 실패 ({article_url}): {e}", file=sys.stderr)
                continue  # 실패하면 state에 기록하지 않아 다음 실행에 재시도
            state["valorant"].append(article_url)
        state["valorant"] = state["valorant"][-200:]

    # PUBG: 새 글만 오래된 순으로 처리 (있으면 배너부터 한 번 전송)
    if check_pubg:
        new_pubg = [i for i in pubg_ids if i not in state["pubg"]]
        if new_pubg:
            send_banner(WEBHOOK_PUBG, PUBG_BANNER_URL)
        for article_url in reversed(new_pubg):
            try:
                process(article_url, WEBHOOK_PUBG, color=0xF2A900)
            except Exception as e:
                print(f"[오류] PUBG 글 처리 실패 ({article_url}): {e}", file=sys.stderr)
                continue
            state["pubg"].append(article_url)
        state["pubg"] = state["pubg"][-200:]

    # 오버워치: 새 글만 오래된 순으로 처리 (있으면 배너부터 한 번 전송)
    if check_overwatch:
        new_overwatch = [i for i in overwatch_ids if i not in state["overwatch"]]
        if new_overwatch:
            send_banner(WEBHOOK_OVERWATCH, OVERWATCH_BANNER_URL)
        for article_url in reversed(new_overwatch):
            try:
                process(article_url, WEBHOOK_OVERWATCH, color=0xF99E1A)
            except Exception as e:
                print(f"[오류] 오버워치 글 처리 실패 ({article_url}): {e}", file=sys.stderr)
                continue
            state["overwatch"].append(article_url)
        state["overwatch"] = state["overwatch"][-200:]

    save_state(state)


if __name__ == "__main__":
    main()
