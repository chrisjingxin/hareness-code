"""harness_agent stdlib Handler 不得把 record.getMessage() 写入 Diagnostic Log。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from harness_agent.diagnostic_log.runtime import DiagnosticSettings, create_diagnostic_log
from harness_agent.diagnostic_log.stdlib import install_harness_stdlib_handler


CANARY = "CANARY_HC163_STDLIB_GETMESSAGE"


def _scan_jsonl(root: Path) -> str:
    parts: list[str] = []
    for path in root.rglob("*.jsonl"):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_stdlib_handler_never_writes_getmessage_canary(tmp_path: Path) -> None:
    """harness_agent logger 的 format string 不得出现在 JSONL。"""
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path / "logs",
        settings=DiagnosticSettings(level="debug"),
    )
    install_harness_stdlib_handler(log)
    logger = logging.getLogger("harness_agent.extensions.mcp")
    logger.warning("secret path %s", CANARY)
    try:
        raise RuntimeError(CANARY)
    except RuntimeError:
        logger.exception("failed with %s", CANARY)
    await lifecycle.close()
    dumped = _scan_jsonl(tmp_path / "logs")
    assert CANARY not in dumped


@pytest.mark.asyncio
async def test_stdlib_handler_does_not_capture_third_party_loggers(tmp_path: Path) -> None:
    """第三方 logger 原文不进入 Diagnostic Log。"""
    log, lifecycle = create_diagnostic_log(
        component="agent",
        project_fingerprint="a" * 64,
        root=tmp_path / "logs",
        settings=DiagnosticSettings(level="debug"),
    )
    install_harness_stdlib_handler(log)
    logging.getLogger("langchain_mcp_adapters.client").error(CANARY)
    await lifecycle.close()
    dumped = _scan_jsonl(tmp_path / "logs")
    assert CANARY not in dumped
