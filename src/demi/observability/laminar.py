from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

try:
    from lmnr import Laminar
    from lmnr import observe as _lmnr_observe
except Exception:  # noqa: BLE001
    Laminar = None
    _lmnr_observe = None

_laminar_initialized = False
_laminar_unavailable_logged = False
_laminar_init_error_logged = False


def observe(*args: Any, **kwargs: Any):
    if _lmnr_observe is None:
        if args and len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def _noop(func: Callable[..., Any]) -> Callable[..., Any]:
            return func

        return _noop
    return _lmnr_observe(*args, **kwargs)


def initialize_laminar(project_api_key: str | None = None) -> bool:
    global _laminar_initialized
    global _laminar_unavailable_logged
    global _laminar_init_error_logged

    if _laminar_initialized:
        return True

    key = str(project_api_key or os.getenv("LMNR_PROJECT_API_KEY") or "").strip()
    if not key:
        return False

    if Laminar is None:
        if not _laminar_unavailable_logged:
            logger.warning(
                "LMNR_PROJECT_API_KEY is set but lmnr is not installed; Laminar tracing disabled."
            )
            _laminar_unavailable_logged = True
        return False

    try:
        Laminar.initialize(project_api_key=key)
        _laminar_initialized = True
        return True
    except Exception:  # noqa: BLE001
        if not _laminar_init_error_logged:
            logger.exception("Failed to initialize Laminar; tracing disabled.")
            _laminar_init_error_logged = True
        return False
