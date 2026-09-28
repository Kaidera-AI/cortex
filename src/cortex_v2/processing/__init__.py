"""Cortex v2 Processing module: durable jobs, projections and model ports."""

from __future__ import annotations

__all__ = ["OPERATIONS"]


def __getattr__(name: str):
    if name == "OPERATIONS":
        from .operations import OPERATIONS

        return OPERATIONS
    raise AttributeError(name)
