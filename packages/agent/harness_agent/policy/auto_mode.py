"""AUTO 模式四层过滤器。

AUTO 审批模式下，``evaluate_permission()`` 的模式权限矩阵把某次工具调用标记为
``"filter"`` 时，由本模块的四层过滤器管线决定其最终去向：

- F1 acceptEdits 快速通道：EDIT 类工具不经分类器直接放行，仅受保护路径
  （敏感文件/目录、``.github/workflows/`` 前缀）命中时转入后续层判断；
- F2 安全工具白名单：READ/INTERACT/PLAN 等只读/交互类工具自动放行；
- F3 破坏性命令守卫：对 Shell 命令使用 ``extract_segments`` 拆分后逐段检查
  ``DESTRUCTIVE_PATTERNS``；命中破坏性模式时默认硬拦截（deny），但如果用户
  最近消息中包含明确的破坏意图关键词（如"删除""销毁""force"等），则降级为
  人工确认（ask），避免误拦用户主动发起的合法危险操作；DELETE 类工具目标为
  绝对路径且层级过浅（如 ``/``、``/home``、``C:/Users``）时同样硬拦截，
  防止误删高层目录；
- F4 LLM 分类器：当前为占位实现，分类器尚未接入，一律回退人工审批（ask）。

任何一层无法给出确定性结论时继续进入下一层；没有任何一层能自动放行或硬拦截
时，最终安全回退到人工审批。本模块不依赖任何 LLM API 调用。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from harness_agent.policy.bash_parser import extract_segments
from harness_agent.policy.sensitive_paths import is_protected_edit_path
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
    # git checkout -- .：丢弃工作区所有未暂存改动
    re.compile(r"\bgit\s+checkout\b[^|&;\n]*--\s*\."),
    # git stash drop：删除指定（或最新）stash
    re.compile(r"\bgit\s+stash\s+drop\b"),
    # git stash clear：清空所有 stash
    re.compile(r"\bgit\s+stash\s+clear\b"),
    # git commit --amend：改写最近一次提交历史
    re.compile(r"\bgit\s+commit\b[^|&;\n]*--amend\b"),
    # git branch -D：强制删除分支（含 --delete --force 缩写变体）
    re.compile(r"\bgit\s+branch\b[^|&;\n]*-D\b"),
    # kubectl delete：删除 Kubernetes 资源
    re.compile(r"\bkubectl\s+delete\b"),
    # chmod -R 777：递归放开全部权限
    re.compile(r"\bchmod\s+-R\s+777\b"),
    # chown -R：递归变更文件所有者
    re.compile(r"\bchown\s+-R\b"),
    # pulumi destroy：销毁 Pulumi 管理的云资源
    re.compile(r"\bpulumi\s+destroy\b"),
    # cdk destroy：销毁 CDK（AWS Cloud Development Kit）部署的资源
    re.compile(r"\bcdk\s+destroy\b"),
]

# should_block=True 时各层对应的最终决策：F3 是硬拒绝，F3_exempt 是有破坏意图
# 时降级的人工确认，F4 是回退人工审批。
_BLOCKED_OUTCOME: dict[str, str] = {"F3": "deny", "F3_exempt": "ask", "F4": "ask"}


def has_destructive_intent(user_messages: list[str]) -> bool:
    """检测用户最近消息是否包含明确的破坏意图关键词。

    用于 F3 破坏性命令守卫的意图豁免：当命令命中破坏性模式但用户最近消息中
    有明确的"删除""重置""destroy""force"等关键词时，认为用户主动发起危险操作，
    降级为人工确认而非硬拦截。

    Args:
        user_messages: 最近几条用户消息（非模型消息）的文本列表。

    Returns:
        任一条消息匹配任一关键词则返回 ``True``。
    """
    if not user_messages:
        return False

    _DESTRUCTIVE_INTENT_ZH: frozenset[str] = frozenset({
        "丢弃", "清除", "重置", "撤销", "回滚", "删除", "销毁", "彻底", "强制", "放弃",
    })
    _DESTRUCTIVE_INTENT_EN: frozenset[str] = frozenset({
        "discard", "wipe", "reset", "destroy", "force", "nuke", "clean", "purge", "obliterate",
    })

    combined = user_messages
    for msg in combined:
        msg_lower = msg.lower()
        for kw in _DESTRUCTIVE_INTENT_ZH:
            if kw in msg_lower:
                return True
        for kw in _DESTRUCTIVE_INTENT_EN:
            if kw in msg_lower:
                return True
    return False


def evaluate_auto_mode(
    tool_name: str,
    tool_args: dict[str, Any],
    workspace_root: str | None = None,
    consecutive_reject_count: int = 0,
    user_messages: list[str] | None = None,
) -> tuple[str, str]:
    """运行 AUTO 模式四层过滤器，返回工具调用的处置决策。

    四层过滤器按 F1 → F2 → F3 → F4 顺序短路执行：前序层未给出结论时才进入
    下一层。F4 目前为占位实现，任何到达该层的调用都回退人工审批。

    F3 破坏性命令守卫支持用户意图豁免：当命令命中破坏性模式但 user_messages
    中包含明确的破坏意图关键词时，降级为人工确认（ask）而非硬拦截（deny）。

    Args:
        tool_name: 工具名称。
        tool_args: 工具参数字典。
        workspace_root: 工作区根目录；当前各层不直接依赖此参数（保留以兼容
            现有调用方）。
        consecutive_reject_count: 用户连续拒绝次数；>= 3 时 F4 层直接回退
            人工审批，避免过滤器在用户已多次拒绝后仍反复自动判定。
        user_messages: 最近几条用户消息文本列表，供 F3 意图豁免判断；为 None
            时跳过豁免，匹配破坏性模式直接 deny。

    Returns:
        (decision, reason) 二元组：decision 为 "allow"（自动放行）、"deny"
        （硬拦截）或 "ask"（回退人工审批），reason 为决策原因。
    """
    kind = get_tool_kind(tool_name)

    decision = (
        _filter_accept_edits(kind, tool_args)
        or _filter_safe_allowlist(kind)
        or _filter_destructive_command(tool_args, user_messages)
        or _filter_destructive_delete(kind, tool_args)
        or _filter_llm_classifier(consecutive_reject_count)
    )

    if not decision.should_block:
        return "allow", decision.reason
    return _BLOCKED_OUTCOME[decision.via], decision.reason


def _filter_accept_edits(
    kind: ToolKind,
    tool_args: dict[str, Any],
) -> AutoModeDecision | None:
    """F1 acceptEdits 快速通道：EDIT 类工具不经分类器直接放行。

    仅受保护路径（敏感文件/目录、``.github/workflows/`` 前缀）命中时放行
    进入后续过滤层；其余 EDIT 调用（含工作区外编辑）一律直接 allow。
    Delete 类工具不进入本层（``ToolKind.DELETE`` ≠ ``ToolKind.EDIT``），
    非 AUTO 模式由调用入口做模式过滤，本函数不做额外检查。
    """
    if kind is not ToolKind.EDIT:
        return None
    file_path = tool_args.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    if is_protected_edit_path(file_path):
        return None
    return AutoModeDecision(
        via="F1",
        should_block=False,
        reason="auto mode edit fast-path",
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


def _filter_destructive_command(
    tool_args: dict[str, Any],
    user_messages: list[str] | None = None,
) -> AutoModeDecision | None:
    """F3 破坏性命令守卫：逐段检查命令是否匹配破坏性模式。

    使用 ``extract_segments`` 将命令拆分为独立段后逐段匹配
    ``DESTRUCTIVE_PATTERNS``。命中破坏性模式时：

    - 若 ``user_messages`` 非空且 ``has_destructive_intent(user_messages)``
      为 True → 认定用户有明确破坏意图，降级为人工确认（ask）。
    - 否则 → 硬拦截（deny）。

    只检查参数中携带的 ``command`` 字符串；无命令参数或未命中任何模式时
    返回 None，交由后续层处理。
    """
    command = tool_args.get("command")
    if not isinstance(command, str) or not command:
        return None

    segments = extract_segments(command)
    for segment in segments:
        for pattern in DESTRUCTIVE_PATTERNS:
            if pattern.search(segment):
                if user_messages and has_destructive_intent(user_messages):
                    return AutoModeDecision(
                        via="F3_exempt",
                        should_block=True,
                        reason=(
                            f"破坏性命令守卫：命令段 '{segment}' 匹配破坏性模式 "
                            f"{pattern.pattern}，但用户有明确破坏意图，降级为人工确认"
                        ),
                    )
                return AutoModeDecision(
                    via="F3",
                    should_block=True,
                    reason=f"破坏性命令守卫：命令段 '{segment}' 匹配破坏性模式 {pattern.pattern}",
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



