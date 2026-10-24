"""Narrow Windows cleanup workaround for genlayer-test's Direct Mode loader."""

import os
import tempfile

_original_unlink = os.unlink
_deferred_paths = []
_temp_root = os.path.abspath(tempfile.gettempdir()).casefold()


def _unlink_with_deferred_direct_mode_cleanup(path, *args, **kwargs):
    try:
        return _original_unlink(path, *args, **kwargs)
    except PermissionError as error:
        candidate = os.path.abspath(os.fspath(path))
        is_direct_mode_temp = (
            os.name == "nt"
            and getattr(error, "winerror", None) == 32
            and os.path.dirname(candidate).casefold() == _temp_root
            and os.path.basename(candidate).startswith("tmp")
        )
        if not is_direct_mode_temp:
            raise
        if candidate not in _deferred_paths:
            _deferred_paths.append(candidate)
        return None


if os.name == "nt":
    os.unlink = _unlink_with_deferred_direct_mode_cleanup


def pytest_sessionfinish(session, exitstatus):
    if not os.name == "nt":
        return
    os.unlink = _original_unlink
    for path in _deferred_paths:
        try:
            _original_unlink(path)
        except FileNotFoundError:
            pass
