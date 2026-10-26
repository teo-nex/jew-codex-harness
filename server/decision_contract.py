"""Shared identity contract for the typed Jev v1 response schema."""

import re

REQUEST_MODEL = "jev-latest"
_MODEL = re.compile(r"(?:typesafe/)?jev-(?:latest|1\.\d+(?:\.\d+)?(?:-\d{8})?)\Z")


def compatible_model(value):
    """Accept v1 identities; callers must still validate their typed answers."""
    return isinstance(value, str) and _MODEL.fullmatch(value) is not None
