"""AUTO 模式四层过滤器。

AUTO 审批模式下，``evaluate_permission()`` 的模式权限矩阵把某次工具调用标记为
``"filter"`` 时，由本模块的四层过滤器管线决定其最终去向：

- F1 acceptEdits 快速通道：EDIT 类工具且目标路径位于工作区内、不命中敏感路径
  时自动放行；
- F2 安全工具白名单：READ/INTERACT/PLAN 等只读/交互类工具自动放行；
- F3 破坏性命令守卫：匹配 ``DESTRUCTIVE_PATTERNS`` 中破坏性命令模式的 Shell
  调用硬拦截（deny）；DELETE 类工具目标为绝对路径且层级过浅（如 ``/``、
  ``/home``、``C:/Users``）时同样硬拦截，防止误删高层目录；
- F4 LLM 分类器：当前为占位实现，分类器尚未接入，一律回退人工审批（ask）。

任何一层无法给出确定性结论时继续进入下一层；没有任何一层能自动放行或硬拦截
时，最终安全回退到人工审批。本模块不依赖任何 LLM API 调用。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_agent.policy.sensitive_paths import is_sensitive_path
from harness_agent.policy.tool_risk import ToolKind, get_tool_kind


@dataclass(frozen=True)
class AutoModeDecision:
    """AUTO 模式过滤器管线中某一层给出的结构化决策。

    Attributes:
        via: 产生该决策的过滤器层标识（"F1"、"F2"、"F3" 或 "F4"）。
        should_block: True 表示该调用不能自动执行（硬拒绝或回退人工审批）。
        reason: 人类可读的决策原因，供日志记录与界面展示。
    """

    via: str
    should_block: bool
    reason: str


# F3 层匹配的破坏性 Shell 命令模式。模式遵循保守的"宁可误拦、不可漏放"原则：
# 只用于硬拦截明显危险的命令；未命中的命令会继续进入 F4 层判断。
DESTRUCTIVE_PATTERNS: list[re.Pattern[str]] = [
    # rm -rf /：递归强制删除根目录（含 -fr 等选项顺序变体）
    re.compile(r"\brm\s+(?:-[A-Za-z]*r[A-Za-z]*f|-[A-Za-z]*f[A-Za-z]*r)[A-Za-z]*\s+/(?:\s|$)"),
    # git push --force：强制推送覆盖远端历史（含 -f 缩写）
    re.compile(r"\bgit\s+push\b[^|&;\n]*(?:--force\b|-f\b)"),
    # git reset --hard：丢弃全部未提交改动
    re.compile(r"\bgit\s+reset\b[^|&;\n]*--hard\b"),
    # git clean -f：强制删除未跟踪文件（含 -fd、-xdf 等组合选项）
    re.compile(r"\bgit\s+clean\b[^|&;\n]*-[A-Za-z]*f[A-Za-z]*\b"),
    # terraform destroy：销毁全部受管云资源
    re.compile(r"\bterraform\s+destroy\b"),
    # mkfs.*：格式化文件系统（如 mkfs.ext4）
    re.compile(r"\bmkfs\."),
    # dd of=/dev/...：直接向块设备写入
    re.compile(r"\bdd\b[^|&;\n]*\bof=/dev/"),
    # chmod -R 777 /...：对绝对路径递归放开全部权限
    re.compile(r"\bchmod\s+-R\s+0?777\s+/"),
    # > /etc/ 或 >> /etc/：重定向覆盖关键系统配置
    re.compile(r">{1,2}\s*/etc/"),
]

# should_block=True 时各层对应的最终决策：F3 是硬拒绝，F4 是回退人工审批。
_BLOCKED_OUTCOME: dict[str, str] = {"F3": "deny", "F4": "ask"}


def evaluate_auto_mode(
    tool_name: str,
    tool_args: dict[str, Any],
    workspace_root: str | None = None,
    consecutive_reject_count: int = 0,
    *,
    sensitive_check_fn: Callable[[str], bool] | None = None,
) -> tuple[str, str]:
    """运行 AUTO 模式四层过滤器，返回工具调用的处置决策。

    四层过滤器按 F1 → F2 → F3 → F4 顺序短路执行：前序层未给出结论时才进入
    下一层。F4 目前为占位实现，任何到达该层的调用都回退人工审批。

    Args:
        tool_name: 工具名称。
        tool_args: 工具参数字典。
        workspace_root: 工作区根目录，F1 层判断路径归属时必需；为 None 时
            F1 层不产生结论。
        consecutive_reject_count: 用户连续拒绝次数；>= 3 时 F4 层直接回退
            人工审批，避免过滤器在用户已多次拒绝后仍反复自动判定。
        sensitive_check_fn: 自定义敏感路径判断函数；默认使用
            ``is_sensitive_path``。

    Returns:
        (decision, reason) 二元组：decision 为 "allow"（自动放行）、"deny"
        （硬拦截）或 "ask"（回退人工审批），reason 为决策原因。
    """
    kind = get_tool_kind(tool_name)
    sensitive_check = sensitive_check_fn or is_sensitive_path

    decision = (
        _filter_accept_edits(kind, tool_args, workspace_root, sensitive_check)
        or _filter_safe_allowlist(kind)
        or _filter_destructive_command(tool_args)
        or _filter_destructive_delete(kind, tool_args)
        or _filter_llm_classifier(consecutive_reject_count)
    )

    if not decision.should_block:
        return "allow", decision.reason
    return _BLOCKED_OUTCOME[decision.via], decision.reason


def _filter_accept_edits(
    kind: ToolKind,
    tool_args: dict[str, Any],
    workspace_root: str | None,
    sensitive_check: Callable[[str], bool],
) -> AutoModeDecision | None:
    """F1 acceptEdits 快速通道：工作区内且非敏感的 EDIT 调用自动放行。

    目标路径缺失、不在工作区内或命中敏感路径时返回 None，交由后续层处理。
    """
    if kind is not ToolKind.EDIT or workspace_root is None:
        return None
    file_path = tool_args.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    if not _path_within_workspace(file_path, workspace_root):
        return None
    if sensitive_check(file_path):
        return None
    return AutoModeDecision(
        via="F1",
        should_block=False,
        reason="acceptEdits 快速通道：工作区内非敏感编辑，自动放行",
    )


def _filter_safe_allowlist(kind: ToolKind) -> AutoModeDecision | None:
    """F2 安全工具白名单：READ/INTERACT/PLAN 类工具自动放行。"""
    if kind in (ToolKind.READ, ToolKind.INTERACT, ToolKind.PLAN):
        return AutoModeDecision(
            via="F2",
            should_block=False,
            reason="安全工具白名单：只读/交互类工具自动放行",
        )
    return None


def _filter_destructive_command(tool_args: dict[str, Any]) -> AutoModeDecision | None:
    """F3 破坏性命令守卫：命令参数命中破坏性模式时硬拦截。

    只检查参数中携带的 ``command`` 字符串；无命令参数或未命中任何模式时
    返回 None，交由后续层处理。
    """
    command = tool_args.get("command")
    if not isinstance(command, str) or not command:
        return None
    for pattern in DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return AutoModeDecision(
                via="F3",
                should_block=True,
                reason=f"破坏性命令守卫：命令匹配破坏性模式 {pattern.pattern}",
            )
    return None


def _filter_destructive_delete(kind: ToolKind, tool_args: dict[str, Any]) -> AutoModeDecision | None:
    """F3 破坏性删除守卫：DELETE 类工具目标路径层级过浅时硬拦截。

    仅对绝对路径生效（POSIX 以 ``/`` 开头，或 Windows 盘符前缀如 ``C:/``）；
    相对路径返回 None 交由后续层处理，避免误伤 ``tmp/cache.db`` 这类工作区
    相对路径。去掉盘符前缀后路径深度不超过 2 层（如 ``/``、``/home``、
    ``/usr/local``、``C:/Users``）时视为疑似高层目录，返回 deny 决策。
    """
    if kind is not ToolKind.DELETE:
        return None
    file_path = tool_args.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None

    normalized = file_path.replace("\\", "/")
    if not (normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized)):
        return None

    # 去掉盘符前缀后按 "/" 拆分并过滤空段，得到真实路径深度。
    stripped = re.sub(r"^[A-Za-z]:", "", normalized)
    depth = len([part for part in stripped.split("/") if part])
    if depth <= 2:
        return AutoModeDecision(
            via="F3",
            should_block=True,
            reason=f"破坏性命令守卫：删除目标 {file_path} 路径层级过浅，疑似高层目录",
        )
    return None


def _filter_llm_classifier(consecutive_reject_count: int) -> AutoModeDecision:
    """F4 LLM 分类器：占位实现，分类器未接入前一律回退人工审批。

    用户已连续拒绝 3 次及以上时同样直接回退人工审批，避免过滤器在用户意图
    已明确否定的情况下仍反复自动判定。
    """
    if consecutive_reject_count >= 3:
        return AutoModeDecision(
            via="F4",
            should_block=True,
            reason="LLM 分类器：连续拒绝次数达到阈值，回退人工审批",
        )
    return AutoModeDecision(
        via="F4",
        should_block=True,
        reason="LLM 分类器尚未实现，回退人工审批",
    )


def _path_within_workspace(file_path: str, workspace_root: str) -> bool:
    """判断文件路径经规范化解析后是否包含在工作区内。

    与工作区边界中间件的约定一致：Windows 上 ``/`` 开头的虚拟路径按工作区
    相对路径拼接后解析；其余路径按真实绝对路径解析后再做 containment 检查。
    解析失败一律视为越界（fail-closed）。
    """
    workspace = Path(workspace_root).resolve(strict=False)
    if file_path.startswith("/") and sys.platform == "win32":
        candidate = (workspace / file_path.lstrip("/")).resolve(strict=False)
    else:
        candidate = Path(file_path).resolve(strict=False)
    try:
        candidate.relative_to(workspace)
        return True
    except (ValueError, OSError, RuntimeError):
        return False
