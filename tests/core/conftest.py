"""core-engine test path — must be first and only service on sys.path."""
import sys, os
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE = os.path.join(REPO, "core-engine")
# Inside the Docker container the source lives directly at REPO (no subdirectory).
if not os.path.isdir(CORE):
    CORE = REPO
if CORE not in sys.path:
    sys.path.insert(0, CORE)
