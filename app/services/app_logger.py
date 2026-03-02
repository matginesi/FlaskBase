from __future__ import annotations

import functools
import logging
import sys
import threading
import warnings
from typing import Any, Callable

from flask import has_request_context, request
from werkzeug.exceptions import HTTPException


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(name or "app")


def log_event(
    level: int | str,
    event_type: str,
    message: str,
    *,
    logger: logging.Logger | None = None,
    context: dict[str, Any] | None = None,
    exc_info: Any = None,
) -> None:
    target = logger or get_logger()
    level_no = getattr(logging, str(level).upper(), level) if isinstance(level, str) else int(level)
    target.log(
        level_no,
        message,
        extra={"event_type": str(event_type or "app.event")[:80], "context": dict(context or {})},
        exc_info=exc_info,
    )


def log_warning(event_type: str, message: str, *, logger: logging.Logger | None = None, context: dict[str, Any] | None = None) -> None:
    log_event(logging.WARNING, event_type, message, logger=logger, context=context)


def log_error(
    event_type: str,
    message: str,
    *,
    logger: logging.Logger | None = None,
    context: dict[str, Any] | None = None,
    exc_info: Any = None,
) -> None:
    log_event(logging.ERROR, event_type, message, logger=logger, context=context, exc_info=exc_info)


def log_exception(
    exc: BaseException,
    *,
    event_type: str = "exception",
    message: str | None = None,
    logger: logging.Logger | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    ctx = dict(context or {})
    ctx.setdefault("exception_type", type(exc).__name__)
    if has_request_context():
        ctx.setdefault("path", request.path)
        ctx.setdefault("method", request.method)
        ctx.setdefault("endpoint", request.endpoint)
    log_event(
        logging.ERROR,
        event_type,
        message or str(exc) or type(exc).__name__,
        logger=logger,
        context=ctx,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def wrap_callable(func: Callable[..., Any], *, logger: logging.Logger | None = None, event_type: str | None = None) -> Callable[..., Any]:
    if getattr(func, "_app_logger_wrapped", False):
        return func

    target_logger = logger or get_logger(getattr(func, "__module__", "app"))
    target_event = event_type or f"{getattr(func, '__module__', 'app')}.{getattr(func, '__name__', 'call')}.failed"

    @functools.wraps(func)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            setattr(exc, "_app_logged", True)
            log_exception(
                exc,
                event_type=target_event,
                message=f"{getattr(func, '__name__', 'call')} failed",
                logger=target_logger,
                context={"callable": getattr(func, "__qualname__", getattr(func, "__name__", "call"))},
            )
            raise

    setattr(_wrapped, "_app_logger_wrapped", True)
    return _wrapped


def instrument_app_views(app: Any) -> None:
    for endpoint, view_func in list((app.view_functions or {}).items()):
        if not callable(view_func):
            continue
        if endpoint == "static" or endpoint.endswith(".static"):
            continue
        app.view_functions[endpoint] = wrap_callable(
            view_func,
            logger=get_logger(getattr(view_func, "__module__", endpoint)),
            event_type=f"view.{endpoint}.failed",
        )


def install_runtime_logging_hooks() -> None:
    logging.captureWarnings(True)
    warnings.simplefilter("default")

    def _sys_excepthook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log_event(
            logging.CRITICAL,
            "runtime.unhandled_exception",
            str(exc) or exc_type.__name__,
            logger=get_logger("runtime"),
            context={"exception_type": exc_type.__name__, "hook": "sys.excepthook"},
            exc_info=(exc_type, exc, tb),
        )
        sys.__excepthook__(exc_type, exc, tb)

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        log_event(
            logging.CRITICAL,
            "runtime.thread_exception",
            str(args.exc_value) or type(args.exc_value).__name__,
            logger=get_logger("runtime.thread"),
            context={
                "thread_name": getattr(args.thread, "name", None),
                "thread_ident": getattr(args.thread, "ident", None),
                "exception_type": type(args.exc_value).__name__,
            },
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_excepthook
    threading.excepthook = _thread_excepthook

    if hasattr(sys, "unraisablehook"):
        def _unraisable_hook(unraisable: Any) -> None:
            exc = getattr(unraisable, "exc_value", RuntimeError("unraisable"))
            log_event(
                logging.ERROR,
                "runtime.unraisable_exception",
                str(exc) or type(exc).__name__,
                logger=get_logger("runtime.unraisable"),
                context={
                    "exception_type": type(exc).__name__,
                    "object": repr(getattr(unraisable, "object", None))[:300],
                    "err_msg": str(getattr(unraisable, "err_msg", "") or ""),
                },
                exc_info=(getattr(unraisable, "exc_type", type(exc)), exc, getattr(unraisable, "exc_traceback", None)),
            )

        sys.unraisablehook = _unraisable_hook
