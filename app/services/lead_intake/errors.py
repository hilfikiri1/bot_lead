"""Typed errors for the lead-intake saga, carrying a stable machine code."""

from __future__ import annotations


class LeadIntakeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
