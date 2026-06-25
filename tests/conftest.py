"""Shared test setup.

The suite is hermetic — no network, no Google/Anthropic credentials. Source modules read
their API keys into module globals at import time, so individual tests monkeypatch those
globals (e.g. `ticketmaster.TICKETMASTER_API_KEY`) rather than relying on the environment.
This file only guarantees the package root is importable when pytest is run from anywhere.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
