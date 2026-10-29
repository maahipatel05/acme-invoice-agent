import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from acme.config import load_env


class ConfigTests(unittest.TestCase):
    def test_env_load_is_literal_and_does_not_override_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('# comment\nGEMINI_API_KEY="test-only-value"\nGEMINI_MODEL=gemini-file # comment\nGEMINI_TIMEOUT_SECONDS=75\nUNRELATED=no\n')
            with patch.dict(os.environ, {'GEMINI_MODEL': 'gemini-shell'}, clear=True):
                load_env(path)
                self.assertEqual(os.environ['GEMINI_API_KEY'], 'test-only-value')
                self.assertEqual(os.environ['GEMINI_MODEL'], 'gemini-shell')
                self.assertEqual(os.environ['GEMINI_TIMEOUT_SECONDS'], '75')
                self.assertNotIn('UNRELATED', os.environ)

    def test_no_shell_expansion_and_missing_file_is_ok(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            with patch.dict(os.environ, {}, clear=True):
                load_env(path)
                self.assertNotIn('GEMINI_API_KEY', os.environ)
                path.write_text("GEMINI_API_KEY='$(touch /invalid-command)'\n")
                load_env(path)
                self.assertEqual(os.environ['GEMINI_API_KEY'], '$(touch /invalid-command)')
