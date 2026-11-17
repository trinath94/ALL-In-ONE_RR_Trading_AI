"""Pattern intelligence (Parts 27-30 Phase 5, Part 28).

New pattern *detectors* (harmonic, Elliott) beside the 215 original strategies, not inside them: they are not
registered as strategies, never produce strategy signals and never change a trading decision. Their detections are
persisted with a lifecycle, linked to the strategies that support or contradict them, and measured on the price bars
that follow (pattern learning). The catalogue describes the patterns the original strategies already implement,
without redefining them.
"""
