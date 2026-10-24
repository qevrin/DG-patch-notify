"""GitHub Actions production entry: collect, summarize, banner then AI cards."""
import logging
from datetime import datetime
import scheduled_notify as runner


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    failed = False
    for game in runner.due_games(datetime.now(runner.KST)):
        if not runner.run_game(game, 'prepare'):
            failed = True
            continue
        path = runner.DATA / f'{game}.json'
        try:
            data = runner.load(path, game)
            # Actions schedules may start late. Explicitly allow delivery after preparation.
            # Ubuntu keeps the strict 21:00 window; this entry is only for Actions.
            runner.deliver(game, data, path, lambda: datetime.now(runner.KST), enforce_window=False)
        except Exception as exc:
            logging.error('%s 전송 실패 (%s)', game, type(exc).__name__)
            failed = True
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
