from __future__ import annotations

from pkgutil import extend_path
from pathlib import Path

__path__ = extend_path(__path__, __name__)

_src = Path(__file__).resolve().parent.parent / "src" / "demi"
if _src.exists():
    __path__.append(str(_src))
