"""Constructed key configuration checks; no real keys or model requests."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from threadplan_host.model_config import openrouter_key


class ModelConfigChecks(unittest.TestCase):
    def test_missing_key_is_missing_not_a_fake_or_cli_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            self.assertIsNone(openrouter_key(Path(directory)))

    def test_explicit_local_literal_is_not_shell_evaluated(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            path = Path(directory) / ".env"
            path.write_text('# test fixture\nOPENROUTER_API_KEY="test-only-$(literal)"\n', encoding="utf-8")
            self.assertEqual(openrouter_key(Path(directory)), "test-only-$(literal)")
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-env"}):
                self.assertEqual(openrouter_key(Path(directory)), "test-env")
