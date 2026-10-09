"""B01 pre-implementation scaffold: no manifest, validation or generator yet."""
from types import SimpleNamespace


def write_corpus(root, vectors, rows, identity):
    return SimpleNamespace(vectors=vectors, records=rows, identity=identity)
