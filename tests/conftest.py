"""
Root pytest configuration.

These are pure-logic unit tests — no DB, Redis, or Fyers connections.
No test data is written to any persistent store.

Sub-directories set their own sys.path so conflicting module names
(e.g. models/schemas.py exists in both core-engine and simulation-engine)
don't cross-contaminate.
"""
