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
from urllib.parse import urljoin, urlsplit
from pathlib import Path

import requests
from bs4 import BeautifulSoup

VALORANT_LIST_URL = "https://playvalorant.com/ko-kr/news/tags/patch-notes/"
PUBG_LIST_URL = "https://pubg.com/ko/news?category=patch_notes"
class SourceError(RuntimeError):
    """Safe, application-authored source diagnostic; contains no credentials."""


BLIZZARD_PATCH_URL = "https://overwatch.blizzard.com/ko-kr/news/patch-notes/"
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

# "관련 글", "관련 기사" 같은 헤딩을 만나면 그 지점부터는 패치 내용이 아니라
# 사이트에서 추천하는 다른 글 목록이므로 더 이상 읽지 않는다.
STOP_HEADING_RE = re.compile(r"(관련\s*글|관련\s*기사|관련\s*뉴스|함께\s*보면)")

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


def parse_overwatch_links(hrefs):
    """Accept numeric article paths with or without a slug; ignore navigation."""
    ids = []
    for href in hrefs:
        url = urlsplit(urljoin(OVERWATCH_LIST_URL, href or ""))
        if url.scheme != "https" or url.netloc != "overwatch.nexon.com":
            continue
        if not re.fullmatch(r"/news/patchnotes/\d+(?:/[^/?#]+)?/?", url.path):
            continue
        canonical = f"https://overwatch.nexon.com{url.path.rstrip('/')}"
        if canonical not in ids:
            ids.append(canonical)
    return ids


def fetch_nexon_overwatch_ids():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            response = page.goto(OVERWATCH_LIST_URL, wait_until="domcontentloaded", timeout=30000)
            if response and response.status >= 400:
                raise SourceError(f"넥슨 오버워치 목록 HTTP {response.status}")
            # Wait for article links, not the first navigation link on the page.
            try:
                page.wait_for_function(r"""() => Array.from(document.querySelectorAll('a[href]'))
                    .some(a => {
                        const u = new URL(a.href, location.href);
                        return u.hostname === 'overwatch.nexon.com' &&
                            /^\/news\/patchnotes\/\d+(?:\/[^/?#]+)?\/?$/.test(u.pathname);
                    })""", timeout=30000)
            except Exception:
                # Safe bounded diagnostics: only same-site patch-note paths, never tokens/query strings.
                hrefs = page.locator('a[href]').evaluate_all("nodes => nodes.map(a => a.href)")
                paths = []
                for href in hrefs:
                    url = urlsplit(href)
                    if url.hostname == 'overwatch.nexon.com' and url.path.startswith('/news/patchnotes'):
                        if url.path not in paths:
                            paths.append(url.path)
                print(f"[오버워치] 게시글 로딩 대기 실패: 전체 링크 {len(hrefs)}개, 패치 경로 {paths[:10]}",
                      file=sys.stderr)
                raise SourceError("넥슨 패치노트 게시글 링크를 찾지 못했습니다.") from None
            hrefs = page.locator('a[href]').evaluate_all("nodes => nodes.map(a => a.href)")
            ids = parse_overwatch_links(hrefs)
            print(f"[오버워치] 넥슨 목록 수집: 게시글 {len(ids)}개", flush=True)
            return ids
        finally:
            browser.close()


def blizzard_blocks(html):
    soup = BeautifulSoup(html, "html.parser")
    blocks = []
    seen = set()
    for anchor in soup.select('[id^="patch-"]'):
        patch_id = anchor['id']
        if patch_id in seen:
            continue
        # Live HTML uses an empty .anchor INSIDE the patch container.
        # The date ID identifies a location; its parent contains title and body.
        if 'PatchNotes-patch' in anchor.get('class', []):
            container = anchor
        else:
            container = anchor.find_parent(class_='PatchNotes-patch')
        if container is None:
            # Support a date anchor immediately before a sibling patch container.
            sibling = anchor.find_next_sibling()
            if sibling and 'PatchNotes-patch' in sibling.get('class', []):
                container = sibling
        if container is None:
            continue
        if not container.select_one('.PatchNotes-patchTitle, h2, h3, h4') or not container.select_one('p, li'):
            continue
        # Work on an independent copy: do not change source DOM ancestry/IDs.
        block = BeautifulSoup(str(container), 'html.parser').find()
        block['id'] = patch_id
        blocks.append(block)
        seen.add(patch_id)
    return blocks


def report_blizzard_structure(html, stage, status):
    soup = BeautifulSoup(html, 'html.parser')
    report = {
        'stage': stage, 'status': status, 'html_chars': len(html),
        'patch_id_nodes': len(soup.select('[id^="patch-"]')),
        'patch_containers': len(soup.select('.PatchNotes-patch')),
        'patch_titles': len(soup.select('.PatchNotes-patchTitle')),
        'patch_bodies': len(soup.select('.PatchNotes-patchBody')),
        'accepted_blocks': len(blizzard_blocks(html)),
        'patch_nodes': [{'tag': n.name, 'id': n.get('id'), 'classes': n.get('class', []),
                         'headings': len(n.select('h2,h3,h4')), 'paragraphs': len(n.select('p,li'))}
                        for n in soup.select('[id^="patch-"], .PatchNotes-patch')[:15]],
    }
    print('[오버워치 구조 진단] ' + json.dumps(report, ensure_ascii=False), flush=True)
    output = os.environ.get('SOURCE_DIAGNOSTIC_DIR')
    if output:
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'blizzard-{stage}.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def load_blizzard_html(url):
    response = requests.get(url.split('#')[0], headers=HEADERS, timeout=30)
    report_blizzard_structure(response.text, 'http', response.status_code)
    if response.status_code >= 400:
        raise SourceError(f'블리자드 HTML 요청 HTTP {response.status_code}')
    if blizzard_blocks(response.text):
        return response.text
    # Some versions hydrate patch blocks in the browser.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=HEADERS['User-Agent'])
            response = page.goto(url, wait_until='domcontentloaded', timeout=30000)
            if response and response.status >= 400:
                raise SourceError(f'블리자드 패치노트 HTTP {response.status}')
            try:
                page.wait_for_selector('[id^="patch-"]', state='attached', timeout=30000)
            finally:
                report_blizzard_structure(page.content(), 'browser', response.status if response else None)
            return page.content()
        finally:
            browser.close()


def fetch_blizzard_overwatch_ids():
    blocks = blizzard_blocks(load_blizzard_html(BLIZZARD_PATCH_URL))
    if not blocks:
        raise SourceError('블리자드 날짜별 패치 블록을 찾지 못했습니다.')
    ids = list(dict.fromkeys(f"{BLIZZARD_PATCH_URL}#{block['id']}" for block in blocks))
    print(f'[오버워치] 블리자드 날짜별 패치 {len(ids)}개 수집', flush=True)
    return ids


def fetch_overwatch_ids():
    # Set OVERWATCH_SOURCE=blizzard to bypass Nexon explicitly during diagnosis.
    if os.environ.get('OVERWATCH_SOURCE') == 'blizzard':
        return fetch_blizzard_overwatch_ids()
    try:
        ids = fetch_nexon_overwatch_ids()
        if ids:
            return ids
    except Exception as exc:
        print(f'[오버워치] 넥슨 수집 실패 ({type(exc).__name__}), 블리자드로 전환', flush=True)
    return fetch_blizzard_overwatch_ids()


def parse_blizzard_article(html, url):
    fragment = urlsplit(url).fragment
    block = next((b for b in blizzard_blocks(html) if b['id'] == fragment), None)
    if block is None:
        raise ValueError('요청한 날짜의 블리자드 패치 본문을 찾지 못했습니다.')
    title_tag = block.select_one('.PatchNotes-patchTitle, h2, h3, h4')
    title = title_tag.get_text(' ', strip=True)
    body = block.select_one('.PatchNotes-patchBody') or block
    raw = body.get_text('\n', strip=True)
    sections = []
    for heading in body.select('h2,h3,h4'):
        bullets = []
        for element in heading.find_all_next():
            if element not in body.descendants or element.name in ['h2', 'h3', 'h4']:
                break
            if element.name in ['p', 'li'] and not element.find(['p', 'li']):
                text = element.get_text(' ', strip=True)
                if text and text not in bullets:
                    bullets.append(text)
        if bullets:
            sections.append({'title': heading.get_text(' ', strip=True), 'bullets': bullets[:MAX_BULLETS]})
    return {'title': title, 'url': url, 'description': raw[:400], 'image': None,
            'sections': sections[:MAX_SECTIONS], 'raw_text': raw[:16000]}


# ---------------------------------------------------------------------------
# 개별 글 페이지에서 제목/설명/이미지/섹션 파싱 (두 사이트 공통 로직)
# ---------------------------------------------------------------------------

def fetch_article_details(url):
    if urlsplit(url).hostname == "overwatch.blizzard.com":
        return parse_blizzard_article(load_blizzard_html(url), url)
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

    # "관련 글" 같은 헤딩을 만나면 그 이후는 잘라낸다 (패치 내용이 아니므로).
    trimmed_headings = []
    for h in headings:
        if STOP_HEADING_RE.search(h.get_text(" ", strip=True)):
            break
        trimmed_headings.append(h)
    headings = trimmed_headings

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
        if el.name in ("h2", "h3", "h4") and STOP_HEADING_RE.search(el.get_text(" ", strip=True)):
            break  # "관련 글" 등 패치 내용이 아닌 구간을 만나면 그만 모은다
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# AI 요약 (운영에서는 성공한 요약만 전송, 실패 시 보류)
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
AI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"

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
    "원문 끝부분에 '관련 글', '이전 패치노트' 목록처럼 이번 패치와 무관한 다른 글 "
    "추천 목록이 섞여 있을 수 있는데, 이런 내용은 절대 섹션으로 만들지 마세요. "
    "원문에 없는 내용을 추측해서 넣지 마세요. "
    "제품명, 장치명, 영웅명, 무기명, 모드명, 도전 과제명 등 고유명사는 "
    "원문의 철자와 언어를 그대로 유지하세요. 익숙한 이름으로 추정해 번역하거나 "
    "음역하거나 오타를 임의로 교정하지 마세요. 예를 들어 원문이 DuanSense이면 "
    "듀얼센스나 DualSense로 바꾸지 말고 DuanSense 그대로 쓰세요. "
    "어떤 도전 과제의 문제인지 식별할 수 있도록 원문에 명시된 도전 과제명도 보존하세요. "
    "버그 수정은 emoji=🛠️, title=버그 수정 섹션에 정리하세요. "
    "리플레이 코드 삭제·초기화, 호환성 제한, 이용 제한, 사용자 조치 필요 등 "
    "이용자가 알아야 할 주의사항은 버그 수정과 섞지 말고 별도의 마지막 섹션 "
    "emoji=⚠️, title=참고사항에 정리하세요. "
    "예를 들어 원문에 리플레이 코드가 모두 제거되었다고 쓰여 있으면 "
    "참고사항에 '기존 리플레이 코드 모두 삭제'로 정리하세요. "
    "이러한 주의사항은 중요하므로 다른 변경점보다 먼저 누락되지 않게 확보하세요. "
    "동일 항목을 버그 수정과 참고사항에 중복해서 넣지 마세요. "
    "원문에 해당 내용이 없으면 참고사항 섹션을 만들지 마세요. "
    "고유명사 보존과 의미의 정확성이 40자 내외 길이 목표보다 우선합니다."
)


def available_flash_model():
    """Discover a stable text Flash model; availability does not imply free quota."""
    token = None
    names = []
    for _ in range(10):
        params = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        response = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": GEMINI_API_KEY}, params=params, timeout=30,
        )
        if response.status_code != 200:
            print(f"[경고] Gemini 모델 목록 조회 HTTP {response.status_code}", file=sys.stderr)
            return None
        payload = response.json()
        for model in payload.get("models", []):
            name = model.get("name", "").removeprefix("models/")
            if (re.fullmatch(r"gemini-\d+(?:\.\d+)?-flash(?:-lite)?", name)
                    and "generateContent" in model.get("supportedGenerationMethods", [])):
                names.append(name)
        token = payload.get("nextPageToken")
        if not token:
            break
    if token:
        print("[경고] Gemini 모델 목록 조회가 완료되지 않았습니다.", file=sys.stderr)
        return None
    if not names:
        print("[경고] 사용 가능한 안정 버전 Flash 텍스트 모델이 없습니다.", file=sys.stderr)
        return None
    return max(names, key=lambda name: (tuple(int(n) for n in re.findall(r"\d+", name)),
                                       name.endswith("-lite")))


def summarize_with_ai(raw_text):
    if not GEMINI_API_KEY or not raw_text:
        return None
    try:
        model = AI_MODEL.removeprefix("models/")
        request_body = {
            "systemInstruction": {"parts": [{"text": AI_SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": raw_text}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 8192},
        }
        def generate(selected):
            print(f"[AI] Gemini 모델: {selected}", flush=True)
            for attempt in range(2):
                try:
                    return requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{selected}:generateContent",
                        headers={"x-goog-api-key": GEMINI_API_KEY},
                        json=request_body,
                        timeout=(10, 180),
                    )
                except requests.exceptions.ReadTimeout:
                    if attempt == 1:
                        raise
                    print("[경고] Gemini 응답 대기 시간 초과. 한 번만 재시도합니다.", file=sys.stderr, flush=True)
        resp = generate(model)
        if resp.status_code == 404:
            print(f"[경고] 모델 {model}을 찾지 못해 사용 가능한 Flash 모델을 조회합니다.", file=sys.stderr)
            discovered = available_flash_model()
            if not discovered or discovered == model:
                return None
            resp = generate(discovered)
        if resp.status_code != 200:
            # Never log request headers, API keys, or complete provider responses.
            print(f"[경고] Gemini API HTTP {resp.status_code}: 키·무료 할당량·모델 사용 가능 여부를 확인하세요.", file=sys.stderr)
            return None
        candidates = resp.json().get("candidates", [])
        if not candidates or candidates[0].get("finishReason") != "STOP":
            print("[경고] Gemini 요약이 완료되지 않았습니다 (차단 또는 출력 길이 제한).", file=sys.stderr)
            return None
        text = "".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])
                       if not part.get("thought"))
        result = json.loads(text)
        if not isinstance(result, dict) or not isinstance(result.get("intro"), str):
            raise ValueError("잘못된 요약 형식")
        sections = result.get("sections")
        if not isinstance(sections, list) or not sections:
            raise ValueError("요약 섹션 없음")
        for section in sections:
            if not isinstance(section, dict) or not isinstance(section.get("title"), str):
                raise ValueError("잘못된 섹션 형식")
            bullets = section.get("bullets")
            if not isinstance(bullets, list) or not all(isinstance(item, str) for item in bullets):
                raise ValueError("잘못된 항목 형식")
        return result
    except Exception as exc:
        print(f"[경고] Gemini AI 요약 실패 ({type(exc).__name__}).", file=sys.stderr)
        return None


def build_embed_from_ai(details, ai_data, color):
    embed = {
        "title": details["title"][:250],
        "url": details["url"],
        "description": (ai_data.get("intro") or "")[:400].rstrip() + "\n\u200b",
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
        # Preserve one blank line in Discord without adding separate spacer fields.
        for field in fields[:-1]:
            field["value"] += "\n\u200b"
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
    for i, section in enumerate(details["sections"]):
        value = "\n".join(f"• {b}" for b in section["bullets"])
        if len(value) > MAX_FIELD_CHARS:
            value = value[:MAX_FIELD_CHARS] + "…"
        if used_chars + len(value) > 5000:  # 임베드 전체 길이 여유 확보
            break
        if i > 0:
            fields.append({"name": "\u200b", "value": "\u200b", "inline": False})
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

def build_message(article_url, color):
    """글 하나를 읽어서 디스코드로 보낼 임베드를 만든다 (전송은 하지 않는다)."""
    details = fetch_article_details(article_url)
    ai_data = summarize_with_ai(details.get("raw_text"))
    if not ai_data:
        raise SourceError("AI 요약 실패 또는 GEMINI_API_KEY 누락: 원문 전송 없이 보류합니다.")
    return build_embed_from_ai(details, ai_data, color)


def handle_game(label, ids, key, webhook, banner_url, color, state):
    """한 게임의 새 글을 처리한다.

    1) 새 글마다 카드를 먼저 전부 만들어 본다.
    2) 하나라도 만들어졌을 때만 배너를 한 번 보내고, 이어서 카드를 보낸다.
       (카드를 못 만든 날 배너만 올라가서 매일 반복되는 일을 막기 위함)
    3) 실제로 전송에 성공한 글만 "보낸 글 목록"에 기록한다.
    """
    seen = state.setdefault(key, [])
    new_ids = [i for i in ids if i not in seen]
    print(f"[{label}] 목록에서 {len(ids)}개 발견 / 기존 기록 {len(seen)}개 / 새 글 {len(new_ids)}개")
    if ids:
        print(f"[{label}] 목록 첫 번째 주소: {ids[0]}")
    if not new_ids:
        return

    prepared = []
    for article_url in reversed(new_ids):  # 오래된 순
        try:
            prepared.append((article_url, build_message(article_url, color)))
        except Exception as e:
            print(f"[오류] {label} 글 읽기/요약 실패 ({article_url}): {e}", file=sys.stderr)

    if not prepared:
        print(f"[{label}] 새 글은 있었지만 카드를 하나도 못 만들어서 전송하지 않았습니다.")
        return

    send_banner(webhook, banner_url)
    for article_url, embed in prepared:
        try:
            send_discord(webhook, embed)
        except Exception as e:
            print(f"[오류] {label} 전송 실패 ({article_url}): {e}", file=sys.stderr)
            continue  # 기록하지 않아 다음 실행에 재시도
        seen.append(article_url)

    state[key] = seen[-200:]


def main():
    # 커맨드라인 인자로 "valorant", "pubg", "overwatch" 중 하나 이상을
    # 쉼표로 구분해서 주면 그 게임들만 확인한다 (예: "valorant,pubg").
    # 인자가 없으면(all) 전부 확인한다.
    game_filter = sys.argv[1] if len(sys.argv) > 1 else "all"
    games = [g.strip() for g in game_filter.split(",")]

    targets = []  # (표시 이름, 상태 키, 목록 함수, 웹훅, 배너, 색상)
    if "all" in games or "valorant" in games:
        targets.append(("발로란트", "valorant", fetch_valorant_ids, WEBHOOK_VALORANT, VALORANT_BANNER_URL, 0xFF4655))
    if "all" in games or "pubg" in games:
        targets.append(("PUBG", "pubg", fetch_pubg_ids, WEBHOOK_PUBG, PUBG_BANNER_URL, 0xF2A900))
    if "all" in games or "overwatch" in games:
        targets.append(("오버워치", "overwatch", fetch_overwatch_ids, WEBHOOK_OVERWATCH, OVERWATCH_BANNER_URL, 0xF99E1A))

    state, existed = load_state()
    state.setdefault("overwatch", [])

    fetched = {}
    for label, key, fetch_fn, *_ in targets:
        try:
            fetched[key] = fetch_fn()
        except Exception as e:
            fetched[key] = []
            print(f"[오류] {label} 목록 조회 실패: {e}", file=sys.stderr)

    if not existed:
        for _, key, *_ in targets:
            state[key] = fetched[key]
        save_state(state)
        print("최초 실행: 기존 글을 기준선으로 저장했습니다 (알림 없음).")
        return

    for label, key, _fetch_fn, webhook, banner_url, color in targets:
        handle_game(label, fetched[key], key, webhook, banner_url, color, state)

    save_state(state)


if __name__ == "__main__":
    main()
