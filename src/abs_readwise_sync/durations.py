"""Validate saved Readwise audiobook lengths without accepting booleans or strings."""

import math


def saved_audio_length(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 < value <= 360000 or not math.isfinite(value) or int(value) != value:
        return None
    return int(value)
