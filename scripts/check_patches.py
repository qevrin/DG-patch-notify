"""
발로란트 / 배틀그라운드 패치노트를 감지해서 디스코드 채널로 자동 전송하는 스크립트.

동작 방식:
1. 각 게임의 공식 패치노트 목록 페이지를 확인한다.
2. 이전 실행 때 저장해둔 state/state.json 과 비교해서 "새 글"만 골라낸다.
3. 새 글이 있으면 해당 게임 채널의 디스코드 웹훅으로 임베드 메시지를 보낸다.
4. 보낸 글의 URL을 state.json에 기록하고 저장한다 (GitHub Actions가 커밋/푸시).

최초 실행(state.json이 없을 때)은 기존 글을 전부 "새 글"로 오인해 한꺼번에
스팸 알림을 보내는 걸 막기 위해, 알림 없이 현재 목록을 기준선(baseline)으로만
저장하고 끝낸다. 그 다음 실행부터 정상적으로 새 글만 알린다.
"""

import os
import re
import sys
import json

import requests
from bs4 import BeautifulSoup

VALORANT_URL = "https://playvalorant.com/ko-kr/news/tags/patch-notes/"
PUBG_URL = "https://pubg.com/ko/news?category=patch_notes"

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "state.json")

WEBHOOK_VALORANT = os.environ.get("DISCORD_WEBHOOK_VALORANT")
WEBHOOK_PUBG = os.environ.get("DISCORD_WEBHOOK_PUBG")
# 이슈사항 채널용 (나중에 정의되면 채워서 사용)
WEBHOOK_VALORANT_ISSUES = os.environ.get("DISCORD_WEBHOOK_VALORANT_ISSUES")

HEADERS = {"User-Agent": "Mozilla/5.0 (patch-notify-bot)"}


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f), True
    return {"valorant": [], "pubg": []}, False


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_discord(webhook_url, title, url, description, color):
    if not webhook_url:
        print(f"[스킵] 웹훅이 설정되지 않아 전송 생략: {title}")
        return
    payload = {
        "embeds": [
            {
                "title": title,
                "url": url,
                "description": description,
                "color": color,
            }
        ]
    }
    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()
    print(f"[전송완료] {title}")


def fetch_valorant_patches():
    resp = requests.get(VALORANT_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    items = []
    for a in soup.select("a[href*='patch-notes']"):
        href = a.get("href")
        if not href:
            continue
        full_url = href if href.startswith("http") else f"https://playvalorant.com{href}"
        text = a.get_text(" ", strip=True)
        m = re.search(r"(발로란트\s*[\d.]+[a-z]?\s*패치\s*노트)", text)
        title = m.group(1) if m else text[:80]
        items.append({"id": full_url, "title": title, "url": full_url})

    return _dedupe(items)


def fetch_pubg_patches():
    # PUBG 뉴스 목록은 클라이언트 렌더링이라 Playwright(headless 브라우저)로 접근한다.
    from playwright.sync_api import sync_playwright

    items = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(PUBG_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_selector("a[href*='/news/']", timeout=15000)
        anchors = page.query_selector_all("a[href*='/news/']")
        for a in anchors:
            href = a.get_attribute("href")
            text = (a.inner_text() or "").strip()
            if not href or "패치" not in text:
                continue
            full_url = href if href.startswith("http") else f"https://pubg.com{href}"
            title = text.split("\n")[0].strip()
            items.append({"id": full_url, "title": title, "url": full_url})
        browser.close()

    return _dedupe(items)


def _dedupe(items):
    seen = set()
    uniq = []
    for it in items:
        if it["id"] not in seen:
            seen.add(it["id"])
            uniq.append(it)
    return uniq


def main():
    state, existed = load_state()

    try:
        valorant_items = fetch_valorant_patches()
    except Exception as e:
        print(f"[오류] 발로란트 패치노트 조회 실패: {e}", file=sys.stderr)
        valorant_items = []

    try:
        pubg_items = fetch_pubg_patches()
    except Exception as e:
        print(f"[오류] PUBG 패치노트 조회 실패: {e}", file=sys.stderr)
        pubg_items = []

    if not existed:
        # 최초 실행: 알림 없이 기준선만 저장
        state["valorant"] = [it["id"] for it in valorant_items]
        state["pubg"] = [it["id"] for it in pubg_items]
        save_state(state)
        print("최초 실행: 기존 글을 기준선으로 저장했습니다 (알림 없음).")
        return

    new_valorant = [it for it in valorant_items if it["id"] not in state["valorant"]]
    for it in reversed(new_valorant):  # 오래된 것부터 순서대로 전송
        send_discord(
            WEBHOOK_VALORANT,
            it["title"],
            it["url"],
            "새 발로란트 패치노트가 등록되었습니다.",
            color=0xFF4655,
        )
        state["valorant"].append(it["id"])

    new_pubg = [it for it in pubg_items if it["id"] not in state["pubg"]]
    for it in reversed(new_pubg):
        send_discord(
            WEBHOOK_PUBG,
            it["title"],
            it["url"],
            "새 배틀그라운드 패치노트가 등록되었습니다.",
            color=0xF2A900,
        )
        state["pubg"].append(it["id"])

    # 상태 파일이 무한정 커지지 않도록 최근 200개만 유지
    state["valorant"] = state["valorant"][-200:]
    state["pubg"] = state["pubg"][-200:]
    save_state(state)


if __name__ == "__main__":
    main()
