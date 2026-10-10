import json
import logging

import pytest

from app import config
from app.logging_config import JsonFormatter, TextFormatter
from app.services.cleanup import CleanupLoop


def _record(**extra):
    rec = logging.LogRecord("app.test", logging.INFO, __file__, 1, "Job started", (), None)
    rec.__dict__.update(extra)
    return rec


def test_json_log_line_contains_context_fields():
    line = json.loads(JsonFormatter().format(_record(job_id="abc123", certificates=5)))
    assert line["message"] == "Job started" and line["level"] == "INFO"
    assert line["job_id"] == "abc123" and line["certificates"] == 5 and line["time"]


def test_text_log_line_contains_context_fields():
    line = TextFormatter("%(levelname)s %(message)s").format(_record(job_id="abc123"))
    assert line == "INFO Job started [job_id=abc123]"


def test_invalid_integer_setting_fails_fast_with_clear_message(monkeypatch):
    monkeypatch.setenv("MAX_RECIPIENTS", "lots")
    with pytest.raises(RuntimeError, match="MAX_RECIPIENTS must be an integer"):
        config._int("MAX_RECIPIENTS", 5000)


def test_cleanup_loop_disabled_when_interval_is_zero(monkeypatch):
    monkeypatch.setattr(config, "CLEANUP_INTERVAL_MINUTES", 0)
    loop = CleanupLoop()
    loop.start()
    assert loop._thread is None
