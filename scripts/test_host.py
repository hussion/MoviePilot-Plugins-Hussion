"""Run tests in an actual MoviePilot image, without starting its services."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
name = 'app.plugins.manualtmdbidentity'
spec = importlib.util.spec_from_file_location(name, ROOT / 'plugins.v3/manualtmdbidentity/__init__.py')
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests/v3/manualtmdbidentity'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
