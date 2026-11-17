"""Dataset versions (Parts 27-30 Phase 7, Parts 30.3 and 30.7).

Every model records, in ``dataset_ref.dataset_version``, a SHA-256 over the exact training data (rows, columns and
values, in order). The same data always gives the same version; any change gives a different one. With the
dataset's source, symbols, time range and row counts (already recorded), a model can be traced to exactly what it
learned from, and two models can be shown to share or not share their data.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd


def dataset_version(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, pd.DataFrame):
            h.update(",".join(map(str, p.columns)).encode())
            h.update(pd.util.hash_pandas_object(p, index=False).values.tobytes())
        elif isinstance(p, pd.Series):
            h.update(pd.util.hash_pandas_object(p, index=False).values.tobytes())
        elif isinstance(p, np.ndarray):
            h.update(str(p.dtype).encode() + str(p.shape).encode())
            h.update(np.ascontiguousarray(p).tobytes())
        else:
            h.update(json.dumps(p, sort_keys=True, default=str).encode())
    return "sha256:" + h.hexdigest()
