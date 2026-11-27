"""v2's tests run from v2/ (`cd v2 && python -m pytest`, rootdir set by v2/pytest.ini).

The repo-root v1 run (`python -m pytest -q` at the root) walks into this tree too; skip it there
so v1's run and its results stay exactly as they were.
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent


def pytest_ignore_collect(collection_path, config):
    if config.rootpath.resolve() != HERE:
        return True
    return None
