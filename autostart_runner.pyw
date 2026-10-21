import os
import pathlib
import socket
import subprocess
import sys
import time

BASE_DIR = pathlib.Path(__file__).parent.resolve()
PYTHON_EXE = r"C:\Users\Chanson Huang\AppData\Local\Programs\Python\Python311\python.exe"
SERVER_PY = str(BASE_DIR / "server.py")
CREATE_NO_WINDOW = 0x08000000


def is_port_in_use(port: int = 8765) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.2)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def main():
    while True:
        try:
            if not is_port_in_use(8765):
                # 启动 server.py 并同步阻塞等待
                proc = subprocess.Popen(
                    [PYTHON_EXE, SERVER_PY],
                    cwd=str(BASE_DIR),
                    creationflags=CREATE_NO_WINDOW,
                )
                proc.wait()
                time.sleep(1)
            else:
                time.sleep(6)
        except Exception:
            time.sleep(3)


if __name__ == "__main__":
    main()
