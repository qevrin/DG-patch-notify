# 발로란트 / 배틀그라운드 패치노트 디스코드 자동 알림

GitHub Actions가 한국시각 격주 화요일 오후 9시마다 클라우드에서 자동 실행되기 때문에, **본인 컴퓨터가 꺼져 있어도 동작**합니다.

## 1. 이 폴더를 GitHub 저장소로 올리기

1. GitHub에서 새 저장소를 만듭니다 (Private로 만들어도 됩니다).
2. 이 폴더(patch-notify) 전체를 그 저장소에 push 합니다.

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

디스코드에서 알림을 받을 각 채널마다 웹훅을 하나씩 만들어야 합니다.

1. 알림 받을 채널(예: #발로란트-패치) 우클릭 → **채널 편집**
2. **연동(Integrations)** → **웹후크(Webhooks)** → **새 웹후크**
3. 이름을 정하고 **웹후크 URL 복사** 클릭
4. #배틀그라운드-패치 채널에서도 같은 방식으로 웹훅 하나 더 생성

## 3. GitHub 저장소에 웹훅 URL 등록하기 (Secrets)

저장소 페이지에서:

**Settings → Secrets and variables → Actions → New repository secret**

다음 두 개를 각각 등록합니다.

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_VALORANT` | 발로란트 패치 채널 웹훅 URL |
| `DISCORD_WEBHOOK_PUBG` | 배틀그라운드 패치 채널 웹훅 URL |

> 이슈사항 채널은 나중에 정의되면 `DISCORD_WEBHOOK_VALORANT_ISSUES` 라는 이름으로 같은 방식으로 추가하면 됩니다. (스크립트에 이미 자리는 만들어 두었습니다.)

## 4. Actions 활성화 및 최초 실행

1. 저장소 상단 **Actions** 탭 클릭 → 워크플로우 사용(Enable) 확인
2. `Patch Notes Notifier` 워크플로우 선택 → **Run workflow** 버튼으로 수동 1회 실행

**주의:** 최초 실행에서는 기존에 올라와 있던 패치노트들을 전부 "새 글"로 착각해 한꺼번에 쏟아붓지 않도록, **알림 없이 현재 목록만 기준선으로 저장**하고 끝납니다. 그 다음 실행부터(또는 다음 30분 주기부터) 새로 올라온 패치노트만 정상적으로 알림이 갑니다.

## 5. 잘 도는지 확인하기

- Actions 탭 → 최근 실행 로그에서 `[전송완료]` 또는 `[스킵]` 문구로 확인 가능합니다.
- 저장소의 `state/state.json` 파일이 실행할 때마다 자동으로 갱신되며, 이 파일이 "이미 보낸 글 목록" 역할을 합니다.

## 주기 조정

`.github/workflows/patch-notify.yml`의 `cron: "*/30 * * * *"` 부분을 수정하면 확인 주기를 바꿀 수 있습니다. (예: 1시간마다 → `"0 * * * *"`)

## 참고

- 발로란트 패치노트는 `playvalorant.com` 목록 페이지를 직접 파싱합니다.
- 배틀그라운드 패치노트는 `pubg.com` 목록이 자바스크립트로 렌더링되기 때문에 Playwright(headless 크롬)로 접근합니다. 사이트 구조가 바뀌면 `scripts/check_patches.py`의 선택자를 수정해야 할 수 있습니다.
