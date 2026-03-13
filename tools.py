"""
Agent 工具定义 — Agent 的「手」
每个工具 = 一个 Claude 可以调用的能力
"""

import os
import subprocess
import re
import json
from pathlib import Path


# ══════════════════════════════════════════════
#  工具 Schema（告诉 Claude 有哪些工具可用）
# ══════════════════════════════════════════════

TOOL_SCHEMAS = [
    {
        "name": "read_file",
        "description": "读取文件内容。用于读参考代码、检查已有文件。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径或相对于项目根的路径"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "创建或覆盖文件。自动创建不存在的目录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "文件内容"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "list_dir",
        "description": "列出目录结构，了解项目现有上下文和文件布局。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径"},
                "max_depth": {"type": "integer", "description": "最大深度，默认3", "default": 3}
            },
            "required": ["path"]
        }
    },
    {
        "name": "run_command",
        "description": "执行 shell 命令。用于 go build、go test 等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "workdir": {"type": "string", "description": "工作目录，默认项目根目录"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "check_architecture",
        "description": "检查指定上下文的依赖方向是否合规。扫描 domain/ 的 import，确保不依赖外层。",
        "input_schema": {
            "type": "object",
            "properties": {
                "context_path": {"type": "string", "description": "上下文路径，如 internal/payment"}
            },
            "required": ["context_path"]
        }
    },
    {
        "name": "search_code",
        "description": "在项目中搜索代码模式。用于查找已有实现作为参考。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "搜索的正则表达式"},
                "file_glob": {"type": "string", "description": "文件过滤，如 *.go", "default": "*.go"},
                "path": {"type": "string", "description": "搜索目录"}
            },
            "required": ["pattern"]
        }
    }
]


# ══════════════════════════════════════════════
#  工具执行器
# ══════════════════════════════════════════════

class ToolExecutor:
    def __init__(self, project_root: str):
        self.project_root = project_root

    def execute(self, tool_name: str, tool_input: dict) -> str:
        method = getattr(self, f"_tool_{tool_name}", None)
        if not method:
            return f"Error: unknown tool '{tool_name}'"
        try:
            return method(tool_input)
        except Exception as e:
            return f"Error: {e}"

    def _resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.join(self.project_root, path)

    def _tool_read_file(self, input: dict) -> str:
        path = self._resolve_path(input["path"])
        if not os.path.exists(path):
            return f"Error: file not found: {path}"
        with open(path, "r") as f:
            return f.read()

    def _tool_write_file(self, input: dict) -> str:
        path = self._resolve_path(input["path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(input["content"])
        return f"OK: wrote {path}"

    def _tool_list_dir(self, input: dict) -> str:
        path = self._resolve_path(input["path"])
        max_depth = input.get("max_depth", 3)
        if not os.path.isdir(path):
            return f"Error: not a directory: {path}"
        lines = []
        base_depth = path.rstrip("/").count("/")
        for root, dirs, files in os.walk(path):
            depth = root.rstrip("/").count("/") - base_depth
            if depth >= max_depth:
                dirs.clear()
                continue
            indent = "  " * depth
            lines.append(f"{indent}{os.path.basename(root)}/")
            for f in sorted(files):
                lines.append(f"{indent}  {f}")
        return "\n".join(lines)

    def _tool_run_command(self, input: dict) -> str:
        workdir = self._resolve_path(input.get("workdir", "."))
        result = subprocess.run(
            input["command"],
            shell=True,
            capture_output=True,
            text=True,
            cwd=workdir,
            timeout=60
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output or "(no output)"

    def _tool_check_architecture(self, input: dict) -> str:
        """检查 DDD 依赖方向：domain 不能 import adapter/handler/application"""
        context_path = self._resolve_path(input["context_path"])
        violations = []

        # 规则定义
        rules = {
            "domain": ["adapter/", "handler/", "application/", "infra/"],
            "application": ["adapter/", "handler/", "infra/"],
        }

        for layer, forbidden in rules.items():
            layer_path = os.path.join(context_path, layer)
            if not os.path.isdir(layer_path):
                continue
            for root, _, files in os.walk(layer_path):
                for f in files:
                    if not f.endswith(".go") or f.endswith("_test.go"):
                        continue
                    filepath = os.path.join(root, f)
                    with open(filepath) as fh:
                        content = fh.read()
                    # 提取 import 块
                    import_match = re.search(r'import\s*\((.*?)\)', content, re.DOTALL)
                    if not import_match:
                        continue
                    imports = import_match.group(1)
                    for fb in forbidden:
                        # 检查是否 import 了自己上下文内的 forbidden 层
                        if fb in imports:
                            rel = os.path.relpath(filepath, context_path)
                            violations.append(f"  {layer}/{rel}: imports '{fb}'")

        if not violations:
            return "PASS: 依赖方向合规"
        return "VIOLATIONS:\n" + "\n".join(violations)

    def _tool_search_code(self, input: dict) -> str:
        path = self._resolve_path(input.get("path", "."))
        pattern = input["pattern"]
        file_glob = input.get("file_glob", "*.go")
        results = []
        for root, _, files in os.walk(path):
            for f in files:
                if not self._match_glob(f, file_glob):
                    continue
                filepath = os.path.join(root, f)
                try:
                    with open(filepath) as fh:
                        for i, line in enumerate(fh, 1):
                            if re.search(pattern, line):
                                rel = os.path.relpath(filepath, self.project_root)
                                results.append(f"{rel}:{i}: {line.rstrip()}")
                except (UnicodeDecodeError, PermissionError):
                    continue
        if not results:
            return f"No matches for '{pattern}'"
        return "\n".join(results[:50])  # 限制输出

    @staticmethod
    def _match_glob(filename: str, glob: str) -> bool:
        if glob == "*":
            return True
        if glob.startswith("*."):
            return filename.endswith(glob[1:])
        return filename == glob
