"""Cortex v2 Retrieval module (W3): hybrid search, memory graph, code graph.

Transport-free domain package. Query planning, fusion, ranking and graph
traversal live here; provider-facing work (query embedding, vector candidate
lookup, reranking) is expressed as outward ports, and durable extraction runs
in the leased ``graph`` worker role through ``cortex_v2.retrieval.jobs``.
"""
