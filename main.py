#!/usr/bin/env python3
"""
DDD Dev Agent — CLI 入口

用法:
  # 新任务（生成模式）
  python main.py -p profiles/payment-demo.yaml "给商城加优惠券功能"

  # 断点恢复（从上次中断的阶段继续）
  python main.py -p profiles/payment-demo.yaml --resume

  # 指定会话恢复
  python main.py -p profiles/payment-demo.yaml --resume --session 20260313-143022

  # Review 模式（审查并修复已有代码）
  python main.py -p profiles/payment-demo.yaml --review internal/card

  # Review 模式 — 只审查变更部分
  python main.py -p profiles/payment-demo.yaml --review internal/card --diff
  python main.py -p profiles/payment-demo.yaml --review internal/card --diff HEAD~3
"""

import argparse
import os
import subprocess
import sys

from project_profile import load_profile
from agent import DDDAgent
from checkpoint import find_latest_session


def _get_changed_files(project_root: str, context_path: str, base_ref: str) -> list[str]:
    """用 git diff 获取指定上下文内的变更 .go 文件"""
    try:
        # 已提交但未推送 + 暂存区 + 工作区的变更
        result = subprocess.run(
            ["git", "diff", "--name-only", base_ref, "--", context_path],
            capture_output=True, text=True, cwd=project_root
        )
        files = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()

        # 加上未跟踪的新文件
        result2 = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--", context_path],
            capture_output=True, text=True, cwd=project_root
        )
        if result2.stdout.strip():
            files.update(result2.stdout.strip().split("\n"))

        # 只保留 .go 文件
        return sorted(f for f in files if f.endswith(".go"))
    except Exception as e:
        print(f"Warning: git diff 失败: {e}，将回退到全量审查")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="DDD Dev Agent — 从领域设计到代码实现的全自动 Agent"
    )
    parser.add_argument(
        "requirement",
        nargs="?",  # --resume / --review 时不需要
        default="",
        help="需求描述，如「给商城加优惠券功能」"
    )
    parser.add_argument(
        "--profile", "-p",
        required=True,
        help="项目特征配置文件路径（YAML）"
    )
    parser.add_argument(
        "--model", "-m",
        default="claude-sonnet-4-6",
        help="Claude 模型 ID（默认 claude-sonnet-4-6）"
    )
    parser.add_argument(
        "--resume", "-r",
        action="store_true",
        help="从上次中断的位置恢复"
    )
    parser.add_argument(
        "--session",
        default=None,
        help="指定恢复的会话 ID（默认恢复最近一次）"
    )
    parser.add_argument(
        "--review",
        default=None,
        metavar="CONTEXT_PATH",
        help="Review 模式：审查并修复已有上下文代码（如 internal/card）"
    )
    parser.add_argument(
        "--diff",
        nargs="?",
        const="HEAD",
        default=None,
        metavar="BASE_REF",
        help="只审查相对于 BASE_REF 的变更文件（默认 HEAD，即未提交的改动）"
    )

    args = parser.parse_args()

    # 加载项目配置
    try:
        profile = load_profile(args.profile)
    except FileNotFoundError:
        print(f"Error: 配置文件不存在: {args.profile}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: 配置文件解析失败: {e}")
        sys.exit(1)

    # --diff 只能配合 --review 使用
    if args.diff is not None and not args.review:
        parser.error("--diff 必须配合 --review 使用")

    # Review 模式
    if args.review:
        # 如果指定了 --diff，用 git 获取变更文件列表
        changed_files = None
        if args.diff is not None:
            changed_files = _get_changed_files(profile.project_root, args.review, args.diff)
            if not changed_files:
                print("没有检测到变更文件，无需审查")
                return

        scope = "增量" if changed_files else "全量"
        review_label = args.review[:32]
        print(f"""
╔══════════════════════════════════════════╗
║         DDD Dev Agent — Review           ║
╠══════════════════════════════════════════╣
║  项目:  {profile.name:<32}║
║  模型:  {args.model:<32}║
║  范围:  {scope:<32}║
║  审查:  {review_label:<32}║
╚══════════════════════════════════════════╝
""")
        if changed_files:
            print(f"变更文件 ({len(changed_files)} 个):")
            for f in changed_files:
                print(f"  - {f}")
            print()

        agent = DDDAgent(profile=profile, model=args.model)
        agent.run_review(args.review, changed_files=changed_files)
        return

    # 恢复模式：查找上次会话
    session_id = args.session
    if args.resume and not session_id:
        session_id = find_latest_session(profile.project_root)
        if not session_id:
            print("Error: 未找到可恢复的会话")
            sys.exit(1)

    # 非恢复模式必须提供需求
    if not args.resume and not args.requirement:
        parser.error("请提供需求描述，或使用 --resume 恢复上次任务")

    mode = "恢复" if args.resume else "新建"
    print(f"""
╔══════════════════════════════════════════╗
║         DDD Dev Agent                    ║
╠══════════════════════════════════════════╣
║  项目:  {profile.name:<32}║
║  模型:  {args.model:<32}║
║  模式:  {mode:<32}║
║  上下文: {', '.join(profile.existing_contexts) or '(无)':<31}║
╚══════════════════════════════════════════╝
""")

    if not args.resume:
        print(f"需求: {args.requirement}\n")

    # 创建并运行 Agent
    agent = DDDAgent(profile=profile, model=args.model, session_id=session_id)
    agent.run_pipeline(args.requirement, resume=args.resume)


if __name__ == "__main__":
    main()
