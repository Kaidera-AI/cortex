"""B01 pre-implementation scaffold: deliberately lacks authoritative eligibility."""
from types import SimpleNamespace


def rank(corpus, query, **options):
    return SimpleNamespace(ids=lambda mode: [row["id"] for row in corpus.records],
                           eligible_count=len(corpus.records))
