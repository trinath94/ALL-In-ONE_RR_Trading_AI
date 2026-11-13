"""Isolated experimental strategy environment (Part 25).

Nothing in this package is imported by the production registry. Experimental strategy *source code* is
stored in ``strategy_experiments.source_code`` and only ever executed inside ``runner.py`` in a separate,
resource-limited subprocess (see ``sandbox.py``).
"""
