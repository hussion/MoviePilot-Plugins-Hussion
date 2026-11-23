"""Validate the MP index and build its installable release archive."""
import ast
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = 'ManualTmdbIdentity'
entry = json.loads((ROOT / 'package.v3.json').read_text())[PLUGIN]
directory = ROOT / 'plugins.v3' / PLUGIN.lower()
tree = ast.parse((directory / '__init__.py').read_text())
cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == PLUGIN)
versions = [ast.literal_eval(node.value) for node in cls.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'plugin_version' for target in node.targets)]
assert versions == [entry['version']], 'Plugin and market versions differ'
assert next(iter(entry['history'])) == 'v' + entry['version'], 'Current changelog must be first'
destination = ROOT / 'dist'
destination.mkdir(exist_ok=True)
archive = destination / f"{PLUGIN.lower()}_v{entry['version']}.zip"
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
    for path in sorted(directory.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            z.write(path, path.relative_to(directory.parent))
print(archive)
