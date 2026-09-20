import logging
import unittest

from imapgw.config import RedactingFilter


class RedactionTests(unittest.TestCase):
    def make_logger(self, flt):
        logger = logging.getLogger(f"redaction-test-{id(flt)}")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(logging.Formatter("%(message)s").format(record))

        handler = Capture()
        handler.addFilter(flt)
        logger.addHandler(handler)
        return logger, records

    def test_message_args_and_traceback_are_redacted(self):
        flt = RedactingFilter()
        flt.register("s3cret-value")
        logger, records = self.make_logger(flt)
        logger.info("key is %s", "s3cret-value")
        try:
            raise ValueError("Invalid header value b'Bearer s3cret-value\\r\\nX: 1'")
        except ValueError:
            logger.exception("boom with s3cret-value")
        joined = "\n".join(records)
        self.assertNotIn("s3cret-value", joined)
        self.assertIn("***", joined)
        self.assertIn("ValueError", joined)  # traceback still present, just scrubbed

    def test_reference_counting_keeps_shared_secret_redacted(self):
        flt = RedactingFilter()
        flt.register("shared")
        flt.register("shared")
        flt.unregister("shared")
        logger, records = self.make_logger(flt)
        logger.info("still %s", "shared")
        self.assertEqual(records[-1], "still ***")
        flt.unregister("shared")
        logger.info("now %s", "shared")
        self.assertEqual(records[-1], "now shared")

    def test_no_secrets_registered_is_a_no_op(self):
        flt = RedactingFilter()
        logger, records = self.make_logger(flt)
        logger.info("plain %s", "text")
        self.assertEqual(records, ["plain text"])


class SettingsEnvTests(unittest.TestCase):
    def test_parser_and_timeout_settings_from_environment(self):
        from pathlib import Path

        from imapgw.config import load_settings

        settings = load_settings(
            environ={
                "IMAPGW_MAX_COMMAND_BYTES": "2048",
                "IMAPGW_MAX_LITERALS": "3",
                "IMAPGW_MAX_LINE": "512",
                "IMAPGW_MAX_LITERAL": "1024",
                "IMAPGW_ASSEMBLY_TIMEOUT": "7.5",
                "IMAPGW_WRITE_TIMEOUT": "2",
            },
            dotenv=Path("/nonexistent/.env"),
        )
        self.assertEqual(
            (
                settings.max_command_bytes,
                settings.max_literals,
                settings.max_line,
                settings.max_literal,
            ),
            (2048, 3, 512, 1024),
        )
        self.assertEqual((settings.assembly_timeout, settings.write_timeout), (7.5, 2.0))
        with self.assertRaises(ValueError):
            load_settings(environ={"IMAPGW_MAX_LITERALS": "many"}, dotenv=None)
