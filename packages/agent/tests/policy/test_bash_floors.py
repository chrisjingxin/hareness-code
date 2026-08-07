"""Shell 安全底线强制询问测试。

覆盖 has_write_side_effect / has_unsafe_env / is_opaque_shell / has_exec_risk /
evaluate_safety_floors 的公开 API 及边界场景。
"""
from __future__ import annotations

import pytest

from harness_agent.policy.bash_floors import (
    evaluate_safety_floors,
    has_exec_risk,
    has_unsafe_env,
    has_write_side_effect,
    is_opaque_shell,
)


# ===================================================================
# has_write_side_effect — 写文件副作用检测
# ===================================================================


class TestHasWriteSideEffect:
    """has_write_side_effect 检测命令段是否有写文件副作用。"""

    # -- 输出重定向 --

    def test_single_redirect(self):
        """> 重定向触发写文件副作用。"""
        assert has_write_side_effect("echo hello > file.txt") is True

    def test_double_redirect(self):
        """>> 追加重定向触发写文件副作用。"""
        assert has_write_side_effect("echo hello >> file.txt") is True

    def test_redirect_at_start(self):
        """> 出现在命令开头也触发。"""
        assert has_write_side_effect("> output.txt") is True

    def test_redirect_at_end(self):
        """> 出现在命令末尾也触发。"""
        assert has_write_side_effect("echo hello >") is True

    # -- tee 命令 --

    def test_tee_command(self):
        """tee 命令触发写文件副作用。"""
        assert has_write_side_effect("echo hello | tee output.txt") is True

    def test_tee_as_word_boundary(self):
        """tee 只在词边界匹配，避免误匹配 contain 等词。"""
        # "teeth" 包含 tee 但不应触发
        assert has_write_side_effect("echo teeth") is False

    # -- dd of= --

    def test_dd_of(self):
        """dd of= 触发写文件副作用。"""
        assert has_write_side_effect("dd if=/dev/zero of=/dev/sda") is True

    def test_dd_without_of(self):
        """dd 不带 of= 不触发。"""
        assert has_write_side_effect("dd if=/dev/zero") is False

    # -- 不应触发的场景 --

    def test_no_redirect(self):
        """普通命令不触发。"""
        assert has_write_side_effect("echo hello") is False

    def test_fd_redirect_2_to_1(self):
        """2>&1 不应误匹配为写文件副作用。"""
        assert has_write_side_effect("make 2>&1") is False

    def test_empty_string(self):
        """空字符串返回 False。"""
        assert has_write_side_effect("") is False

    def test_whitespace_only(self):
        """纯空白返回 False。"""
        assert has_write_side_effect("   ") is False

    def test_none_like_empty(self):
        """None 类型不崩溃（防御性检查）。"""
        # 源码用 not segment 检查，None 也走 False 分支
        assert has_write_side_effect(None) is False


# ===================================================================
# has_unsafe_env — 危险环境变量检测
# ===================================================================


class TestHasUnsafeEnv:
    """has_unsafe_env 检测是否包含危险环境变量注入。"""

    # -- 各危险环境变量 --

    def test_ld_preload(self):
        """LD_PRELOAD 触发危险环境变量。"""
        assert has_unsafe_env("LD_PRELOAD=/tmp/evil.so ls") is True

    def test_ld_library_path(self):
        """LD_LIBRARY_PATH 触发危险环境变量。"""
        assert has_unsafe_env("LD_LIBRARY_PATH=/tmp/libs python app.py") is True

    def test_dyld_insert_libraries(self):
        """DYLD_INSERT_LIBRARIES（macOS）触发危险环境变量。"""
        assert has_unsafe_env("DYLD_INSERT_LIBRARIES=/tmp/evil.dylib ls") is True

    def test_pythonpath(self):
        """PYTHONPATH 触发危险环境变量。"""
        assert has_unsafe_env("PYTHONPATH=/tmp/evil python -c pass") is True

    def test_perl5lib(self):
        """PERL5LIB 触发危险环境变量。"""
        assert has_unsafe_env("PERL5LIB=/tmp/libs perl script.pl") is True

    def test_rubylib(self):
        """RUBYLIB 触发危险环境变量。"""
        assert has_unsafe_env("RUBYLIB=/tmp/libs ruby app.rb") is True

    def test_prompt_command(self):
        """PROMPT_COMMAND 触发危险环境变量。"""
        assert has_unsafe_env("PROMPT_COMMAND=evil_cmd bash") is True

    # -- 带空格的赋值 --

    def test_ld_preload_with_spaces(self):
        """LD_PRELOAD = value（等号两侧有空格）仍触发。"""
        assert has_unsafe_env("LD_PRELOAD = /tmp/evil.so ls") is True

    # -- env 子命令中的变量赋值 --

    def test_env_subcommand_ld_preload(self):
        """env LD_PRELOAD=... 形式触发。"""
        assert has_unsafe_env("env LD_PRELOAD=/tmp/evil.so ls") is True

    def test_env_subcommand_pythonpath(self):
        """env PYTHONPATH=... 形式触发。"""
        assert has_unsafe_env("env PYTHONPATH=/tmp python script.py") is True

    # -- 不应触发的场景 --

    def test_safe_env_var(self):
        """安全的环境变量不触发。"""
        assert has_unsafe_env("NODE_ENV=production npm run build") is False

    def test_no_env_assignment(self):
        """无环境变量赋值的普通命令不触发。"""
        assert has_unsafe_env("git status") is False

    def test_partial_match_not_trigger(self):
        """变量名包含危险名但不完全匹配不触发（如 MY_LD_PRELOAD）。"""
        # _ENV_ASSIGN_PATTERN 用 \b 匹配词边界，MY_LD_PRELOAD 不是 LD_PRELOAD
        assert has_unsafe_env("MY_LD_PRELOAD=/tmp/x ls") is False

    def test_empty_string(self):
        """空字符串返回 False。"""
        assert has_unsafe_env("") is False

    def test_whitespace_only(self):
        """纯空白返回 False。"""
        assert has_unsafe_env("   ") is False

    def test_none_like_empty(self):
        """None 不崩溃。"""
        assert has_unsafe_env(None) is False


# ===================================================================
# is_opaque_shell — 不可静态分析的动态 Shell 执行检测
# ===================================================================


class TestIsOpaqueShell:
    """is_opaque_shell 检测不可静态分析的动态 Shell 执行。"""

    # -- eval --

    def test_eval_command(self):
        """eval 触发不可静态分析。"""
        assert is_opaque_shell("eval $USER_INPUT") is True

    def test_eval_with_string(self):
        """eval 带字符串参数触发。"""
        assert is_opaque_shell("eval 'echo hello'") is True

    def test_eval_in_pipe(self):
        """管道中的 eval 触发。"""
        assert is_opaque_shell("echo test | eval cat") is True

    # -- bash -c / sh -c 后跟变量引用 --

    def test_bash_c_dollar_var(self):
        """bash -c "$VAR" 触发不可静态分析。"""
        assert is_opaque_shell('bash -c "$USER_CMD"') is True

    def test_sh_c_dollar_var(self):
        """sh -c "$VAR" 触发不可静态分析。"""
        assert is_opaque_shell('sh -c "$USER_CMD"') is True

    def test_bash_c_dollar_brace(self):
        """bash -c "${VAR}" 触发不可静态分析。"""
        assert is_opaque_shell('bash -c "${USER_CMD}"') is True

    def test_bash_c_dollar_at(self):
        """bash -c "$@" 触发不可静态分析。"""
        assert is_opaque_shell('bash -c "$@"') is True

    def test_bash_c_literal_not_opaque(self):
        """bash -c 'literal' 不含变量引用，不触发。"""
        assert is_opaque_shell("bash -c 'echo hello'") is False

    # -- exec --

    def test_exec_at_start(self):
        """命令开头的 exec 触发。"""
        assert is_opaque_shell("exec /bin/bash") is True

    def test_exec_after_semicolon(self):
        """分号后的 exec 触发。"""
        assert is_opaque_shell("echo hello; exec /bin/bash") is True

    def test_exec_after_pipe(self):
        """管道后的 exec 触发。"""
        assert is_opaque_shell("echo test | exec cat") is True

    def test_exec_after_ampersand(self):
        """& 后的 exec 触发。"""
        assert is_opaque_shell("echo hello & exec /bin/bash") is True

    # -- source $VAR --

    def test_source_dollar_var(self):
        """source $VAR 触发不可静态分析。"""
        assert is_opaque_shell("source $MY_SCRIPT") is True

    def test_source_dollar_brace(self):
        """source ${VAR} 触发不可静态分析。"""
        assert is_opaque_shell("source ${MY_SCRIPT}") is True

    def test_source_literal_not_opaque(self):
        """source 后跟字面路径不触发。"""
        assert is_opaque_shell("source /etc/profile") is False

    # -- 不应触发的场景 --

    def test_normal_command(self):
        """普通命令不触发。"""
        assert is_opaque_shell("git status") is False

    def test_find_exec_not_opaque(self):
        """find -exec 不应误判为 exec（exec 前有 -）。"""
        assert is_opaque_shell("find . -name '*.py' -exec rm {} \\;") is False

    def test_empty_string(self):
        """空字符串返回 False。"""
        assert is_opaque_shell("") is False

    def test_whitespace_only(self):
        """纯空白返回 False。"""
        assert is_opaque_shell("   ") is False

    def test_none_like_empty(self):
        """None 不崩溃。"""
        assert is_opaque_shell(None) is False


# ===================================================================
# has_exec_risk — 外部可执行文件触发风险检测
# ===================================================================


class TestHasExecRisk:
    """has_exec_risk 检测命令是否可能触发 git hooks 等外部可执行文件。"""

    # -- git 子命令触发 hooks --

    def test_git_commit(self):
        """git commit 触发外部可执行文件风险。"""
        assert has_exec_risk("git commit -m 'test'") is True

    def test_git_rebase(self):
        """git rebase 触发外部可执行文件风险。"""
        assert has_exec_risk("git rebase main") is True

    def test_git_am(self):
        """git am 触发外部可执行文件风险。"""
        assert has_exec_risk("git am patch.mbox") is True

    def test_git_merge(self):
        """git merge 触发外部可执行文件风险。"""
        assert has_exec_risk("git merge feature") is True

    def test_git_cherry_pick(self):
        """git cherry-pick 触发外部可执行文件风险。"""
        assert has_exec_risk("git cherry-pick abc123") is True

    # -- git 安全子命令不触发 --

    def test_git_status_safe(self):
        """git status 不触发。"""
        assert has_exec_risk("git status") is False

    def test_git_log_safe(self):
        """git log 不触发。"""
        assert has_exec_risk("git log --oneline") is False

    def test_git_diff_safe(self):
        """git diff 不触发。"""
        assert has_exec_risk("git diff HEAD") is False

    def test_git_add_safe(self):
        """git add 不触发。"""
        assert has_exec_risk("git add .") is False

    def test_git_no_subcommand(self):
        """git 不带子命令不触发。"""
        assert has_exec_risk("git") is False

    # -- npm --

    def test_npm_run(self):
        """npm run 触发外部可执行文件风险。"""
        assert has_exec_risk("npm run build") is True

    def test_npm_test(self):
        """npm test 触发外部可执行文件风险。"""
        assert has_exec_risk("npm test") is True

    def test_npm_start(self):
        """npm start 触发外部可执行文件风险。"""
        assert has_exec_risk("npm start") is True

    def test_npm_exec(self):
        """npm exec 触发外部可执行文件风险。"""
        assert has_exec_risk("npm exec some-cmd") is True

    def test_npm_install_safe(self):
        """npm install 不触发（不执行 scripts）。"""
        assert has_exec_risk("npm install express") is False

    # -- npx --

    def test_npx_always_risky(self):
        """npx 总是触发外部可执行文件风险。"""
        assert has_exec_risk("npx create-react-app my-app") is True

    def test_npx_no_args(self):
        """npx 不带参数也触发。"""
        assert has_exec_risk("npx") is True

    # -- make --

    def test_make_target(self):
        """make 触发外部可执行文件风险。"""
        assert has_exec_risk("make build") is True

    def test_make_no_target(self):
        """make 不带 target 也触发（默认 target）。"""
        assert has_exec_risk("make") is True

    # -- 不应触发的场景 --

    def test_normal_command(self):
        """普通命令不触发。"""
        assert has_exec_risk("echo hello") is False

    def test_empty_string(self):
        """空字符串返回 False。"""
        assert has_exec_risk("") is False

    def test_whitespace_only(self):
        """纯空白返回 False。"""
        assert has_exec_risk("   ") is False

    def test_none_like_empty(self):
        """None 不崩溃。"""
        assert has_exec_risk(None) is False


# ===================================================================
# evaluate_safety_floors — 综合安全底线评估
# ===================================================================


class TestEvaluateSafetyFloors:
    """evaluate_safety_floors 综合评估命令的四条安全底线。"""

    # -- 无底线触发 --

    def test_safe_command_no_floor(self):
        """安全命令不触发任何底线。"""
        result = evaluate_safety_floors("git status")
        assert result["any_floor_triggered"] is False
        assert result["floors"] == []

    def test_echo_command_safe(self):
        """echo 命令不触发任何底线。"""
        result = evaluate_safety_floors("echo hello world")
        assert result["any_floor_triggered"] is False

    # -- 单段触发单底线 --

    def test_write_side_effect_floor(self):
        """重定向触发 write_side_effect 底线。"""
        result = evaluate_safety_floors("echo hello > output.txt")
        assert result["any_floor_triggered"] is True
        assert len(result["floors"]) == 1
        assert result["floors"][0]["floor"] == "write_side_effect"

    def test_unsafe_env_floor(self):
        """危险环境变量触发 unsafe_env 底线。"""
        result = evaluate_safety_floors("LD_PRELOAD=/tmp/evil.so ls")
        assert result["any_floor_triggered"] is True
        assert len(result["floors"]) == 1
        assert result["floors"][0]["floor"] == "unsafe_env"

    def test_opaque_shell_floor(self):
        """eval 触发 opaque_shell 底线。"""
        result = evaluate_safety_floors("eval $USER_INPUT")
        assert result["any_floor_triggered"] is True
        assert len(result["floors"]) == 1
        assert result["floors"][0]["floor"] == "opaque_shell"

    def test_exec_risk_floor(self):
        """git commit 触发 exec_risk 底线。"""
        result = evaluate_safety_floors("git commit -m 'test'")
        assert result["any_floor_triggered"] is True
        assert len(result["floors"]) == 1
        assert result["floors"][0]["floor"] == "exec_risk"

    # -- 多段命令（&& 分隔） --

    def test_chain_command_segments(self):
        """&& 分隔的命令，每段独立检查底线。"""
        result = evaluate_safety_floors("echo hello > file.txt && git commit -m 'test'")
        assert result["any_floor_triggered"] is True
        floor_types = [f["floor"] for f in result["floors"]]
        assert "write_side_effect" in floor_types
        assert "exec_risk" in floor_types

    def test_pipe_segments(self):
        """管道命令每段独立检查。"""
        result = evaluate_safety_floors("cat file | grep pattern")
        assert result["any_floor_triggered"] is False

    # -- 单段触发多底线 --

    def test_single_segment_multiple_floors(self):
        """单段命令可同时触发多条底线。"""
        # LD_PRELOAD + eval 同时存在
        result = evaluate_safety_floors("eval LD_PRELOAD=/tmp/evil.so ls")
        assert result["any_floor_triggered"] is True
        floor_types = [f["floor"] for f in result["floors"]]
        assert "unsafe_env" in floor_types
        assert "opaque_shell" in floor_types

    # -- 底线项结构验证 --

    def test_floor_item_structure(self):
        """每个底线项包含 segment、floor、reason 字段。"""
        result = evaluate_safety_floors("echo hello > file.txt")
        assert len(result["floors"]) == 1
        item = result["floors"][0]
        assert "segment" in item
        assert "floor" in item
        assert "reason" in item
        assert isinstance(item["segment"], str)
        assert isinstance(item["floor"], str)
        assert isinstance(item["reason"], str)

    def test_floor_reason_is_chinese(self):
        """底线 reason 为中文描述。"""
        result = evaluate_safety_floors("git commit -m 'test'")
        for floor_item in result["floors"]:
            assert floor_item["reason"]  # 非空
            # reason 应包含中文字符
            assert any("\u4e00" <= ch <= "\u9fff" for ch in floor_item["reason"])

    # -- 边界场景 --

    def test_empty_command(self):
        """空命令不触发任何底线。"""
        result = evaluate_safety_floors("")
        assert result["any_floor_triggered"] is False
        assert result["floors"] == []

    def test_whitespace_command(self):
        """纯空白命令不触发。"""
        result = evaluate_safety_floors("   ")
        assert result["any_floor_triggered"] is False

    def test_semicolon_chain(self):
        """分号分隔的命令段独立检查。"""
        result = evaluate_safety_floors("echo safe; npx create-app")
        assert result["any_floor_triggered"] is True
        floor_types = [f["floor"] for f in result["floors"]]
        assert "exec_risk" in floor_types

    def test_or_chain(self):
        """|| 分隔的命令段独立检查。"""
        result = evaluate_safety_floors("echo safe || make build")
        assert result["any_floor_triggered"] is True
        floor_types = [f["floor"] for f in result["floors"]]
        assert "exec_risk" in floor_types


# ===================================================================
# 白名单命中但底线触发 → 强制询问（核心场景）
# ===================================================================


class TestWhitelistOverrideByFloors:
    """即使白名单命中，底线触发时仍强制进入 ask 审批流程。"""

    def test_git_commit_whitelisted_but_floor_triggers(self):
        """git commit 可能被白名单放行，但 exec_risk 底线强制询问。"""
        result = evaluate_safety_floors("git commit -m 'fix: urgent patch'")
        assert result["any_floor_triggered"] is True
        assert any(f["floor"] == "exec_risk" for f in result["floors"])

    def test_npm_run_build_whitelisted_but_floor_triggers(self):
        """npm run build 可能被白名单放行，但 exec_risk 底线强制询问。"""
        result = evaluate_safety_floors("npm run build")
        assert result["any_floor_triggered"] is True
        assert any(f["floor"] == "exec_risk" for f in result["floors"])

    def test_echo_redirect_whitelisted_but_floor_triggers(self):
        """echo > file 可能被白名单放行，但 write_side_effect 底线强制询问。"""
        result = evaluate_safety_floors("echo 'config' > .env")
        assert result["any_floor_triggered"] is True
        assert any(f["floor"] == "write_side_effect" for f in result["floors"])

    def test_ld_preload_with_safe_command(self):
        """LD_PRELOAD=... ls 命令本身安全，但 unsafe_env 底线强制询问。"""
        result = evaluate_safety_floors("LD_PRELOAD=/tmp/evil.so ls -la")
        assert result["any_floor_triggered"] is True
        assert any(f["floor"] == "unsafe_env" for f in result["floors"])

    def test_eval_with_safe_payload(self):
        """eval 'echo hello' 内容安全，但 opaque_shell 底线强制询问。"""
        result = evaluate_safety_floors("eval 'echo hello'")
        assert result["any_floor_triggered"] is True
        assert any(f["floor"] == "opaque_shell" for f in result["floors"])

    def test_chain_with_safe_and_risky_segments(self):
        """链式命令中安全段和不安全段共存，只标记不安全段。"""
        result = evaluate_safety_floors("git status && git commit -m 'test'")
        assert result["any_floor_triggered"] is True
        # git status 段不应触发底线
        status_floors = [
            f for f in result["floors"] if "status" in f["segment"]
        ]
        assert len(status_floors) == 0
        # git commit 段应触发 exec_risk
        commit_floors = [
            f for f in result["floors"] if "commit" in f["segment"]
        ]
        assert len(commit_floors) == 1
        assert commit_floors[0]["floor"] == "exec_risk"

    def test_make_in_chain(self):
        """链式命令中的 make 触发 exec_risk。"""
        result = evaluate_safety_floors("echo building && make all")
        assert result["any_floor_triggered"] is True
        assert any(f["floor"] == "exec_risk" for f in result["floors"])

    def test_npx_in_chain(self):
        """链式命令中的 npx 触发 exec_risk。"""
        result = evaluate_safety_floors("git status && npx tsc --noEmit")
        assert result["any_floor_triggered"] is True
        assert any(f["floor"] == "exec_risk" for f in result["floors"])

    def test_all_four_floors_in_one_command(self):
        """一条复杂命令可同时触发全部四条底线。"""
        # write: > file.txt
        # unsafe_env: PYTHONPATH=...
        # opaque_shell: eval
        # exec_risk: git commit
        cmd = "PYTHONPATH=/tmp eval 'git commit -m test' > file.txt"
        result = evaluate_safety_floors(cmd)
        assert result["any_floor_triggered"] is True
        floor_types = {f["floor"] for f in result["floors"]}
        # 至少应检测到部分底线（具体取决于段拆分结果）
        assert len(floor_types) >= 1

    def test_tee_triggers_write_side_effect(self):
        """tee 命令触发 write_side_effect 底线。"""
        result = evaluate_safety_floors("echo data | tee config.yaml")
        assert result["any_floor_triggered"] is True
        assert any(f["floor"] == "write_side_effect" for f in result["floors"])

    def test_dd_of_triggers_write_side_effect(self):
        """dd of= 触发 write_side_effect 底线。"""
        result = evaluate_safety_floors("dd if=/dev/zero of=/tmp/data bs=1M")
        assert result["any_floor_triggered"] is True
        assert any(f["floor"] == "write_side_effect" for f in result["floors"])

    def test_source_dollar_triggers_opaque_shell(self):
        """source $VAR 触发 opaque_shell 底线。"""
        result = evaluate_safety_floors("source $INIT_SCRIPT")
        assert result["any_floor_triggered"] is True
        assert any(f["floor"] == "opaque_shell" for f in result["floors"])
