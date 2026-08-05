"""基于 tree-sitter 的 Bash 命令解析器。

本模块提供 Bash 命令的 AST 解析、链式命令拆分、包装器剥离、命令根提取、
"Always Allow" 规则生成以及危险参数检测功能。所有函数在 tree-sitter 解析
失败时均会优雅降级，不抛出异常。
"""
from __future__ import annotations

import logging
from typing import Any

import tree_sitter_bash as tsbash
from tree_sitter import Language, Parser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模块级 tree-sitter 初始化（单例，避免重复创建 Language/Parser）
# ---------------------------------------------------------------------------

_LANG = Language(tsbash.language())
_PARSER = Parser(_LANG)

# ---------------------------------------------------------------------------
# 危险参数模式
# ---------------------------------------------------------------------------

_DANGEROUS_ARG_PATTERNS: frozenset[str] = frozenset({
    "--pre",
    "--preview-script",
    "--exec",
    "-exec",
    "--allow-run",
})

# ---------------------------------------------------------------------------
# tree-sitter-bash 中表示"参数/值"的节点类型集合
# ---------------------------------------------------------------------------

_ARG_NODE_TYPES: frozenset[str] = frozenset({
    "word",
    "string",
    "raw_string",
    "number",
    "expansion",
    "simple_expansion",
    "concatenation",
    "ansi_c_string",
})


# ===================================================================
# 内部辅助函数
# ===================================================================


def _parse_bash_ast(command: str):
    """解析命令字符串为原始 tree-sitter AST。返回 Tree 或 None。"""
    if not command or not command.strip():
        return None
    try:
        tree = _PARSER.parse(command.encode("utf-8"))
        return tree
    except Exception:
        logger.debug("tree-sitter 解析失败: %r", command, exc_info=True)
        return None


def _node_text(node, source: bytes) -> str:
    """从原始字节中提取节点文本。"""
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _ast_to_dict(node, source: bytes) -> dict[str, Any]:
    """将 tree-sitter 节点递归转换为字典表示。"""
    result: dict[str, Any] = {
        "type": node.type,
        "text": _node_text(node, source),
    }
    children = []
    for child in node.children:
        children.append(_ast_to_dict(child, source))
    if children:
        result["children"] = children
    return result


def _extract_segments_from_node(node, source: bytes) -> list[str]:
    """从 tree-sitter 节点递归提取链式命令段。

    处理 ``list``（``;`` / ``&&`` / ``||`` 分隔）和 ``pipeline``（``|`` 分隔）节点，
    将它们拆分为独立段。
    """
    results: list[str] = []

    if node.type == "program":
        for child in node.children:
            results.extend(_extract_segments_from_node(child, source))
        return results

    if node.type == "list":
        # 每个子节点是一个独立命令段
        for child in node.children:
            if child.type in ("command", "pipeline", "compound_statement",
                              "redirected_statement", "declaration_command",
                              "test_command", "negated_command", "subshell"):
                results.extend(_extract_segments_from_node(child, source))
        return results

    if node.type == "pipeline":
        for child in node.children:
            if child.type == "command":
                results.append(_node_text(child, source).strip())
        return results

    # 叶子命令节点
    if node.type in ("command", "compound_statement", "redirected_statement",
                     "declaration_command", "test_command", "negated_command"):
        return [_node_text(node, source).strip()]

    return results


def _split_by_separators_fallback(command: str) -> list[str]:
    """按 ``&&`` / ``||`` / ``;`` / ``|`` 分隔符拆分命令字符串。

    使用简单状态机跟踪引号状态，确保引号内的分隔符不被拆分。
    """
    segments: list[str] = []
    current: list[str] = []
    i = 0
    n = len(command)
    in_single = False
    in_double = False

    while i < n:
        ch = command[i]

        # 引号状态跟踪
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue
        # 转义
        if ch == "\\" and (in_double or not in_single):
            current.append(ch)
            i += 1
            if i < n:
                current.append(command[i])
            i += 1
            continue

        if not in_single and not in_double:
            # 检测 && 分隔符
            if ch == "&" and i + 1 < n and command[i + 1] == "&":
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 2
                continue
            # 检测 || 分隔符
            if ch == "|" and i + 1 < n and command[i + 1] == "|":
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 2
                continue
            # 检测 ; 分隔符
            if ch == ";":
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 1
                continue
            # 检测 | 分隔符（管道）
            if ch == "|":
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 1
                continue

        current.append(ch)
        i += 1

    seg = "".join(current).strip()
    if seg:
        segments.append(seg)

    return segments


def _walk_tree(node):
    """深度优先遍历 tree-sitter 节点树。"""
    yield node
    for child in node.children:
        yield from _walk_tree(child)


def _is_arg_node(node) -> bool:
    """判断 node 是否为参数/值类型的节点。"""
    return node.type in _ARG_NODE_TYPES


def _arg_inner_text(node, source: bytes) -> str:
    """提取参数节点的内部文本（剥离外层引号并反转义）。

    对 ``string`` / ``raw_string`` 节点，返回引号内的内容并处理转义；
    对 ``word`` 节点，返回原始文本。
    """
    text = _node_text(node, source)
    if node.type in ("string", "ansi_c_string"):
        if len(text) >= 2:
            inner = text[1:-1]
            # 处理双引号内的转义：\\ → \，\" → "，\$ → $，\` → `
            inner = inner.replace("\\\\", "\x00").replace('\\"', '"')
            inner = inner.replace("\\$", "$").replace("\\`", "`")
            inner = inner.replace("\x00", "\\")
            return inner
    elif node.type == "raw_string":
        if len(text) >= 2:
            return text[1:-1]
    return text


def _collect_arg_nodes(cmd_children: list, source: bytes) -> tuple[list[str], list[int], list[str]]:
    """从命令子节点中收集参数节点。

    遍历所有子节点，识别参数节点和变量赋值节点，返回三元组：
    ``(arg_values, arg_indices, env_assignments)``。

    - ``arg_values``：每个参数节点的原始文本
    - ``arg_indices``：每个参数节点在 ``cmd_children`` 中的索引
    - ``env_assignments``：变量赋值节点的原始文本
    """
    arg_values: list[str] = []
    arg_indices: list[int] = []
    env_assignments: list[str] = []
    for idx, child in enumerate(cmd_children):
        if _is_arg_node(child):
            arg_values.append(_node_text(child, source))
            arg_indices.append(idx)
        elif child.type == "variable_assignment":
            env_assignments.append(_node_text(child, source))
    return arg_values, arg_indices, env_assignments


# ===================================================================
# 公开 API
# ===================================================================


def parse_bash(command: str) -> dict[str, Any] | None:
    """使用 tree-sitter-bash 解析 Bash 命令字符串为 AST 字典。

    Args:
        command: 待解析的 Bash 命令字符串。

    Returns:
        解析成功时返回 tree-sitter AST 的字典表示（包含 ``type``、``text``
        和可选的 ``children`` 键），解析失败时返回 ``None``。
    """
    tree = _parse_bash_ast(command)
    if tree is None:
        return None
    return _ast_to_dict(tree.root_node, tree.root_node.text)


def extract_segments(command: str) -> list[str]:
    """将包含 ``&&`` / ``||`` / ``;`` / ``|`` 的链式命令拆分为独立段。

    优先使用 tree-sitter AST 拆分，解析失败时回退到基于引号感知的字符串分割。

    Args:
        command: 待拆分的链式命令字符串。

    Returns:
        拆分后的命令段列表。

    Examples:
        ``"git add . && npm test"`` → ``["git add .", "npm test"]``
        ``"cat file | grep x"`` → ``["cat file", "grep x"]``
        ``"echo hello && ls -la; pwd"`` → ``["echo hello", "ls -la", "pwd"]``
    """
    tree = _parse_bash_ast(command)
    if tree is not None:
        try:
            segments = _extract_segments_from_node(tree.root_node, tree.root_node.text)
            if segments:
                return segments
        except Exception:
            logger.debug("AST 段拆分失败，回退到字符串分割", exc_info=True)

    return _split_by_separators_fallback(command)


def strip_wrappers(command: str, max_depth: int = 3) -> str:
    """剥离无安全影响的命令包装器，提取核心命令。

    可剥离的包装器包括 ``timeout``、``nice``、``nohup``、``stdbuf``、
    ``env``、``bash -c`` / ``sh -c``、以及前导环境变量赋值。

    Args:
        command: 待剥离的命令字符串。
        max_depth: 最大递归剥离深度，默认 3。

    Returns:
        剥离包装器后的核心命令字符串。

    Examples:
        ``"timeout 30 env NODE_ENV=test bash -c 'echo hello'"`` → ``"echo hello"``
        ``'bash -c "bash -c \\"git status\\""'`` → ``"git status"``
    """
    if max_depth <= 0:
        return command

    tree = _parse_bash_ast(command)
    if tree is None:
        return command

    source = tree.root_node.text
    root = tree.root_node

    # 找到第一个命令节点
    cmd_node = None
    for node in _walk_tree(root):
        if node.type == "command":
            cmd_node = node
            break

    if cmd_node is None:
        return command

    cmd_name_node = None
    cmd_children: list[Any] = list(cmd_node.children)

    for child in cmd_children:
        if child.type == "command_name":
            cmd_name_node = child
            break

    if cmd_name_node is None:
        # 没有 command_name，尝试剥离前导环境变量
        stripped = _strip_env_assignments(source, cmd_children)
        if stripped != command:
            return strip_wrappers(stripped, max_depth - 1)
        return command

    cmd_name = _node_text(cmd_name_node, source).strip()

    # 收集所有参数节点（word / string / number / ...）和变量赋值
    arg_values, arg_indices, env_assignments = _collect_arg_nodes(
        cmd_children, source
    )

    # -- timeout N ... --
    if cmd_name == "timeout":
        # 跳过数值参数
        skip = 0
        for w in arg_values:
            # timeout 数值可能带单位后缀（s/m/h/d）
            try:
                float(w.rstrip("smhd"))
                skip += 1
            except ValueError:
                break
        if skip < len(arg_values):
            inner = _rebuild_from_indices(source, cmd_children,
                                          arg_indices[skip:])
            if inner:
                return strip_wrappers(inner, max_depth - 1)
        return command

    # -- nice / nohup / stdbuf ... --
    if cmd_name in ("nice", "nohup", "stdbuf"):
        inner = _rebuild_from_indices(source, cmd_children, arg_indices)
        if inner:
            return strip_wrappers(inner, max_depth - 1)
        return command

    # -- env VAR=value ... / env -S '...' --
    if cmd_name == "env":
        inner = _handle_env_wrapper(source, cmd_children)
        if inner and inner != command:
            return strip_wrappers(inner, max_depth - 1)
        return command

    # -- bash -c '...' / sh -c '...' --
    if cmd_name in ("bash", "sh") and len(arg_values) >= 2:
        if arg_values[0] == "-c":
            # 提取 -c 后面的命令字符串（去除引号）
            inner_cmd = _arg_inner_text(cmd_children[arg_indices[1]], source)
            if inner_cmd:
                return strip_wrappers(inner_cmd, max_depth - 1)

    # -- 前导环境变量赋值 --
    if env_assignments:
        stripped = _strip_env_assignments(source, cmd_children)
        if stripped != command:
            return strip_wrappers(stripped, max_depth - 1)

    return command


def _rebuild_from_indices(source: bytes, cmd_children: list,
                          indices: list[int]) -> str | None:
    """从指定索引位置的所有子节点重建命令字符串。"""
    if not indices:
        return None
    parts: list[str] = []
    for idx in indices:
        parts.append(_node_text(cmd_children[idx], source))
    return " ".join(parts)


def _handle_env_wrapper(source: bytes, cmd_children: list) -> str | None:
    """处理 env 包装器：env VAR=value ... 或 env -S '...'."""
    env_assign_indices: list[int] = []
    non_env_indices: list[int] = []
    has_s_flag = False

    for idx, child in enumerate(cmd_children):
        if _is_arg_node(child):
            text = _node_text(child, source)
            if text in ("-S", "-s"):
                has_s_flag = True
            elif "=" in text and not text.startswith("-"):
                env_assign_indices.append(idx)
            else:
                non_env_indices.append(idx)
        elif child.type == "variable_assignment":
            env_assign_indices.append(idx)

    if has_s_flag and non_env_indices:
        # -S 后面的是命令字符串（去除引号）
        inner_cmd = _arg_inner_text(cmd_children[non_env_indices[0]], source)
        if inner_cmd:
            return inner_cmd
        return None

    if non_env_indices:
        # env 之后有非赋值参数，这些是实际命令
        return _rebuild_from_indices(source, cmd_children, non_env_indices)

    return None


def _strip_env_assignments(source: bytes, cmd_children: list) -> str:
    """剥离命令开头所有环境变量赋值（VAR=value）。"""
    result_parts: list[str] = []
    found_start = False
    for child in cmd_children:
        if not found_start:
            if child.type == "variable_assignment":
                continue
            if child.type == "word":
                text = _node_text(child, source)
                if "=" in text and not text.startswith("-"):
                    continue
            found_start = True
        if found_start:
            result_parts.append(_node_text(child, source))
    if result_parts:
        return " ".join(result_parts)
    return ""


def get_command_root(segment: str) -> str:
    """提取命令的第一个词（命令根），跳过前导空白和环境变量前缀。

    Args:
        segment: 命令字符串。

    Returns:
        命令根字符串。

    Examples:
        ``"git status --porcelain"`` → ``"git"``
        ``"NODE_ENV=test npm run build"`` → ``"npm"``
        ``"  ls -la"`` → ``"ls"``
    """
    segment = segment.strip()
    if not segment:
        return ""

    tree = _parse_bash_ast(segment)
    if tree is not None:
        source = tree.root_node.text
        for node in _walk_tree(tree.root_node):
            if node.type == "command_name":
                return _node_text(node, source).strip()
            if node.type == "command":
                # 如果 command 节点没有 command_name 子节点，
                # 提取第一个非 variable_assignment 的 word
                first_word = None
                for child in node.children:
                    if child.type == "command_name":
                        first_word = _node_text(child, source)
                        break
                    if child.type == "word" and first_word is None:
                        text = _node_text(child, source)
                        if "=" not in text or text.startswith("-"):
                            first_word = text
                if first_word:
                    return first_word.strip()

    # 回退：简单按空白分割，跳过 VAR=value 前缀
    words = segment.split()
    for word in words:
        if "=" not in word or word.startswith("-"):
            return word
    return words[0] if words else ""


def extract_command_rule(command: str) -> str:
    """基于 AST 提取最小范围的规则字符串，用于生成 "Always Allow" 持久化规则。

    规则生成逻辑：
    - 单命令无参：``"whoami"`` → ``"whoami"``
    - 单命令有参：保留根命令＋第一非 flag 子命令，参数通配
    - 二级命令：``"npm install express"`` → ``"npm install *"``
    - 三级命令：``"docker compose up -d"`` → ``"docker compose up *"``
    - 不支持的命令：回退到 ``get_command_root(command) + " *"``

    Args:
        command: 命令字符串。

    Returns:
        "Always Allow" 规则字符串。
    """
    tree = _parse_bash_ast(command)
    if tree is not None:
        source = tree.root_node.text
        for node in _walk_tree(tree.root_node):
            if node.type == "command":
                cmd_name = None
                # 收集所有非 flag、非赋值参数及其位置信息
                candidates: list[tuple[str, int]] = []
                has_flag_after_last_candidate = False
                for child in node.children:
                    if child.type == "command_name":
                        cmd_name = _node_text(child, source)
                    elif _is_arg_node(child):
                        text = _node_text(child, source)
                        if text.startswith("-"):
                            has_flag_after_last_candidate = True
                        elif "=" not in text:
                            candidates.append((text, child.start_byte))
                            has_flag_after_last_candidate = False
                if cmd_name:
                    if not candidates:
                        return cmd_name
                    # 如果最后一个候选之后没有 flag，去掉它（更可能是参数而非子命令）
                    if not has_flag_after_last_candidate and len(candidates) > 1:
                        candidates.pop()
                    rule_parts = [cmd_name] + [c[0] for c in candidates[:2]]
                    return " ".join(rule_parts) + " *"
                break

    # 回退
    root = get_command_root(command)
    if root:
        return root + " *"
    return command + " *"


def has_dangerous_args(segment: str) -> bool:
    """检测命令是否携带危险参数，使白名单安全假设失效。

    检测以下危险参数模式：
    - ``--pre``、``--preview-script``（rg 执行外部预处理器）
    - ``--exec``、``-exec``（find 执行外部命令）
    - ``--allow-run``（deno / git hooks）

    Args:
        segment: 命令字符串。

    Returns:
        如果携带危险参数返回 ``True``，否则返回 ``False``。

    Examples:
        ``"rg --pre 'echo evil' pattern"`` → ``True``
    """
    if not segment or not segment.strip():
        return False

    tree = _parse_bash_ast(segment)
    if tree is not None:
        source = tree.root_node.text
        # 遍历所有参数节点，检查是否匹配危险模式
        for node in _walk_tree(tree.root_node):
            if _is_arg_node(node):
                # 提取参数名（对 string/raw_string 取内部文本）
                arg_text = _arg_inner_text(node, source)
                if arg_text in _DANGEROUS_ARG_PATTERNS:
                    return True
                # 匹配 --exec=<xxx> 形式
                if "=" in arg_text:
                    arg_name = arg_text.split("=", 1)[0]
                    if arg_name in _DANGEROUS_ARG_PATTERNS:
                        return True
        return False

    # 回退：简单字符串匹配
    import shlex

    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()

    for token in tokens:
        token_clean = token.strip("'\"")
        if token_clean in _DANGEROUS_ARG_PATTERNS:
            return True
        if "=" in token_clean:
            arg_name = token_clean.split("=", 1)[0]
            if arg_name in _DANGEROUS_ARG_PATTERNS:
                return True

    return False
