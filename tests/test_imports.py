"""Cheap CLI operations must not import heavy network clients."""

import subprocess
import sys


def test_cli_import_does_not_load_acquisition_clients():
    code = r"""
import sys
import openplaces_ph.cli
assert 'overturemaps' not in sys.modules
assert 'huggingface_hub' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True)
