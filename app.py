#!/usr/bin/env python3
"""Run with Python 3.11+; all dependencies are part of Python's standard library."""
import argparse
import threading
import webbrowser

from finder.server import Server


def main():
    parser = argparse.ArgumentParser(description='Elsevier 补充材料查找 · 本地网页端')
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', '0.0.0.0'])
    parser.add_argument('--port', type=int, default=13006)
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    options = parser.parse_args()
    if not 1 <= options.port <= 65535:
        parser.error('端口应为 1–65535')
    try:
        server = Server((options.host, options.port))
    except OSError as error:
        parser.exit(1, f'无法启动：端口可能被占用。可用 --port 13007 更换端口。\n{error}\n')
    address = f'http://127.0.0.1:{options.port}'
    print(f'Elsevier Supplement Finder 1.0.0\n{address}\nPress Ctrl+C to stop.', flush=True)
    if not options.no_browser:
        timer = threading.Timer(0.5, lambda: webbrowser.open(address))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
