# -*- coding: utf-8 -*-
"""
Grok Register - Web 管理控制台后端
提供 REST API + SSE 实时日志推送，配合 templates/index.html 使用。
启动: python web_server.py
访问: http://localhost:7860
"""

import datetime
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)

# ─────────────────────────── 路径常量 ───────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_EXAMPLE_PATH = BASE_DIR / "config.example.json"
SSO_DIR = BASE_DIR / "sso"
LOG_DIR = BASE_DIR / "logs"
SCRIPT_PATH = BASE_DIR / "DrissionPage_example.py"

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_AS_ASCII"] = False

# ─────────────────────────── 进程状态 ───────────────────────────
_state = {
    "status": "idle",        # idle | running | stopping
    "process": None,
    "current_round": 0,
    "total_rounds": 0,
    "success_count": 0,
    "fail_count": 0,
    "started_at": None,
    "finished_at": None,
    "collected_sso": [],
}
_state_lock = threading.Lock()

# ─────────────────────────── SSE 日志队列 ───────────────────────
_log_queues = []
_log_queues_lock = threading.Lock()
_log_history = []
MAX_HISTORY = 500


def _broadcast_log(line: str):
    """向所有已连接的 SSE 客户端广播一行日志。"""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    msg = f"[{ts}] {line.rstrip()}"
    with _log_queues_lock:
        _log_history.append(msg)
        if len(_log_history) > MAX_HISTORY:
            del _log_history[:-MAX_HISTORY]
        for q in _log_queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def _parse_progress(line: str):
    """从输出行解析进度信息，更新 _state。"""
    with _state_lock:
        if "开始第" in line and "轮注册" in line:
            try:
                part = line.split("开始第")[1].split("轮")[0].strip()
                _state["current_round"] = int(part)
            except Exception:
                pass
        if "本轮注册完成" in line:
            _state["success_count"] += 1
        if "轮失败" in line or "[Error]" in line:
            _state["fail_count"] += 1


# ─────────────────────────── 注册线程 ───────────────────────────
def _run_register_thread(count: int, extra_args: list):
    """在后台线程中启动注册子进程，实时捕获输出。"""
    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "--count", str(count),
    ] + extra_args

    _broadcast_log(f"[Web] 启动注册进程: {' '.join(cmd)}")
    exit_code = -1

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"},
            cwd=str(BASE_DIR),
        )
        with _state_lock:
            _state["process"] = proc
            _state["status"] = "running"

        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            _broadcast_log(line)
            _parse_progress(line)
            if "已追加写入 sso 到文件:" in line:
                _refresh_sso_list()

        proc.wait()
        exit_code = proc.returncode

    except Exception as exc:
        _broadcast_log(f"[Web] 子进程异常: {exc}")

    finally:
        _refresh_sso_list()
        with _state_lock:
            _state["status"] = "idle"
            _state["process"] = None
            _state["finished_at"] = datetime.datetime.now().isoformat()
        _broadcast_log(f"[Web] 注册进程结束，退出码: {exit_code}")


def _refresh_sso_list():
    """扫描所有 sso/*.txt，汇总去重后更新到 _state。"""
    tokens = []
    SSO_DIR.mkdir(exist_ok=True)
    for txt in sorted(SSO_DIR.glob("*.txt")):
        try:
            lines = txt.read_text(encoding="utf-8").splitlines()
            tokens.extend([l.strip() for l in lines if l.strip()])
        except Exception:
            pass
    seen = set()
    deduped = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    with _state_lock:
        _state["collected_sso"] = deduped


# ─────────────────────────── 工具函数 ───────────────────────────
def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    if CONFIG_EXAMPLE_PATH.exists():
        try:
            return json.loads(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_config(data: dict):
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8"
    )


def _list_log_files() -> list:
    LOG_DIR.mkdir(exist_ok=True)
    result = []
    for f in sorted(LOG_DIR.glob("run_*.log"), reverse=True)[:20]:
        stat = f.stat()
        result.append({
            "name": f.name,
            "size": stat.st_size,
            "mtime": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return result


def _push_tokens_impl(new_tokens: list):
    """独立实现 push_sso_to_api，避免 import DrissionPage_example 带来的副作用。"""
    try:
        import urllib3
        import requests as req
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        _broadcast_log("[Push] 缺少 requests 库，请 pip install requests")
        return

    conf = _load_config()
    api_conf = conf.get("api", {})
    endpoint = str(api_conf.get("endpoint", "")).strip()
    api_token = str(api_conf.get("token", "")).strip()
    append_mode = api_conf.get("append", True)

    if not endpoint or not api_token:
        _broadcast_log("[Push] 未配置 API endpoint 或 token，跳过推送")
        return

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    tokens_to_push = [t for t in new_tokens if t]

    get_failed = False
    if append_mode:
        try:
            get_resp = req.get(endpoint, headers=headers, timeout=10, verify=False)
            if get_resp.status_code == 200:
                existing = get_resp.json().get("ssoBasic", [])
                existing_tokens = [
                    item["token"] if isinstance(item, dict) else str(item)
                    for item in existing if item
                ]
                seen = set()
                deduped = []
                for t in existing_tokens + tokens_to_push:
                    if t not in seen:
                        seen.add(t)
                        deduped.append(t)
                tokens_to_push = deduped
                _broadcast_log(
                    f"[Push] 线上已有 {len(existing_tokens)} 个，"
                    f"合并本次 {len(new_tokens)} 个，共 {len(deduped)} 个"
                )
            else:
                _broadcast_log(f"[Push] 查询线上 token 失败: HTTP {get_resp.status_code}，仅推送本次 {len(tokens_to_push)} 个")
                get_failed = True
        except Exception as e:
            host = endpoint.split('/')[2] if '/' in endpoint else endpoint
            _broadcast_log(f"[Push] 无法连接到 {host}，请检查 grok2api 服务是否运行。仅保存本地 SSO 文件。")
            _broadcast_log(f"[Push] 跳过推送（连接错误: {type(e).__name__}）")
            return  # 连接失败直接返回，不尝试 POST

    try:
        resp = req.post(
            endpoint,
            json={"ssoBasic": tokens_to_push},
            headers=headers,
            timeout=60,
            verify=False,
        )
        if resp.status_code == 200:
            _broadcast_log(f"[Push] 推送成功，共 {len(tokens_to_push)} 个 token -> {endpoint}")
        else:
            _broadcast_log(f"[Push] 推送返回异常: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        _broadcast_log(f"[Push] POST 推送失败: {type(e).__name__}: {e}")


# ─────────────────────────── 路由 ───────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ping", methods=["POST"])
def api_ping():
    """测试 grok2api 连通性。"""
    try:
        import urllib3
        import requests as req
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        return jsonify({"ok": False, "msg": "缺少 requests 库"}), 500

    body = request.get_json(force=True, silent=True) or {}
    endpoint = str(body.get("endpoint", "")).strip()
    token = str(body.get("token", "")).strip()

    if not endpoint:
        conf = _load_config()
        endpoint = str(conf.get("api", {}).get("endpoint", "")).strip()
        token = str(conf.get("api", {}).get("token", "")).strip()

    if not endpoint:
        return jsonify({"ok": False, "msg": "未配置 API endpoint"})

    try:
        resp = req.get(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
            verify=False,
        )
        if resp.status_code == 200:
            data = resp.json()
            groups = data.get("tokens") or {}
            count = 0
            if isinstance(groups, dict):
                for _gname, items in groups.items():
                    if isinstance(items, list):
                        count += sum(1 for item in items
                                     if isinstance(item, dict)
                                     and (item.get("token") or "").strip()
                                     and (item.get("token") or "").strip() != _gname)
            elif isinstance(groups, list):
                count = len(groups)
            return jsonify({"ok": True, "msg": f"连接成功，线上共 {count} 个 token", "status": resp.status_code})
        else:
            return jsonify({"ok": False, "msg": f"HTTP {resp.status_code}: {resp.text[:200]}"})
    except Exception as e:
        err = str(e)
        if "10061" in err or "Connection refused" in err.lower() or "NewConnectionError" in err:
            return jsonify({"ok": False, "msg": f"连接被拒绝：{endpoint.split('/')[2] if '/' in endpoint else endpoint} 未运行"})
        return jsonify({"ok": False, "msg": f"连接失败: {type(e).__name__}: {str(e)[:200]}"})


@app.route("/api/status")
def api_status():
    _refresh_sso_list()
    with _state_lock:
        snap = {
            "status": _state["status"],
            "current_round": _state["current_round"],
            "total_rounds": _state["total_rounds"],
            "success_count": _state["success_count"],
            "fail_count": _state["fail_count"],
            "started_at": _state["started_at"],
            "finished_at": _state["finished_at"],
            "sso_count": len(_state["collected_sso"]),
        }
    return jsonify(snap)


@app.route("/api/start", methods=["POST"])
def api_start():
    with _state_lock:
        if _state["status"] == "running":
            return jsonify({"ok": False, "msg": "已有任务正在运行"}), 400

    body = request.get_json(force=True, silent=True) or {}
    count = int(body.get("count", 1))
    extract_numbers = bool(body.get("extract_numbers", False))
    extra = ["--extract-numbers"] if extract_numbers else []

    with _state_lock:
        _state["status"] = "running"
        _state["current_round"] = 0
        _state["total_rounds"] = count
        _state["success_count"] = 0
        _state["fail_count"] = 0
        _state["started_at"] = datetime.datetime.now().isoformat()
        _state["finished_at"] = None

    t = threading.Thread(target=_run_register_thread, args=(count, extra), daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": f"已启动，共 {count} 轮"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with _state_lock:
        proc = _state.get("process")
        if proc is None:
            return jsonify({"ok": False, "msg": "当前没有运行中的任务"}), 400
        _state["status"] = "stopping"
    try:
        proc.terminate()
        _broadcast_log("[Web] 已发送停止信号（SIGTERM）")
        return jsonify({"ok": True, "msg": "已发送停止信号"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(_load_config())


@app.route("/api/config", methods=["POST"])
def api_config_post():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "msg": "无效的 JSON"}), 400
    try:
        _save_config(body)
        return jsonify({"ok": True, "msg": "配置已保存"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/sso")
def api_sso():
    _refresh_sso_list()
    with _state_lock:
        tokens = list(_state["collected_sso"])
    return jsonify({"count": len(tokens), "tokens": tokens})


@app.route("/api/sso/files")
def api_sso_files():
    SSO_DIR.mkdir(exist_ok=True)
    files = []
    for txt in sorted(SSO_DIR.glob("*.txt"), reverse=True):
        stat = txt.stat()
        files.append({
            "name": txt.name,
            "size": stat.st_size,
            "mtime": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return jsonify(files)


@app.route("/api/logs")
def api_logs():
    return jsonify(_list_log_files())


@app.route("/api/logs/<filename>")
def api_log_content(filename):
    if not filename.startswith("run_") or not filename.endswith(".log"):
        return jsonify({"ok": False, "msg": "非法文件名"}), 400
    path = LOG_DIR / filename
    if not path.exists():
        return jsonify({"ok": False, "msg": "文件不存在"}), 404
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return jsonify({"ok": True, "content": content})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/push", methods=["POST"])
def api_push():
    _refresh_sso_list()
    with _state_lock:
        tokens = list(_state["collected_sso"])
    if not tokens:
        return jsonify({"ok": False, "msg": "本地没有可推送的 SSO token"}), 400
    t = threading.Thread(target=_push_tokens_impl, args=(tokens,), daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": f"开始推送 {len(tokens)} 个 token"})


@app.route("/api/log/stream")
def api_log_stream():
    """别名：与 /api/stream 相同，供前端 SSE 使用。"""
    return api_stream()


@app.route("/api/sso/push", methods=["POST"])
def api_sso_push():
    """别名：与 /api/push 相同，供前端推送按钮使用。"""
    return api_push()


@app.route("/api/log/files")
def api_log_files():
    """返回日志文件列表，格式适配前端。"""
    LOG_DIR.mkdir(exist_ok=True)
    files = []
    for f in sorted(LOG_DIR.glob("run_*.log"), reverse=True):
        stat = f.stat()
        files.append({
            "name": f.name,
            "size": _fmt_size(stat.st_size),
            "mtime": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return jsonify({"files": files})


@app.route("/api/log/files/<filename>")
def api_log_file_content(filename):
    """返回单个日志文件内容。"""
    if not filename.startswith("run_") or not filename.endswith(".log"):
        return jsonify({"ok": False, "msg": "非法文件名"}), 400
    path = LOG_DIR / filename
    if not path.exists():
        return jsonify({"ok": False, "msg": "文件不存在"}), 404
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return jsonify({"ok": True, "content": content})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/sso/files/<filename>")
def api_sso_file_content(filename):
    """返回单个 SSO 文件内容。"""
    if ".." in filename or not filename.endswith(".txt"):
        return jsonify({"ok": False, "msg": "非法文件名"}), 400
    path = SSO_DIR / filename
    if not path.exists():
        return jsonify({"ok": False, "msg": "文件不存在"}), 404
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return jsonify({"ok": True, "content": content})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    else:
        return f"{n/1024/1024:.1f} MB"


@app.route("/api/log/stream")
@app.route("/api/stream")
def api_stream():
    client_q = queue.Queue(maxsize=200)
    with _log_queues_lock:
        history_snapshot = list(_log_history)
        _log_queues.append(client_q)

    @stream_with_context
    def generate():
        for line in history_snapshot:
            yield f"data: {json.dumps(line, ensure_ascii=False)}\n\n"
        while True:
            try:
                msg = client_q.get(timeout=20)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield 'data: "__ping__"\n\n'
            except GeneratorExit:
                break
        with _log_queues_lock:
            try:
                _log_queues.remove(client_q)
            except ValueError:
                pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ─────────────────────────── 入口 ───────────────────────────────
if __name__ == "__main__":
    _refresh_sso_list()
    print("[Web] Grok Register 管理控制台启动: http://localhost:7860")
    app.run(host="0.0.0.0", port=7860, debug=False, threaded=True)

