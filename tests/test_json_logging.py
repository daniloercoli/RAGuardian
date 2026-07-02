"""Tests for JSON structured logging formatter."""
import json
import logging
import pytest
from app.utils.logging_config import JsonLogFormatter


class TestJsonLogFormatter:
    def test_basic_format(self):
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test_file.py",
            lineno=42,
            msg="Hello world",
            args=(),
            exc_info=None,
        )
        record.module = "test_module"
        record.funcName = "test_func"
        record.file = "test_file.py"
        record.taskName = ""

        output = json.loads(formatter.format(record))
        assert output["message"] == "Hello world"
        assert output["level"] == "INFO"
        assert output["logger"] == "test.logger"
        assert "timestamp" in output

    def test_extra_fields(self):
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="t.py",
            lineno=1,
            msg="test",
            args=(),
            exc_info=None,
        )
        record.module = "t"
        record.funcName = "f"
        record.file = "t.py"
        record.taskName = ""
        record.custom_key = "custom_value"
        record.custom_number = 42
        record.custom_bool = True

        output = json.loads(formatter.format(record))
        assert output["custom_key"] == "custom_value"
        assert output["custom_number"] == 42
        assert output["custom_bool"] == True

    def test_masks_api_keys_in_message_and_extra_fields(self):
        formatter = JsonLogFormatter()
        raw_key = "rag_1234567890abcdef_deadbeef"
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="t.py",
            lineno=1,
            msg="Using api_key=%s",
            args=(raw_key,),
            exc_info=None,
        )
        record.module = "t"
        record.funcName = "f"
        record.file = "t.py"
        record.taskName = ""
        record.api_key = raw_key

        output = json.loads(formatter.format(record))

        assert raw_key not in output["message"]
        assert output["message"] == "Using api_key=rag_1234...beef"
        assert output["api_key"] == "rag_1234...beef"

    def test_exception_formatting(self):
        formatter = JsonLogFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="t.py",
            lineno=1,
            msg="Error occurred",
            args=(),
            exc_info=exc_info,
        )
        record.module = "t"
        record.funcName = "f"
        record.file = "t.py"
        record.taskName = ""

        output = json.loads(formatter.format(record))
        assert "exception" in output
        assert "ValueError: test error" in output["exception"]

    def test_unicode_in_message(self):
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="t.py",
            lineno=1,
            msg="Test with emoji ðŸŽ‰ and unicode Ã¨Ã¬Ã²",
            args=(),
            exc_info=None,
        )
        record.module = "t"
        record.funcName = "f"
        record.file = "t.py"
        record.taskName = ""

        output = json.loads(formatter.format(record))
        assert output["message"] == "Test with emoji ðŸŽ‰ and unicode Ã¨Ã¬Ã²"

    def test_standard_attrs_not_leaked(self):
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="/path/to/file.py",
            lineno=99,
            msg="test message",
            args=(),
            exc_info=None,
        )
        record.module = "mymodule"
        record.funcName = "my_func"
        record.file = "file.py"
        record.taskName = "task"

        output = json.loads(formatter.format(record))
        # These should NOT be in the output
        for key in ("args", "levelno", "pathname"):
            assert key not in output
        # These SHOULD be in the output
        assert output["level"] == "INFO"
        assert output["logger"] == "test.logger"
        assert output["message"] == "test message"
