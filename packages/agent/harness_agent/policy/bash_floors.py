"""Shell 安全底线强制询问。

即使白名单命中，四条底线触发时仍强制进入 ask 审批流程：

1. 写文件副作用（``>``、``>>``、``tee``、``dd of=``）
2. 危险环境变量注入（``LD_PRELOAD``、``PYTHONPATH`` 等）
3. 不可静态分析的动态 Shell 执行（``eval``、``bash -c "$VAR"``）
4. 外部可执行文件触发风险（git hooks、npm scripts、make）
"""

from __future__ import annotations

import re

from harness_agent.policy.bash_parser import extract_segments

# ---------------------------------------------------------------------------
# 写文件副作用检测
# ---------------------------------------------------------------------------


def has_write_side_effect(segment: str) -> bool:
    """检测命令段是否有写文件副作用。

    检测以下模式：

    - 包含 ``>`` 或 ``>>``（输出重定向）
    - 包含 ``tee ``（tee 命令写入文件）
    - 包含 ``dd of=``（dd 直接写入文件）

    Args:
        segment: 命令段字符串。

    Returns:
        存在写文件副作用时返回 ``True``，否则返回 ``False``。
    """
    if not segment or not segment.strip():
        return False

    # 输出重定向（注意不能误匹配 >> 中的 > 已包含，用 \b 避免匹配 2>&1 等）
    if re.search(r"(?:^|\s)>>?(?:\s|$)", segment):
        return True

    # tee 命令
    if re.search(r"\btee\b", segment):
        return True

    # dd of=
    if re.search(r"\bdd\b.*\bof=", segment):
        return True

    return False


# ---------------------------------------------------------------------------
# 危险环境变量检测
# ---------------------------------------------------------------------------

_DANGEROUS_ENV_VARS: frozenset[str] = frozenset({
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH",
    "PERL5LIB",
    "RUBYLIB",
    "PROMPT_COMMAND",
})

# 匹配 VAR=value 格式（VAR 为字母下划线开头、可含数字和下划线）
_ENV_ASSIGN_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\S+")

# 匹配 env 子命令中的 VAR=value 模式
_ENV_CMD_PATTERN = re.compile(r"\benv\s+.*\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\S+", re.DOTALL)


def has_unsafe_env(segment: str) -> bool:
    """检测是否包含危险环境变量注入。

    危险环境变量包括：

    - ``LD_PRELOAD=`` / ``LD_PRELOAD =`` → 可注入共享库
    - ``LD_LIBRARY_PATH=`` → 可改变库搜索路径
    - ``DYLD_INSERT_LIBRARIES=`` → macOS 动态库注入
    - ``PYTHONPATH=`` → 可加载恶意模块
    - ``PERL5LIB=`` → Perl 库路径劫持
    - ``RUBYLIB=`` → Ruby 库路径劫持
    - ``PROMPT_COMMAND=`` → Bash 提示符命令注入

    同时检测 ``env`` 子命令中的 ``VAR=value`` 模式。

    Args:
        segment: 命令段字符串。

    Returns:
        包含危险环境变量注入时返回 ``True``，否则返回 ``False``。
    """
    if not segment or not segment.strip():
        return False

    # 检测 VAR=value 格式
    for match in _ENV_ASSIGN_PATTERN.finditer(segment):
        var_name = match.group(1)
        if var_name in _DANGEROUS_ENV_VARS:
            return True

    # 检测 env 子命令中的变量赋值
    for match in _ENV_CMD_PATTERN.finditer(segment):
        var_name = match.group(1)
        if var_name in _DANGEROUS_ENV_VARS:
            return True

    return False


# ---------------------------------------------------------------------------
# 不可静态分析的动态 Shell 执行检测
# ---------------------------------------------------------------------------

# bash -c 或 sh -c 后跟 $ 开头的变量引用
_OPAQUE_BASH_SH_C_PATTERN = re.compile(
    r"\b(?:bash|sh)\s+-c\s+['\"$]?\s*\$[\w@{}]"
)


def is_opaque_shell(segment: str) -> bool:
    """检测是否为不可静态分析的动态 Shell 执行。

    检测以下不可静态分析的命令模式：

    - 包含 ``eval ``（动态执行任意字符串）
    - ``bash -c`` 或 ``sh -c`` 后跟以 ``$`` 开头的变量引用
    - 包含 ``exec ``（替换当前进程）
    - ``source `` 后跟变量引用

    Args:
        segment: 命令段字符串。

    Returns:
        存在不可静态分析的动态执行时返回 ``True``，否则返回 ``False``。
    """
    if not segment or not segment.strip():
        return False

    # eval（以空白开头或出现在管道/逻辑连接之后）
    if re.search(r"\beval\s+", segment):
        return True

    # bash -c / sh -c 后跟 $VAR、${VAR}、$@ 等变量引用
    if _OPAQUE_BASH_SH_C_PATTERN.search(segment):
        return True

    # exec（独立命令，不是 find -exec）
    if re.search(r"(?:^|[;|&])\s*exec\s+", segment):
        return True

    # source 后跟变量引用
    if re.search(r"\bsource\s+\$", segment):
        return True

    return False


# ---------------------------------------------------------------------------
# 外部可执行文件触发风险检测
# ---------------------------------------------------------------------------

_GIT_HOOK_SUBCOMMANDS: frozenset[str] = frozenset({
    "commit",
    "rebase",
    "am",
    "merge",
    "cherry-pick",
})


def has_exec_risk(segment: str) -> bool:
    """检测命令是否可能触发 git hooks 等外部可执行文件。

    检测以下风险模式：

    - 命令根是 ``git`` 且子命令在 ``commit``、``rebase``、``am``、
      ``merge``、``cherry-pick`` 中 → 可能触发 git hooks
    - 包含 ``npm run`` → 可能通过 package.json scripts 执行任意代码
    - 包含 ``make`` → 可能通过 Makefile target 执行任意代码

    Args:
        segment: 命令段字符串。

    Returns:
        存在外部可执行文件触发风险时返回 ``True``，否则返回 ``False``。
    """
    if not segment or not segment.strip():
        return False

    tokens = _simple_tokenize(segment)
    if not tokens:
        return False

    root = tokens[0]

    # git 的子命令可能触发 hooks
    if root == "git" and len(tokens) >= 2:
        subcommand = tokens[1]
        if subcommand in _GIT_HOOK_SUBCOMMANDS:
            return True

    # npm run 或 npm test（test 也是 scripts）
    if root == "npm" and len(tokens) >= 2:
        if tokens[1] in ("run", "test", "start", "exec"):
            return True

    # npx 总是执行外部包
    if root == "npx":
        return True

    # make
    if root == "make":
        return True

    return False


# ===================================================================
# 综合安全底线评估
# ===================================================================


def evaluate_safety_floors(command: str) -> dict:
    """综合评估一条命令的四条安全底线。

    流程：
    1. 调用 ``extract_segments`` 将命令拆分为独立段
    2. 每个段依次检查四条底线
    3. 汇总所有触发的底线

    四条底线检查顺序：

    - ``write_side_effect``：是否有写文件副作用（:func:`has_write_side_effect`）
    - ``unsafe_env``：是否有危险环境变量注入（:func:`has_unsafe_env`）
    - ``opaque_shell``：是否不可静态分析（:func:`is_opaque_shell`）
    - ``exec_risk``：是否可能触发外部可执行文件（:func:`has_exec_risk`）

    Args:
        command: 待评估的 Shell 命令字符串。

    Returns:
        包含以下字段的字典：

        - ``any_floor_triggered``：是否有任何底线被触发
        - ``floors``：触发的底线列表，每项包含 ``segment``、``floor``、
          ``reason`` 字段
    """
    segments = extract_segments(command)
    floors: list[dict] = []

    for segment in segments:
        seg = segment.strip()
        if not seg:
            continue

        if has_write_side_effect(seg):
            floors.append({
                "segment": seg,
                "floor": "write_side_effect",
                "reason": "检测到写文件副作用（重定向、tee 或 dd of=）",
            })

        if has_unsafe_env(seg):
            floors.append({
                "segment": seg,
                "floor": "unsafe_env",
                "reason": "检测到危险环境变量注入",
            })

        if is_opaque_shell(seg):
            floors.append({
                "segment": seg,
                "floor": "opaque_shell",
                "reason": "检测到不可静态分析的动态 Shell 执行",
            })

        if has_exec_risk(seg):
            floors.append({
                "segment": seg,
                "floor": "exec_risk",
                "reason": "检测到可能触发外部可执行文件（git hooks / npm scripts / make）",
            })

    return {
        "any_floor_triggered": len(floors) > 0,
        "floors": floors,
    }


# ===================================================================
# 内部辅助函数
# ===================================================================


def _simple_tokenize(segment: str) -> list[str]:
    """简单按空白切分命令段为 token 列表。

    使用引号感知的状态机，确保引号内的空白不被拆分。
    """
    tokens: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(segment)

    while i < n:
        ch = segment[i]
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
        if ch == "\\" and (in_double or not in_single):
            current.append(ch)
            i += 1
            if i < n:
                current.append(segment[i])
            i += 1
            continue
        if not in_single and not in_double and ch in (" ", "\t", "\n"):
            if current:
                tokens.append("".join(current))
                current = []
            i += 1
            continue
        current.append(ch)
        i += 1

    if current:
        tokens.append("".join(current))
    return tokens
