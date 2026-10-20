#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
猜数字游戏 · Web 版（PC / 手机通用）
✅支持Windows、Mac(苹果)、华为安卓手机
✅支持【同一局域网】+【跨不同网络外网访问】(pyngrok内网穿透)

打包命令：
    pip install pyngrok
    pyinstaller --onefile --name GuessGame --console guess_game_web.py

运行：
    python guess_game_web.py
    启动后会打印：本地地址、局域网地址、公网外网地址（pyngrok隧道）
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
import socket
import sys
import tempfile
import threading
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ============ 新增：pyngrok内网穿透 实现跨不同网络访问 ============
NGROK_AVAILABLE = False
try:
    from pyngrok import ngrok
    NGROK_AVAILABLE = True
except ImportError:
    pass

# ============================================================
# 加密存储（与原版一致：XOR + HMAC）
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
# 数据目录
# ============================================================
def get_data_dir() -> str:
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
    return os.path.join(base, "GuessGame")


DATA_DIR = get_data_dir()
RECORDS_FILE = os.path.join(DATA_DIR, "records.dat")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")


def ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)


# ============================================================
# 记录读写
# ============================================================
_records_lock = threading.Lock()


def _read_records_unlocked():
    if not os.path.exists(RECORDS_FILE):
        return []
    try:
        with open(RECORDS_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if not token:
            return []
        data = json.loads(decrypt_text(token))
        return data if isinstance(data, list) else []
    except Exception:
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = os.path.join(DATA_DIR, f"records.corrupt_{stamp}.dat")
            os.replace(RECORDS_FILE, bak)
        except OSError:
            pass
        return []


def _write_records_unlocked(records) -> None:
    ensure_dirs()
    token = encrypt_text(json.dumps(records, ensure_ascii=False))
    fd, tmp = tempfile.mkstemp(prefix="records.", suffix=".tmp", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        os.replace(tmp, RECORDS_FILE)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_records():
    with _records_lock:
        return _read_records_unlocked()


def append_record(record) -> bool:
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
        "id": gid,
        "name": name,
        "max_num": max_num,
        "max_attempts": max_att,
        "answer": answer,
        "low": 1,
        "high": max_num,
        "guesses": [],
        "marks": [],
        "finished": False,
        "win": False,
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
                "win": g["win"],
                "level_name": g["name"],
                "answer": answer,
                "attempts": attempts,
                "max_attempts": g["max_attempts"],
                "guesses": list(g["guesses"]),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            _games.pop(gid, None)

    if record:
        append_record(record)
    return result


def history_page(page: int, size: int) -> dict:
    records = list(reversed(load_records()))  # 最新在前
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
    win_attempts = sum(r["attempts"] for r in wins)
    win_max = sum(r["max_attempts"] for r in wins)
    return {
        "total": total,
        "wins": n_win,
        "win_rate": (n_win / total * 100) if total else 0.0,
        "win_attempts": win_attempts,
        "win_max_attempts": win_max,
        "win_percent": (win_attempts / win_max * 100) if win_max else 0.0,
        "avg_attempts": (win_attempts / n_win) if n_win else 0.0,
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
# 前端页面（内嵌，无需外部文件）
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b1220">
<title>猜数字游戏</title>
<style>
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#0b1220;color:#e8eefc;min-height:100vh;
  font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  touch-action:manipulation}
.hidden{display:none!important}
.topbar{position:fixed;top:0;left:0;right:0;z-index:10;display:flex;
  align-items:center;justify-content:space-between;padding:14px 16px;
  padding-top:calc(14px + env(safe-area-inset-top));
  background:#0e1730;border-bottom:1px solid #1e2b47}
.brand{font-size:17px;font-weight:700}
.status{font-size:12px;color:#7f93b8;border:1px solid #24334f;
  border-radius:999px;padding:3px 10px}
.status.bad{color:#ff8b8b;border-color:#5a2a2a}
main{padding:16px;padding-top:calc(74px + env(safe-area-inset-top));
  padding-bottom:calc(90px + env(safe-area-inset-bottom));
  max-width:640px;margin:0 auto}
.title{font-size:16px;font-weight:600;color:#a9bce0;margin:0 0 12px}
.levels{display:grid;gap:10px;grid-template-columns:1fr}
@media(min-width:520px){.levels{grid-template-columns:1fr 1fr}}
.level{display:flex;flex-direction:column;gap:2px;align-items:flex-start;
  width:100%;padding:14px 16px;border-radius:14px;min-height:64px;
  background:#16213a;border:1px solid #24334f;color:#e8eefc;
  font-size:16px;text-align:left;cursor:pointer;
  transition:transform .08s ease,background .15s ease}
.level:active{transform:scale(.985);background:#1c2a46}
.level b{font-size:16px}
.level small{font-size:12px;color:#8fa3c8}
.card{background:#16213a;border:1px solid #24334f;border-radius:14px;
  padding:14px;margin-bottom:12px}
input[type=number],select{width:100%;padding:12px 14px;border-radius:12px;
  background:#0f1830;border:1px solid #2a3a5c;color:#e8eefc;
  font-size:16px;outline:none}
input[type=number]::-webkit-outer-spin-button,
input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
input:focus,select:focus{border-color:#4f8cff}
label{display:block;font-size:13px;color:#8fa3c8;margin:10px 0 6px}
label:first-child{margin-top:0}
.btn{display:inline-flex;align-items:center;justify-content:center;
  min-height:48px;padding:0 18px;border-radius:12px;font-size:16px;
  font-weight:600;cursor:pointer;border:1px solid #2a3a5c;
  background:#1c2a46;color:#e8eefc;transition:transform .08s ease}
.btn:active{transform:scale(.97)}
.btn.primary{background:linear-gradient(180deg,#4f8cff,#3b6fe0);
  border-color:#3b6fe0;color:#fff}
.btn.ghost{background:transparent}
.btn.block{width:100%}
.btn:disabled{opacity:.45;cursor:not-allowed}
.row{display:flex;align-items:center;gap:10px}
.row.between{justify-content:space-between}
.muted{color:#8fa3c8}.small{font-size:13px}
.range{display:flex;justify-content:center;align-items:baseline;gap:10px;
  font-size:26px;font-weight:700;margin:10px 0}
.bar{height:6px;border-radius:3px;background:#0f1830;overflow:hidden}
.bar i{display:block;height:100%;width:0;
  background:linear-gradient(90deg,#4f8cff,#38d39f);transition:width .25s}
.feedback{min-height:44px;display:flex;align-items:center;
  font-size:15px;color:#a9bce0;margin-bottom:10px}
.feedback.low{color:#8fbcff}.feedback.high{color:#ffc861}
.feedback.win{color:#5ee0b0;font-weight:700}
.feedback.lose{color:#ff9b9b;font-weight:700}
.feedback.warn{color:#ffc861}
.input-row{display:flex;gap:8px}
.input-row input{flex:1;min-width:0}
.input-row .btn{flex:0 0 auto;padding:0 24px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{font-size:13px;padding:4px 10px;border-radius:999px;
  background:#0f1830;border:1px solid #2a3a5c;color:#a9bce0}
.chip.low{border-color:#3b6fe0;color:#8fbcff}
.chip.high{border-color:#7a5a1a;color:#ffc861}
.chip.win{border-color:#2a7a5c;color:#5ee0b0;font-weight:700}
.list{display:flex;flex-direction:column;gap:10px}
.list-item{background:#16213a;border:1px solid #24334f;
  border-left:4px solid #2a3a5c;border-radius:12px;padding:12px 14px}
.list-item.win{border-left-color:#38d39f}
.list-item.lose{border-left-color:#ff6b6b}
.li-head{display:flex;justify-content:space-between;align-items:center;
  gap:8px;margin-bottom:6px}
.li-row{display:flex;justify-content:space-between;gap:12px;
  font-size:14px;color:#8fa3c8;padding:2px 0}
.li-row b{color:#e8eefc;font-weight:600;text-align:right;word-break:break-all}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
.tag{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid}
.tag.ok{color:#5ee0b0;border-color:#2a7a5c}
.tag.bad{color:#ff9b9b;border-color:#7a2a2a}
.pager{display:flex;align-items:center;justify-content:space-between;
  gap:10px;margin-top:14px}
.pager .btn{min-height:42px;padding:0 14px;font-size:14px}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stat{background:#16213a;border:1px solid #24334f;
  border-radius:12px;padding:14px}
.stat .k{font-size:12px;color:#8fa3c8}
.stat .v{font-size:22px;font-weight:700;margin-top:4px}
.stat.wide{grid-column:1/-1}
.btn-row{display:flex;gap:10px;margin-top:12px}
.btn-row .btn{flex:1}
.hint{font-size:12px;color:#7f93b8;margin:10px 0 0}
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:10;
  display:grid;grid-template-columns:repeat(4,1fr);background:#0e1730;
  border-top:1px solid #1e2b47;padding-bottom:env(safe-area-inset-bottom)}
.tab{border:0;background:transparent;color:#7f93b8;padding:12px 4px;
  font-size:13px;cursor:pointer;min-height:52px}
.tab.active{color:#4f8cff;font-weight:700}
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

<script>
(function(){
"use strict";
var $=function(id){return document.getElementById(id)};
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

function bind(){
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

function init(){bind();updateCustomHint();setView("menu");setStatus("就绪")}
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
    server_version = "GuessGame/1.0"

    def log_message(self, fmt, *args):
        pass  # 关闭访问日志，避免控制台刷屏

    # ---------- 输出工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        print(f"[错误] 无法创建数据目录：{DATA_DIR}\n原因：{e}")
        input("按回车退出...")
        sys.exit(1)

    port = find_port(8000)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    lan_ip = get_lan_ip()

    # ============启动pyngrok隧道，实现不同网络访问 ============
    public_url = None
    if NGROK_AVAILABLE:
        try:
            print("\n正在启动外网隧道（pyngrok），请稍候...")
            tunnel = ngrok.connect(port, "http")
            public_url = tunnel.public_url
        except Exception as ex:
            print(f"⚠️ 外网隧道启动失败：{ex}，仅局域网可用")
    else:
        print("💡未安装pyngrok，只能同一wifi局域网访问；执行 pip install pyngrok 开启跨网访问")

    print("=" * 70)
    print("  🎯猜数字游戏 · Web多端版")
    print("=" * 70)
    print(f"  ✅本机访问：    http://127.0.0.1:{port}")
    print(f"  ✅同WiFi手机：  http://{lan_ip}:{port}")
    if public_url:
        print(f"  ✅外网跨网络：  {public_url}")
    print(f"  📂数据目录：    {DATA_DIR}")
    print()
    print("  ⚠️ 同WiFi：手机连同一个wifi，输入局域网地址")
    print("  ⚠️ 跨不同网络：复制【外网跨网络】链接发给手机（苹果/华为都可以浏览器打开）")
    print("  ⚠️ Windows防火墙弹出，请允许访问网络")
    print("  Ctrl+C 停止服务")
    print("=" * 70)

    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        if NGROK_AVAILABLE:
            ngrok.disconnect(public_url)
            ngrok.kill()
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
