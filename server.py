import asyncio
import base64
import copy
import ctypes
from ctypes import wintypes
import hashlib
import json
import mimetypes
import os
import pathlib
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import unquote
import uuid
import pythoncom
import win32com.client

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        import uvicorn.loops.asyncio
        uvicorn.loops.asyncio.asyncio_loop_factory = lambda *a, **k: asyncio.SelectorEventLoop
    except Exception:
        pass

if sys.stdout is None or sys.stderr is None:
    _log_file = open(pathlib.Path(__file__).parent / "server.log", "a", encoding="utf-8")
    sys.stdout = _log_file
    sys.stderr = _log_file

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
import httpx
import uvicorn

BASE_DIR = pathlib.Path(__file__).parent
BRAIN_DIR = pathlib.Path(os.environ.get("AGY_BRAIN_DIR", pathlib.Path.home() / ".gemini" / "antigravity" / "brain"))
INDEX_HTML = BASE_DIR / "index.html"
MARKED_JS = BASE_DIR / "marked.min.js"
SECRET_KEY = os.environ.get("AGY_SECRET_KEY", "chanson888")
ID_RE = re.compile(r"^[a-fA-F0-9\-]{8,64}$")
RUNNING = "CASCADE_RUN_STATUS_RUNNING"
MAX_STEPS = 150

app = FastAPI(title="Antigravity Mobile Web Bridge")
app.add_middleware(GZipMiddleware, minimum_size=800)

_CACHED_LS: dict[str, Any] = {"csrf": None, "base_url": None, "cascade_config": None}
_HTTP_CLIENT: httpx.AsyncClient | None = None
_DISCOVER_LOCK = asyncio.Lock()
_TRANSCRIPT_CACHE: dict[str, dict[str, Any]] = {}
_TRAJECTORIES_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}
_MODELS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_DISK_CONV_CACHE: dict[str, Any] = {"ts": 0.0, "items": {}}


class RpcError(Exception):
    pass


@app.exception_handler(RpcError)
async def _rpc_error_handler(_request: Request, exc: RpcError):
    return JSONResponse(status_code=502, content={"error": str(exc)})


@app.exception_handler(Exception)
async def _generic_error_handler(_request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})


def get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(verify=False, timeout=20.0)
    return _HTTP_CLIENT


def invalidate_caches():
    _TRAJECTORIES_CACHE["ts"] = 0.0
    _MODELS_CACHE["ts"] = 0.0
    _DISK_CONV_CACHE["ts"] = 0.0


# ---------------------------------------------------------------------------
# Language server discovery & RPC
# ---------------------------------------------------------------------------

class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def _get_process_listening_ports(pid: int) -> list[int]:
    try:
        iphlpapi = ctypes.windll.iphlpapi
        size = wintypes.DWORD(0)
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), True, 2, 5, 0)
        buf = ctypes.create_string_buffer(size.value)
        if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), True, 2, 5, 0) != 0:
            return []
        num_entries = struct.unpack("I", buf.raw[:4])[0]
        row_size = ctypes.sizeof(MIB_TCPROW_OWNER_PID)
        ports = []
        for i in range(num_entries):
            offset = 4 + i * row_size
            row = MIB_TCPROW_OWNER_PID.from_buffer_copy(buf.raw[offset : offset + row_size])
            if row.dwOwningPid == pid and row.dwState == 2:  # MIB_TCP_STATE_LISTEN = 2
                ports.append(socket.ntohs(row.dwLocalPort))
        return ports
    except Exception:
        return []


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def keep_upnp_alive():
    """Continuously maps external TCP port 8765 on the router and refreshes it every 30 seconds so router UPnP lease never expires."""
    while True:
        try:
            pythoncom.CoInitialize()
            upnp = win32com.client.Dispatch("HNetCfg.NATUPnP")
            mappings = upnp.StaticPortMappingCollection
            if mappings is not None:
                local_ip = get_local_ip()
                if local_ip and not local_ip.startswith("127."):
                    try:
                        mappings.Add(8765, "TCP", 8765, local_ip, True, "AntigravityMobileWeb")
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


def discover_language_server() -> tuple[str, list[int]]:
    """In-process discovery of Antigravity language_server port and CSRF token using WMI and iphlpapi (zero PowerShell popups)."""
    pythoncom.CoInitialize()
    try:
        wmi = win32com.client.GetObject("winmgmts:")
        procs = list(wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'language_server.exe'"))

        if not procs:
            agy_exe = pathlib.Path(r"C:\Users\Chanson Huang\AppData\Local\Programs\antigravity\Antigravity.exe")
            if agy_exe.exists():
                subprocess.Popen([str(agy_exe)], creationflags=0x08000000)
                for _ in range(15):
                    time.sleep(1.0)
                    procs = list(wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'language_server.exe'"))
                    if procs:
                        break

        if not procs:
            raise RpcError("未检测到正在运行的 Antigravity language_server.exe 进程。")

        proc = procs[0]
        pid = int(proc.ProcessId)
        cmd = proc.CommandLine or ""
        m = re.search(r"--csrf_token\s+([^\s\"']+)", cmd)
        if not m:
            raise RpcError("无法从 language_server.exe 获取 CSRF Token。")
        ports = _get_process_listening_ports(pid)
        return m.group(1), ports
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _headers(csrf: str) -> dict[str, str]:
    return {
        "x-codeium-csrf-token": csrf,
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
    }


def _rpc_error_text(resp: httpx.Response) -> str:
    try:
        j = resp.json()
        return j.get("message") or resp.text[:300]
    except Exception:
        return resp.text[:300]


def _is_connect_error(resp: httpx.Response) -> bool:
    """True if the response is a ConnectRPC application error (i.e. we hit the right server)."""
    if resp.status_code in (401, 403, 404):
        return False
    try:
        return "code" in resp.json()
    except Exception:
        return False


async def call_ls_rpc(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = get_http_client()
    path = f"/exa.language_server_pb.LanguageServerService/{method}"

    if _CACHED_LS["csrf"] and _CACHED_LS["base_url"]:
        try:
            resp = await client.post(_CACHED_LS["base_url"] + path, headers=_headers(_CACHED_LS["csrf"]), json=payload)
        except httpx.TransportError:
            _CACHED_LS["base_url"] = None
        else:
            if resp.status_code == 200:
                return resp.json()
            if _is_connect_error(resp):
                raise RpcError(f"{method}: {_rpc_error_text(resp)}")
            _CACHED_LS["base_url"] = None

    async with _DISCOVER_LOCK:
        csrf_token, ports = await asyncio.to_thread(discover_language_server)
    last_err = "no ports"
    for port in sorted(ports, reverse=True):
        for scheme in ("http", "https"):
            base_url = f"{scheme}://127.0.0.1:{port}"
            try:
                resp = await client.post(base_url + path, headers=_headers(csrf_token), json=payload)
            except Exception as e:
                last_err = str(e)
                continue
            if resp.status_code == 200 or _is_connect_error(resp):
                _CACHED_LS["csrf"] = csrf_token
                _CACHED_LS["base_url"] = base_url
                if resp.status_code == 200:
                    return resp.json()
                raise RpcError(f"{method}: {_rpc_error_text(resp)}")
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
    raise RpcError(f"调用 Antigravity RPC ({method}) 失败: {last_err}")


async def get_cached_trajectories(max_age: float = 1.2) -> dict[str, Any]:
    now = time.time()
    if now - _TRAJECTORIES_CACHE["ts"] < max_age and _TRAJECTORIES_CACHE["data"]:
        return _TRAJECTORIES_CACHE["data"]
    data = await call_ls_rpc("GetAllCascadeTrajectories", {"excludeSubtrajectories": True})
    _TRAJECTORIES_CACHE["ts"] = now
    _TRAJECTORIES_CACHE["data"] = data
    return data


async def get_meta(cascade_id: str, max_age: float = 1.0) -> dict[str, Any]:
    try:
        data = await get_cached_trajectories(max_age=max_age)
        meta = data.get("trajectorySummaries", {}).get(cascade_id, {})
        if meta:
            return meta
    except Exception:
        pass
    disk_convs = scan_disk_conversations()
    return disk_convs.get(cascade_id, {})


async def get_cascade_config(cascade_id: str | None = None, model_override: str | None = None) -> dict[str, Any]:
    cfg = None
    if _CACHED_LS["cascade_config"] is not None:
        cfg = copy.deepcopy(_CACHED_LS["cascade_config"])
    else:
        candidate_ids = [cascade_id] if cascade_id else []
        try:
            all_t = await get_cached_trajectories(max_age=5.0)
            for cid in all_t.get("trajectorySummaries", {}).keys():
                if cid not in candidate_ids:
                    candidate_ids.append(cid)
        except Exception:
            pass
        for cid in candidate_ids:
            try:
                traj = await call_ls_rpc("GetCascadeTrajectory", {"cascadeId": cid})
                for st in reversed(traj.get("trajectory", {}).get("steps", [])):
                    ui = st.get("userInput") or {}
                    uc = ui.get("userConfig") or ui.get("lastUserConfig")
                    if uc and uc.get("plannerConfig", {}).get("planModel"):
                        _CACHED_LS["cascade_config"] = copy.deepcopy(uc)
                        cfg = copy.deepcopy(uc)
                        break
                if cfg:
                    break
            except Exception:
                continue

    if not cfg:
        cfg = {
            "plannerConfig": {
                "planModel": "MODEL_PLACEHOLDER_M418",
                "useAiCredits": False,
                "supportsLatexRendering": True,
            },
            "executorConfig": {"useCoreDirect": True},
            "conversationHistoryConfig": {},
        }
    if model_override:
        cfg.setdefault("plannerConfig", {})["planModel"] = model_override
    return cfg


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def check_access(
    request: Request,
    key: str | None = Query(default=None),
    agy_key: str | None = Cookie(default=None),
):
    client_host = (request.client.host if request.client else "") or ""
    if client_host in ("127.0.0.1", "::1"):
        return
    hdr_key = request.headers.get("x-agy-key")
    if key == SECRET_KEY or agy_key == SECRET_KEY or hdr_key == SECRET_KEY:
        return
    raise HTTPException(status_code=403, detail="Forbidden")


def check_id(cascade_id: str):
    if not ID_RE.match(cascade_id):
        raise HTTPException(status_code=400, detail="Invalid conversation ID")


# ---------------------------------------------------------------------------
# Models & quota
# ---------------------------------------------------------------------------

@app.get("/api/models", dependencies=[Depends(check_access)])
async def list_models(refresh: bool = False):
    now = time.time()
    if not refresh and _MODELS_CACHE["data"] and (now - _MODELS_CACHE["ts"] < 4.0):
        return _MODELS_CACHE["data"]

    res = await call_ls_rpc("GetUserStatus", {})
    us = res.get("userStatus", {})
    cm = us.get("cascadeModelConfigData", {})
    default_model = (cm.get("defaultOverrideModelConfig") or {}).get("modelOrAlias", {}).get("model", "MODEL_PLACEHOLDER_M418")

    sort_order = []
    for s in cm.get("clientModelSorts", []):
        for g in s.get("groups", []):
            sort_order.extend(g.get("modelLabels", []))

    by_label: dict[str, dict[str, Any]] = {}
    items = []
    for m in cm.get("clientModelConfigs", []):
        label = m.get("label", "")
        model_enum = (m.get("modelOrAlias") or {}).get("model", "")
        if not label or not model_enum:
            continue
        q = m.get("quotaInfo") or {}
        # In Go/proto3, remaining_fraction has `json:",omitempty"`, so 0.0 is omitted.
        # If resetTime is present but remainingFraction is missing, the quota is at 0.0.
        if "remainingFraction" in q:
            rem = float(q["remainingFraction"])
        elif "resetTime" in q:
            rem = 0.0
        else:
            rem = 1.0
        rem_pct = round(max(0.0, min(1.0, rem)) * 100, 1)
        entry = {
            "label": label,
            "model": model_enum,
            "modelId": m.get("modelId", ""),
            "remainingPct": rem_pct,
            "usedPct": round(100.0 - rem_pct, 1),
            "resetTime": q.get("resetTime", ""),
            "tag": m.get("tagTitle", ""),
        }
        by_label[label] = entry
        items.append(entry)

    if sort_order:
        ordered = [by_label[lbl] for lbl in sort_order if lbl in by_label]
        seen = set(sort_order)
        ordered.extend(it for it in items if it["label"] not in seen)
        items = ordered

    ps = us.get("planStatus") or {}
    plan_info = ps.get("planInfo") or {}
    payload = {
        "defaultModel": default_model,
        "planName": plan_info.get("planName", "Pro"),
        "promptCredits": ps.get("availablePromptCredits", 0),
        "flowCredits": ps.get("availableFlowCredits", 0),
        "models": items,
    }
    _MODELS_CACHE["ts"] = now
    _MODELS_CACHE["data"] = payload
    return payload


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

@app.get("/api/media", dependencies=[Depends(check_access)])
async def serve_media(path: str):
    clean = unquote(path).strip()
    for prefix in ("file:///", "file://"):
        if clean.lower().startswith(prefix):
            clean = clean[len(prefix):]
    if clean.startswith("/") and len(clean) > 2 and clean[2] == ":":
        clean = clean[1:]
    p = pathlib.Path(clean)
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {clean}")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def scan_disk_conversations() -> dict[str, dict[str, Any]]:
    now = time.time()
    if now - _DISK_CONV_CACHE["ts"] < 2.5 and _DISK_CONV_CACHE["items"]:
        return _DISK_CONV_CACHE["items"]

    convs = {}
    if not BRAIN_DIR.exists():
        return convs

    for p in BRAIN_DIR.iterdir():
        if not p.is_dir() or p.name == "tempmediaStorage" or not ID_RE.match(p.name):
            continue
        cid = p.name
        log_dir = p / ".system_generated" / "logs"
        tfile = log_dir / "transcript.jsonl"
        if not tfile.exists():
            tfile = log_dir / "transcript_full.jsonl"
        title = ""
        last_time = ""
        step_count = 0
        if tfile.exists():
            try:
                with open(tfile, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        step_count += 1
                        try:
                            obj = json.loads(line)
                            if "created_at" in obj:
                                last_time = obj["created_at"]
                            if not title and obj.get("type") == "USER_INPUT":
                                c = obj.get("content", "")
                                m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", c, re.DOTALL)
                                req_text = m.group(1).strip() if m else c.strip()
                                for line_text in req_text.splitlines():
                                    line_text = line_text.strip()
                                    if line_text and not line_text.startswith("<") and not line_text.startswith("#"):
                                        title = line_text[:60]
                                        break
                        except Exception:
                            pass
            except Exception:
                pass
        if not last_time:
            st = p.stat()
            last_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))
        convs[cid] = {
            "id": cid,
            "summary": title or "新对话",
            "status": "",
            "stepCount": step_count,
            "lastModifiedTime": last_time,
        }
    _DISK_CONV_CACHE["ts"] = now
    _DISK_CONV_CACHE["items"] = convs
    return convs


@app.get("/api/conversations", dependencies=[Depends(check_access)])
async def list_conversations():
    disk_convs = scan_disk_conversations()
    try:
        rpc_data = await get_cached_trajectories(max_age=1.0)
        rpc_summaries = rpc_data.get("trajectorySummaries", {})
    except Exception:
        rpc_summaries = {}

    merged = dict(disk_convs)
    for cid, meta in rpc_summaries.items():
        if cid in merged:
            merged[cid]["status"] = meta.get("status", merged[cid]["status"])
            if meta.get("summary"):
                merged[cid]["summary"] = meta.get("summary")
            if meta.get("stepCount"):
                merged[cid]["stepCount"] = meta.get("stepCount")
            if meta.get("lastModifiedTime"):
                merged[cid]["lastModifiedTime"] = meta.get("lastModifiedTime")
        else:
            merged[cid] = {
                "id": cid,
                "summary": meta.get("summary") or "新对话",
                "status": meta.get("status", ""),
                "stepCount": meta.get("stepCount", 0),
                "lastModifiedTime": meta.get("lastModifiedTime", ""),
            }

    items = list(merged.values())
    items.sort(key=lambda x: x.get("lastModifiedTime") or "", reverse=True)
    return {"conversations": items}


def _tool_summary(name: str, args: Any) -> dict[str, str]:
    if not isinstance(args, dict):
        args = {}
    return {"name": name or "", "summary": args.get("toolAction") or args.get("toolSummary") or name or ""}


def _parse_transcript_obj(obj: dict[str, Any]) -> dict[str, Any] | None:
    step_type = obj.get("type", "")
    status = obj.get("status", "")
    if status == "ERROR" or step_type == "ERROR":
        content = obj.get("content") or obj.get("error") or obj.get("errorMessage") or "执行过程中出现错误"
        content = re.sub(r"^(Created At:.*?\nCompleted At:.*?\n)", "", content, flags=re.DOTALL).strip()
        return {
            "i": obj.get("step_index"),
            "type": "ERROR",
            "content": content,
            "t": obj.get("created_at", ""),
        }
    if step_type == "USER_INPUT":
        content = obj.get("content", "") or ""
        m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.DOTALL)
        if m:
            content = m.group(1)
        media = [med["uri"] for med in (obj.get("media") or []) if isinstance(med, dict) and med.get("uri")]
        return {"i": obj.get("step_index"), "type": "USER_INPUT", "content": content, "media": media, "t": obj.get("created_at", "")}
    if step_type == "PLANNER_RESPONSE":
        tools = [_tool_summary(tc.get("name", ""), tc.get("arguments")) for tc in (obj.get("tool_calls") or []) if isinstance(tc, dict)]
        return {
            "i": obj.get("step_index"),
            "type": "PLANNER_RESPONSE",
            "content": obj.get("content", "") or "",
            "thinking": obj.get("thinking", "") or "",
            "tools": tools,
            "t": obj.get("created_at", ""),
        }
    return None


def parse_transcript_steps(cascade_id: str) -> tuple[list[dict[str, Any]], int]:
    """Incrementally parses the transcript (only new bytes are read). Returns (steps, max_step_index)."""
    log_dir = BRAIN_DIR / cascade_id / ".system_generated" / "logs"
    full_file = log_dir / "transcript_full.jsonl"
    target = full_file if full_file.exists() else log_dir / "transcript.jsonl"
    if not target.exists():
        _TRANSCRIPT_CACHE.pop(cascade_id, None)
        return [], -1

    st = target.stat()
    entry = _TRANSCRIPT_CACHE.get(cascade_id)
    if entry and entry["path"] == str(target) and entry["mtime"] == st.st_mtime_ns and entry["size"] == st.st_size:
        return entry["steps"], entry["max_idx"]

    if not entry or entry["path"] != str(target) or st.st_size < entry["offset"]:
        entry = {"path": str(target), "offset": 0, "partial": b"", "by_idx": {}, "max_idx": -1, "auto": 0}

    with open(target, "rb") as f:
        f.seek(entry["offset"])
        chunk = f.read()
    entry["offset"] += len(chunk)
    lines = (entry["partial"] + chunk).split(b"\n")
    entry["partial"] = lines.pop()

    def handle(raw: bytes) -> bool:
        if not raw.strip():
            return True
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return False
        idx = obj.get("step_index")
        if isinstance(idx, int):
            entry["max_idx"] = max(entry["max_idx"], idx)
        else:
            entry["auto"] += 1
            idx = f"x{entry['auto']}"
        parsed = _parse_transcript_obj(obj)
        if parsed:
            entry["by_idx"][idx] = parsed
        return True

    for raw in lines:
        handle(raw)
    if entry["partial"] and handle(entry["partial"]):
        entry["partial"] = b""

    by_idx = entry["by_idx"]
    if len(by_idx) > 600:
        for k in list(by_idx.keys())[: len(by_idx) - 600]:
            del by_idx[k]
    entry["steps"] = list(by_idx.values())[-MAX_STEPS:]
    entry["mtime"] = st.st_mtime_ns
    entry["size"] = st.st_size
    _TRANSCRIPT_CACHE[cascade_id] = entry
    return entry["steps"], entry["max_idx"]


_ACTIVITY_LABELS = {
    "PLANNER_RESPONSE": "正在思考",
    "RUN_COMMAND": "正在执行命令",
    "VIEW_FILE": "正在读取文件",
    "LIST_DIRECTORY": "正在浏览目录",
    "SEARCH_WEB": "正在搜索网页",
    "READ_URL_CONTENT": "正在读取网页",
    "CODE_ACTION": "正在修改代码",
    "WRITE_TO_FILE": "正在写入文件",
    "GREP_SEARCH": "正在搜索代码",
    "FIND": "正在查找文件",
    "INVOKE_SUBAGENT": "子代理工作中",
    "SEND_MESSAGE": "正在发送消息",
    "GENERATE_IMAGE": "正在生成图片",
}
_ACTIVE_STATUSES = ("RUNNING", "GENERATING", "PENDING", "WAITING")


def _parse_rpc_step(s: dict[str, Any], idx: int) -> dict[str, Any] | None:
    st_type = (s.get("type") or "").replace("CORTEX_STEP_TYPE_", "")
    status = (s.get("status") or "").replace("CORTEX_STEP_STATUS_", "")
    created = (s.get("metadata") or {}).get("createdAt", "")
    err_msg = s.get("errorMessage") or s.get("error") or ""
    if status in ("ERROR", "FAILED") or err_msg:
        content = err_msg or (s.get("generic") or {}).get("content") or "执行出错"
        content = re.sub(r"^(Created At:.*?\nCompleted At:.*?\n)", "", content, flags=re.DOTALL).strip()
        return {"i": idx, "type": "ERROR", "content": content, "t": created, "live": True}
    if st_type == "USER_INPUT":
        ui = s.get("userInput") or {}
        text_parts = [it.get("text", "") for it in (ui.get("items") or []) if isinstance(it, dict) and it.get("text")]
        media = []
        for it in (ui.get("items") or []):
            if isinstance(it, dict):
                for med in (it.get("media") or []):
                    if isinstance(med, dict) and med.get("uri"):
                        media.append(med["uri"])
        return {"i": idx, "type": "USER_INPUT", "content": "\n".join(text_parts) or ui.get("userResponse", "") or "", "media": media, "t": created}
    if st_type == "PLANNER_RESPONSE":
        pr = s.get("plannerResponse") or {}
        tools = []
        for tc in pr.get("toolCalls") or []:
            if isinstance(tc, dict):
                try:
                    args = json.loads(tc.get("argumentsJson") or "{}")
                except Exception:
                    args = {}
                tools.append(_tool_summary(tc.get("name", ""), args))
        return {
            "i": idx,
            "type": "PLANNER_RESPONSE",
            "content": pr.get("modifiedResponse") or pr.get("response") or "",
            "thinking": pr.get("thinking") or "",
            "tools": tools,
            "t": created,
            "live": True,
        }
    return None


async def fetch_live_tail(cascade_id: str, after_index: int) -> tuple[list[dict[str, Any]], str]:
    """Fetches trajectory steps newer than the transcript (in-progress replies) plus a human-readable activity label."""
    res = await call_ls_rpc("GetCascadeTrajectorySteps", {"cascadeId": cascade_id, "stepOffset": max(0, after_index + 1)})
    out, activity = [], ""
    for k, s in enumerate(res.get("steps", []) or []):
        parsed = _parse_rpc_step(s, after_index + 1 + k)
        if parsed:
            out.append(parsed)
        status = (s.get("status") or "").replace("CORTEX_STEP_STATUS_", "")
        if status in _ACTIVE_STATUSES:
            st_type = (s.get("type") or "").replace("CORTEX_STEP_TYPE_", "")
            activity = _ACTIVITY_LABELS.get(st_type, "正在执行操作")
    return out, activity


@app.get("/api/conversations/{cascade_id}/messages", dependencies=[Depends(check_access)])
async def get_messages(cascade_id: str, v: str = ""):
    check_id(cascade_id)
    steps, max_idx = await asyncio.to_thread(parse_transcript_steps, cascade_id)
    meta = await get_meta(cascade_id)
    status = meta.get("status", "UNKNOWN")
    summary = meta.get("summary") or "新对话"
    step_count = int(meta.get("stepCount") or 0)

    activity = ""
    if status == RUNNING or step_count > max_idx + 1 or not steps:
        try:
            live, activity = await fetch_live_tail(cascade_id, max_idx)
            if live:
                steps = (steps + live)[-MAX_STEPS:]
        except Exception:
            pass
    if status == RUNNING and not activity:
        activity = "正在思考"

    # Last tool summary gives the activity more context
    if activity:
        for s in reversed(steps):
            if s["type"] == "PLANNER_RESPONSE" and s.get("tools"):
                activity = f"{activity} · {s['tools'][-1]['summary']}"
                break

    error_msg = meta.get("errorMessage") or meta.get("error") or ""
    if not error_msg and status in ("CASCADE_RUN_STATUS_ERROR", "CASCADE_RUN_STATUS_FAILED"):
        error_msg = "对话执行中断或遇到异常"

    body = json.dumps([steps, status, summary, activity, error_msg], ensure_ascii=False, sort_keys=True)
    version = hashlib.md5(body.encode("utf-8")).hexdigest()[:16]
    if v and v == version:
        return {"unchanged": True, "status": status, "summary": summary, "version": version, "error": error_msg}
    return {
        "cascade_id": cascade_id,
        "summary": summary,
        "status": status,
        "activity": activity,
        "steps": steps,
        "version": version,
        "error": error_msg,
    }


def _save_uploaded_media(cascade_id: str, image_item: dict[str, Any]) -> dict[str, str] | None:
    raw_data = image_item.get("data") or ""
    if not raw_data:
        return None
    mime_type = image_item.get("mimeType") or image_item.get("mime_type") or ""
    if "," in raw_data and raw_data.startswith("data:"):
        header, encoded = raw_data.split(",", 1)
        if not mime_type and ";" in header:
            mime_type = header[5:].split(";", 1)[0]
    else:
        encoded = raw_data
    try:
        binary_data = base64.b64decode(encoded)
    except Exception:
        return None
    if not mime_type:
        mime_type = "image/jpeg"
    ext = mimetypes.guess_extension(mime_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    if not ext.startswith("."):
        ext = "." + ext
    filename = image_item.get("name") or ""
    if filename and "." in filename:
        custom_ext = "." + filename.rsplit(".", 1)[-1].lower()
        if custom_ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
            ext = custom_ext
    target_dir = BRAIN_DIR / cascade_id / ".user_uploaded"
    target_dir.mkdir(parents=True, exist_ok=True)
    ts_ms = int(time.time() * 1000)
    rand_hex = uuid.uuid4().hex[:8]
    file_name = f"media_{ts_ms}_{rand_hex}{ext}"
    file_path = target_dir / file_name
    file_path.write_bytes(binary_data)
    uri = str(file_path.resolve()).replace("\\", "/")
    return {
        "mimeType": mime_type,
        "uri": uri,
    }


@app.post("/api/conversations", dependencies=[Depends(check_access)])
async def start_conversation(request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    model = (body.get("model") or "").strip() or None
    images = body.get("images") or []
    if not text and not images:
        raise HTTPException(status_code=400, detail="消息或图片不能为空")
    if not text:
        text = "请查看我发送的图片。"
    res = await call_ls_rpc("StartCascade", {"source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT"})
    cascade_id = res.get("cascadeId")
    if not cascade_id:
        raise RpcError("StartCascade 未返回 cascadeId")
    media_items = []
    for img in images:
        if isinstance(img, dict):
            saved = _save_uploaded_media(cascade_id, img)
            if saved:
                media_items.append(saved)
    cascade_config = await get_cascade_config(cascade_id, model_override=model)
    item_payload: dict[str, Any] = {"text": text}
    if media_items:
        item_payload["media"] = media_items
    await call_ls_rpc("SendUserCascadeMessage", {
        "cascadeId": cascade_id,
        "items": [item_payload],
        "cascadeConfig": cascade_config,
    })
    invalidate_caches()
    return {"cascade_id": cascade_id}


@app.post("/api/conversations/{cascade_id}/send", dependencies=[Depends(check_access)])
async def send_message(cascade_id: str, request: Request):
    check_id(cascade_id)
    body = await request.json()
    text = (body.get("text") or "").strip()
    model = (body.get("model") or "").strip() or None
    images = body.get("images") or []
    if not text and not images:
        raise HTTPException(status_code=400, detail="消息或图片不能为空")
    if not text:
        text = "请查看我发送的图片。"
    media_items = []
    for img in images:
        if isinstance(img, dict):
            saved = _save_uploaded_media(cascade_id, img)
            if saved:
                media_items.append(saved)
    cascade_config = await get_cascade_config(cascade_id, model_override=model)
    item_payload: dict[str, Any] = {"text": text}
    if media_items:
        item_payload["media"] = media_items
    await call_ls_rpc("SendUserCascadeMessage", {
        "cascadeId": cascade_id,
        "items": [item_payload],
        "cascadeConfig": cascade_config,
    })
    invalidate_caches()
    return {"ok": True}


@app.post("/api/conversations/{cascade_id}/cancel", dependencies=[Depends(check_access)])
async def cancel_invocation(cascade_id: str):
    check_id(cascade_id)
    await call_ls_rpc("CancelCascadeInvocation", {"cascadeId": cascade_id})
    invalidate_caches()
    return {"ok": True}


@app.patch("/api/conversations/{cascade_id}", dependencies=[Depends(check_access)])
async def rename_conversation(cascade_id: str, request: Request):
    check_id(cascade_id)
    body = await request.json()
    title = (body.get("title") or "").strip()[:120]
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    meta = await get_meta(cascade_id, max_age=0)
    annotations = dict(meta.get("annotations") or {})
    annotations["title"] = title
    await call_ls_rpc("UpdateConversationAnnotations", {"cascadeId": cascade_id, "annotations": annotations})
    invalidate_caches()
    return {"ok": True, "title": title}


@app.delete("/api/conversations/{cascade_id}", dependencies=[Depends(check_access)])
async def delete_conversation(cascade_id: str, force: bool = False):
    check_id(cascade_id)
    meta = await get_meta(cascade_id, max_age=0)
    if meta.get("status") == RUNNING:
        if not force:
            raise HTTPException(status_code=409, detail="该对话正在运行中，请先停止")
        try:
            await call_ls_rpc("CancelCascadeInvocation", {"cascadeId": cascade_id})
            await asyncio.sleep(0.6)
        except Exception:
            pass
    await call_ls_rpc("DeleteCascadeTrajectory", {"cascadeId": cascade_id})
    _TRANSCRIPT_CACHE.pop(cascade_id, None)
    invalidate_caches()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Colab Qwen 27B Bridge & Reverse Proxy
# ---------------------------------------------------------------------------
QWEN_CONFIG_FILE = BASE_DIR / "qwen_endpoint.json"
_QWEN_PROXY_CLIENT: httpx.AsyncClient | None = None


def get_qwen_endpoint() -> str | None:
    if not QWEN_CONFIG_FILE.exists():
        return None
    try:
        data = json.loads(QWEN_CONFIG_FILE.read_text(encoding="utf-8"))
        return data.get("url")
    except Exception:
        return None


def set_qwen_endpoint(url: str | None):
    try:
        QWEN_CONFIG_FILE.write_text(json.dumps({"url": url, "updated_at": time.time()}), encoding="utf-8")
    except Exception as e:
        print(f"Failed to write qwen config: {e}")


def get_qwen_client() -> httpx.AsyncClient:
    global _QWEN_PROXY_CLIENT
    if _QWEN_PROXY_CLIENT is None or _QWEN_PROXY_CLIENT.is_closed:
        _QWEN_PROXY_CLIENT = httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(3600.0, connect=15.0),
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
    return _QWEN_PROXY_CLIENT


@app.post("/api/qwen/register", dependencies=[Depends(check_access)])
async def register_qwen_endpoint(request: Request):
    data = await request.json()
    url = (data.get("url") or "").strip().rstrip("/")
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid URL format")
    set_qwen_endpoint(url)
    return {"ok": True, "url": url}


@app.get("/api/qwen/status", dependencies=[Depends(check_access)])
async def qwen_status():
    url = get_qwen_endpoint()
    if not url:
        return {"online": False, "url": None, "msg": "未注册 Colab 地址"}
    client = get_http_client()
    try:
        resp = await client.get(f"{url}/qwen/health", timeout=2.5)
        online = (resp.status_code == 200)
    except Exception:
        try:
            resp = await client.get(f"{url}/health", timeout=2.5)
            online = (resp.status_code == 200)
        except Exception:
            online = False
    return {"online": online, "url": url}


@app.get("/qwen", dependencies=[Depends(check_access)])
async def qwen_root_redirect():
    return RedirectResponse(url="/qwen/", status_code=307)


@app.api_route(
    "/qwen/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    dependencies=[Depends(check_access)],
)
async def qwen_proxy(path: str, request: Request):
    upstream_base = get_qwen_endpoint()
    if not upstream_base:
        return HTMLResponse(
            """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { background: #0f1117; color: #e6edf3; font-family: -apple-system, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; text-align: center; padding: 20px; box-sizing: border-box; }
  .card { background: #181b24; border: 1px solid #2a2f3d; border-radius: 16px; padding: 28px 20px; max-width: 400px; width: 100%; box-shadow: 0 8px 24px rgba(0,0,0,0.5); }
  h2 { margin: 0 0 10px; font-size: 19px; }
  p { color: #8b949e; font-size: 14px; line-height: 1.6; margin-bottom: 20px; }
  .btn { display: inline-block; background: #4f8cff; color: #fff; padding: 10px 18px; border-radius: 10px; text-decoration: none; font-weight: 500; font-size: 14px; }
</style>
</head>
<body>
  <div class="card">
    <div style="font-size:38px;margin-bottom:12px">🧠</div>
    <h2>Colab Qwen 服务尚未连接</h2>
    <p>请先在 Google Colab 中运行一键部署笔记本，启动后会自动连接并保存所有对话。</p>
    <a href="/" class="btn">返回 Antigravity</a>
  </div>
</body>
</html>""",
            status_code=503,
        )

    target_url = f"{upstream_base}/qwen/{path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    HOP_BY_HOP = {
        "host", "content-length", "transfer-encoding", "connection",
        "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "upgrade",
    }

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    headers["host"] = upstream_base.split("://", 1)[1].split("/", 1)[0]

    body = await request.body()
    client = get_qwen_client()

    try:
        upstream_req = client.build_request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )
        resp = await client.send(upstream_req, stream=True)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"连接 Colab 隧道失败: {e}"})

    resp_headers = {}
    for k, v in resp.headers.items():
        lk = k.lower()
        if lk not in HOP_BY_HOP and lk not in ("content-security-policy", "x-frame-options"):
            resp_headers[k] = v

    resp_headers["X-Frame-Options"] = "SAMEORIGIN"

    async def stream_body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=resp.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@app.get("/static/marked.js")
async def serve_marked():
    if MARKED_JS.exists():
        return FileResponse(MARKED_JS, media_type="application/javascript", headers={"Cache-Control": "public, max-age=604800"})
    return Response(status_code=404)


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(check_access)])
async def index():
    html = INDEX_HTML.read_text(encoding="utf-8")
    resp = HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
    resp.set_cookie(key="agy_key", value=SECRET_KEY, max_age=315360000, httponly=False)
    return resp


if __name__ == "__main__":
    threading.Thread(target=keep_upnp_alive, daemon=True).start()
    print("=== Antigravity Mobile Web Bridge v3 已启动 ===")
    while True:
        try:
            uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning", loop="asyncio")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Uvicorn service restarted after error: {e}")
            time.sleep(1.5)
