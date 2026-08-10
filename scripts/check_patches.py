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

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "state.json")

WEBHOOK_VALORANT = os.environ.get("DISCORD_WEBHOOK_VALORANT")
WEBHOOK_PUBG = os.environ.get("DISCORD_WEBHOOK_PUBG")
WEBHOOK_VALORANT_ISSUES = os.environ.get("DISCORD_WEBHOOK_VALORANT_ISSUES")  # 나중에 사용

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
    return {"valorant": [], "pubg": []}, False


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

    sections = []
    headings = soup.find_all("h2")
    for h in headings[:MAX_SECTIONS]:
        heading_text = h.get_text(" ", strip=True)
        if not heading_text:
            continue
        bullets = []
        for el in h.find_all_next():
            if el.name == "h2":
                break
            if el.name == "li":
                text = el.get_text(" ", strip=True)
                if text and text not in bullets:
                    bullets.append(text)
            if len(bullets) >= MAX_BULLETS:
                # 이후 li는 더 안 모으되, 다음 h2가 나올 때까지는 계속 훑어야
                # 섹션 경계를 놓치지 않는다. 개수만 더 안 늘린다.
                continue
        if bullets:
            sections.append({"title": heading_text, "bullets": bullets[:MAX_BULLETS]})

    return {
        "title": title,
        "url": url,
        "description": description,
        "image": image,
        "sections": sections,
    }


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


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main():
    state, existed = load_state()

    try:
        valorant_ids = fetch_valorant_ids()
    except Exception as e:
        print(f"[오류] 발로란트 목록 조회 실패: {e}", file=sys.stderr)
        valorant_ids = []

    try:
        pubg_ids = fetch_pubg_ids()
    except Exception as e:
        print(f"[오류] PUBG 목록 조회 실패: {e}", file=sys.stderr)
        pubg_ids = []

    if not existed:
        state["valorant"] = valorant_ids
        state["pubg"] = pubg_ids
        save_state(state)
        print("최초 실행: 기존 글을 기준선으로 저장했습니다 (알림 없음).")
        return

    # 발로란트: 새 글만 오래된 순으로 처리
    new_valorant = [i for i in valorant_ids if i not in state["valorant"]]
    for article_url in reversed(new_valorant):
        try:
            details = fetch_article_details(article_url)
            send_discord(WEBHOOK_VALORANT, build_embed(details, color=0xFF4655))
        except Exception as e:
            print(f"[오류] 발로란트 글 처리 실패 ({article_url}): {e}", file=sys.stderr)
            continue  # 실패하면 state에 기록하지 않아 다음 실행에 재시도
        state["valorant"].append(article_url)

    # PUBG: 새 글만 오래된 순으로 처리
    new_pubg = [i for i in pubg_ids if i not in state["pubg"]]
    for article_url in reversed(new_pubg):
        try:
            details = fetch_article_details(article_url)
            send_discord(WEBHOOK_PUBG, build_embed(details, color=0xF2A900))
        except Exception as e:
            print(f"[오류] PUBG 글 처리 실패 ({article_url}): {e}", file=sys.stderr)
            continue
        state["pubg"].append(article_url)

    # 상태 파일이 무한정 커지지 않도록 최근 200개만 유지
    state["valorant"] = state["valorant"][-200:]
    state["pubg"] = state["pubg"][-200:]
    save_state(state)


if __name__ == "__main__":
    main()
