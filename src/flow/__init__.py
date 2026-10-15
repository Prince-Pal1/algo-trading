"""Level-gated order flow analysis.

You supply the levels. This package watches what flow does when price reaches
them, and reports evidence — it never picks a level and never places an order.

See `src/flow/zone_state.py` for the state machine that drives it.
"""
