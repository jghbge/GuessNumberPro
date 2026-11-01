#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
猜数字游戏 · Web 版（PC / 手机通用）+ 设置面板
打包：pyinstaller --onefile --name GuessNumberPro --console guess_game_web.py
"""

import base64
import csv
import hashlib
import hmac
import html
import io
import json
import os
import random
import shutil
import socket
import sys
import tempfile
import threading
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


# ============================================================
# 加密存储
# ============================================================
SECRET_KEY = "guess_game_secret_key_#2024@secure!"


def _key_bytes() -> bytes:
    return hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _mac(cipher: bytes) -> str:
    return hmac.new(_key_bytes(), cipher, hashlib.sha256).hexdigest()


def encrypt_text(text: str) -> str:
    cipher = _xor(text.encode("utf-8"), _key_bytes())
    payload = {"c": base64.b64encode(cipher).decode("ascii"), "m": _mac(cipher)}
    return base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def decrypt_text(token: str) -> str:
    raw = base64.b64decode(token.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    cipher = base64.b64decode(payload["c"].encode("ascii"))
    if not hmac.compare_digest(payload.get("m", ""), _mac(cipher)):
        raise ValueError("数据校验失败")
    return _xor(cipher, _key_bytes()).decode("utf-8")


# ============================================================
# 路径 & 配置（支持运行时切换数据目录）
# ============================================================
def _default_base() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"),
                            "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or \
               os.path.join(os.path.expanduser("~"), ".local", "share")
    if not base or base == "~":
        base = tempfile.gettempdir()
    return base


DEFAULT_BASE = _default_base()
CONFIG_DIR = os.path.join(DEFAULT_BASE, "GuessGame")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

_config_lock = threading.Lock()
_paths_lock = threading.Lock()
_paths = {"data_dir": None}


def _read_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def get_data_dir() -> str:
    with _paths_lock:
        if _paths["data_dir"] is None:
            with _config_lock:
                cfg = _read_config()
            d = cfg.get("data_dir") or os.path.join(DEFAULT_BASE, "GuessGame")
            _paths["data_dir"] = d
        return _paths["data_dir"]


def records_file() -> str:
    return os.path.join(get_data_dir(), "records.dat")


def export_dir() -> str:
    return os.path.join(get_data_dir(), "exports")


def ensure_dirs() -> None:
    os.makedirs(get_data_dir(), exist_ok=True)
    os.makedirs(export_dir(), exist_ok=True)


def change_data_dir(raw_path: str) -> dict:
    """更改数据目录并迁移现有记录。"""
    raw = (raw_path or "").strip()
    if not raw:
        raise ValueError("路径不能为空")
    new_dir = os.path.abspath(os.path.expanduser(raw))

    if not os.path.isabs(new_dir):
        raise ValueError("请提供绝对路径")

    try:
        os.makedirs(new_dir, exist_ok=True)
        test = os.path.join(new_dir, ".write_test")
        with open(test, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test)
    except OSError as e:
        raise ValueError(f"目标目录不可写：{e}")

    old_dir = get_data_dir()
    old_records = records_file()

    if os.path.abspath(new_dir) == os.path.abspath(old_dir):
        return {"data_dir": new_dir, "migrated": False, "same": True}

    migrated = False
    if os.path.exists(old_records):
        new_records = os.path.join(new_dir, "records.dat")
        if os.path.exists(new_records):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            try:
                os.replace(
                    new_records,
                    os.path.join(new_dir, f"records.pre_migrate_{stamp}.dat"),
                )
            except OSError:
                pass
        try:
            shutil.copy2(old_records, new_records)
            migrated = True
        except OSError as e:
            raise ValueError(f"迁移记录失败：{e}")

    with _config_lock:
        cfg = _read_config()
        cfg["data_dir"] = new_dir
        _write_config(cfg)

    with _paths_lock:
        _paths["data_dir"] = new_dir

    try:
        os.makedirs(os.path.join(new_dir, "exports"), exist_ok=True)
    except OSError:
        pass

    return {"data_dir": new_dir, "migrated": migrated, "same": False}


# ============================================================
# 记录读写
# ============================================================
_records_lock = threading.Lock()


def _read_records_unlocked() -> list:
    path = records_file()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if not token:
            return []
        data = json.loads(decrypt_text(token))
        return data if isinstance(data, list) else []
    except Exception:
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = os.path.join(get_data_dir(), f"records.corrupt_{stamp}.dat")
            os.replace(path, bak)
        except OSError:
            pass
        return []


def _write_records_unlocked(records: list) -> None:
    ensure_dirs()
    token = encrypt_text(json.dumps(records, ensure_ascii=False))
    d = get_data_dir()
    fd, tmp = tempfile.mkstemp(prefix="records.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        os.replace(tmp, records_file())
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_records() -> list:
    with _records_lock:
        return _read_records_unlocked()


def append_record(record: dict) -> bool:
    with _records_lock:
        records = _read_records_unlocked()
        records.append(record)
        try:
            _write_records_unlocked(records)
            return True
        except OSError:
            return False


# ============================================================
# 游戏逻辑
# ============================================================
LEVELS = {
    "1": {"name": "简单", "max_num": 1000, "max_attempts": 20},
    "2": {"name": "中等", "max_num": 10000, "max_attempts": 30},
    "3": {"name": "困难", "max_num": 100000, "max_attempts": 40},
}

_games: dict = {}
_games_lock = threading.Lock()


def _resolve_level(level: str, payload: dict):
    if level in LEVELS:
        c = LEVELS[level]
        return c["name"], c["max_num"], c["max_attempts"]
    if level == "4":
        try:
            max_num = int(payload.get("max_num"))
            max_att = int(payload.get("max_attempts"))
        except (TypeError, ValueError):
            raise ValueError("自定义难度参数无效")
        if max_num < 2:
            raise ValueError("范围上限至少为 2")
        if max_att < 1:
            raise ValueError("猜测次数至少为 1")
        return f"自定义(1-{max_num}, {max_att}次)", max_num, max_att
    raise ValueError("难度无效")


def _snapshot(g: dict) -> dict:
    attempts = len(g["guesses"])
    return {
        "id": g["id"],
        "name": g["name"],
        "max_num": g["max_num"],
        "max_attempts": g["max_attempts"],
        "low": g["low"],
        "high": g["high"],
        "attempts": attempts,
        "remaining": g["max_attempts"] - attempts,
        "guesses": list(g["guesses"]),
        "marks": list(g["marks"]),
        "finished": g["finished"],
        "answer": g["answer"] if g["finished"] else None,
    }


def new_game(level: str, payload: dict) -> dict:
    name, max_num, max_att = _resolve_level(level, payload)
    answer = random.randint(1, max_num)
    gid = uuid.uuid4().hex
    game = {
        "id": gid, "name": name, "max_num": max_num,
        "max_attempts": max_att, "answer": answer,
        "low": 1, "high": max_num,
        "guesses": [], "marks": [],
        "finished": False, "win": False,
    }
    with _games_lock:
        _games[gid] = game
    return _snapshot(game)


def submit_guess(gid: str, value: int) -> dict:
    record = None
    with _games_lock:
        g = _games.get(gid)
        if g is None:
            raise KeyError("游戏不存在或已过期")
        if g["finished"]:
            raise ValueError("本局已结束")
        if value < g["low"] or value > g["high"]:
            raise ValueError(f"请猜 {g['low']} 到 {g['high']} 之间的数字")
        if value in g["guesses"]:
            raise ValueError("这个数字已经猜过了")

        g["guesses"].append(value)
        attempts = len(g["guesses"])
        answer = g["answer"]

        if value < answer:
            g["low"] = max(g["low"], value + 1)
            direction = "low"
            g["marks"].append("low")
        elif value > answer:
            g["high"] = min(g["high"], value - 1)
            direction = "high"
            g["marks"].append("high")
        else:
            direction = "win"
            g["marks"].append("win")
            g["finished"] = True
            g["win"] = True

        if not g["finished"] and attempts >= g["max_attempts"]:
            direction = "lose"
            g["finished"] = True
            g["win"] = False

        result = _snapshot(g)
        result["direction"] = direction
        result["percent"] = (
            attempts / g["max_attempts"] * 100 if g["win"] else None
        )

        if g["finished"]:
            record = {
                "win": g["win"], "level_name": g["name"],
                "answer": answer, "attempts": attempts,
                "max_attempts": g["max_attempts"],
                "guesses": list(g["guesses"]),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            _games.pop(gid, None)

    if record:
        append_record(record)
    return result


def history_page(page: int, size: int) -> dict:
    records = list(reversed(load_records()))
    total = len(records)
    pages = max(1, (total + size - 1) // size)
    page = max(1, min(page, pages))
    start = (page - 1) * size
    items = []
    for i, r in enumerate(records[start:start + size]):
        items.append({
            "no": total - (start + i),
            "win": r["win"],
            "level_name": r["level_name"],
            "answer": r["answer"],
            "attempts": r["attempts"],
            "max_attempts": r["max_attempts"],
            "percent": (
                f"{r['attempts'] / r['max_attempts'] * 100:.2f}%"
                if r["win"] and r["max_attempts"] else ""
            ),
            "guesses": r["guesses"],
            "time": r["time"],
        })
    return {"page": page, "pages": pages, "total": total, "items": items}


def stats() -> dict:
    records = load_records()
    total = len(records)
    wins = [r for r in records if r["win"]]
    n_win = len(wins)
    wa = sum(r["attempts"] for r in wins)
    wm = sum(r["max_attempts"] for r in wins)
    return {
        "total": total, "wins": n_win,
        "win_rate": (n_win / total * 100) if total else 0.0,
        "win_attempts": wa, "win_max_attempts": wm,
        "win_percent": (wa / wm * 100) if wm else 0.0,
        "avg_attempts": (wa / n_win) if n_win else 0.0,
    }


# ============================================================
# 导出
# ============================================================
def export_csv(records) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["序号", "时间", "难度", "结果", "正确答案",
                "使用次数", "最大次数", "使用次数百分比", "猜测序列"])
    for i, r in enumerate(records, 1):
        pct = (f"{r['attempts'] / r['max_attempts'] * 100:.2f}%"
               if r["win"] and r["max_attempts"] else "")
        guesses = " -> ".join(map(str, r["guesses"])) if r["guesses"] else ""
        w.writerow([
            i, r["time"], r["level_name"],
            "猜对" if r["win"] else "失败",
            r["answer"], r["attempts"], r["max_attempts"], pct, guesses,
        ])
    return buf.getvalue()


def export_html(records) -> str:
    parts = [
        "<!DOCTYPE html>",
        "<html lang='zh-CN'><head><meta charset='utf-8'>",
        "<title>猜数字游戏历史记录</title>",
        "<style>",
        "body{font-family:'Microsoft YaHei',Arial,sans-serif;margin:20px}",
        "h1{color:#333}table{border-collapse:collapse;width:100%}",
        "th,td{border:1px solid #999;padding:6px 10px;text-align:center}",
        "th{background:#f0f0f0}tr:nth-child(even){background:#fafafa}",
        ".win{color:green;font-weight:bold}.lose{color:red;font-weight:bold}",
        "</style></head><body>",
        "<h1>猜数字游戏历史记录</h1>",
        f"<p>导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}，"
        f"共 {len(records)} 条记录</p>",
        "<table><tr>"
        "<th>序号</th><th>时间</th><th>难度</th><th>结果</th>"
        "<th>正确答案</th><th>使用次数</th><th>最大次数</th>"
        "<th>使用次数百分比</th><th>猜测序列</th>"
        "</tr>",
    ]
    for i, r in enumerate(records, 1):
        cls = "win" if r["win"] else "lose"
        status = "猜对" if r["win"] else "失败"
        pct = (f"{r['attempts'] / r['max_attempts'] * 100:.2f}%"
               if r["win"] and r["max_attempts"] else "")
        guesses = (" -> ".join(map(str, r["guesses"]))
                   if r["guesses"] else "")
        parts.append(
            f"<tr><td>{i}</td><td>{html.escape(r['time'])}</td>"
            f"<td>{html.escape(r['level_name'])}</td>"
            f"<td class='{cls}'>{status}</td>"
            f"<td>{r['answer']}</td><td>{r['attempts']}</td>"
            f"<td>{r['max_attempts']}</td><td>{pct}</td>"
            f"<td>{html.escape(guesses)}</td></tr>"
        )
    parts.append("</table></body></html>")
    return "\n".join(parts)


# ============================================================
# 前端页面（内嵌）
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b1220">
<title>猜数字游戏</title>
<style>
:root{
  --bg:#0b1220;--bg-card:#16213a;--bg-input:#0f1830;
  --border:#24334f;--border-strong:#2a3a5c;
  --text:#e8eefc;--text-muted:#8fa3c8;
  --primary:#4f8cff;--primary-2:#3b6fe0;
  --success:#38d39f;--danger:#ff6b6b;--warning:#ffc861;
  --font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  --font-size:16px;--font-color:#e8eefc;
}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--font-color);min-height:100vh;
  font-family:var(--font-family);font-size:var(--font-size);line-height:1.55;
  touch-action:manipulation;transition:background .2s,color .2s}
.hidden{display:none!important}

.topbar{position:fixed;top:0;left:0;right:0;z-index:10;display:flex;
  align-items:center;justify-content:space-between;padding:14px 16px;
  padding-top:calc(14px + env(safe-area-inset-top));
  background:var(--bg-card);border-bottom:1px solid var(--border)}
.brand{font-size:17px;font-weight:700}
.status{font-size:12px;color:var(--text-muted);border:1px solid var(--border);
  border-radius:999px;padding:3px 10px}
.status.bad{color:var(--danger);border-color:var(--danger)}

main{padding:16px;padding-top:calc(74px + env(safe-area-inset-top));
  padding-bottom:calc(96px + env(safe-area-inset-bottom));
  max-width:640px;margin:0 auto}
.title{font-size:16px;font-weight:600;color:var(--text-muted);margin:0 0 12px}

.levels{display:grid;gap:10px;grid-template-columns:1fr}
@media(min-width:520px){.levels{grid-template-columns:1fr 1fr}}
.level{display:flex;flex-direction:column;gap:2px;align-items:flex-start;
  width:100%;padding:14px 16px;border-radius:14px;min-height:64px;
  background:var(--bg-card);border:1px solid var(--border);color:var(--text);
  font-size:16px;text-align:left;cursor:pointer;font-family:inherit;
  transition:transform .08s,background .15s}
.level:active{transform:scale(.985)}
.level b{font-size:16px}
.level small{font-size:12px;color:var(--text-muted)}

.card{background:var(--bg-card);border:1px solid var(--border);
  border-radius:14px;padding:14px;margin-bottom:12px}
input[type=number],select{width:100%;padding:12px 14px;border-radius:12px;
  background:var(--bg-input);border:1px solid var(--border-strong);
  color:var(--text);font-size:16px;outline:none;font-family:inherit}
input[type=number]::-webkit-outer-spin-button,
input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
input:focus,select:focus{border-color:var(--primary)}
label{display:block;font-size:13px;color:var(--text-muted);margin:10px 0 6px}
label:first-child{margin-top:0}

.btn{display:inline-flex;align-items:center;justify-content:center;
  min-height:48px;padding:0 18px;border-radius:12px;font-size:16px;
  font-weight:600;cursor:pointer;border:1px solid var(--border-strong);
  background:var(--bg-input);color:var(--text);transition:transform .08s;
  font-family:inherit}
.btn:active{transform:scale(.97)}
.btn.primary{background:linear-gradient(180deg,var(--primary),var(--primary-2));
  border-color:var(--primary-2);color:#fff}
.btn.ghost{background:transparent}
.btn.block{width:100%}
.btn.small{min-height:36px;padding:0 12px;font-size:13px}
.btn:disabled{opacity:.45;cursor:not-allowed}

.row{display:flex;align-items:center;gap:10px}
.row.between{justify-content:space-between}
.muted{color:var(--text-muted)}.small{font-size:13px}
.range{display:flex;justify-content:center;align-items:baseline;gap:10px;
  font-size:26px;font-weight:700;margin:10px 0}
.bar{height:6px;border-radius:3px;background:var(--bg-input);overflow:hidden}
.bar i{display:block;height:100%;width:0;
  background:linear-gradient(90deg,var(--primary),var(--success));
  transition:width .25s}
.feedback{min-height:44px;display:flex;align-items:center;
  font-size:15px;color:var(--text-muted);margin-bottom:10px}
.feedback.low{color:var(--primary)}
.feedback.high{color:var(--warning)}
.feedback.win{color:var(--success);font-weight:700}
.feedback.lose{color:var(--danger);font-weight:700}
.feedback.warn{color:var(--warning)}
.input-row{display:flex;gap:8px}
.input-row input{flex:1;min-width:0}
.input-row .btn{flex:0 0 auto;padding:0 24px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{font-size:13px;padding:4px 10px;border-radius:999px;
  background:var(--bg-input);border:1px solid var(--border-strong);
  color:var(--text-muted)}
.chip.low{border-color:var(--primary);color:var(--primary)}
.chip.high{border-color:var(--warning);color:var(--warning)}
.chip.win{border-color:var(--success);color:var(--success);font-weight:700}

.list{display:flex;flex-direction:column;gap:10px}
.list-item{background:var(--bg-card);border:1px solid var(--border);
  border-left:4px solid var(--border-strong);border-radius:12px;padding:12px 14px}
.list-item.win{border-left-color:var(--success)}
.list-item.lose{border-left-color:var(--danger)}
.li-head{display:flex;justify-content:space-between;align-items:center;
  gap:8px;margin-bottom:6px}
.li-row{display:flex;justify-content:space-between;gap:12px;
  font-size:14px;color:var(--text-muted);padding:2px 0}
.li-row b{color:var(--text);font-weight:600;text-align:right;word-break:break-all}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
.tag{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid}
.tag.ok{color:var(--success);border-color:var(--success)}
.tag.bad{color:var(--danger);border-color:var(--danger)}
.pager{display:flex;align-items:center;justify-content:space-between;
  gap:10px;margin-top:14px}
.pager .btn{min-height:42px;padding:0 14px;font-size:14px}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stat{background:var(--bg-card);border:1px solid var(--border);
  border-radius:12px;padding:14px}
.stat .k{font-size:12px;color:var(--text-muted)}
.stat .v{font-size:22px;font-weight:700;margin-top:4px}
.stat.wide{grid-column:1/-1}
.btn-row{display:flex;gap:10px;margin-top:12px}
.btn-row .btn{flex:1}
.hint{font-size:12px;color:var(--text-muted);margin:10px 0 0}

.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:10;
  display:grid;grid-template-columns:repeat(4,1fr);background:var(--bg-card);
  border-top:1px solid var(--border);padding-bottom:env(safe-area-inset-bottom)}
.tab{border:0;background:transparent;color:var(--text-muted);padding:12px 4px;
  font-size:13px;cursor:pointer;min-height:52px;font-family:inherit}
.tab.active{color:var(--primary);font-weight:700}

/* ---- 设置图标（左下角浮动） ---- */
.settings-fab{position:fixed;left:16px;z-index:60;
  bottom:calc(76px + env(safe-area-inset-bottom));
  width:50px;height:50px;border-radius:50%;
  background:var(--bg-card);border:1px solid var(--border);
  color:var(--text);display:flex;align-items:center;justify-content:center;
  cursor:pointer;box-shadow:0 6px 18px rgba(0,0,0,.35);
  transition:transform .1s,background .15s}
.settings-fab:active{transform:scale(.93)}
.settings-fab svg{width:24px;height:24px}

/* ---- 设置抽屉 ---- */
.settings-drawer{position:fixed;inset:0;z-index:100;pointer-events:none}
.settings-drawer.open{pointer-events:auto}
.settings-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.45);
  opacity:0;transition:opacity .2s}
.settings-drawer.open .settings-backdrop{opacity:1}
.settings-panel{position:absolute;top:0;left:0;bottom:0;
  width:min(440px,100%);background:var(--bg-card);
  border-right:1px solid var(--border);transform:translateX(-100%);
  transition:transform .25s ease;display:flex;flex-direction:column;
  padding-bottom:env(safe-area-inset-bottom)}
.settings-drawer.open .settings-panel{transform:translateX(0)}
.settings-head{display:flex;align-items:center;justify-content:space-between;
  padding:16px;padding-top:calc(16px + env(safe-area-inset-top));
  border-bottom:1px solid var(--border)}
.settings-head h2{margin:0;font-size:18px}
.btn-close{width:36px;height:36px;border-radius:50%;border:1px solid var(--border);
  background:transparent;color:var(--text);font-size:18px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;font-family:inherit}
.settings-body{flex:1;overflow-y:auto;padding:16px}
.settings-sec{margin-bottom:24px}
.settings-sec h3{font-size:14px;color:var(--text-muted);margin:0 0 10px;
  font-weight:600;text-transform:uppercase;letter-spacing:.5px}
.seg{display:flex;gap:6px;background:var(--bg-input);border-radius:12px;
  padding:4px;border:1px solid var(--border)}
.seg button{flex:1;border:0;background:transparent;color:var(--text-muted);
  padding:10px;border-radius:9px;cursor:pointer;font-size:14px;
  font-family:inherit;font-weight:600}
.seg button.on{background:var(--primary);color:#fff}

.color-row{display:flex;align-items:center;justify-content:space-between;
  padding:10px 0;border-bottom:1px dashed var(--border)}
.color-row:last-of-type{border-bottom:0}
.color-row span{font-size:14px}
.color-swatch{width:42px;height:32px;border-radius:8px;
  border:1px solid var(--border-strong);cursor:pointer;padding:0;
  background:#000;position:relative;overflow:hidden}
.color-swatch::after{content:"";position:absolute;inset:0;
  background:linear-gradient(135deg,transparent 40%,rgba(255,255,255,.35) 100%)}

/* ---- 颜色选择器 ---- */
.picker-overlay{position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.55);
  display:none;align-items:center;justify-content:center;padding:16px;
  padding-top:calc(16px + env(safe-area-inset-top));
  padding-bottom:calc(16px + env(safe-area-inset-bottom))}
.picker-overlay.open{display:flex}
.picker-panel{width:100%;max-width:420px;max-height:100%;overflow-y:auto;
  background:var(--bg-card);border:1px solid var(--border);
  border-radius:16px;display:flex;flex-direction:column}
.picker-panel header{display:flex;align-items:center;justify-content:space-between;
  padding:14px 16px;border-bottom:1px solid var(--border)}
.picker-panel header h3{margin:0;font-size:16px}
.picker-body{padding:14px 16px}
.picker-grid{display:grid;gap:6px;margin-bottom:14px}
.picker-grid.g32{grid-template-columns:repeat(8,1fr)}
.picker-grid.g16{grid-template-columns:repeat(4,1fr)}
.picker-grid button{aspect-ratio:1;border-radius:6px;border:2px solid transparent;
  cursor:pointer;padding:0}
.picker-grid button.on{border-color:var(--text)}
.picker-wheel-wrap{position:relative;width:240px;height:240px;margin:0 auto 10px}
.picker-wheel-wrap canvas{width:100%;height:100%;border-radius:50%;
  display:block;cursor:crosshair;touch-action:none}
.picker-wheel-dot{position:absolute;width:16px;height:16px;border-radius:50%;
  border:3px solid #fff;box-shadow:0 0 0 1px #000,0 0 6px rgba(0,0,0,.5);
  pointer-events:none;transform:translate(-50%,-50%)}
.picker-light{margin:10px 0 12px}
.picker-light label{display:flex;justify-content:space-between;
  font-size:13px;color:var(--text-muted);margin-bottom:6px}
.picker-light input[type=range]{width:100%}
.picker-preview{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.picker-preview-color{width:52px;height:44px;border-radius:10px;
  border:1px solid var(--border-strong)}
.picker-preview input{flex:1;padding:10px 12px;border-radius:10px;
  background:var(--bg-input);border:1px solid var(--border-strong);
  color:var(--text);font-family:inherit;font-size:15px}
.picker-mix{border-top:1px dashed var(--border);padding-top:12px;margin-bottom:12px}
.picker-mix label{font-size:13px;color:var(--text-muted);display:block;margin-bottom:6px}
.picker-mix-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.picker-mix-row .color-swatch{width:36px;height:28px}
.picker-mix-row input[type=range]{flex:1}
.picker-actions{display:flex;gap:10px;margin-top:6px}
.picker-actions .btn{flex:1}
</style>
</head>
<body>

<header class="topbar">
  <div class="brand">🎯 猜数字</div>
  <div class="status" id="status">就绪</div>
</header>

<main>
  <section class="view" id="view-menu">
    <h2 class="title">选择难度</h2>
    <div class="levels">
      <button class="level" data-level="1"><b>简单</b><small>1 ~ 1000 · 20 次机会</small></button>
      <button class="level" data-level="2"><b>中等</b><small>1 ~ 10000 · 30 次机会</small></button>
      <button class="level" data-level="3"><b>困难</b><small>1 ~ 100000 · 40 次机会</small></button>
      <button class="level" data-level="4"><b>自定义</b><small>自己设定范围和次数</small></button>
    </div>
    <div class="card hidden" id="custom-card" style="margin-top:12px">
      <label for="in-max">数字范围上限（≥ 2）</label>
      <input id="in-max" type="number" inputmode="numeric" min="2" value="5000">
      <label for="in-att">最大猜测次数（≥ 1）</label>
      <input id="in-att" type="number" inputmode="numeric" min="1" value="15">
      <p class="hint" id="custom-hint"></p>
      <button class="btn primary block" id="btn-custom-start" style="margin-top:12px">开始游戏</button>
    </div>
  </section>

  <section class="view hidden" id="view-game">
    <div class="card">
      <div class="row between">
        <span class="muted" id="g-level">—</span>
        <span class="muted">剩余 <b id="g-remain">0</b> 次</span>
      </div>
      <div class="range"><span id="g-low">1</span><span class="muted">~</span><span id="g-high">1000</span></div>
      <div class="bar"><i id="g-bar"></i></div>
    </div>
    <div class="card">
      <div class="feedback" id="g-feedback">输入一个数字开始猜吧</div>
      <form id="guess-form" autocomplete="off">
        <div class="input-row">
          <input id="guess-input" type="number" inputmode="numeric" enterkeyhint="go"
                 placeholder="输入数字" autocomplete="off">
          <button type="submit" class="btn primary">猜</button>
        </div>
      </form>
      <button class="btn primary block hidden" id="btn-again" style="margin-top:10px">再来一局</button>
    </div>
    <div class="card">
      <div class="muted small">猜测记录</div>
      <div class="chips" id="g-chips"><span class="muted small">暂无</span></div>
    </div>
    <button class="btn ghost block" id="btn-menu">返回菜单</button>
  </section>

  <section class="view hidden" id="view-history">
    <h2 class="title">历史记录（最新在前）</h2>
    <div id="history-list" class="list"></div>
    <div class="pager">
      <button class="btn ghost" id="h-prev">上一页</button>
      <span class="muted small" id="h-info">—</span>
      <button class="btn ghost" id="h-next">下一页</button>
    </div>
  </section>

  <section class="view hidden" id="view-stats">
    <h2 class="title">统计</h2>
    <div class="stats" id="stats-box"></div>
  </section>

  <section class="view hidden" id="view-export">
    <h2 class="title">导出记录</h2>
    <div class="card">
      <label for="ex-scope">导出范围</label>
      <select id="ex-scope">
        <option value="all">全部记录</option>
        <option value="recent">最近 10 条</option>
        <option value="win">仅猜对</option>
        <option value="lose">仅失败</option>
      </select>
      <div class="btn-row">
        <button class="btn primary" data-export="csv">导出 CSV</button>
        <button class="btn primary" data-export="html">导出 HTML</button>
      </div>
      <p class="hint">CSV 用 WPS / Excel 打开；HTML 用浏览器打开。</p>
    </div>
  </section>
</main>

<nav class="tabbar">
  <button class="tab active" data-view="menu">开始</button>
  <button class="tab" data-view="history">历史</button>
  <button class="tab" data-view="stats">统计</button>
  <button class="tab" data-view="export">导出</button>
</nav>

<!-- 设置图标 -->
<button class="settings-fab" id="btn-settings" aria-label="设置">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
       stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="3"/>
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
  </svg>
</button>

<!-- 设置抽屉 -->
<div class="settings-drawer" id="settings-drawer">
  <div class="settings-backdrop" id="settings-backdrop"></div>
  <aside class="settings-panel">
    <header class="settings-head">
      <h2>设置</h2>
      <button class="btn-close" id="btn-settings-close" aria-label="关闭">✕</button>
    </header>
    <div class="settings-body">
      <section class="settings-sec">
        <h3>主题</h3>
        <div class="seg" id="theme-mode">
          <button data-mode="light">亮色</button>
          <button data-mode="dark">暗色</button>
          <button data-mode="custom">自定义</button>
        </div>
        <div id="theme-custom" class="hidden" style="margin-top:10px">
          <div class="color-row">
            <span>背景色</span>
            <button class="color-swatch" data-theme-key="bg"></button>
          </div>
          <div class="color-row">
            <span>卡片色</span>
            <button class="color-swatch" data-theme-key="card"></button>
          </div>
          <div class="color-row">
            <span>文字色</span>
            <button class="color-swatch" data-theme-key="text"></button>
          </div>
          <div class="color-row">
            <span>主色调</span>
            <button class="color-swatch" data-theme-key="primary"></button>
          </div>
          <button class="btn ghost block" id="btn-theme-reset" style="margin-top:8px">
            恢复默认自定义值
          </button>
        </div>
      </section>

      <section class="settings-sec">
        <h3>字体</h3>
        <label for="font-select">字体</label>
        <select id="font-select"></select>
        <label for="font-size">字号：<span id="font-size-val">16</span>px</label>
        <input type="range" id="font-size" min="12" max="24" step="1" value="16" style="width:100%">
        <div class="color-row">
          <span>字体颜色</span>
          <button class="color-swatch" id="font-color-swatch"></button>
        </div>
      </section>

      <section class="settings-sec">
        <h3>数据目录</h3>
        <p class="small muted" id="data-dir-current" style="word-break:break-all;margin:0 0 10px">加载中…</p>
        <button class="btn primary block" id="btn-change-dir">更改数据目录</button>
        <p class="hint">更改后会把现有记录迁移到新目录（原目录记录会保留作备份）。</p>
      </section>
    </div>
  </aside>
</div>

<!-- 颜色选择器 -->
<div class="picker-overlay" id="picker-overlay">
  <div class="picker-panel">
    <header>
      <h3 id="picker-title">选择颜色</h3>
      <button class="btn-close" id="picker-close">✕</button>
    </header>
    <div class="picker-body">
      <div class="picker-grid g32" id="picker-grid"></div>
      <div class="picker-wheel-wrap">
        <canvas id="picker-wheel" width="240" height="240"></canvas>
        <div class="picker-wheel-dot" id="picker-wheel-dot"></div>
      </div>
      <div class="picker-light">
        <label><span>明度</span><span id="picker-light-val">50%</span></label>
        <input type="range" id="picker-light" min="0" max="100" value="50">
      </div>
      <div class="picker-preview">
        <div class="picker-preview-color" id="picker-preview"></div>
        <input type="text" id="picker-hex" maxlength="7" placeholder="#rrggbb">
      </div>
      <div class="picker-mix">
        <label>混色（A → B）</label>
        <div class="picker-mix-row">
          <input type="color" id="mix-a" style="width:40px;height:32px;border:0;background:none;padding:0">
          <input type="range" id="mix-ratio" min="0" max="100" value="50">
          <input type="color" id="mix-b" value="#ffffff" style="width:40px;height:32px;border:0;background:none;padding:0">
        </div>
        <div class="picker-mix-row">
          <span class="small muted" style="flex:1">混合结果</span>
          <button class="color-swatch" id="mix-result"></button>
          <button class="btn ghost small" id="mix-apply">应用</button>
        </div>
      </div>
      <div class="picker-actions">
        <button class="btn ghost" id="picker-cancel">取消</button>
        <button class="btn primary" id="picker-ok">确定</button>
      </div>
    </div>
  </div>
</div>

<script>
(function(){
"use strict";
var $=function(id){return document.getElementById(id)};

/* ============================================================
 * 颜色工具
 * ============================================================ */
function clamp(v,a,b){return Math.max(a,Math.min(b,v))}
function hexToRgb(hex){
  var m=/^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if(!m)return{r:0,g:0,b:0};
  return{r:parseInt(m[1],16),g:parseInt(m[2],16),b:parseInt(m[3],16)};
}
function rgbToHex(r,g,b){
  return "#"+[r,g,b].map(function(v){
    return clamp(Math.round(v),0,255).toString(16).padStart(2,"0")
  }).join("");
}
function hslToRgb(h,s,l){
  h=((h%360)+360)%360;s=clamp(s,0,1);l=clamp(l,0,1);
  var c=(1-Math.abs(2*l-1))*s,hp=h/60,x=c*(1-Math.abs((hp%2)-1));
  var r,g,b;
  if(hp<1){r=c;g=x;b=0}
  else if(hp<2){r=x;g=c;b=0}
  else if(hp<3){r=0;g=c;b=x}
  else if(hp<4){r=0;g=x;b=c}
  else if(hp<5){r=x;g=0;b=c}
  else{r=c;g=0;b=x}
  var m=l-c/2;
  return[Math.round((r+m)*255),Math.round((g+m)*255),Math.round((b+m)*255)];
}
function hslToHex(h,s,l){
  var rgb=hslToRgb(h,s/100,l/100);
  return rgbToHex(rgb[0],rgb[1],rgb[2]);
}
function mixColor(a,b,t){
  var ca=hexToRgb(a),cb=hexToRgb(b);
  return rgbToHex(ca.r+(cb.r-ca.r)*t,ca.g+(cb.g-ca.g)*t,ca.b+(cb.b-ca.b)*t);
}
function rgbToHsl(hex){
  var c=hexToRgb(hex);
  var r=c.r/255,g=c.g/255,b=c.b/255;
  var max=Math.max(r,g,b),min=Math.min(r,g,b);
  var h=0,s=0,l=(max+min)/2;
  if(max!==min){
    var d=max-min;
    s=l>0.5?d/(2-max-min):d/(max+min);
    if(max===r)h=(g-b)/d+(g<b?6:0);
    else if(max===g)h=(b-r)/d+2;
    else h=(r-g)/d+4;
    h*=60;
  }
  return{h:h,s:s,l:l};
}

/* ============================================================
 * 设置存储
 * ============================================================ */
var SETTINGS_KEY="guess_number_pro_settings_v1";

var FONTS=[
  {name:"系统默认",css:'-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif'},
  {name:"微软雅黑",css:'"Microsoft YaHei",sans-serif'},
  {name:"黑体",css:'SimHei,"Microsoft YaHei",sans-serif'},
  {name:"宋体",css:'SimSun,"Times New Roman",serif'},
  {name:"楷体",css:'KaiTi,STKaiti,serif'},
  {name:"仿宋",css:'FangSong,STFangsong,serif'},
  {name:"苹方",css:'"PingFang SC",-apple-system,sans-serif'},
  {name:"华文细黑",css:'"STHeiti","Microsoft YaHei",sans-serif'},
  {name:"思源黑体",css:'"Source Han Sans SC","Noto Sans CJK SC",sans-serif'},
  {name:"等线",css:'DengXian,"Microsoft YaHei",sans-serif'},
  {name:"Arial",css:'Arial,Helvetica,sans-serif'},
  {name:"Helvetica",css:'Helvetica,Arial,sans-serif'},
  {name:"Verdana",css:'Verdana,Geneva,sans-serif'},
  {name:"Tahoma",css:'Tahoma,Geneva,sans-serif'},
  {name:"Trebuchet MS",css:'"Trebuchet MS",sans-serif'},
  {name:"Georgia",css:'Georgia,serif'},
  {name:"Times New Roman",css:'"Times New Roman",Times,serif'},
  {name:"Palatino",css:'"Palatino Linotype","Book Antiqua",Palatino,serif'},
  {name:"Courier New",css:'"Courier New",Courier,monospace'},
  {name:"Consolas",css:'Consolas,Menlo,monospace'}
];

var DEFAULT_SETTINGS={
  theme:{mode:"dark"},
  customTheme:{bg:"#0b1220",card:"#16213a",text:"#e8eefc",primary:"#4f8cff"},
  font:{familyIndex:0,size:16,color:"#e8eefc"}
};

function loadSettings(){
  try{
    var raw=localStorage.getItem(SETTINGS_KEY);
    if(!raw)return JSON.parse(JSON.stringify(DEFAULT_SETTINGS));
    var s=JSON.parse(raw);
    return {
      theme:Object.assign({},DEFAULT_SETTINGS.theme,s.theme||{}),
      customTheme:Object.assign({},DEFAULT_SETTINGS.customTheme,s.customTheme||{}),
      font:Object.assign({},DEFAULT_SETTINGS.font,s.font||{})
    };
  }catch(e){return JSON.parse(JSON.stringify(DEFAULT_SETTINGS))}
}
function saveSettings(s){
  try{localStorage.setItem(SETTINGS_KEY,JSON.stringify(s))}catch(e){}
}

var settings=loadSettings();

/* ============================================================
 * 主题应用
 * ============================================================ */
function applyTheme(){
  var root=document.documentElement.style;
  var t=settings.theme;
  var vars;

  if(t.mode==="light"){
    vars={
      "--bg":"#f4f6fb","--bg-card":"#ffffff","--bg-input":"#f0f3fa",
      "--border":"#d8e0ee","--border-strong":"#c4d0e3",
      "--text":"#1a2338","--text-muted":"#6b7a99",
      "--primary":"#4f8cff","--primary-2":"#3b6fe0",
      "--success":"#0fa76a","--danger":"#e5484d","--warning":"#d97706"
    };
    document.querySelector('meta[name="theme-color"]').setAttribute("content","#ffffff");
  }else if(t.mode==="custom"){
    var c=settings.customTheme;
    var card=c.card,bg=c.bg,text=c.text,pri=c.primary;
    vars={
      "--bg":bg,"--bg-card":card,
      "--bg-input":mixColor(card,bg,0.55),
      "--border":mixColor(card,text,0.14),
      "--border-strong":mixColor(card,text,0.24),
      "--text":text,"--text-muted":mixColor(text,bg,0.45),
      "--primary":pri,"--primary-2":mixColor(pri,"#000000",0.22),
      "--success":"#38d39f","--danger":"#ff6b6b","--warning":"#ffc861"
    };
    document.querySelector('meta[name="theme-color"]').setAttribute("content",card);
  }else{
    vars={
      "--bg":"#0b1220","--bg-card":"#16213a","--bg-input":"#0f1830",
      "--border":"#24334f","--border-strong":"#2a3a5c",
      "--text":"#e8eefc","--text-muted":"#8fa3c8",
      "--primary":"#4f8cff","--primary-2":"#3b6fe0",
      "--success":"#38d39f","--danger":"#ff6b6b","--warning":"#ffc861"
    };
    document.querySelector('meta[name="theme-color"]').setAttribute("content","#0b1220");
  }

  Object.keys(vars).forEach(function(k){root.setProperty(k,vars[k])});
}

function applyFont(){
  var f=settings.font;
  var font=FONTS[f.familyIndex]||FONTS[0];
  var root=document.documentElement.style;
  root.setProperty("--font-family",font.css);
  root.setProperty("--font-size",f.size+"px");
  root.setProperty("--font-color",f.color);
}

function applyAll(){applyTheme();applyFont()}

/* ============================================================
 * 设置面板 UI
 * ============================================================ */
function openSettings(){
  $("settings-drawer").classList.add("open");
  document.body.style.overflow="hidden";
  syncSettingsUI();
  loadDataDirInfo();
}
function closeSettings(){
  $("settings-drawer").classList.remove("open");
  document.body.style.overflow="";
}

function syncSettingsUI(){
  // 主题模式
  Array.prototype.forEach.call(document.querySelectorAll("#theme-mode button"),function(b){
    b.classList.toggle("on",b.dataset.mode===settings.theme.mode);
  });
  // 自定义主题区显示
  $("theme-custom").classList.toggle("hidden",settings.theme.mode!=="custom");
  // 主题色块
  Array.prototype.forEach.call(document.querySelectorAll("[data-theme-key]"),function(sw){
    sw.style.background=settings.customTheme[sw.dataset.themeKey];
  });
  // 字体
  $("font-select").value=String(settings.font.familyIndex);
  $("font-size").value=settings.font.size;
  $("font-size-val").textContent=settings.font.size;
  $("font-color-swatch").style.background=settings.font.color;
}

function loadDataDirInfo(){
  fetch("/api/config").then(function(r){return r.json()}).then(function(d){
    $("data-dir-current").textContent=d.data_dir||"—";
  }).catch(function(){$("data-dir-current").textContent="无法获取"});
}

/* ============================================================
 * 颜色选择器
 * ============================================================ */
var pickerState={
  open:false,
  color:"#000000",
  light:50,
  hue:0,
  sat:0,
  paletteCount:32,
  onPick:null,
  mixA:"#000000",
  mixB:"#ffffff"
};

function buildPalette(count){
  var colors=[];
  if(count===32){
    var hueSet=[0,45,90,135,180,225,270,315];
    var levels=[30,50,70,90];
    levels.forEach(function(l){
      hueSet.forEach(function(h){colors.push(hslToHex(h,70,l))});
    });
  }else{
    var hueSet2=[0,45,90,135,180,225,270,315];
    var levels2=[40,75];
    levels2.forEach(function(l){
      hueSet2.forEach(function(h){colors.push(hslToHex(h,70,l))});
    });
  }
  return colors;
}

function drawWheel(){
  var canvas=$("picker-wheel");
  var ctx=canvas.getContext("2d");
  var w=canvas.width,h=canvas.height;
  var cx=w/2,cy=h/2,r=Math.min(w,h)/2-1;
  var img=ctx.createImageData(w,h);
  var data=img.data;
  for(var y=0;y<h;y++){
    for(var x=0;x<w;x++){
      var dx=x-cx,dy=y-cy;
      var dist=Math.sqrt(dx*dx+dy*dy);
      if(dist>r)continue;
      var angle=Math.atan2(dy,dx);
      var hue=((angle*180/Math.PI)+360)%360;
      var sat=dist/r;
      var rgb=hslToRgb(hue,sat,0.5);
      var idx=(y*w+x)*4;
      data[idx]=rgb[0];data[idx+1]=rgb[1];data[idx+2]=rgb[2];data[idx+3]=255;
    }
  }
  ctx.putImageData(img,0,0);
}

function updateWheelDot(){
  var canvas=$("picker-wheel");
  var dot=$("picker-wheel-dot");
  var r=canvas.width/2-1;
  var rad=pickerState.hue*Math.PI/180;
  var dist=pickerState.sat*r;
  var cx=canvas.width/2+Math.cos(rad)*dist;
  var cy=canvas.height/2+Math.sin(rad)*dist;
  dot.style.left=cx+"px";
  dot.style.top=cy+"px";
  dot.style.background=pickerState.color;
}

function updatePickerPreview(){
  $("picker-preview").style.background=pickerState.color;
  $("picker-hex").value=pickerState.color;
  $("picker-light").value=pickerState.light;
  $("picker-light-val").textContent=pickerState.light+"%";
  updateWheelDot();
  Array.prototype.forEach.call(document.querySelectorAll("#picker-grid button"),function(b){
    b.classList.toggle("on",b.dataset.color.toLowerCase()===pickerState.color.toLowerCase());
  });
}

function setPickerColor(hex,fromWheel){
  hex=hex.toLowerCase();
  if(!/^#[0-9a-f]{6}$/.test(hex))return;
  pickerState.color=hex;
  if(!fromWheel){
    var hsl=rgbToHsl(hex);
    pickerState.hue=hsl.h;
    pickerState.sat=hsl.s;
    pickerState.light=Math.round(hsl.l*100);
  }
  updatePickerPreview();
}

function setColorFromWheelEvent(e){
  var canvas=$("picker-wheel");
  var rect=canvas.getBoundingClientRect();
  var clientX=e.touches?e.touches[0].clientX:e.clientX;
  var clientY=e.touches?e.touches[0].clientY:e.clientY;
  var x=(clientX-rect.left)*(canvas.width/rect.width);
  var y=(clientY-rect.top)*(canvas.height/rect.height);
  var cx=canvas.width/2,cy=canvas.height/2;
  var dx=x-cx,dy=y-cy;
  var r=canvas.width/2-1;
  var dist=Math.min(Math.sqrt(dx*dx+dy*dy),r);
  var hue=((Math.atan2(dy,dx)*180/Math.PI)+360)%360;
  var sat=dist/r;
  pickerState.hue=hue;
  pickerState.sat=sat;
  pickerState.color=hslToHex(hue,sat*100,pickerState.light);
  updatePickerPreview();
}

function openColorPicker(initial,count,onPick,title){
  pickerState.paletteCount=count;
  pickerState.onPick=onPick;
  pickerState.open=true;
  $("picker-title").textContent=title||"选择颜色";
  // 生成色板
  var grid=$("picker-grid");
  grid.className="picker-grid "+(count===32?"g32":"g16");
  var colors=buildPalette(count);
  grid.innerHTML="";
  colors.forEach(function(c){
    var b=document.createElement("button");
    b.type="button";
    b.style.background=c;
    b.dataset.color=c;
    b.addEventListener("click",function(){setPickerColor(c,false)});
    grid.appendChild(b);
  });
  // 初始化状态
  setPickerColor(initial,false);
  // 混色初始值
  pickerState.mixA=initial;
  pickerState.mixB="#ffffff";
  $("mix-a").value=initial;
  $("mix-b").value="#ffffff";
  $("mix-ratio").value=50;
  updateMixResult();
  // 绘制色轮（一次即可）
  drawWheel();
  updatePickerPreview();
  $("picker-overlay").classList.add("open");
  document.body.style.overflow="hidden";
}

function closeColorPicker(){
  pickerState.open=false;
  $("picker-overlay").classList.remove("open");
  if(!$("settings-drawer").classList.contains("open")){
    document.body.style.overflow="";
  }
}

function updateMixResult(){
  var a=$("mix-a").value,b=$("mix-b").value;
  var t=parseInt($("mix-ratio").value,10)/100;
  var c=mixColor(a,b,t);
  $("mix-result").style.background=c;
  $("mix-result").dataset.color=c;
  pickerState.mixA=a;pickerState.mixB=b;
}

/* ============================================================
 * 设置项绑定
 * ============================================================ */
function bindSettings(){
  $("btn-settings").addEventListener("click",openSettings);
  $("btn-settings-close").addEventListener("click",closeSettings);
  $("settings-backdrop").addEventListener("click",closeSettings);

  // 主题模式
  Array.prototype.forEach.call(document.querySelectorAll("#theme-mode button"),function(b){
    b.addEventListener("click",function(){
      settings.theme.mode=b.dataset.mode;
      saveSettings(settings);
      applyTheme();
      syncSettingsUI();
    });
  });

  // 自定义主题色块
  Array.prototype.forEach.call(document.querySelectorAll("[data-theme-key]"),function(sw){
    sw.addEventListener("click",function(){
      var key=sw.dataset.themeKey;
      var cur=settings.customTheme[key];
      openColorPicker(cur,32,function(color){
        settings.customTheme[key]=color;
        saveSettings(settings);
        applyTheme();
        syncSettingsUI();
      },"主题 · "+({bg:"背景色",card:"卡片色",text:"文字色",primary:"主色调"}[key]||key));
    });
  });

  $("btn-theme-reset").addEventListener("click",function(){
    settings.customTheme=Object.assign({},DEFAULT_SETTINGS.customTheme);
    saveSettings(settings);
    applyTheme();
    syncSettingsUI();
  });

  // 字体
  $("font-select").addEventListener("change",function(){
    settings.font.familyIndex=parseInt(this.value,10)||0;
    saveSettings(settings);
    applyFont();
  });
  $("font-size").addEventListener("input",function(){
    settings.font.size=parseInt(this.value,10)||16;
    $("font-size-val").textContent=settings.font.size;
    saveSettings(settings);
    applyFont();
  });
  $("font-color-swatch").addEventListener("click",function(){
    openColorPicker(settings.font.color,16,function(color){
      settings.font.color=color;
      saveSettings(settings);
      applyFont();
      syncSettingsUI();
    },"字体颜色");
  });

  // 数据目录
  $("btn-change-dir").addEventListener("click",async function(){
    var cur=$("data-dir-current").textContent.trim();
    var path=window.prompt(
      "请输入新的数据目录（绝对路径）：\n例如 Windows: D:\\GuessNumberData\n例如 Linux/macOS: /home/user/guess_data",
      cur
    );
    if(path===null)return;
    path=path.trim();
    if(!path)return;
    try{
      var res=await fetch("/api/config/data_dir",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({path:path})
      });
      var d=await res.json();
      if(!res.ok)throw new Error(d.error||"更改失败");
      $("data-dir-current").textContent=d.data_dir;
      alert(d.migrated?"数据目录已更改，记录已迁移。":"数据目录已更改。");
    }catch(e){
      alert("更改失败："+e.message);
    }
  });
}

/* ============================================================
 * 颜色选择器事件
 * ============================================================ */
function bindPicker(){
  $("picker-close").addEventListener("click",closeColorPicker);
  $("picker-cancel").addEventListener("click",closeColorPicker);
  $("picker-overlay").addEventListener("click",function(e){
    if(e.target===this)closeColorPicker();
  });

  $("picker-ok").addEventListener("click",function(){
    if(typeof pickerState.onPick==="function"){
      pickerState.onPick(pickerState.color);
    }
    closeColorPicker();
  });

  $("picker-light").addEventListener("input",function(){
    pickerState.light=parseInt(this.value,10);
    pickerState.color=hslToHex(pickerState.hue,pickerState.sat*100,pickerState.light);
    updatePickerPreview();
  });

  $("picker-hex").addEventListener("change",function(){
    var v=this.value.trim();
    if(/^[0-9a-f]{6}$/i.test(v))v="#"+v;
    if(/^#[0-9a-f]{6}$/i.test(v))setPickerColor(v.toLowerCase(),false);
    else updatePickerPreview();
  });

  // 色轮交互
  var wheel=$("picker-wheel");
  var dragging=false;
  function onDown(e){
    dragging=true;
    setColorFromWheelEvent(e);
    e.preventDefault();
  }
  function onMove(e){
    if(!dragging)return;
    setColorFromWheelEvent(e);
    e.preventDefault();
  }
  function onUp(){dragging=false}
  wheel.addEventListener("mousedown",onDown);
  wheel.addEventListener("touchstart",onDown,{passive:false});
  window.addEventListener("mousemove",onMove);
  window.addEventListener("touchmove",onMove,{passive:false});
  window.addEventListener("mouseup",onUp);
  window.addEventListener("touchend",onUp);

  // 混色
  $("mix-a").addEventListener("input",updateMixResult);
  $("mix-b").addEventListener("input",updateMixResult);
  $("mix-ratio").addEventListener("input",updateMixResult);
  $("mix-apply").addEventListener("click",function(){
    var c=$("mix-result").dataset.color;
    if(c)setPickerColor(c,false);
  });
}

/* ============================================================
 * 游戏逻辑（沿用原版）
 * ============================================================ */
var state={view:"menu",game:null,lastLevel:null,lastExtra:null,historyPage:1};

function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
function setStatus(t,c){var e=$("status");e.textContent=t;e.className="status"+(c?" "+c:"")}
function show(e,on){e.classList.toggle("hidden",!on)}

async function api(m,u,b){
  var o={method:m,headers:{Accept:"application/json"}};
  if(b!==undefined){o.headers["Content-Type"]="application/json";o.body=JSON.stringify(b)}
  var r=await fetch(u,o);var d={};
  try{d=await r.json()}catch(e){d={}}
  if(!r.ok)throw new Error(d.error||("请求失败 "+r.status));
  return d;
}
function focusInput(){var e=$("guess-input");if(e.disabled)return;
  setTimeout(function(){try{e.focus({preventScroll:true})}catch(x){try{e.focus()}catch(y){}}},60)}

var VIEWS=["menu","game","history","stats","export"];
function setView(n){
  state.view=n;
  VIEWS.forEach(function(v){show($("view-"+v),v===n)});
  var tn=(n==="game")?"menu":n;
  Array.prototype.forEach.call(document.querySelectorAll(".tab"),function(t){
    t.classList.toggle("active",t.dataset.view===tn)});
  if(n==="history")loadHistory(state.historyPage);
  if(n==="stats")loadStats();
  window.scrollTo(0,0);
}

async function startGame(level,extra){
  setStatus("正在开始…");
  try{
    var p=Object.assign({level:String(level)},extra||{});
    var d=await api("POST","/api/game",p);
    state.lastLevel=String(level);state.lastExtra=extra||null;
    state.game={id:d.id,name:d.name,max_num:d.max_num,max_attempts:d.max_attempts,
      low:d.low,high:d.high,attempts:d.attempts,remaining:d.remaining,
      guesses:d.guesses,marks:d.marks,finished:d.finished,answer:d.answer};
    $("guess-input").value="";
    setFeedback("输入一个数字开始猜吧","");
    renderGame();setView("game");setStatus("游戏中");focusInput();
  }catch(e){setStatus("出错","bad");alert("无法开始游戏："+e.message)}
}

function renderGame(){
  var g=state.game;if(!g)return;
  $("g-level").textContent=g.name;
  $("g-remain").textContent=g.remaining;
  $("g-low").textContent=g.low;$("g-high").textContent=g.high;
  $("g-bar").style.width=Math.min(100,(g.attempts/g.max_attempts)*100)+"%";
  var c=$("g-chips");
  if(!g.guesses||!g.guesses.length){c.innerHTML='<span class="muted small">暂无</span>'}
  else{c.innerHTML=g.guesses.map(function(n,i){
    var m=(g.marks&&g.marks[i])||"";
    return '<span class="chip'+(m?" "+m:"")+'">'+esc(n)+"</span>"}).join("")}
  var inp=$("guess-input");
  inp.disabled=!!g.finished;inp.min=g.low;inp.max=g.high;
  show($("btn-again"),!!g.finished);
}

function setFeedback(t,c){var e=$("g-feedback");e.textContent=t;
  e.className="feedback"+(c?" "+c:"")}

async function submitGuess(raw){
  var g=state.game;if(!g||g.finished)return;
  var t=String(raw).trim();
  if(!t){setFeedback("请输入一个数字","warn");return}
  var v=Number(t);
  if(!isFinite(v)||Math.floor(v)!==v){setFeedback("请输入整数","warn");return}
  try{var d=await api("POST","/api/guess",{id:g.id,guess:v});applyGuess(d)}
  catch(e){setFeedback(e.message,"warn")}
}

function applyGuess(d){
  var g=state.game;
  g.low=d.low;g.high=d.high;g.attempts=d.attempts;g.remaining=d.remaining;
  g.guesses=d.guesses;g.marks=d.marks;g.finished=d.finished;
  if(d.answer!==undefined&&d.answer!==null)g.answer=d.answer;
  $("guess-input").value="";
  if(d.direction==="low")setFeedback("太小了，往大猜 ⬆️","low");
  else if(d.direction==="high")setFeedback("太大了，往小猜 ⬇️","high");
  else if(d.direction==="win"){
    setFeedback("🎉 猜对了！答案是 "+d.answer+"，用了 "+d.attempts+" 次（"+
      (d.percent!=null?d.percent.toFixed(2)+"%":"")+"）","win");
    setStatus("已结束");
  }else if(d.direction==="lose"){
    setFeedback("😵 次数用完了，正确答案是 "+d.answer,"lose");
    setStatus("已结束");
  }
  renderGame();
  if(!g.finished)focusInput();
}

async function loadHistory(page){
  var box=$("history-list");
  try{
    var d=await api("GET","/api/history?page="+encodeURIComponent(page)+"&size=10");
    state.historyPage=d.page;
    $("h-info").textContent="第 "+d.page+" / "+d.pages+" 页 · 共 "+d.total+" 条";
    $("h-prev").disabled=d.page<=1;$("h-next").disabled=d.page>=d.pages;
    if(!d.items.length){box.innerHTML='<p class="muted">暂无记录</p>';return}
    box.innerHTML=d.items.map(function(r){
      var gs=(r.guesses&&r.guesses.length)?r.guesses.join(" → "):"无";
      return '<div class="list-item '+(r.win?"win":"lose")+'">'+
        '<div class="li-head"><span class="tag '+(r.win?"ok":"bad")+'">'+
        (r.win?"猜对":"失败")+'</span><span class="muted small">#'+r.no+" · "+esc(r.time)+"</span></div>"+
        '<div class="li-row"><span>难度</span><b>'+esc(r.level_name)+"</b></div>"+
        '<div class="li-row"><span>答案</span><b>'+esc(r.answer)+"</b></div>"+
        '<div class="li-row"><span>使用次数</span><b>'+esc(r.attempts)+" / "+esc(r.max_attempts)+"</b></div>"+
        (r.percent?'<div class="li-row"><span>使用次数百分比</span><b>'+esc(r.percent)+"</b></div>":"")+
        '<div class="li-row"><span>猜测序列</span><b class="mono">'+esc(gs)+"</b></div></div>"
    }).join("");
  }catch(e){box.innerHTML='<p class="feedback lose">加载失败：'+esc(e.message)+"</p>"}
}

async function loadStats(){
  var box=$("stats-box");
  try{
    var d=await api("GET","/api/stats");
    if(!d.total){box.innerHTML='<p class="muted">暂无数据</p>';return}
    function cell(k,v,w){return '<div class="stat'+(w?" wide":"")+'"><div class="k">'+
      esc(k)+'</div><div class="v">'+esc(v)+"</div></div>"}
    var h=cell("总局数",d.total)+cell("猜对局数",d.wins)+
          cell("猜对率",d.win_rate.toFixed(2)+"%",true);
    if(d.wins){
      h+=cell("猜对累计使用次数",d.win_attempts)+
         cell("猜对累计可用次数",d.win_max_attempts)+
         cell("猜对使用次数百分比",d.win_percent.toFixed(2)+"%",true)+
         cell("平均每局猜对使用次数",d.avg_attempts.toFixed(2),true);
    }else{h+=cell("猜对使用次数百分比","暂无数据",true)}
    box.innerHTML=h;
  }catch(e){box.innerHTML='<p class="feedback lose">加载失败：'+esc(e.message)+"</p>"}
}

function updateCustomHint(){
  var maxN=parseInt($("in-max").value,10),att=parseInt($("in-att").value,10);
  var h=$("custom-hint");
  if(!isFinite(maxN)||maxN<2){h.textContent="范围上限至少为 2。";return}
  var need=Math.ceil(Math.log(maxN+1)/Math.log(2));
  var t="理论最少 "+need+" 次即可保证猜中。";
  if(isFinite(att)&&att>0&&att<need)t+=" 你设置的次数偏少，可能会很难获胜。";
  h.textContent=t;
}

function bindGame(){
  Array.prototype.forEach.call(document.querySelectorAll(".level"),function(b){
    b.addEventListener("click",function(){
      var lv=b.dataset.level;
      if(lv==="4"){show($("custom-card"),true);updateCustomHint();
        try{$("in-max").focus()}catch(e){}}
      else{show($("custom-card"),false);startGame(lv)}
    })});
  $("in-max").addEventListener("input",updateCustomHint);
  $("in-att").addEventListener("input",updateCustomHint);
  $("btn-custom-start").addEventListener("click",function(){
    var maxN=parseInt($("in-max").value,10),att=parseInt($("in-att").value,10);
    if(!isFinite(maxN)||maxN<2){alert("范围上限至少为 2");return}
    if(!isFinite(att)||att<1){alert("猜测次数至少为 1");return}
    startGame("4",{max_num:maxN,max_attempts:att});
  });
  $("guess-form").addEventListener("submit",function(e){
    e.preventDefault();submitGuess($("guess-input").value)});
  $("btn-again").addEventListener("click",function(){
    if(state.lastLevel)startGame(state.lastLevel,state.lastExtra)});
  $("btn-menu").addEventListener("click",function(){
    state.game=null;setView("menu");setStatus("就绪")});
  Array.prototype.forEach.call(document.querySelectorAll(".tab"),function(t){
    t.addEventListener("click",function(){
      var v=t.dataset.view;
      setView(v==="menu"?(state.game?"game":"menu"):v)})});
  $("h-prev").addEventListener("click",function(){
    if(state.historyPage>1)loadHistory(state.historyPage-1)});
  $("h-next").addEventListener("click",function(){loadHistory(state.historyPage+1)});
  Array.prototype.forEach.call(document.querySelectorAll("[data-export]"),function(b){
    b.addEventListener("click",function(){
      var fmt=b.dataset.export,scope=$("ex-scope").value;
      setStatus("正在导出…");
      window.location.href="/api/export?fmt="+encodeURIComponent(fmt)+
                           "&scope="+encodeURIComponent(scope);
      setTimeout(function(){setStatus("就绪")},1500);
    })});
}

/* ============================================================
 * 初始化
 * ============================================================ */
function initFontSelect(){
  var sel=$("font-select");
  sel.innerHTML="";
  FONTS.forEach(function(f,i){
    var opt=document.createElement("option");
    opt.value=String(i);opt.textContent=f.name;
    sel.appendChild(opt);
  });
}

function init(){
  initFontSelect();
  applyAll();
  bindGame();
  bindSettings();
  bindPicker();
  setView("menu");
  setStatus("就绪");
  loadDataDirInfo();
}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",init);
else init();
})();
</script>
</body>
</html>
"""


# ============================================================
# HTTP 服务
# ============================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "GuessNumberPro/1.1"

    def log_message(self, fmt, *args):
        # 关闭访问日志，避免控制台刷屏
        pass

    # ---------- 输出工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("请求格式错误")

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, HTML_PAGE, "text/html; charset=utf-8")

            elif u.path == "/api/history":
                page = int(q.get("page", ["1"])[0])
                size = max(1, min(50, int(q.get("size", ["10"])[0])))
                self._json(200, history_page(page, size))

            elif u.path == "/api/stats":
                self._json(200, stats())

            elif u.path == "/api/config":
                self._json(200, {
                    "data_dir": get_data_dir(),
                    "records_file": records_file(),
                    "config_file": CONFIG_FILE,
                    "default_dir": os.path.join(DEFAULT_BASE, "GuessGame"),
                })

            elif u.path == "/api/export":
                self._handle_export(q)

            else:
                self._json(404, {"error": "未找到"})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(500, {"error": "服务器错误：" + str(e)})

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/game":
                payload = self._read_json()
                level = str(payload.get("level", ""))
                self._json(200, new_game(level, payload))

            elif u.path == "/api/guess":
                payload = self._read_json()
                gid = str(payload.get("id", ""))
                try:
                    guess = int(payload.get("guess"))
                except (TypeError, ValueError):
                    raise ValueError("请输入整数")
                self._json(200, submit_guess(gid, guess))

            elif u.path == "/api/config/data_dir":
                payload = self._read_json()
                path = str(payload.get("path", ""))
                result = change_data_dir(path)
                self._json(200, result)

            else:
                self._json(404, {"error": "未找到"})
        except KeyError as e:
            self._json(404, {"error": str(e).strip("'\"")})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(500, {"error": "服务器错误：" + str(e)})

    # ---------- 导出 ----------
    def _handle_export(self, q):
        fmt = q.get("fmt", ["csv"])[0].lower()
        scope = q.get("scope", ["all"])[0]
        records = load_records()

        if scope == "recent":
            data = list(reversed(records[-10:]))
        elif scope == "win":
            data = [r for r in records if r["win"]]
        elif scope == "lose":
            data = [r for r in records if not r["win"]]
        else:
            data = list(records)

        if not data:
            self._json(400, {"error": "该范围内没有记录"})
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if fmt == "html":
            body = export_html(data).encode("utf-8")
            fname = f"guess_records_{scope}_{stamp}.html"
            ctype = "text/html; charset=utf-8"
        else:
            body = export_csv(data).encode("utf-8-sig")
            fname = f"guess_records_{scope}_{stamp}.csv"
            ctype = "text/csv; charset=utf-8"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{fname}"',
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ============================================================
# 启动
# ============================================================
def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def find_port(start: int = 8000, tries: int = 30) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def main() -> None:
    try:
        ensure_dirs()
    except OSError as e:
        print(f"[错误] 无法创建数据目录：{get_data_dir()}\n原因：{e}")
        input("按回车退出...")
        sys.exit(1)

    try:
        port = find_port(8000)
    except RuntimeError as e:
        print(f"[错误] {e}")
        input("按回车退出...")
        sys.exit(1)

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    lan_ip = get_lan_ip()

    print("=" * 60)
    print("  GuessNumberPro · 猜数字游戏（Web 版）")
    print("=" * 60)
    print(f"  电脑访问：  http://127.0.0.1:{port}")
    print(f"  手机访问：  http://{lan_ip}:{port}")
    print(f"  数据目录：  {get_data_dir()}")
    print(f"  配置文件：  {CONFIG_FILE}")
    print()
    print("  ⚠ 手机需和电脑连在同一个 Wi-Fi 下")
    print("  ⚠ 首次运行如弹防火墙提示，请点【允许访问】")
    print("  💡 页面左下角齿轮图标可打开设置")
    print("  💡 设置里可更换主题、字体、数据目录")
    print("  按 Ctrl+C 退出")
    print("=" * 60)

    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()