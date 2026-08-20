# 발로란트 / 배틀그라운드 / 오버워치 패치노트 디스코드 자동 알림

GitHub Actions가 클라우드에서 정해진 주기로 자동 실행되기 때문에, **본인 컴퓨터가 꺼져 있어도 동작**합니다.

## 동작 요약

1. 정해진 시각에 GitHub 서버가 자동으로 깨어남
2. 각 게임의 공식 패치노트 목록을 확인
3. 이전에 보낸 적 없는 **새 글만** 골라냄 (`state/state.json`에 기록된 목록과 비교)
4. 새 글이 있으면:
   - (배너 이미지가 설정되어 있으면) **꾸며둔 배너 이미지를 먼저** 한 장 전송
   - 그 글의 실제 페이지 본문을 가져와 **Claude AI로 핵심만 요약**한 카드를 전송
   - AI 요약이 실패하거나 키가 없으면 원문 섹션을 그대로 정리한 형태로 자동 대체
5. 게임별로 지정된 디스코드 채널에 임베드 카드(제목 + 요약 + 대표 이미지 + 섹션별 항목)로 전송
6. 보낸 글 목록을 저장소에 자동 커밋 → 다음 실행 때 중복 전송 방지
7. 새 글이 하나도 없으면 아무 메시지도 안 보내고 조용히 종료

## 확인 주기 (게임마다 다름)

패치 주기가 게임마다 달라서, 워크플로우도 2개로 나눠서 운영합니다.

| 게임 | 워크플로우 파일 | 주기 |
|---|---|---|
| 발로란트, 배틀그라운드 | `.github/workflows/patch-notify.yml` | **매주 수요일 오후 9시(한국시간)** |
| 오버워치 | `.github/workflows/overwatch-notify.yml` | **매일 오후 9시(한국시간)** |

- 오버워치는 정규 시즌 업데이트 외에 밸런스 핫픽스가 며칠 간격으로 불규칙하게 나오기 때문에, 발로란트/배그보다 훨씬 자주(매일) 확인합니다.
- 둘 다 UTC 12:00에 실행되도록 cron이 설정되어 있습니다 (`cron: "0 12 * * 3"` = 매주 수요일, `cron: "0 12 * * *"` = 매일).
- 급하게 확인하고 싶을 땐 Actions 탭에서 원하는 워크플로우를 선택해 **Run workflow**로 언제든 수동 실행 가능

## 1. 이 폴더를 GitHub 저장소로 올리기

1. GitHub에서 새 저장소를 만듭니다 (Public 권장 — Actions 실행 시간이 무제한이라 매일 돌려도 부담 없습니다. 웹훅/API 키 같은 민감 정보는 저장소가 공개여도 Secrets에 암호화 저장되어 노출되지 않습니다).
2. 이 폴더 전체를 그 저장소에 push 합니다.

```bash
cd patch-notify
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<내계정>/<저장소이름>.git
git push -u origin main
```

## 2. 디스코드 웹훅 만들기

알림 받을 채널마다 웹훅을 하나씩 만들어야 합니다 (발로란트 패치 채널, 배틀그라운드 패치 채널, 오버워치 패치 채널).

1. 채널 우클릭 → **채널 편집**
2. **연동(Integrations)** → **웹후크(Webhooks)** → **새 웹후크**
3. 이름을 정하고 **웹후크 URL 복사**

## 3. GitHub 저장소에 Secrets 등록하기

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_VALORANT` | 발로란트 패치 채널 웹훅 URL |
| `DISCORD_WEBHOOK_PUBG` | 배틀그라운드 패치 채널 웹훅 URL |
| `DISCORD_WEBHOOK_OVERWATCH` | 오버워치 패치 채널 웹훅 URL |
| `ANTHROPIC_API_KEY` | Claude API 키 (AI 요약용, [console.anthropic.com](https://console.anthropic.com)에서 발급) |

> 발로란트 이슈사항 채널은 정의가 확정되면 `DISCORD_WEBHOOK_VALORANT_ISSUES`라는 이름으로 같은 방식으로 추가하면 됩니다. (스크립트에 이미 자리는 만들어 두었습니다.)

`ANTHROPIC_API_KEY`가 없거나 API 호출이 실패해도 자동으로 원문 섹션 방식으로 대체되어 전송되니, 등록을 깜빡해도 알림 자체가 끊기지는 않습니다.

## 4. 배너 이미지 넣기 (선택)

새 패치노트 카드보다 먼저 꾸며둔 배너 이미지를 보내고 싶다면, 저장소의 `assets` 폴더에 아래 이름으로 이미지를 올려두면 됩니다. 없는 게임은 그냥 배너 없이 카드만 전송됩니다.

- `assets/valorant_banner.png`
- `assets/pubg_banner.png`
- `assets/overwatch_banner.png`

## 5. Actions 활성화 및 최초 실행

1. 저장소 상단 **Actions** 탭 → 워크플로우 사용(Enable)
2. `Valorant & PUBG Patch Notes Notifier`와 `Overwatch Patch Notes Notifier` 각각 **Run workflow**로 수동 1회 실행

**주의:** `state/state.json`이 없는 완전히 새로운 저장소라면, 최초 실행은 기존 글을 전부 "새 글"로 착각해 한꺼번에 쏟아붓지 않도록 **알림 없이 현재 목록만 기준선으로 저장**하고 끝납니다. 그다음 실행부터 새로 올라온 패치노트만 정상적으로 알림이 갑니다.

## 6. 잘 도는지 확인하기

- Actions 탭 → 최근 실행 로그에서 `[전송완료]`, `[스킵]`, `[경고] AI 요약 실패` 등의 문구로 확인 가능
- `state/state.json` 파일이 실행할 때마다 자동으로 갱신되며, "이미 보낸 글 목록" 역할을 함 (`valorant` / `pubg` / `overwatch` 세 개의 배열로 관리)

## 주기 조정

- 요일/시각을 바꾸려면 해당 워크플로우 파일의 `cron:` 값을 수정하세요. (첫 번째 숫자=분, 두 번째=UTC 시, 다섯 번째=요일(0=일 ~ 6=토, `*`=매일))
- `scripts/check_patches.py`는 커맨드라인 인자로 어떤 게임을 확인할지 받습니다. 쉼표로 여러 개 지정 가능합니다.
  - `python scripts/check_patches.py valorant,pubg`
  - `python scripts/check_patches.py overwatch`
  - 인자 없이 실행하면(`all`) 세 게임 전부 확인

## 참고 (구현 세부사항)

- 발로란트, 오버워치 목록·본문 페이지는 서버에서 바로 HTML로 렌더링되는 부분이 많아 `requests` + `BeautifulSoup`로 직접 스크래핑합니다.
- 배틀그라운드·오버워치의 **목록** 페이지는 자바스크립트로 렌더링되는 페이지라 Playwright(headless 크롬)로 글 ID/링크만 추출하고, **본문**은 개별 글 페이지(순수 HTML)를 다시 `requests`로 가져와 처리합니다.
- 본문 파싱 시, 실제 패치 제목과 일치하는 헤딩 태그를 먼저 찾아 그 이후의 섹션 헤딩만 훑습니다 (사이트 상단 메뉴 등 관련 없는 헤딩을 패치 섹션으로 오인하지 않도록). 사이트마다 섹션 제목에 쓰는 헤딩 레벨이 달라서(발로란트·배그는 `h2`, 오버워치는 `h4`), 실제로 가장 많이 쓰인 레벨을 자동으로 감지해서 사용합니다.
- 중첩된 하위 목록으로 인한 문장 중복은 리프(leaf) `<li>`만 채택하는 방식으로 방지했습니다.
- 사이트 구조가 바뀌면 `scripts/check_patches.py`의 선택자/정규식 수정이 필요할 수 있습니다.
