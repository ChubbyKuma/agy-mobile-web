import pathlib
import socket
import subprocess
import time
import os
import sys

BASE_DIR = pathlib.Path(__file__).parent.resolve()
PYTHONW_EXE = r"C:\Users\Chanson Huang\AppData\Local\Programs\Python\Python311\pythonw.exe"
SERVER_PY = str(BASE_DIR / "server.py")
CREATE_NO_WINDOW = 0x08000000


def is_port_in_use(port: int = 8765) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.5)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def is_process_running(name: str) -> bool:
    try:
        res = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}"],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        return name.lower() in res.stdout.lower()
    except Exception:
        return False


def ensure_server_running():
    # 1. 保证桌面端 Antigravity 启动
    agy_exe = pathlib.Path(r"C:\Users\Chanson Huang\AppData\Local\Programs\antigravity\Antigravity.exe")
    if agy_exe.exists() and not is_process_running("Antigravity.exe"):
        try:
            subprocess.Popen([str(agy_exe)], creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass

    # 2. 如果端口没有被占用，启动 server.py
    if not is_port_in_use(8765):
        try:
            subprocess.Popen(
                [PYTHONW_EXE, SERVER_PY],
                cwd=str(BASE_DIR),
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            pass


def main():
    """常驻后台守护：开机自启、检测掉线、秒级拉起"""
    # 首次启动确保运行
    ensure_server_running()

    # 进入静默心跳监听循环（每 10 秒检测一次端口，如果服务挂了自动拉起）
    while True:
        try:
            time.sleep(10)
            if not is_port_in_use(8765):
                ensure_server_running()
        except Exception:
            time.sleep(10)


if __name__ == "__main__":
    main()
