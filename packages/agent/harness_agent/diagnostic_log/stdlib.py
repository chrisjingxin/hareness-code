"""受控 stdlib Handler：只拦截 harness_agent.*，绝不调用 getMessage()。"""

from __future__ import annotations

import logging
from typing import Any

_HANDLER_ATTR = "_harness_diagnostic_stdlib_handler"


class DiagnosticStdlibHandler(logging.Handler):
    """吞掉 harness_agent stdlib 记录，禁止 format string 进入 Diagnostic Log。"""

    def emit(self, record: logging.LogRecord) -> None:
        """只读取 logger 名、级别和异常类型；禁止 getMessage()。"""
        if not str(record.name).startswith("harness_agent"):
            return
        _ = record.name
        _ = record.levelno
        if record.exc_info and record.exc_info[0] is not None:
            _ = record.exc_info[0].__name__


def install_harness_stdlib_handler(_log: Any | None = None) -> DiagnosticStdlibHandler:
    """把受控 Handler 装到 harness_agent logger，重复安装时复用已有实例。"""
    logger = logging.getLogger("harness_agent")
    existing = getattr(logger, _HANDLER_ATTR, None)
    if isinstance(existing, DiagnosticStdlibHandler):
        return existing
    handler = DiagnosticStdlibHandler()
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    setattr(logger, _HANDLER_ATTR, handler)
    return handler
