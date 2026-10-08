# -*- coding: utf-8 -*-
"""
猜数字游戏
- 数据存放在用户可写目录，不依赖程序所在目录
- 无防移动锁，文件夹可自由移动/复制
- 记录文件使用混淆 + HMAC 校验（非强加密，仅防手改）
"""

import os
import sys
import csv
import json
import math
import hmac
import html
import base64
import random
import hashlib
import shutil
import secrets
import tempfile
import unittest
import ctypes
from ctypes import wintypes
from datetime import datetime
from typing import TypedDict
from unittest import mock


# ============================================================
# 类型定义
# ============================================================
class GameRecord(TypedDict):
    win: bool
    level_name: str
    answer: int
    attempts: int
    max_attempts: int
    guesses: list
    time: str


# ============================================================
# 常量
# ============================================================
LEVELS = {
    "1": {"name": "简单", "max_num": 1000, "max_attempts": 20},
    "2": {"name": "中等", "max_num": 10000, "max_attempts": 30},
    "3": {"name": "困难", "max_num": 100000, "max_attempts": 40},
}

SECRET_KEY = "guess_game_secret_key_#2024@secure!"

MAX_BACKUPS = 10
USE_SINGLE_INSTANCE = True


# ============================================================
# 路径
# ============================================================
def get_data_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if not base:
            base = os.path.expanduser("~")
    elif sys.platform == "darwin":
        home = os.path.expanduser("~")
        base = (
            os.path.join(home, "Library", "Application Support")
            if home and home != "~"
            else ""
        )
    else:
        base = os.environ.get("XDG_DATA_HOME")
        if not base:
            home = os.path.expanduser("~")
            base = (
                os.path.join(home, ".local", "share")
                if home and home != "~"
                else ""
            )

    if not base or base == "~":
        base = tempfile.gettempdir()
    return os.path.join(base, "GuessGame")


DATA_DIR = get_data_dir()
RECORDS_FILE = os.path.join(DATA_DIR, "records.dat")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
LOCK_FILE = os.path.join(DATA_DIR, "instance.lock")


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)


# ============================================================
# 混淆 + HMAC 校验
# ============================================================
def _key_bytes() -> bytes:
    return hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _make_mac(cipher: bytes) -> str:
    return hmac.new(_key_bytes(), cipher, hashlib.sha256).hexdigest()


def encrypt_text(text: str) -> str:
    cipher = _xor_bytes(text.encode("utf-8"), _key_bytes())
    payload = {
        "c": base64.b64encode(cipher).decode("ascii"),
        "m": _make_mac(cipher),
    }
    return base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def decrypt_text(token: str) -> str:
    raw = base64.b64decode(token.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    cipher = base64.b64decode(payload["c"].encode("ascii"))
    expected = _make_mac(cipher)
    if not hmac.compare_digest(payload.get("m", ""), expected):
        raise ValueError("数据校验失败，文件可能被篡改")
    return _xor_bytes(cipher, _key_bytes()).decode("utf-8")


# ============================================================
# 备份管理
# ============================================================
def _cleanup_backups() -> None:
    """两类备份文件各自保留最近 MAX_BACKUPS 个。

    按文件名排序（文件名内嵌时间戳），比 mtime 更稳定：
    同一秒内多次备份 mtime 可能相同，文件名末尾有 _1、_2 区分。
    """
    try:
        entries = os.listdir(BACKUP_DIR)
    except OSError:
        return

    for prefix in ("records.bak_", "records.corrupt_"):
        files = [
            os.path.join(BACKUP_DIR, f)
            for f in entries
            if f.startswith(prefix)
        ]
        files.sort(key=lambda p: os.path.basename(p), reverse=True)
        for old in files[MAX_BACKUPS:]:
            try:
                os.remove(old)
            except OSError:
                pass


def _backup_corrupt_file(reason: str, quiet: bool = False) -> str:
    ensure_data_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    target = os.path.join(BACKUP_DIR, f"records.bak_{stamp}")
    counter = 1
    while os.path.exists(target):
        target = os.path.join(BACKUP_DIR, f"records.bak_{stamp}_{counter}")
        counter += 1

    shutil.copyfile(RECORDS_FILE, target)
    try:
        os.chmod(target, 0o644)
    except OSError:
        pass

    corrupt_path = os.path.join(BACKUP_DIR, f"records.corrupt_{stamp}")
    counter = 1
    while os.path.exists(corrupt_path):
        corrupt_path = os.path.join(
            BACKUP_DIR, f"records.corrupt_{stamp}_{counter}"
        )
        counter += 1
    try:
        os.replace(RECORDS_FILE, corrupt_path)
    except OSError:
        pass

    _cleanup_backups()

    if not quiet:
        print("\n" + "!" * 60)
        print("警告：记录文件无法读取，可能已损坏或被外部修改。")
        print(f"原因：{reason}")
        print(f"原文件已移动至：{corrupt_path}")
        print(f"副本已备份至：  {target}")
        print("本次启动将以空记录开始。")
        print("!" * 60)
    return target


# ============================================================
# 记录读写
# ============================================================
def load_records(interactive: bool = True) -> list:
    """读取记录。

    interactive=False 时，损坏不再阻塞 input()，也不打印警告，
    用于测试 / 集成。
    """
    if not os.path.exists(RECORDS_FILE):
        return []

    try:
        with open(RECORDS_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if not token:
            return []
        data = json.loads(decrypt_text(token))
        if not isinstance(data, list):
            raise ValueError("记录文件结构异常")
        return data
    except Exception as e:
        _backup_corrupt_file(str(e), quiet=not interactive)
        if interactive:
            try:
                input("按回车继续...")
            except EOFError:
                pass
        return []


def save_records(records: list) -> bool:
    try:
        ensure_data_dir()
        token = encrypt_text(json.dumps(records, ensure_ascii=False))

        fd, tmp_path = tempfile.mkstemp(
            prefix="records.", suffix=".tmp", dir=DATA_DIR
        )
        try:
            try:
                f = os.fdopen(fd, "w", encoding="utf-8")
            except Exception:
                # fdopen 失败：fd 还没被接管，自己关掉，避免泄漏
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

            with f:
                f.write(token)

            os.replace(tmp_path, RECORDS_FILE)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        return True
    except OSError as e:
        print(f"\n[错误] 保存记录失败：{e}")
        print("本局记录未能写入磁盘，将从内存中回滚，避免退出后丢失。")
        return False


def append_record(records: list, record: GameRecord) -> bool:
    records.append(record)
    if not save_records(records):
        records.pop()
        return False
    return True


# ============================================================
# 单实例锁（含陈旧锁检测）
# ============================================================
_WIN_KERNEL32 = None  # 缓存 kernel32，避免每次重新声明签名


def _get_win_kernel32():
    """懒加载 kernel32 并显式声明签名。

    默认 restype 是 c_int，64 位 Windows 上 HANDLE 是 64 位，
    句柄值超过 2^31-1 会被截断成错误值，导致 CloseHandle 拿错句柄。
    这里显式声明 restype / argtypes，避免截断。
    """
    global _WIN_KERNEL32
    if _WIN_KERNEL32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _WIN_KERNEL32 = kernel32
    return _WIN_KERNEL32


def _is_stale_lock(path: str) -> bool:
    try:
        with open(path, "r", encoding="ascii") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return True

    if pid == os.getpid():
        return False

    if os.name == "nt":
        # Windows: 千万不要用 os.kill(pid, 0)！Python 在 Windows 上
        # 把 sig 直接传给 TerminateProcess，会把无辜进程杀掉。
        # 用 OpenProcess 只做存在性检查。
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_INVALID_PARAMETER = 87

        kernel32 = _get_win_kernel32()
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            kernel32.CloseHandle(handle)
            return False

        err = ctypes.get_last_error()
        if err == ERROR_INVALID_PARAMETER:
            # 进程确实不存在 → 陈旧锁
            return True
        # 权限不足等其他错误：保守视为进程存在，避免误删别人的锁
        return False

    # Unix: os.kill(pid, 0) 只检查存在性，不发信号
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        # 进程存在，只是属于别的用户，不算陈旧
        return False
    except OSError:
        return True


class InstanceLock:
    def __init__(self, path: str):
        self.path = path
        self.fd = None

    def acquire(self) -> bool:
        try:
            ensure_data_dir()
            fd = os.open(
                self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            if _is_stale_lock(self.path):
                try:
                    os.remove(self.path)
                except OSError:
                    return False
                return self.acquire()
            return False
        except OSError:
            # 目录不可写之类，不阻止运行
            return True

        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError:
            os.close(fd)
            try:
                os.remove(self.path)
            except OSError:
                pass
            return True

        self.fd = fd
        return True

    def release(self) -> None:
        try:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

            if not os.path.exists(self.path):
                return

            # 只有锁文件里还是自己的 PID 时才删除，
            # 避免竞态场景下误删别的实例的锁。
            try:
                with open(self.path, "r", encoding="ascii") as f:
                    owner = f.read().strip()
            except OSError:
                return

            if owner == str(os.getpid()):
                try:
                    os.remove(self.path)
                except OSError:
                    pass
        except OSError:
            pass


# ============================================================
# HTML 转义
# ============================================================
def _esc(value) -> str:
    return html.escape(str(value), quote=True)


# ============================================================
# 导出
# ============================================================
def export_to_csv(records: list, filepath: str) -> None:
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "序号", "时间", "难度", "结果", "正确答案",
            "使用次数", "最大次数", "使用次数百分比", "猜测序列",
        ])

        for i, rec in enumerate(records, 1):
            status = "猜对" if rec["win"] else "失败"
            percent = ""
            if rec["win"] and rec["max_attempts"] > 0:
                percent = f"{rec['attempts'] / rec['max_attempts'] * 100:.2f}%"

            guesses = " -> ".join(map(str, rec["guesses"])) if rec["guesses"] else ""

            writer.writerow([
                i,
                rec["time"],
                rec["level_name"],
                status,
                rec["answer"],
                rec["attempts"],
                rec["max_attempts"],
                percent,
                guesses,
            ])


def export_to_html(records: list, filepath: str) -> None:
    lines = [
        "<!DOCTYPE html>",
        "<html lang='zh-CN'>",
        "<head>",
        "<meta charset='utf-8'>",
        "<title>猜数字游戏历史记录</title>",
        "<style>",
        "body { font-family: 'Microsoft YaHei', Arial, sans-serif; margin: 20px; }",
        "h1 { color: #333; }",
        "table { border-collapse: collapse; width: 100%; }",
        "th, td { border: 1px solid #999; padding: 6px 10px; text-align: center; }",
        "th { background-color: #f0f0f0; }",
        "tr:nth-child(even) { background-color: #fafafa; }",
        ".win { color: green; font-weight: bold; }",
        ".lose { color: red; font-weight: bold; }",
        "</style>",
        "</head>",
        "<body>",
        "<h1>猜数字游戏历史记录</h1>",
        f"<p>导出时间：{_esc(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}，"
        f"共 {len(records)} 条记录</p>",
        "<table>",
        "<tr>"
        "<th>序号</th><th>时间</th><th>难度</th><th>结果</th>"
        "<th>正确答案</th><th>使用次数</th><th>最大次数</th>"
        "<th>使用次数百分比</th><th>猜测序列</th>"
        "</tr>",
    ]

    for i, rec in enumerate(records, 1):
        status_class = "win" if rec["win"] else "lose"
        status_text = "猜对" if rec["win"] else "失败"

        percent = ""
        if rec["win"] and rec["max_attempts"] > 0:
            percent = f"{rec['attempts'] / rec['max_attempts'] * 100:.2f}%"

        guesses = " -> ".join(map(str, rec["guesses"])) if rec["guesses"] else ""

        lines.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{_esc(rec['time'])}</td>"
            f"<td>{_esc(rec['level_name'])}</td>"
            f"<td class='{status_class}'>{status_text}</td>"
            f"<td>{_esc(rec['answer'])}</td>"
            f"<td>{_esc(rec['attempts'])}</td>"
            f"<td>{_esc(rec['max_attempts'])}</td>"
            f"<td>{_esc(percent)}</td>"
            f"<td>{_esc(guesses)}</td>"
            "</tr>"
        )

    lines.extend(["</table>", "</body>", "</html>"])

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _unique_export_path(scope_name: str, ext: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = secrets.token_hex(2)
    return os.path.join(
        EXPORT_DIR, f"guess_records_{scope_name}_{timestamp}_{suffix}.{ext}"
    )


def export_records(records: list) -> None:
    clear_screen()
    print("=" * 60)
    print("导出历史记录")
    print("=" * 60)

    if not records:
        print("暂无记录，无法导出。")
        input("\n按回车返回主菜单...")
        return

    print(f"当前共有 {len(records)} 条记录。")
    print()
    print("请选择导出格式：")
    print("1 - CSV（推荐，WPS 表格可直接打开）")
    print("2 - HTML（WPS 文字 / 浏览器可打开）")
    print("3 - 同时导出 CSV 和 HTML")
    print("0 - 取消")

    choice = input("\n请输入：").strip()
    if choice == "0":
        return
    if choice not in ("1", "2", "3"):
        print("输入无效。")
        input("按回车返回...")
        return

    print()
    print("请选择导出范围：")
    print("1 - 全部记录")
    print("2 - 仅最近 10 条（最新在上）")
    print("3 - 仅猜对的记录")
    print("4 - 仅失败的记录")
    scope = input("请输入：").strip()

    if scope == "1":
        data, scope_name = list(records), "全部"
    elif scope == "2":
        data, scope_name = list(reversed(records[-10:])), "最近10条"
    elif scope == "3":
        data, scope_name = [r for r in records if r["win"]], "猜对"
    elif scope == "4":
        data, scope_name = [r for r in records if not r["win"]], "失败"
    else:
        print("输入无效。")
        input("按回车返回...")
        return

    if not data:
        print("该范围内没有记录。")
        input("按回车返回...")
        return

    ensure_data_dir()
    exported_files = []

    try:
        if choice in ("1", "3"):
            csv_path = _unique_export_path(scope_name, "csv")
            export_to_csv(data, csv_path)
            exported_files.append(csv_path)

        if choice in ("2", "3"):
            html_path = _unique_export_path(scope_name, "html")
            export_to_html(data, html_path)
            exported_files.append(html_path)
    except OSError as e:
        print(f"导出失败：{e}")
        input("按回车返回...")
        return

    print("\n" + "=" * 60)
    print("导出成功！")
    print("=" * 60)
    for p in exported_files:
        print(f"  {p}")
    print()
    print("提示：")
    print("- CSV 文件用 WPS 表格打开即可，中文已处理不会乱码。")
    print("- HTML 文件可直接用浏览器打开，也可用 WPS 文字打开。")
    print(f"- 导出文件位置：{EXPORT_DIR}")
    input("\n按回车返回主菜单...")


# ============================================================
# 界面工具
# ============================================================
def clear_screen() -> None:
    if not sys.stdout.isatty():
        return
    os.system("cls" if os.name == "nt" else "clear")


# ============================================================
# 游戏主体
# ============================================================
def get_custom_level() -> dict:
    clear_screen()
    print("=" * 40)
    print("自定义难度设置")
    print("=" * 40)

    while True:
        try:
            max_num = int(input("请输入数字范围上限（至少为 2，例如 5000）：").strip())
            if max_num < 2:
                print("范围上限必须至少为 2！请重新输入。")
                continue
            break
        except ValueError:
            print("只能输入正整数！请重新输入。")

    while True:
        try:
            max_attempts = int(
                input("请输入最大猜测次数（至少为 1，例如 15）：").strip()
            )
            if max_attempts < 1:
                print("猜测次数必须至少为 1！请重新输入。")
                continue
            break
        except ValueError:
            print("只能输入正整数！请重新输入。")

    min_needed = math.ceil(math.log2(max_num + 1))
    print(f"\n提示：对于 1-{max_num} 的范围，理论上最少 {min_needed} 次即可猜中。")
    if max_attempts < min_needed:
        print(
            f"注意：你设置的次数（{max_attempts}）低于理论最少次数"
            f"（{min_needed}），可能会很难获胜！"
        )
    input("按回车确认...")

    return {
        "name": f"自定义(1-{max_num}, {max_attempts}次)",
        "max_num": max_num,
        "max_attempts": max_attempts,
    }


def play_one_round() -> GameRecord:
    config = None

    while config is None:
        clear_screen()
        print("请选择难度等级：")
        print("1 - 简单 (1-1000，最多猜 20 次)")
        print("2 - 中等 (1-10000，最多猜 30 次)")
        print("3 - 困难 (1-100000，最多猜 40 次)")
        print("4 - 自定义 (自己设置范围和次数)")
        diff = input("\n请输入数字 1、2、3 或 4：").strip()

        if diff in LEVELS:
            config = LEVELS[diff]
        elif diff == "4":
            config = get_custom_level()
        else:
            print("输入无效，按回车重新选择...")
            input()

    max_num = config["max_num"]
    max_attempts = config["max_attempts"]
    answer = random.randint(1, max_num)

    attempts = 0
    history = []
    min_range, max_range = 1, max_num

    clear_screen()
    print(f"难度已选择：【{config['name']}】")
    print(f"我已经想好了一个 1 到 {max_num} 之间的数字。")
    print(f"你有 {max_attempts} 次机会。")

    while attempts < max_attempts:
        remain = max_attempts - attempts
        hist_str = " -> ".join(map(str, history)) if history else "无"

        print("=" * 40)
        print(f"当前范围：{min_range} ~ {max_range} | 剩余次数：{remain}")
        print(f"历史猜测：{hist_str}")

        try:
            guess_str = input(
                f"请输入你猜测的数字（{min_range}-{max_range}）："
            ).strip()
            if guess_str == "":
                print("不能为空，请重新输入！")
                continue
            guess = int(guess_str)
        except ValueError:
            print("只能输入正整数！")
            continue

        if guess < min_range or guess > max_range:
            print("这个数不在当前可能范围内！")
            continue

        if guess in history:
            print("你已经猜过这个数字了！")
            continue

        attempts += 1
        history.append(guess)

        if guess < answer:
            min_range = max(min_range, guess + 1)
            print("太小了！")
        elif guess > answer:
            max_range = min(max_range, guess - 1)
            print("太大了！")
        else:
            used_percent = attempts / max_attempts * 100
            print("=" * 40)
            print(
                f"恭喜你，猜对了！正确答案就是 {answer}，"
                f"你一共猜了 {attempts} 次。"
            )
            print(
                f"本局猜对使用次数百分比："
                f"{attempts}/{max_attempts} = {used_percent:.2f}%"
            )
            return {
                "win": True,
                "level_name": config["name"],
                "answer": answer,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "guesses": history.copy(),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    print("=" * 40)
    print(f"很遗憾，次数用完了。正确答案是：{answer}")
    return {
        "win": False,
        "level_name": config["name"],
        "answer": answer,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "guesses": history.copy(),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def view_history(records: list) -> None:
    clear_screen()
    print("=" * 60)
    print(f"历史记录（共 {len(records)} 条）")
    print("=" * 60)

    if not records:
        print("暂无历史记录。")
        input("\n按回车返回主菜单...")
        return

    PAGE_SIZE = 10
    total_pages = (len(records) + PAGE_SIZE - 1) // PAGE_SIZE
    page = 1

    while True:
        clear_screen()
        print("=" * 60)
        print(f"历史记录（第 {page}/{total_pages} 页，共 {len(records)} 条）")
        print("=" * 60)

        start = (page - 1) * PAGE_SIZE
        end = min(start + PAGE_SIZE, len(records))

        for i in range(start, end):
            rec = records[i]
            status = "猜对" if rec["win"] else "失败"
            print(f"{i + 1}. [{rec['level_name']}] {status}")
            print(
                f"   答案：{rec['answer']}  "
                f"使用次数：{rec['attempts']}/{rec['max_attempts']}"
            )

            if rec["win"] and rec["max_attempts"] > 0:
                percent = rec["attempts"] / rec["max_attempts"] * 100
                print(f"   使用次数百分比：{percent:.2f}%")

            guesses = (
                " -> ".join(map(str, rec["guesses"])) if rec["guesses"] else "无"
            )
            print(f"   猜测序列：{guesses}")
            print(f"   时间：{rec['time']}")
            print("-" * 60)

        print("\n操作：n - 下一页  p - 上一页  f - 跳转页码  0 - 返回主菜单")
        cmd = input("请输入操作：").strip().lower()

        if cmd == "n":
            if page < total_pages:
                page += 1
            else:
                print("已经是最后一页了。")
                input("按回车继续...")
        elif cmd == "p":
            if page > 1:
                page -= 1
            else:
                print("已经是第一页了。")
                input("按回车继续...")
        elif cmd == "f":
            try:
                target = int(input(f"请输入页码（1-{total_pages}）：").strip())
                if 1 <= target <= total_pages:
                    page = target
                else:
                    print("页码超出范围。")
                    input("按回车继续...")
            except ValueError:
                print("输入无效。")
                input("按回车继续...")
        elif cmd == "0":
            return
        else:
            print("输入无效。")
            input("按回车继续...")


def show_stats(records: list) -> None:
    clear_screen()
    print("=" * 60)
    print("游戏统计")
    print("=" * 60)

    total_rounds = len(records)
    win_records = [r for r in records if r["win"]]
    win_rounds = len(win_records)

    print(f"总局数：{total_rounds}")
    print(f"猜对局数：{win_rounds}")

    if total_rounds == 0:
        print("暂无数据。")
        input("\n按回车返回主菜单...")
        return

    print(f"猜对率：{win_rounds / total_rounds * 100:.2f}%")

    if win_rounds == 0:
        print("没有猜对过，无法计算猜对使用次数百分比。")
        input("\n按回车返回主菜单...")
        return

    total_win_attempts = sum(r["attempts"] for r in win_records)
    total_win_max_attempts = sum(r["max_attempts"] for r in win_records)
    percent = (
        total_win_attempts / total_win_max_attempts * 100
        if total_win_max_attempts
        else 0
    )

    print(f"猜对时累计使用次数：{total_win_attempts}")
    print(f"猜对时累计可用次数：{total_win_max_attempts}")
    print(f"猜对使用次数百分比：{percent:.2f}%")
    print(f"平均每局猜对使用次数：{total_win_attempts / win_rounds:.2f}")

    input("\n按回车返回主菜单...")


# ============================================================
# 主流程
# ============================================================
def main() -> None:
    try:
        ensure_data_dir()
    except OSError as e:
        print(f"[错误] 无法创建数据目录：{DATA_DIR}")
        print(f"原因：{e}")
        print("请检查磁盘空间和目录权限。")
        input("按回车退出...")
        sys.exit(1)

    lock = None
    if USE_SINGLE_INSTANCE:
        lock = InstanceLock(LOCK_FILE)
        if not lock.acquire():
            print("检测到猜数字游戏已在运行。")
            print("如果确认没有其他窗口在运行，可删除锁文件后重试：")
            print(f"  {LOCK_FILE}")
            input("按回车退出...")
            sys.exit(0)

    try:
        records = load_records(interactive=True)

        clear_screen()
        print("=" * 60)
        print("欢迎来到猜数字游戏！")
        print("=" * 60)
        print(f"数据目录：{DATA_DIR}")
        print(f"记录文件：{os.path.basename(RECORDS_FILE)}")
        print(f"导出目录：{EXPORT_DIR}")
        print(f"已加载历史记录 {len(records)} 条。")
        input("按回车开始游戏...")

        while True:
            record = play_one_round()

            if not append_record(records, record):
                print("\n注意：本局记录未能保存到磁盘，已从内存中回滚。")
                input("按回车继续...")

            while True:
                print("\n" + "=" * 40)
                print("请选择操作：")
                print("1 - 再来一局")
                print("2 - 查看历史记录")
                print("3 - 查看统计")
                print("4 - 导出记录（CSV / HTML，WPS 可打开）")
                print("5 - 退出游戏")
                choice = input("请输入数字 1、2、3、4 或 5：").strip()

                if choice == "1":
                    break
                elif choice == "2":
                    view_history(records)
                elif choice == "3":
                    show_stats(records)
                elif choice == "4":
                    export_records(records)
                elif choice == "5":
                    print("游戏已退出，感谢游玩！")
                    return
                else:
                    print("输入无效，请重新选择。")
    finally:
        if lock is not None:
            lock.release()


# ============================================================
# 单元测试
# ============================================================
class _Tests(unittest.TestCase):
    def test_theoretical_min(self):
        self.assertEqual(math.ceil(math.log2(2 + 1)), 2)
        self.assertEqual(math.ceil(math.log2(3 + 1)), 2)
        self.assertEqual(math.ceil(math.log2(4 + 1)), 3)
        self.assertEqual(math.ceil(math.log2(1000 + 1)), 10)

    def test_xor_roundtrip(self):
        key = _key_bytes()
        data = "你好，guess_game! 12345"
        cipher = _xor_bytes(data.encode("utf-8"), key)
        plain = _xor_bytes(cipher, key).decode("utf-8")
        self.assertEqual(plain, data)

    def test_encrypt_decrypt_roundtrip(self):
        text = "hello 猜数字"
        self.assertEqual(decrypt_text(encrypt_text(text)), text)

    def test_mac_tamper_detected(self):
        token = encrypt_text("hello")
        raw = json.loads(base64.b64decode(token.encode("ascii")).decode("utf-8"))
        raw["m"] = "0" * 64
        bad = base64.b64encode(
            json.dumps(raw, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        with self.assertRaises(ValueError):
            decrypt_text(bad)

    def test_csv_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.csv")
            export_to_csv([], p)
            with open(p, "r", encoding="utf-8-sig") as f:
                content = f.read()
            self.assertIn("序号", content)

    def test_csv_with_records(self):
        rec = {
            "win": True,
            "level_name": "简单",
            "answer": 42,
            "attempts": 3,
            "max_attempts": 20,
            "guesses": [50, 25, 42],
            "time": "2024-01-01 00:00:00",
        }
        rec2 = {
            "win": False,
            "level_name": "中等",
            "answer": 777,
            "attempts": 30,
            "max_attempts": 30,
            "guesses": [1, 2, 3],
            "time": "2024-01-02 00:00:00",
        }
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.csv")
            export_to_csv([rec, rec2], p)
            with open(p, "r", encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[1][3], "猜对")
            self.assertEqual(rows[1][7], "15.00%")
            self.assertEqual(rows[2][3], "失败")
            self.assertEqual(rows[2][7], "")

    def test_html_escape(self):
        rec = {
            "win": True,
            "level_name": "<script>bad</script>",
            "answer": 1,
            "attempts": 1,
            "max_attempts": 1,
            "guesses": [1],
            "time": "2024-01-01 00:00:00",
        }
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.html")
            export_to_html([rec], p)
            with open(p, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("<script>", content)
            self.assertIn("&lt;script&gt;", content)

    def test_backup_cleanup(self):
        """两类备份文件各自保留 MAX_BACKUPS 个。"""
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys.modules[__name__], "BACKUP_DIR", d), \
                 mock.patch.object(sys.modules[__name__], "MAX_BACKUPS", 2):
                for i in range(5):
                    open(os.path.join(d, f"records.bak_2024010{i}_000000"), "w").close()
                    open(os.path.join(d, f"records.corrupt_2024010{i}_000000"), "w").close()

                _cleanup_backups()

                remaining = os.listdir(d)
                bak = [f for f in remaining if f.startswith("records.bak_")]
                corrupt = [f for f in remaining if f.startswith("records.corrupt_")]
                self.assertEqual(len(bak), 2)
                self.assertEqual(len(corrupt), 2)
                # 保留文件名最大的两个（时间戳最新）
                self.assertIn("records.bak_20240104_000000", bak)

    def test_instance_lock_acquire_release(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "instance.lock")
            lock = InstanceLock(p)
            self.assertTrue(lock.acquire())
            self.assertTrue(os.path.exists(p))

            lock2 = InstanceLock(p)
            self.assertFalse(lock2.acquire())

            lock.release()
            self.assertFalse(os.path.exists(p))

    def test_instance_lock_release_does_not_remove_others(self):
        """release 时若锁文件里的 PID 不是自己的，不应删除。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "instance.lock")
            with open(p, "w", encoding="ascii") as f:
                f.write("12345")  # 假装是别的进程

            lock = InstanceLock(p)
            lock.release()  # self.fd 为 None，但应尝试读取并跳过删除
            self.assertTrue(os.path.exists(p))

    def test_stale_lock_detected(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "instance.lock")
            with open(p, "w", encoding="ascii") as f:
                f.write("99999999")
            self.assertTrue(_is_stale_lock(p))

    def test_append_record_rollback_on_failure(self):
        """save_records 返回 False 时 append_record 应回滚。"""
        records = []
        with mock.patch.object(
            sys.modules[__name__], "save_records", return_value=False
        ):
            ok = append_record(records, {
                "win": True, "level_name": "x", "answer": 1,
                "attempts": 1, "max_attempts": 1,
                "guesses": [1], "time": "t",
            })
        self.assertFalse(ok)
        self.assertEqual(records, [])

    def test_load_records_non_interactive(self):
        """损坏文件在 interactive=False 时不阻塞，返回空列表，且不打印。"""
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys.modules[__name__], "DATA_DIR", d), \
                 mock.patch.object(sys.modules[__name__], "BACKUP_DIR", os.path.join(d, "b")), \
                 mock.patch.object(sys.modules[__name__], "RECORDS_FILE", os.path.join(d, "records.dat")):
                os.makedirs(os.path.join(d, "b"), exist_ok=True)
                with open(os.path.join(d, "records.dat"), "w", encoding="utf-8") as f:
                    f.write("this is not a valid token")
                result = load_records(interactive=False)
                self.assertEqual(result, [])


def _run_tests() -> int:
    """运行单元测试，返回退出码（不直接 sys.exit）。"""
    suite = unittest.TestLoader().loadTestsFromTestCase(_Tests)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(_run_tests())
    else:
        try:
            main()
        except KeyboardInterrupt:
            print("\n\n游戏被中断，已退出。")
            sys.exit(0)