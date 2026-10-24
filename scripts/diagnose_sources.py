"""Compare public sources without Discord/AI secrets, sends, or state writes."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

import check_patches as patches

SOURCES = {'valorant': patches.VALORANT_LIST_URL, 'pubg': patches.PUBG_LIST_URL,
           'overwatch': patches.OVERWATCH_LIST_URL}


def inspect_html(html, base):
    soup = BeautifulSoup(html, 'html.parser')
    # Public page information only, no cookies, headers, storage, or environment.
    links = [a.get('href', '') for a in soup.select('a[href]')]
    relevant = [link for link in links if any(word in link.lower()
                for word in ['patch', '/news/', 'article', 'board'])]
    main = soup.select_one('article') or soup.select_one('main')
    return {
        'title': soup.title.get_text(' ', strip=True) if soup.title else '',
        'html_chars': len(html),
        'all_links': len(links),
        'relevant_links': list(dict.fromkeys(relevant))[:150],
        'overwatch_accepted_links': patches.parse_overwatch_links(links) if 'overwatch' in base else [],
        'headings': [h.get_text(' ', strip=True)[:150] for h in soup.select('h1,h2,h3,h4')][:30],
        'main_text_chars': len(main.get_text(' ', strip=True)) if main else 0,
        'paragraph_chars': sum(len(p.get_text(' ', strip=True)) for p in soup.select('p,li')),
        'buttons': [b.get_text(' ', strip=True)[:100] for b in soup.select('button')][:25],
        'script_sources': [s.get('src') for s in soup.select('script[src]')][:20],
    }


def diagnose(game, output):
    url = SOURCES[game]
    report = {'game': game, 'source': url}
    try:
        response = requests.get(url, headers=patches.HEADERS, timeout=30)
        report['http'] = {'status': response.status_code, 'url': response.url,
                          'page': inspect_html(response.text, url)}
    except Exception as exc:
        report['http'] = {'error_type': type(exc).__name__}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=patches.HEADERS['User-Agent'])
            failures = []
            page.on('requestfailed', lambda request: failures.append({
                'host': urlsplit(request.url).hostname,
                'path': urlsplit(request.url).path,
                'reason': request.failure,
            }))
            response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
            report['browser'] = {'status': response.status if response else None}
            # Compare immediately after DOM load and after hydration; bounded diagnostics only.
            report['browser']['initial'] = inspect_html(page.content(), url)
            page.wait_for_timeout(15000)
            report['browser']['url'] = page.url
            report['browser']['rendered'] = inspect_html(page.content(), url)
            report['browser']['failed_requests'] = failures[:30]
            links = report['browser']['rendered']['relevant_links']
            if game == 'overwatch':
                candidates = report['browser']['rendered']['overwatch_accepted_links']
            elif game == 'pubg':
                candidates = [link for link in links if patches.re.search(r'/news/\d+', link)]
            else:
                candidates = [link for link in links if 'valorant-patch-notes-' in link]
            if candidates:
                article_url = patches.urljoin(page.url, candidates[0])
                report['article_url'] = article_url
                try:
                    response = requests.get(article_url, headers=patches.HEADERS, timeout=30)
                    report['article_http'] = {'status': response.status_code,
                                              'page': inspect_html(response.text, article_url)}
                except Exception as exc:
                    report['article_http'] = {'error_type': type(exc).__name__}
                response = page.goto(article_url, wait_until='domcontentloaded', timeout=45000)
                page.wait_for_timeout(10000)
                report['article_browser'] = {'status': response.status if response else None,
                                             'page': inspect_html(page.content(), article_url)}
        except Exception as exc:
            report['browser_error_type'] = type(exc).__name__
        finally:
            browser.close()
    output.mkdir(parents=True, exist_ok=True)
    (output / f'{game}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if 'browser_error_type' in report:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('game', choices=SOURCES)
    parser.add_argument('--output', type=Path, default=Path('diagnostic-output'))
    args = parser.parse_args()
    diagnose(args.game, args.output)
