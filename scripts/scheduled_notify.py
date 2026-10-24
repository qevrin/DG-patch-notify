"""Prepare at 20:30 KST; deliver cached cards at 21:00 KST on a Linux server."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import check_patches as patches

KST = ZoneInfo('Asia/Seoul')
ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('PATCH_DATA_DIR', ROOT / 'state' / 'server'))
GAMES = {
    'valorant': ('fetch_valorant_ids', 'WEBHOOK_VALORANT', 'VALORANT_BANNER_URL', 0xFF4655),
    'pubg': ('fetch_pubg_ids', 'WEBHOOK_PUBG', 'PUBG_BANNER_URL', 0xF2A900),
    'overwatch': ('fetch_overwatch_ids', 'WEBHOOK_OVERWATCH', 'OVERWATCH_BANNER_URL', 0xF99E1A),
}


def due_games(now):
    now = now.astimezone(KST)
    return list(GAMES) if now.weekday() == 2 else ['overwatch']


def in_send_window(now):
    local = now.astimezone(KST)
    return local.hour == 21 and local.minute == 0


def save(path, data):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def load(path, key):
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    # Import the old repository state once, without modifying it.
    legacy = Path(patches.STATE_PATH)
    old = json.loads(legacy.read_text(encoding='utf-8')) if legacy.exists() else {}
    return {'initialized': bool(old.get(key)), 'seen': old.get(key, []), 'pending': []}


def prepare(key, data, path, now):
    fetch_name, _, _, color = GAMES[key]
    ids = getattr(patches, fetch_name)()
    if not ids:
        raise RuntimeError('목록이 비어 있습니다. 사이트 주소/선택자를 확인하세요.')
    if not data['initialized']:
        data.update(initialized=True, seen=ids, pending=[])
        save(path, data)
        logging.info('%s: 최초 기준선 저장, 전송 없음 (%d개)', key, len(ids))
        return
    # Remove unverified legacy cards; their URLs remain unseen and will be rebuilt.
    data['pending'] = [item for item in data['pending'] if item.get('format') == 'ai']
    save(path, data)
    queued = {item['url'] for item in data['pending']}
    failures = 0
    for url in reversed(ids):
        if url in data['seen'] or url in queued:
            continue
        try:
            embed = patches.build_message(url, color)
        except Exception as exc:
            # Avoid logging exception strings containing webhook/API credentials.
            logging.error('%s: 카드 준비 실패 (%s)', key, type(exc).__name__)
            failures += 1
            continue
        data['pending'].append({'url': url, 'embed': embed, 'format': 'ai'})
        queued.add(url)
        save(path, data)
    data['prepared_on'] = now.astimezone(KST).date().isoformat()
    save(path, data)
    logging.info('%s: 준비된 카드 %d개', key, len(data['pending']))
    if failures:
        raise RuntimeError('일부 카드 준비 실패; 성공한 카드는 저장되었습니다.')


def send_game_banner(key, webhook):
    """Upload a local banner first, or use an explicitly configured public URL."""
    banner_path = ROOT / 'assets' / f'{key}_banner.png'
    if banner_path.is_file():
        with banner_path.open('rb') as image:
            response = patches.requests.post(
                webhook, files={'file': (banner_path.name, image, 'image/png')}, timeout=20)
    else:
        banner_url = getattr(patches, GAMES[key][2])
        if not banner_url:
            raise RuntimeError(f'assets/{key}_banner.png 또는 REPO_RAW_BASE 설정 필요')
        response = patches.requests.post(
            webhook, json={'embeds': [{'image': {'url': banner_url}}]}, timeout=20)
    response.raise_for_status()
    logging.info('%s: 배너 전송 완료', key)


def deliver(key, data, path, clock, *, enforce_window=True):
    def allowed():
        return not enforce_window or in_send_window(clock())
    if not allowed():
        logging.warning('%s: 21:00 전송 창 밖입니다. 다음 예정일까지 보류합니다.', key)
        return
    _, webhook_name, _, _ = GAMES[key]
    webhook = getattr(patches, webhook_name)
    if not webhook:
        raise RuntimeError(f'{webhook_name} 설정 필요; 전송 기록은 변경하지 않습니다.')
    if not data['pending']:
        return
    if any(item.get('format') != 'ai' for item in data['pending']):
        raise RuntimeError('이전 대기 카드의 AI 형식을 확인할 수 없습니다. prepare를 먼저 실행하세요.')
    # Do not send cards if the required banner failed.
    today = clock().astimezone(KST).date().isoformat()
    if data.get('banner_on') != today:
        if not allowed():
            return
        send_game_banner(key, webhook)
        data['banner_on'] = today
        save(path, data)
    while data['pending'] and allowed():
        item = data['pending'][0]
        # No scraping or AI calls here. Failed HTTP requests leave the item queued.
        patches.send_discord(webhook, item['embed'])
        data['seen'] = (data['seen'] + [item['url']])[-200:]
        data['pending'].pop(0)
        save(path, data)
    if data['pending']:
        logging.warning('%s: 전송 창 종료, 남은 카드 %d개 보류', key, len(data['pending']))


def run_game(key, action, clock=lambda: datetime.now(KST)):
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / f'{key}.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.error('%s: 다른 작업 실행 중; 중복 실행 생략', key)
            return False
        path = DATA / f'{key}.json'
        try:
            data = load(path, key)
            if action == 'prepare':
                prepare(key, data, path, clock())
            else:
                deliver(key, data, path, clock)
            return True
        except Exception as exc:
            logging.error('%s %s 실패 (%s)', key, action, type(exc).__name__)
            return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'send'])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    keys = due_games(datetime.now(KST))
    # A slow website or webhook for one game must not block the other games.
    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        results = list(pool.map(lambda key: run_game(key, args.action), keys))
    raise SystemExit(0 if all(results) else 1)


if __name__ == '__main__':
    main()
