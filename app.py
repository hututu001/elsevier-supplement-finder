#!/usr/bin/env python3
"""Run with Python 3.11+; all dependencies are part of Python's standard library."""
import argparse
import errno
import threading
import webbrowser

from finder.server import Server


def start_server(host, port=None):
    try:
        return Server((host, 13006 if port is None else port))
    except OSError as error:
        if port is not None or (error.errno not in (errno.EACCES, errno.EADDRINUSE)
                                and getattr(error, 'winerror', None) not in (10013, 10048)):
            raise
        # Let the OS select and reserve a free port in the same bind operation.
        return Server((host, 0))


def main():
    parser = argparse.ArgumentParser(description='Elsevier 补充材料查找 · 本地网页端')
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', '0.0.0.0'])
    parser.add_argument('--port', type=int, help='指定端口；默认 13006，不可用时自动选择空闲端口')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    options = parser.parse_args()
    if options.port is not None and not 1 <= options.port <= 65535:
        parser.error('端口应为 1–65535')
    try:
        server = start_server(options.host, options.port)
    except OSError as error:
        parser.exit(1, f'无法启动：端口被占用、被系统保留或当前无法监听。请关闭本工具的旧窗口，或不指定 --port 让程序自动选择端口。\n{error}\n')
    port = server.server_address[1]
    if options.port is None and port != 13006:
        print(f'默认端口 13006 不可用，已自动改用 {port}。', flush=True)
    address = f'http://127.0.0.1:{port}'
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
