"""一键启动器：已在运行则直接打开浏览器；否则启动服务器并自动打开浏览器。

关闭本窗口即停止服务。设置环境变量 TP_NO_BROWSER=1 可禁止自动开浏览器。
"""
from __future__ import annotations

import os
import socket
import threading
import webbrowser

URL = "http://127.0.0.1:8000"


def port_open(port: int = 8000) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def main() -> None:
    if os.environ.get("TP_NO_BROWSER") == "1":
        open_browser = lambda: None  # noqa: E731
    else:
        open_browser = lambda: webbrowser.open(URL)  # noqa: E731

    if port_open():
        print("规划器已在运行，正在打开浏览器…")
        open_browser()
        return

    print("正在启动大交通换乘规划… 浏览器将自动打开 " + URL)
    print("提示：关闭本窗口即停止服务；再次双击桌面图标即可重新启动。")
    threading.Timer(1.8, open_browser).start()

    from travel_planner.web import app
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
