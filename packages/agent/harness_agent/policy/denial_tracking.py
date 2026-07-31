"""拒绝追踪与回退：防止 Agent 在连续被拒绝时陷入死循环。"""

from __future__ import annotations

CONSECUTIVE_DENIAL_THRESHOLD = 3
"""连续拒绝达到此阈值时触发警告注入。"""


class DenialTracker:
    """追踪用户连续拒绝次数，超过阈值时建议 Agent 回退或询问用户。"""

    def __init__(self) -> None:
        """初始化追踪器，连续拒绝计数归零。"""
        self.consecutive_count: int = 0

    def record_denial(self) -> None:
        """记录一次拒绝，连续拒绝计数加一。"""
        self.consecutive_count += 1

    def record_approval(self) -> None:
        """记录一次审批通过，重置连续拒绝计数。"""
        self.consecutive_count = 0

    def should_inject_warning(self) -> bool:
        """判断是否应注入警告：连续拒绝次数达到阈值时返回 True。"""
        return self.consecutive_count >= CONSECUTIVE_DENIAL_THRESHOLD

    def warning_message(self) -> str:
        """生成中文警告提示，包含当前连续拒绝次数。"""
        return (
            f"用户已连续拒绝 {self.consecutive_count} 次操作，"
            f"请重新评估方案或使用 ask_user 工具询问用户意图。"
        )

    def reset(self) -> None:
        """重置追踪器状态，连续拒绝计数归零。"""
        self.consecutive_count = 0
