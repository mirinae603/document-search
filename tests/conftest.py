# tests/conftest.py
# Puts the project's `app/` directory on sys.path so the test module can import
# the same way the running application does (`from actions.proposer import ...`,
# `from qa.database import ...`). The app uses `app/` as its package root — it is
# normally run with cwd=app or PYTHONPATH=app — so the tests must mirror that.
import os
import sys

_APP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"
)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
