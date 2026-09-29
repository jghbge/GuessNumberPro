import random
import sys
import os
import math
import json
import csv
import base64
import hashlib
import ctypes
import stat
import shutil
from datetime import datetime

LEVELS = {
    "1": {"name": "简单", "max_num": 1000, "max_attempts": 20},
    "2": {"name": "中等", "max_num": 10000, "max_attempts": 30},
    "3": {"name": "困难", "max_num": 100000, "max_attempts": 40}
}

# ==================== 加密密钥 ====================
SECRET_KEY = "guess_game_secret_key_#2024@secure!"


# ==================== 跨平台清屏 ====================
def clear_screen():
    """Windows 用 cls，其他系统用 clear"""
    os.system('cls' if os.name == 'nt' else 'clear')


# ==================== 路径 ====================
def get_base_dir():
    """获取软件（脚本或打包后的 exe）所在目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()

# 数据文件夹（所有记录、锁、导出都放这里，集中管理）
DATA_DIR = os.path.join(BASE_DIR, "猜数字游戏数据")
RECORDS_FILE = os.path.join(DATA_DIR, "guess_game_records.dat")
EXPORT_DIR = os.path.join(DATA_DIR, "导出记录")

# 多位置锁文件（删一个还有另一个）
LOCK_FILES = [
    os.path.join(BASE_DIR, ".gg_lock"),
    os.path.join(DATA_DIR, ".gg_lock"),
    os.path.join(BASE_DIR, "guess_game_records.dat.lock"),
]


# ==================== 加密工具 ====================
def _key_bytes():
    return hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def encrypt_text(text: str) -> str:
    key = _key_bytes()
    return base64.b64encode(_xor_bytes(text.encode("utf-8"), key)).decode("ascii")


def decrypt_text(token: str) -> str:
    key = _key_bytes()
    return _xor_bytes(base64.b64decode(token.encode("ascii")), key).decode("utf-8")


# ==================== 隐藏与只读 ====================
def hide_and_readonly(path):
    """把文件设置为隐藏 + 只读（Windows 下有效）"""
    if not os.path.exists(path):
        return
    try:
        os.chmod(path, stat.S_IREAD)
    except OSError:
        pass

    if os.name == "nt":
        try:
            FILE_ATTRIBUTE_HIDDEN = 0x02
            FILE_ATTRIBUTE_SYSTEM = 0x04
            FILE_ATTRIBUTE_READONLY = 0x01
            attrs = FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_READONLY
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs)
        except Exception:
            pass


def unhide_for_write(path):
    """写文件前先去掉只读/隐藏"""
    if not os.path.exists(path):
        return
    if os.name == "nt":
        try:
            FILE_ATTRIBUTE_NORMAL = 0x80
            ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_NORMAL)
        except Exception:
            pass
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


# ==================== 数据文件夹 ====================
def ensure_data_dir():
    """创建数据文件夹和导出子文件夹"""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    if not os.path.exists(EXPORT_DIR):
        os.makedirs(EXPORT_DIR)


# ==================== 位置锁 ====================
def _write_locks(path_value: str):
    """把路径加密后写入所有锁文件"""
    token = encrypt_text(path_value)
    for lf in LOCK_FILES:
        try:
            # 确保锁文件所在目录存在
            parent = os.path.dirname(lf)
            if parent and not os.path.exists(parent):
                os.makedirs(parent)
            # 先解除只读/隐藏，避免锁文件已存在时写入失败
            unhide_for_write(lf)
            with open(lf, "w", encoding="utf-8") as f:
                f.write(token)
            hide_and_readonly(lf)
        except OSError:
            pass


def _read_lock():
    """从任意一个锁文件读取路径"""
    for lf in LOCK_FILES:
        if os.path.exists(lf):
            try:
                with open(lf, "r", encoding="utf-8") as f:
                    return os.path.normpath(decrypt_text(f.read().strip()))
            except Exception:
                continue
    return None


def check_location():
    """
    返回 (是否允许运行, 原始路径, 状态码)
    状态码：ok / moved / deleted / new
    """
    current = os.path.normpath(os.path.abspath(BASE_DIR))
    saved = _read_lock()

    # 情况一：所有锁文件都不存在
    if saved is None:
        # 如果记录文件存在但锁没了 → 锁被删了
        if os.path.exists(RECORDS_FILE):
            return False, "已被删除", "deleted"
        # 真正的首次运行：创建数据文件夹 + 写锁
        ensure_data_dir()
        _write_locks(current)
        return True, current, "new"

    # 情况二：路径对不上 → 移动过
    if saved != current:
        return False, saved, "moved"

    # 情况三：路径匹配，但数据文件夹可能被误删，补建
    ensure_data_dir()
    return True, saved, "ok"


# ==================== 记录读写（加密） ====================
def load_records():
    """读取并解密记录文件；失败时备份原文件，避免直接覆盖"""
    if not os.path.exists(RECORDS_FILE):
        return []
    try:
        with open(RECORDS_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if not token:
            return []
        data = json.loads(decrypt_text(token))
        return data if isinstance(data, list) else []
    except Exception as e:
        backup_path = f"{RECORDS_FILE}.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            shutil.copy2(RECORDS_FILE, backup_path)
            print(f"警告：读取记录文件失败（{e}），原文件已备份到：{backup_path}")
        except Exception:
            print(f"警告：读取记录文件失败（{e}），且备份失败，将从头开始记录。")
        return []


def save_records(records):
    """加密并保存记录文件（写完再隐藏+只读）"""
    try:
        unhide_for_write(RECORDS_FILE)
        token = encrypt_text(json.dumps(records, ensure_ascii=False))
        with open(RECORDS_FILE, "w", encoding="utf-8") as f:
            f.write(token)
        hide_and_readonly(RECORDS_FILE)
    except OSError as e:
        print(f"警告：保存记录失败（{e}）。")


def append_record(records, record):
    records.append(record)
    save_records(records)


# ==================== 导出功能 ====================
def export_to_csv(records, filepath):
    """导出记录到 CSV（带 UTF-8 BOM，WPS 打开不乱码）"""
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "序号", "时间", "难度", "结果", "正确答案",
            "使用次数", "最大次数", "使用次数百分比", "猜测序列"
        ])
        for i, rec in enumerate(records, 1):
            status = "猜对" if rec["win"] else "失败"
            percent = ""
            if rec["win"] and rec["max_attempts"] > 0:
                percent = f"{rec['attempts'] / rec['max_attempts'] * 100:.2f}%"
            guesses = " → ".join(map(str, rec["guesses"])) if rec["guesses"] else ""
            writer.writerow([
                i, rec["time"], rec["level_name"], status, rec["answer"],
                rec["attempts"], rec["max_attempts"], percent, guesses
            ])


def export_to_html(records, filepath):
    """导出记录到 HTML 表格，WPS 文字 / 浏览器均可打开"""
    html_lines = [
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
        f"<p>导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　"
        f"共 {len(records)} 条记录</p>",
        "<table>",
        "<tr>"
        "<th>序号</th><th>时间</th><th>难度</th><th>结果</th><th>正确答案</th>"
        "<th>使用次数</th><th>最大次数</th><th>使用次数百分比</th><th>猜测序列</th>"
        "</tr>"
    ]
    for i, rec in enumerate(records, 1):
        status_class = "win" if rec["win"] else "lose"
        status_text = "猜对" if rec["win"] else "失败"
        percent = ""
        if rec["win"] and rec["max_attempts"] > 0:
            percent = f"{rec['attempts'] / rec['max_attempts'] * 100:.2f}%"
        guesses = " → ".join(map(str, rec["guesses"])) if rec["guesses"] else ""
        html_lines.append(
            "<tr>"
            f"<td>{i}</td><td>{rec['time']}</td><td>{rec['level_name']}</td>"
            f"<td class='{status_class}'>{status_text}</td>"
            f"<td>{rec['answer']}</td><td>{rec['attempts']}</td>"
            f"<td>{rec['max_attempts']}</td><td>{percent}</td>"
            f"<td>{guesses}</td>"
            "</tr>"
        )
    html_lines.extend(["</table>", "</body>", "</html>"])
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(html_lines))


def export_records(records):
    """导出记录菜单"""
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
    print("2 - 仅最近 10 条")
    print("3 - 仅猜对的记录")
    print("4 - 仅失败的记录")
    scope = input("请输入：").strip()

    if scope == "1":
        data, scope_name = records, "全部"
    elif scope == "2":
        data, scope_name = records[-10:], "最近10条"
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
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    exported_files = []

    try:
        if choice in ("1", "3"):
            csv_path = os.path.join(EXPORT_DIR, f"guess_records_{scope_name}_{timestamp}.csv")
            export_to_csv(data, csv_path)
            exported_files.append(csv_path)
        if choice in ("2", "3"):
            html_path = os.path.join(EXPORT_DIR, f"guess_records_{scope_name}_{timestamp}.html")
            export_to_html(data, html_path)
            exported_files.append(html_path)
    except OSError as e:
        print(f"导出失败：{e}")
        input("按回车返回...")
        return

    print("\n" + "=" * 60)
    print("✅ 导出成功！")
    print("=" * 60)
    for p in exported_files:
        print(f"  {p}")
    print()
    print("提示：")
    print("- CSV 文件用 WPS 表格打开即可，中文已处理不会乱码。")
    print("- HTML 文件可直接用浏览器打开，也可用 WPS 文字打开。")
    print(f"- 导出文件位置：{EXPORT_DIR}")
    input("\n按回车返回主菜单...")


# ==================== 游戏主体 ====================
def get_custom_level():
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
            max_attempts = int(input("请输入最大猜测次数（至少为 1，例如 15）：").strip())
            if max_attempts < 1:
                print("猜测次数必须至少为 1！请重新输入。")
                continue
            break
        except ValueError:
            print("只能输入正整数！请重新输入。")

    # 对于 1~max_num，二分法理论最少次数是 ceil(log2(max_num + 1))
    min_needed = math.ceil(math.log2(max_num + 1))
    print(f"\n提示：对于 1~{max_num} 的范围，理论上最少 {min_needed} 次即可猜中。")
    if max_attempts < min_needed:
        print(f"注意：你设置的次数（{max_attempts}）低于理论最少次数（{min_needed}），可能会很难获胜！")

    input("按回车确认...")
    return {
        "name": f"自定义(1~{max_num}, {max_attempts}次)",
        "max_num": max_num,
        "max_attempts": max_attempts
    }


def play_one_round():
    config = None
    while config is None:
        clear_screen()
        print("请选择难度等级：")
        print("1 - 简单 (1~1000，最多猜 20 次)")
        print("2 - 中等 (1~10000，最多猜 30 次)")
        print("3 - 困难 (1~100000，最多猜 40 次)")
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
        hist_str = ", ".join(map(str, history)) if history else "无"
        print("-" * 40)
        print(f"当前范围：{min_range} ~ {max_range}  |  剩余次数：{remain}")
        print(f"历史猜测：{hist_str}")

        try:
            guess_str = input(f"请输入你猜测的数字 ({min_range}-{max_range})：").strip()
            if guess_str == "":
                print("不能为空，请重新输入！")
                continue
            guess = int(guess_str)
        except ValueError:
            print("只能输入正整数！")
            continue

        if guess < 1 or guess > max_num or guess < min_range or guess > max_range:
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
            print(f"🎉 恭喜你，猜对了！正确答案就是 {answer}，你一共猜了 {attempts} 次。")
            print(f"本局猜对使用次数百分比：{attempts}/{max_attempts} = {used_percent:.2f}%")
            return {
                "win": True,
                "level_name": config["name"],
                "answer": answer,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "guesses": history.copy(),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

    print("=" * 40)
    print(f"😢 很遗憾，次数用完了。正确答案是：{answer}")
    return {
        "win": False,
        "level_name": config["name"],
        "answer": answer,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "guesses": history.copy(),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


def view_history(records):
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
            status = "✅ 猜对" if rec["win"] else "❌ 失败"
            print(f"{i + 1}. [{rec['level_name']}] {status}")
            print(f"   答案：{rec['answer']}    使用次数：{rec['attempts']}/{rec['max_attempts']}")
            if rec["win"]:
                percent = rec["attempts"] / rec["max_attempts"] * 100
                print(f"   使用次数百分比：{percent:.2f}%")
            print(f"   猜测序列：{', '.join(map(str, rec['guesses'])) if rec['guesses'] else '无'}")
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
                target = int(input(f"请输入页码 (1-{total_pages})：").strip())
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


def show_stats(records):
    clear_screen()
    print("=" * 60)
    print("游戏统计")
    print("=" * 60)

    total_rounds = len(records)
    win_records = [r for r in records if r["win"]]
    win_rounds = len(win_records)

    print(f"总局数：{total_rounds}")
    print(f"猜对局数：{win_rounds}")

    if total_rounds > 0:
        print(f"猜对率：{win_rounds / total_rounds * 100:.2f}%")

    if win_rounds > 0:
        total_win_attempts = sum(r["attempts"] for r in win_records)
        total_win_max_attempts = sum(r["max_attempts"] for r in win_records)
        percent = total_win_attempts / total_win_max_attempts * 100
        print(f"猜对时累计使用次数：{total_win_attempts}")
        print(f"猜对时累计可用次数：{total_win_max_attempts}")
        print(f"猜对使用次数百分比：{percent:.2f}%")
        print(f"平均每局猜对使用次数：{total_win_attempts / win_rounds:.2f}")
    else:
        print("没有猜对过，无法计算猜对使用次数百分比。")

    input("\n按回车返回主菜单...")


def main():
    # ===== 位置检查 =====
    ok, saved_path, status = check_location()

    if not ok:
        clear_screen()
        print("=" * 60)
        if status == "moved":
            print("⛔ 警告：本文件夹不可强行移动！")
            print("=" * 60)
            print(f"软件原始位置：{saved_path}")
            print(f"当前所在位置：{os.path.normpath(os.path.abspath(BASE_DIR))}")
            print()
            print("移动后游戏将崩溃！")
            print("请将整个文件夹放回原始位置后再运行。")
            print()
            print("如确实需要在当前位置运行，请删除以下文件：")
            for lf in LOCK_FILES:
                print(f"  - {os.path.relpath(lf, BASE_DIR)}")
            print(f"  - {os.path.relpath(RECORDS_FILE, BASE_DIR)}")
            print("然后重新启动游戏。")
        elif status == "deleted":
            print("⛔ 警告：检测到保护文件被删除！")
            print("=" * 60)
            print("本文件夹的防移动保护文件已被删除，")
            print("游戏无法确认运行位置，已停止启动。")
            print()
            print("如果你确实要在此位置重新开始，")
            print(f"请同时删除 {os.path.relpath(RECORDS_FILE, BASE_DIR)} 后再启动。")
        print()
        input("按回车退出...")
        sys.exit(1)

    # ===== 加载加密记录 =====
    records = load_records()

    clear_screen()
    print("=" * 60)
    print("欢迎来到猜数字游戏！")
    print("=" * 60)
    print(f"软件目录：{BASE_DIR}")
    print(f"数据文件夹：{DATA_DIR}")
    if status == "new":
        print("（首次运行，已自动创建数据文件夹）")
    print(f"记录文件：{os.path.basename(RECORDS_FILE)}（加密、隐藏、只读）")
    print(f"导出目录：{os.path.basename(EXPORT_DIR)}")
    print(f"位置锁：{len(LOCK_FILES)} 个（请勿删除）")
    print(f"已加载历史记录 {len(records)} 条。")
    input("按回车开始游戏...")

    while True:
        record = play_one_round()
        append_record(records, record)

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
                show_stats(records)
                print("游戏已退出，感谢游玩！")
                return
            else:
                print("输入无效，请重新选择。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n游戏被中断，已退出。")
        sys.exit(0)