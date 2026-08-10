"""ZC-133 评测命令行：mock 默认运行，真实模型必须显式 opt-in。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .runner import CANDIDATE_SPECS, CandidateSpec, run_mock_evaluation, run_real_evaluation, write_report


def _positive_int(value: str) -> int:
    """解析正整数参数。"""

    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """创建评测命令参数解析器。"""

    parser = argparse.ArgumentParser(description="运行 ZC-133 合成 fixture 文件编辑评测")
    parser.add_argument(
        "--real-model",
        action="store_true",
        help="显式调用实际企业模型 Profile；默认只运行 mock replay",
    )
    parser.add_argument("--profile", help="真实模型 Profile ID；仅 --real-model 可用")
    parser.add_argument("--config", type=Path, dest="config_path", help="可选的显式用户配置路径")
    parser.add_argument("--home", type=Path, help="可选的用户配置根目录，测试时使用")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="评测运行的逻辑 workspace")
    parser.add_argument("--repetitions", type=_positive_int, default=3, help="每个 fixture/candidate 的重复次数")
    parser.add_argument(
        "--candidate",
        choices=("all",) + tuple(spec.name for spec in CANDIDATE_SPECS),
        default="all",
        help="默认比较全部候选；指定单候选时仍保留 exact-string 作为安全基线",
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="去敏 JSON/Markdown 报告输出目录")
    return parser


def _selected_candidates(name: str) -> tuple[CandidateSpec, ...]:
    """选择候选，并始终保留 exact-string 基线供门槛比较。"""

    if name == "all":
        return CANDIDATE_SPECS
    return tuple(spec for spec in CANDIDATE_SPECS if spec.name in {"exact-string", name})


def main(argv: list[str] | None = None) -> int:
    """运行 mock 或显式 opt-in 真实模型评测。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.real_model and not args.profile:
        parser.error("--real-model 必须同时提供 --profile <id>")
    if not args.real_model and args.profile:
        parser.error("--profile 仅可与 --real-model 一起使用")
    candidates = _selected_candidates(args.candidate)
    if args.real_model:
        report = asyncio.run(
            run_real_evaluation(
                profile_id=args.profile,
                workspace=args.workspace,
                home=args.home,
                config_path=args.config_path,
                repetitions=args.repetitions,
                candidates=candidates,
            )
        )
    else:
        report = run_mock_evaluation(repetitions=args.repetitions, candidates=candidates)
    json_path, markdown_path = write_report(report, args.output_dir)
    print(f"mode={report.mode} decision={report.decision} fixture_count={report.fixture_count}")
    print(f"json={json_path}")
    print(f"markdown={markdown_path}")
    return 0
