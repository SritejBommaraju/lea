"""The bridge looks for sessions fast only while one is changing; at rest it looks every few seconds (it used to
GET /v1/sessions every 0.7 s forever)."""
from __future__ import annotations

from repairos.live import spacetime_bridge as sb


def test_discovery_is_fast_while_a_session_changes_and_slow_at_rest():
    assert sb.discover_interval(0.0) == sb.DISCOVER_EVERY_S <= 1.0
    assert sb.discover_interval(sb.DISCOVER_ACTIVE_S - 1) == sb.DISCOVER_EVERY_S
    assert 3.0 <= sb.discover_interval(sb.DISCOVER_ACTIVE_S) == sb.DISCOVER_IDLE_S <= 5.0
    assert sb.discover_interval(3600.0) == sb.DISCOVER_IDLE_S
