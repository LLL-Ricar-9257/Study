"""本机 MCP 服务启动 / 停止 / 状态查看脚本（Windows）。

用法（在 Agent/xAgent 目录执行）：
    python scripts/mcp_services.py start
    python scripts/mcp_services.py stop
    python scripts/mcp_services.py status

涉及的相邻项目（不在本仓库内，仅调用，路径相对仓库根目录）：
    宠物医院 Go 后端   Pet-hospital-MCP/pet-hospital-mcp/dist/windows  -> 127.0.0.1:8080
    宠物医院 MCP 服务  Pet-hospital-MCP/pet_hospital_mcp               -> 127.0.0.1:8765/mcp
    AnythingLLM MCP    AnythingLLMMCP                                  -> 127.0.0.1:8081/mcp

注意：AnythingLLM MCP 的 server.py 默认端口是 8080，与宠物医院后端冲突，
这里统一用 PORT=8081 覆盖。日志写到 Agent/xAgent/logs/ 目录（已被 .gitignore 忽略）。
"""
import os
import socket
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(ROOT))
LOG_DIR = os.path.join(ROOT, "logs")

PET_BACKEND_DIR = os.path.join(
    REPO_ROOT, "Pet-hospital-MCP", "pet-hospital-mcp", "dist", "windows"
)
PET_MCP_DIR = os.path.join(REPO_ROOT, "Pet-hospital-MCP", "pet_hospital_mcp")
ANYTHINGLLM_DIR = os.path.join(REPO_ROOT, "AnythingLLMMCP")

SERVICES = [
    {
        "name": "pethospital-backend",
        "port": 8080,
        "exe": os.path.join(PET_BACKEND_DIR, "pethospital.exe"),
        "args": [],
        "cwd": PET_BACKEND_DIR,
        "env": {},
    },
    {
        "name": "pet-hospital-mcp",
        "port": 8765,
        "exe": os.path.join(PET_MCP_DIR, ".venv", "Scripts", "python.exe"),
        "args": ["-m", "pet_hospital_mcp"],
        "cwd": PET_MCP_DIR,
        "env": {},
    },
    {
        "name": "anythingllm-mcp",
        "port": 8081,
        "exe": sys.executable,
        "args": ["server.py"],
        "cwd": ANYTHINGLLM_DIR,
        "env": {"PORT": "8081", "HOST": "127.0.0.1"},
    },
]

DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def port_in_use(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def pids_on_port(port):
    out = subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True, errors="replace"
    ).stdout
    pids = set()
    for line in out.splitlines():
        if "LISTENING" in line and f":{port} " in line:
            pids.add(line.split()[-1])
    return pids


def start():
    os.makedirs(LOG_DIR, exist_ok=True)
    for svc in SERVICES:
        if port_in_use(svc["port"]):
            print(f"[跳过] {svc['name']:<22} 端口 {svc['port']} 已在监听")
            continue
        env = os.environ.copy()
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
        env.update(svc["env"])
        logpath = os.path.join(LOG_DIR, svc["name"] + ".log")
        log = open(logpath, "ab", buffering=0)
        try:
            p = subprocess.Popen(
                [svc["exe"]] + svc["args"],
                cwd=svc["cwd"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=DETACHED,
                close_fds=True,
            )
        except OSError as exc:
            print(f"[失败] {svc['name']:<22} {exc}")
            continue
        print(f"[启动] {svc['name']:<22} pid={p.pid} 端口={svc['port']} 日志={logpath}")


def stop():
    for svc in SERVICES:
        pids = pids_on_port(svc["port"])
        if not pids:
            print(f"[跳过] {svc['name']:<22} 端口 {svc['port']} 无监听进程")
            continue
        for pid in pids:
            r = subprocess.run(
                ["taskkill", "/PID", pid, "/F", "/T"], capture_output=True, text=True
            )
            state = "已停止" if r.returncode == 0 else "停止失败"
            detail = " ".join(r.stdout.split()[:3])
            print(f"[{state}] {svc['name']:<22} pid={pid} {detail}")


def status():
    for svc in SERVICES:
        flag = "监听中" if port_in_use(svc["port"]) else "未运行"
        print(f"{svc['name']:<22} 127.0.0.1:{svc['port']:<5} {flag}")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"start": start, "stop": stop, "status": status}.get(action, status)()
