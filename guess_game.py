import random
import sys
import os

LEVELS = {
    "1": {"name": "简单", "max_num": 1000, "max_attempts": 20},
    "2": {"name": "中等", "max_num": 10000, "max_attempts": 30},
    "3": {"name": "困难", "max_num": 100000, "max_attempts": 40}
}

def play_one_round():
    # 难度选择
    level = None
    while level is None:
        os.system('cls') # 清屏
        print("请选择难度等级：")
        print("1 - 简单 (1~1000，最多猜 20 次)")
        print("2 - 中等 (1~10000，最多猜 30 次)")
        print("3 - 困难 (1~100000，最多猜 40次)")
        diff = input("\n请输入数字 1、2 或 3：").strip()
        
        if diff in LEVELS:
            level = diff
        else:
            print("输入无效，按回车重新选择...")
            input()
    
    config = LEVELS[level]
    max_num = config["max_num"]
    max_attempts = config["max_attempts"]
    
    answer = random.randint(1, max_num)
    attempts = 0
    history = []
    min_range, max_range = 1, max_num
    
    os.system('cls')
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
            print("=" * 40)
            print(f"🎉 恭喜你，猜对了！正确答案就是 {answer}，你一共猜了 {attempts} 次。")
            return True

    print("=" * 40)
    print(f"😢 很遗憾，次数用完了。正确答案是：{answer}")
    return True

def main():
    while True:
        play_one_round()
        again = input("\n是否再来一局？(输入 1 继续，其他退出)：").strip()
        if again != "1":
            print("游戏已退出，感谢游玩！")
            break

if __name__ == "__main__":
    main()