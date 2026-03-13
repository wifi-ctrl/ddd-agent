"""
断点恢复 — 每个阶段完成后存一次，崩了从上次完成的阶段继续

原理很简单：
1. 每个阶段完成 → 把 PhaseResult 序列化到 JSON 文件
2. 启动时 → 检查有没有 checkpoint 文件
3. 有 → 加载已完成的阶段，跳过，从下一个阶段继续
4. 没有 → 从头开始

存储位置: <project_root>/.ddd-agent/<session_id>/checkpoint.json
"""

import json
import os
import time
from dataclasses import asdict
from typing import Optional

from phases import PhaseResult


class Checkpoint:
    def __init__(self, session_dir: str):
        self.session_dir = session_dir
        self.checkpoint_file = os.path.join(session_dir, "checkpoint.json")
        os.makedirs(session_dir, exist_ok=True)

    def save(self, requirement: str, results: dict[str, PhaseResult]):
        """每个阶段完成后调用，保存当前进度"""
        data = {
            "requirement": requirement,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "completed_phases": list(results.keys()),
            "results": {}
        }
        for name, result in results.items():
            data["results"][name] = {
                "phase_name": result.phase_name,
                "output": result.output,
                "success": result.success,
                "files_created": result.files_created,
                "files_modified": result.files_modified,
            }

        with open(self.checkpoint_file, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"  [checkpoint] 已保存进度: {', '.join(results.keys())}")

    def load(self) -> Optional[tuple[str, dict[str, PhaseResult]]]:
        """启动时调用，尝试加载上次进度"""
        if not os.path.exists(self.checkpoint_file):
            return None

        with open(self.checkpoint_file) as f:
            data = json.load(f)

        requirement = data["requirement"]
        results = {}
        for name, r in data["results"].items():
            results[name] = PhaseResult(
                phase_name=r["phase_name"],
                output=r["output"],
                success=r["success"],
                files_created=r.get("files_created", []),
                files_modified=r.get("files_modified", []),
            )

        return requirement, results

    def clear(self):
        """流水线完成后清除 checkpoint"""
        if os.path.exists(self.checkpoint_file):
            os.remove(self.checkpoint_file)
            print("  [checkpoint] 已清除")


def get_session_dir(project_root: str, session_id: Optional[str] = None) -> str:
    """获取会话目录。session_id 为空时自动生成"""
    if not session_id:
        session_id = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(project_root, ".ddd-agent", session_id)


def find_latest_session(project_root: str) -> Optional[str]:
    """查找最近一次未完成的会话"""
    agent_dir = os.path.join(project_root, ".ddd-agent")
    if not os.path.isdir(agent_dir):
        return None

    sessions = []
    for entry in os.listdir(agent_dir):
        checkpoint = os.path.join(agent_dir, entry, "checkpoint.json")
        if os.path.exists(checkpoint):
            sessions.append(entry)

    if not sessions:
        return None

    # 返回最近的
    sessions.sort(reverse=True)
    return sessions[0]
