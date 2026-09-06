"""L8: the POSIX-only ``resource`` module must not be imported at module top."""
from __future__ import annotations

import importlib.util


def test_resource_not_imported_at_module_top():
    from core.services import models as m
    # The import is now lazy (inside _peak_rss_mb), so the module must not
    # bind a top-level ``resource`` name -- importing the module on a
    # non-POSIX platform must not crash.
    assert not hasattr(m, "resource")


def test_peak_rss_mb_is_platform_safe():
    from core.services import models as m
    val = m._peak_rss_mb()
    if importlib.util.find_spec("resource") is not None:
        # POSIX: a real, positive peak-RSS reading in MB
        assert isinstance(val, float) and val > 0
    else:  # non-POSIX (e.g. Windows): graceful None fallback
        assert val is None
