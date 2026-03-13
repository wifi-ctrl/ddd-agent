"""
Agent 核心 — 工具循环 + 阶段编排

这是整个系统的心脏：
1. agent_loop: Claude ↔ 工具 的循环（单阶段内）
2. run_pipeline: 多阶段顺序编排（阶段间状态传递）
"""

import anthropic
import json
import os
import re
import sys
import time
from typing import Optional

from tools import TOOL_SCHEMAS, ToolExecutor
from phases import PhaseConfig, PhaseResult, ALL_PHASES, REVIEW_PHASES
from project_profile import ProjectProfile, profile_to_system_prompt, scan_existing_contexts
from checkpoint import Checkpoint, get_session_dir, find_latest_session


class TokenTracker:
    """统计全流水线 token 用量"""

    def __init__(self):
        self.phases: dict[str, dict] = {}
        self.total_input = 0
        self.total_output = 0

    def record(self, phase_name: str, usage):
        """记录一次 API 调用的 token 用量"""
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        self.total_input += input_tokens
        self.total_output += output_tokens

        if phase_name not in self.phases:
            self.phases[phase_name] = {"input": 0, "output": 0, "calls": 0}
        self.phases[phase_name]["input"] += input_tokens
        self.phases[phase_name]["output"] += output_tokens
        self.phases[phase_name]["calls"] += 1

    def print_summary(self):
        """打印 token 用量报告"""
        print("\n┌─────────────────────────────────────────┐")
        print("│           Token 用量统计                │")
        print("├──────────┬──────────┬──────────┬────────┤")
        print("│ 阶段     │   输入   │   输出   │ 调用次 │")
        print("├──────────┼──────────┼──────────┼────────┤")
        for name, stats in self.phases.items():
            print(f"│ {name:<8} │ {stats['input']:>8,} │ {stats['output']:>8,} │ {stats['calls']:>6} │")
        print("├──────────┼──────────┼──────────┼────────┤")
        print(f"│ {'合计':<7} │ {self.total_input:>8,} │ {self.total_output:>8,} │        │")
        print("└──────────┴──────────┴──────────┴────────┘")


class DDDAgent:
    # 命令白名单前缀 — 只允许这些命令执行
    ALLOWED_COMMANDS = [
        "go build", "go test", "go vet", "go fmt", "go mod",
        "golangci-lint",
        "ls", "cat", "head", "tail", "wc",
        "find", "grep",
    ]

    def __init__(self, profile: ProjectProfile, model: str = "claude-sonnet-4-6",
                 session_id: Optional[str] = None):
        self.profile = profile
        self.model = model
        self.client = anthropic.Anthropic()
        self.executor = ToolExecutor(profile.project_root)
        self.tokens = TokenTracker()

        # 扫描已有上下文
        self.profile.existing_contexts = scan_existing_contexts(profile)

        # 断点恢复
        session_dir = get_session_dir(profile.project_root, session_id)
        self.checkpoint = Checkpoint(session_dir)

    def _check_command_allowed(self, command: str) -> bool:
        """检查命令是否在白名单中"""
        cmd = command.strip()
        return any(cmd.startswith(prefix) for prefix in self.ALLOWED_COMMANDS)

    def run_pipeline(self, requirement: str, resume: bool = False):
        """执行完整 DDD 流水线，支持断点恢复"""
        results: dict[str, PhaseResult] = {}
        completed_phases: set[str] = set()

        # 尝试恢复上次进度
        if resume:
            loaded = self.checkpoint.load()
            if loaded:
                saved_req, saved_results = loaded
                results = saved_results
                completed_phases = set(results.keys())
                print(f"  [恢复] 已完成阶段: {', '.join(completed_phases)}")
                print(f"  [恢复] 需求: {saved_req[:80]}")
                requirement = saved_req
            else:
                print("  [恢复] 未找到进度，从头开始")

        for phase in ALL_PHASES:
            # 跳过已完成的阶段
            if phase.name in completed_phases:
                print(f"\n  [跳过] 阶段 {phase.name}（已完成）")
                continue

            print(f"\n{'='*60}")
            print(f"  阶段: {phase.name}")
            print(f"{'='*60}\n")

            # 构建阶段输入
            phase_input = self._build_phase_input(requirement, phase, results)

            # 执行阶段
            result = self._run_phase(phase, phase_input)
            results[phase.name] = result

            # 每个阶段完成后存 checkpoint
            self.checkpoint.save(requirement, results)

            if not result.success:
                print(f"\n[ERROR] 阶段 {phase.name} 失败，进度已保存")
                print(f"  重新运行加 --resume 从此阶段继续")
                break

            # 需要人工确认的阶段
            if phase.needs_confirmation:
                # 保存技术文档供人工审阅
                doc_path = self._save_design_doc(requirement, results)
                print("\n" + "─" * 40)
                print(f"  📄 技术文档已保存: {doc_path}")
                print(f"  请查阅文档后确认")
                print("─" * 40)
                while True:
                    confirm = input("确认？(y=继续 / n=终止 / r=重做 / m=提修改意见): ").strip().lower()
                    if confirm == "y":
                        break
                    elif confirm == "n":
                        print("用户终止，进度已保存，可用 --resume 恢复")
                        self.tokens.print_summary()
                        return
                    elif confirm == "r":
                        result = self._run_phase(phase, phase_input)
                        results[phase.name] = result
                        self.checkpoint.save(requirement, results)
                        if not result.success:
                            break
                        doc_path = self._save_design_doc(requirement, results)
                        print(f"\n  📄 文档已更新: {doc_path}")
                        break
                    elif confirm == "m":
                        feedback = input("请输入修改意见: ").strip()
                        if not feedback:
                            print("未输入意见，请重新选择")
                            continue
                        modified_input = phase_input + f"\n\n## 用户修改意见\n{feedback}\n\n请根据以上意见，在之前输出的基础上进行修改。"
                        result = self._run_phase(phase, modified_input)
                        results[phase.name] = result
                        self.checkpoint.save(requirement, results)
                        if not result.success:
                            break
                        # 更新技术文档
                        doc_path = self._save_design_doc(requirement, results)
                        print(f"\n  📄 文档已更新: {doc_path}")
                    else:
                        print("请输入 y、n、r 或 m")

        # 全部完成，清除 checkpoint
        if all(phase.name in results for phase in ALL_PHASES):
            self.checkpoint.clear()
            print("\n" + "=" * 60)
            print("  流水线完成")
            print("=" * 60)

        # 打印 token 用量
        self.tokens.print_summary()

    def run_review(self, context_path: str, changed_files: list[str] = None):
        """Review 模式：审查并修复已有上下文代码"""
        results: dict[str, PhaseResult] = {}

        # 构建审查输入
        review_target = f"## 审查目标\n上下文路径: {context_path}\n项目根目录: {self.profile.project_root}"
        if changed_files:
            review_target += f"\n\n## 审查范围（增量模式）\n只需审查以下变更文件，其他文件仅作为上下文参考：\n"
            review_target += "\n".join(f"- {f}" for f in changed_files)
            review_target += "\n\n注意：问题清单只针对上述文件，不要报告未变更文件中的问题。"

        for phase in REVIEW_PHASES:
            print(f"\n{'='*60}")
            print(f"  阶段: {phase.name}")
            print(f"{'='*60}\n")

            # 构建阶段输入
            phase_input = self._build_review_input(review_target, phase, results)

            # 执行阶段
            result = self._run_phase(phase, phase_input)
            results[phase.name] = result

            if not result.success:
                print(f"\n[ERROR] 阶段 {phase.name} 失败")
                break

            # 审查扫描阶段需要人工确认
            if phase.needs_confirmation:
                print("\n" + "─" * 40)
                print("  请审阅以上审查报告")
                print("─" * 40)
                while True:
                    confirm = input("确认修复？(y=开始修复 / n=终止 / m=补充意见): ").strip().lower()
                    if confirm == "y":
                        break
                    elif confirm == "n":
                        print("用户终止")
                        self.tokens.print_summary()
                        return
                    elif confirm == "m":
                        feedback = input("请输入补充意见: ").strip()
                        if not feedback:
                            print("未输入意见，请重新选择")
                            continue
                        modified_input = phase_input + f"\n\n## 用户补充意见\n{feedback}\n\n请根据以上意见更新审查报告。"
                        result = self._run_phase(phase, modified_input)
                        results[phase.name] = result
                        if not result.success:
                            break
                    else:
                        print("请输入 y、n 或 m")

        # 完成
        if all(phase.name in results and results[phase.name].success for phase in REVIEW_PHASES):
            print("\n" + "=" * 60)
            print("  Review 完成")
            print("=" * 60)

        self.tokens.print_summary()

    def _build_review_input(self, review_target: str, phase: PhaseConfig, results: dict) -> str:
        """构建 Review 模式的阶段输入"""
        parts = [review_target]

        # 审查报告传给修复和验证阶段
        if phase.name in ("review_fix", "review_verify") and "review_scan" in results:
            parts.append(f"## 审查报告\n{results['review_scan'].output}")

        # 修复结果传给验证阶段
        if phase.name == "review_verify" and "review_fix" in results:
            files = results["review_fix"].files_modified or []
            files += results["review_fix"].files_created or []
            if files:
                parts.append(f"## 修复涉及的文件\n" + "\n".join(f"- {f}" for f in files))

        return "\n\n".join(parts)

    def _build_phase_input(self, requirement: str, phase: PhaseConfig, results: dict) -> str:
        """构建阶段输入：需求 + 前序阶段输出"""
        parts = [f"## 需求\n{requirement}"]

        # 设计阶段的输出传给后续所有阶段
        if phase.name != "design" and "design" in results:
            parts.append(f"## 领域设计（已确认）\n{results['design'].output}")

        # 验收标准传给 test 和 verify（关键：这是质量保证的锚点）
        if phase.name in ("test", "verify") and "acceptance" in results:
            parts.append(f"## 验收标准（已确认）\n{results['acceptance'].output}")

        # codegen 的文件列表传给 test 和 verify
        if phase.name in ("test", "verify", "main_update") and "codegen" in results:
            files = results["codegen"].files_created
            if files:
                parts.append(f"## 已生成的文件\n" + "\n".join(f"- {f}" for f in files))

        # test 的文件列表传给 verify
        if phase.name == "verify" and "test" in results:
            files = results["test"].files_created
            if files:
                parts.append(f"## 已生成的测试文件\n" + "\n".join(f"- {f}" for f in files))

        return "\n\n".join(parts)

    @staticmethod
    def _print_summary(text: str):
        """只打印非代码块的文本，过滤掉大段代码"""
        # 去掉 ```...``` 代码块
        stripped = re.sub(r'```[\s\S]*?```', '\n  [代码块已省略]\n', text)
        # 只打印非空行
        for line in stripped.split("\n"):
            line = line.rstrip()
            if line:
                print(f"  {line}", flush=True)

    def _save_design_doc(self, requirement: str, results: dict[str, PhaseResult]) -> str:
        """将设计阶段和验收标准合并输出为技术文档"""
        doc_dir = os.path.join(self.profile.project_root, "docs", "plans")
        os.makedirs(doc_dir, exist_ok=True)

        date_str = time.strftime("%Y-%m-%d")
        # 从需求中提取简短名称
        short_name = requirement[:20].replace(" ", "-").replace("/", "-")
        doc_path = os.path.join(doc_dir, f"{date_str}-{short_name}-design.md")

        sections = []
        sections.append(f"# 技术设计文档\n")
        sections.append(f"- **需求**: {requirement}")
        sections.append(f"- **项目**: {self.profile.name}")
        sections.append(f"- **日期**: {date_str}")
        sections.append(f"- **状态**: 待确认\n")

        if "design" in results:
            sections.append("---\n")
            sections.append("## 一、领域设计\n")
            sections.append(results["design"].output)

        if "acceptance" in results:
            sections.append("\n---\n")
            sections.append("## 二、验收标准\n")
            sections.append(results["acceptance"].output)

        with open(doc_path, "w", encoding="utf-8") as f:
            f.write("\n".join(sections))

        return doc_path

    def _run_phase(self, phase: PhaseConfig, user_input: str) -> PhaseResult:
        """运行单个阶段（含重试）"""
        max_attempts = 1 + phase.max_retries
        last_error = ""

        for attempt in range(max_attempts):
            if attempt > 0:
                print(f"\n  [重试 {attempt}/{phase.max_retries}]")
                user_input += f"\n\n## 上次失败原因\n{last_error}\n请修复后重试。"

            result = self._agent_loop(phase, user_input)

            if result.success:
                return result

            last_error = result.output
            print(f"  [失败] {last_error[:200]}")

        return PhaseResult(
            phase_name=phase.name,
            output=f"经过 {max_attempts} 次尝试仍失败: {last_error}",
            success=False
        )

    def _call_api(self, phase: PhaseConfig, system: str, phase_tools: list, messages: list):
        """调用 Claude API，带指数退避重试"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=phase.max_tokens,
                    system=system,
                    tools=phase_tools,
                    messages=messages
                )
                # 记录 token 用量
                self.tokens.record(phase.name, response.usage)
                return response
            except anthropic.RateLimitError:
                wait = 2 ** attempt * 5  # 5s, 10s, 20s
                print(f"  [限流] 等待 {wait}s 后重试...", flush=True)
                time.sleep(wait)
            except anthropic.APIStatusError as e:
                if e.status_code >= 500 and attempt < max_retries - 1:
                    wait = 2 ** attempt * 3
                    print(f"  [服务端错误 {e.status_code}] 等待 {wait}s 后重试...", flush=True)
                    time.sleep(wait)
                else:
                    raise
        raise Exception("API 调用重试耗尽")

    @staticmethod
    def _serialize_content(content) -> list:
        """将 SDK ContentBlock 对象序列化为标准 dict"""
        serialized = []
        for block in content:
            if hasattr(block, "text"):
                serialized.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                serialized.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input
                })
        return serialized

    def _agent_loop(self, phase: PhaseConfig, user_input: str) -> PhaseResult:
        """
        核心 Agent 循环（单阶段内）
        Claude 思考 → 调工具 → 拿结果 → 继续思考 → ... → 完成
        """
        # 构建 system prompt
        project_context = profile_to_system_prompt(self.profile)
        system = f"{project_context}\n\n---\n\n{phase.system_prompt}"

        # 过滤本阶段可用的工具
        phase_tools = [t for t in TOOL_SCHEMAS if t["name"] in phase.tools]

        # 对话历史
        messages = [{"role": "user", "content": user_input}]

        # 收集本阶段创建/修改的文件
        files_created = []
        files_modified = []
        final_text = ""
        step_count = 0

        while True:
            step_count += 1
            if step_count > 50:  # 防止无限循环
                return PhaseResult(phase.name, "超过 50 步，强制停止", success=False)

            # 调用 Claude（带重试）
            try:
                response = self._call_api(phase, system, phase_tools, messages)
            except Exception as e:
                return PhaseResult(phase.name, f"API 错误: {e}", success=False)

            # 处理响应
            assistant_content = response.content
            tool_results = []

            for block in assistant_content:
                if hasattr(block, "text"):
                    final_text += block.text
                    # 只打印非代码块的摘要文本
                    self._print_summary(block.text)

                elif block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input

                    # 命令白名单检查
                    if tool_name == "run_command":
                        cmd = tool_input.get("command", "")
                        if not self._check_command_allowed(cmd):
                            result = f"Error: 命令不在白名单中: {cmd}\n允许的命令前缀: {', '.join(self.ALLOWED_COMMANDS)}"
                            print(f"  [拒绝] {cmd}", flush=True)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result
                            })
                            continue

                    # 精简工具调用输出
                    if tool_name == "write_file":
                        print(f"  [写入] {tool_input.get('path', '')}", flush=True)
                    elif tool_name == "read_file":
                        print(f"  [读取] {tool_input.get('path', '')}", flush=True)
                    elif tool_name == "list_dir":
                        print(f"  [目录] {tool_input.get('path', '')}", flush=True)
                    elif tool_name == "run_command":
                        print(f"  [执行] {tool_input.get('command', '')}", flush=True)
                    elif tool_name == "check_architecture":
                        print(f"  [架构检查] {tool_input.get('context_path', '')}", flush=True)
                    else:
                        print(f"  [{tool_name}]", flush=True)

                    # 写文件前检测是否已存在（用于区分创建/修改）
                    file_existed = False
                    if tool_name == "write_file":
                        path = tool_input.get("path", "")
                        resolved = self.executor._resolve_path(path)
                        file_existed = os.path.exists(resolved)

                    # 执行工具
                    result = self.executor.execute(tool_name, tool_input)

                    # 记录文件操作
                    if tool_name == "write_file":
                        path = tool_input.get("path", "")
                        if "OK:" in result:
                            if file_existed and path not in files_created:
                                if path not in files_modified:
                                    files_modified.append(path)
                                print(f"    ✓ 已修改", flush=True)
                            else:
                                if path not in files_created:
                                    files_created.append(path)
                                print(f"    ✓ 已创建", flush=True)
                        else:
                            print(f"    ✗ {result[:100]}", flush=True)
                    elif tool_name == "run_command":
                        # 命令结果只显示最后几行
                        lines = result.strip().split("\n")
                        if len(lines) > 3:
                            print(f"    ...({len(lines)} 行输出)", flush=True)
                            for line in lines[-3:]:
                                print(f"    {line}", flush=True)
                        else:
                            for line in lines:
                                print(f"    {line}", flush=True)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

            # 如果没有工具调用，Agent 完成了本阶段
            if response.stop_reason == "end_turn":
                print()  # 换行
                return PhaseResult(
                    phase_name=phase.name,
                    output=final_text,
                    success=True,
                    files_created=files_created,
                    files_modified=files_modified
                )

            # 有工具调用，继续循环 — 序列化为标准 dict
            messages.append({"role": "assistant", "content": self._serialize_content(assistant_content)})
            messages.append({"role": "user", "content": tool_results})
