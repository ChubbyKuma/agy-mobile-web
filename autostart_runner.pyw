import os
import pathlib
import socket
import subprocess
import sys
import threading
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


def get_local_ip() -> str:
    for target in ("192.168.1.1", "192.168.0.1", "192.168.31.1", "10.0.0.1", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target, 80))
            ip = s.getsockname()[0]
            s.close()
            if ip.startswith(("192.168.", "10.")) and not ip.startswith("10.8.8."):
                return ip
        except Exception:
            pass
    return "127.0.0.1"


def keep_upnp_alive():
    """Independent background watchdog that constantly renews the router's UPnP lease every 30s."""
    while True:
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            upnp = win32com.client.Dispatch("HNetCfg.NATUPnP")
            mappings = upnp.StaticPortMappingCollection
            if mappings is not None:
                lip = get_local_ip()
                if lip and not lip.startswith("127."):
                    try:
                        mappings.Add(8765, "TCP", 8765, lip, True, "AntigravityMobileWeb")
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        time.sleep(30)
                pythoncom.CoUninitialize()
            except Exception:
                pass
        time.sleep(30)


def main():
    threading.Thread(target=keep_upnp_alive, daemon=True).start()
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
