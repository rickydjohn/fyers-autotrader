"""core-engine test path — must be first and only service on sys.path."""
import sys, os
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE = os.path.join(REPO, "core-engine")
if CORE not in sys.path:
    sys.path.insert(0, CORE)
