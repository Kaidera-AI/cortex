"""W4 operations plane: effect-based doctor, metrics, migration/release status,
retention, typed repair and lossless backup verification for the candidate.

All operations run through the frozen W1 surface (store/receipts) and the
generic operation contract; every mutation returns a typed receipt and no
operation exposes content bytes or unrestricted SQL.
"""
