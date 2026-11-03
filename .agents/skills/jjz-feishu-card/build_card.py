#!/usr/bin/env python3
"""把 jjz 命令输出的 JSON 转成进京证办理状态的飞书 Card 2.0 卡片，规范见同目录 SKILL.md。

只读本地 JSON 文件，不联网、不发送消息。卡片规则在 jjz/feishu.py，系统定时任务的
run --notify 也用它，两边的卡片因此完全一致。
"""
from pathlib import Path
import sys

# .agents/skills/jjz-feishu-card/build_card.py -> project root, also via the .claude/skills link.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from jjz.feishu import main  # noqa: E402

if __name__ == "__main__":
    main()
