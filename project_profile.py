"""
项目特征配置系统 — 让 Agent 了解「你的项目」

这是 Agent 与通用 LLM 的核心差异：
通用 LLM 每次从零理解你的项目，
Agent 通过 profile 记住项目的架构、约定、模板。

每个项目一份 profile.yaml，Agent 启动时加载。
后续可以通过学习历史代码自动更新 profile。
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LayerTemplate:
    """每一层的参考模板文件"""
    domain_event: str = ""
    domain_model: str = ""
    domain_port: str = ""
    domain_service: str = ""
    application: str = ""
    adapter_persistence: str = ""
    adapter_gateway: str = ""
    handler_http: str = ""
    composition_root: str = ""


@dataclass
class ArchitectureRules:
    """架构约束 — Agent 生成代码时必须遵守"""
    dependency_direction: str = "outside-in"  # 依赖方向
    domain_no_import: list = field(default_factory=lambda: ["adapter/", "handler/", "application/", "infra/"])
    application_no_import: list = field(default_factory=lambda: ["adapter/", "handler/", "infra/"])
    cross_context_only_in: list = field(default_factory=lambda: ["adapter/", "main.go"])
    money_type_required: bool = True  # 金额必须用值对象
    event_naming: str = "past_tense"  # 事件命名用过去式


@dataclass
class ProjectProfile:
    """项目特征配置"""
    name: str = ""
    language: str = "go"
    framework: str = ""  # go-zero / gin / 原生
    description: str = ""

    # 项目路径
    project_root: str = ""
    source_root: str = "internal"  # 源码根目录
    test_command: str = "go test ./... -v -count=1"
    build_command: str = "go build ./..."

    # 架构规则
    rules: ArchitectureRules = field(default_factory=ArchitectureRules)

    # 参考模板路径（相对于项目根目录）
    templates: LayerTemplate = field(default_factory=LayerTemplate)

    # 已有上下文列表（Agent 启动时扫描填充）
    existing_contexts: list = field(default_factory=list)

    # 项目特有约定
    conventions: list = field(default_factory=list)

    # 扩展知识（从历史代码中学到的模式）
    learned_patterns: list = field(default_factory=list)


def load_profile(profile_path: str) -> ProjectProfile:
    """从 YAML 文件加载项目配置"""
    with open(profile_path) as f:
        data = yaml.safe_load(f)

    profile = ProjectProfile()
    profile.name = data.get("name", "")
    profile.language = data.get("language", "go")
    profile.framework = data.get("framework", "")
    profile.description = data.get("description", "")
    profile.project_root = data.get("project_root", os.path.dirname(profile_path))
    profile.source_root = data.get("source_root", "internal")
    profile.test_command = data.get("test_command", "go test ./... -v -count=1")
    profile.build_command = data.get("build_command", "go build ./...")

    # 架构规则
    rules_data = data.get("rules", {})
    profile.rules = ArchitectureRules(
        domain_no_import=rules_data.get("domain_no_import", profile.rules.domain_no_import),
        application_no_import=rules_data.get("application_no_import", profile.rules.application_no_import),
        cross_context_only_in=rules_data.get("cross_context_only_in", profile.rules.cross_context_only_in),
        money_type_required=rules_data.get("money_type_required", True),
    )

    # 模板路径
    tpl_data = data.get("templates", {})
    profile.templates = LayerTemplate(
        domain_event=tpl_data.get("domain_event", ""),
        domain_model=tpl_data.get("domain_model", ""),
        domain_port=tpl_data.get("domain_port", ""),
        domain_service=tpl_data.get("domain_service", ""),
        application=tpl_data.get("application", ""),
        adapter_persistence=tpl_data.get("adapter_persistence", ""),
        adapter_gateway=tpl_data.get("adapter_gateway", ""),
        handler_http=tpl_data.get("handler_http", ""),
        composition_root=tpl_data.get("composition_root", ""),
    )

    # 约定
    profile.conventions = data.get("conventions", [])
    profile.learned_patterns = data.get("learned_patterns", [])

    return profile


def scan_existing_contexts(profile: ProjectProfile) -> list[str]:
    """扫描项目中已有的限界上下文"""
    source_path = os.path.join(profile.project_root, profile.source_root)
    if not os.path.isdir(source_path):
        return []

    contexts = []
    for entry in os.listdir(source_path):
        full_path = os.path.join(source_path, entry)
        if os.path.isdir(full_path) and entry != "infra":
            # 检查是否有 domain/ 子目录（说明是一个 DDD 上下文）
            if os.path.isdir(os.path.join(full_path, "domain")):
                contexts.append(entry)

    return contexts


def profile_to_system_prompt(profile: ProjectProfile) -> str:
    """将项目特征转化为 system prompt 片段"""
    lines = [
        f"## 项目: {profile.name}",
        f"语言: {profile.language}, 框架: {profile.framework or '原生'}",
        f"源码目录: {profile.source_root}/",
        "",
        "## 已有上下文",
    ]

    if profile.existing_contexts:
        for ctx in profile.existing_contexts:
            lines.append(f"- {ctx}")
    else:
        lines.append("- (无)")

    lines.append("")
    lines.append("## 架构约束")
    lines.append(f"- domain/ 禁止 import: {', '.join(profile.rules.domain_no_import)}")
    lines.append(f"- application/ 禁止 import: {', '.join(profile.rules.application_no_import)}")
    lines.append(f"- 跨上下文 import 只允许在: {', '.join(profile.rules.cross_context_only_in)}")
    if profile.rules.money_type_required:
        lines.append("- 金额必须用 Money 值对象，禁止裸 float/int")

    if profile.conventions:
        lines.append("")
        lines.append("## 项目约定")
        for conv in profile.conventions:
            lines.append(f"- {conv}")

    if profile.learned_patterns:
        lines.append("")
        lines.append("## 从历史代码学到的模式")
        for pattern in profile.learned_patterns:
            lines.append(f"- {pattern}")

    return "\n".join(lines)
