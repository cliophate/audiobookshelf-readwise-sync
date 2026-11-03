"""Check whether reported audio positions can represent the listening interval."""

import math
import os


class SuspectAudioRange(ValueError):
    """Keep a source for review instead of inventing a replacement position."""


def playback_limit():
    try:
        value = float(os.getenv("AUDIO_MAX_PLAYBACK_SPEED", "4"))
        if not math.isfinite(value) or not 0.25 <= value <= 16:
            raise ValueError
        return value
    except ValueError:
        raise ValueError("AUDIO_MAX_PLAYBACK_SPEED must be a number from 0.25 to 16.") from None


def metrics(source, *, max_speed=None):
    limit = playback_limit() if max_speed is None else max_speed
    values = {}
    for key in ("startTime", "currentTime", "timeListening", "startedAt", "updatedAt"):
        raw = source[key]
        if isinstance(raw, bool):
            raise ValueError("Audio timing must contain finite numbers.")
        values[key] = float(raw)
        if not math.isfinite(values[key]):
            raise ValueError("Audio timing must contain finite numbers.")
    span = values["currentTime"] - values["startTime"]
    listened = values["timeListening"]
    elapsed = (values["updatedAt"] - values["startedAt"]) / 1000
    if span <= 0 or listened <= 0 or elapsed <= 0:
        raise ValueError("Audio timing must have a positive interval.")
    return {
        "audio_seconds": round(span, 3),
        "listening_seconds": round(listened, 3),
        "elapsed_seconds": round(elapsed, 3),
        "implied_playback_speed": round(span / listened, 3),
        "max_playback_speed": limit,
        "plausible": span <= listened * limit + 5 and listened <= elapsed + 5,
    }


def check_audio_range(source):
    values = metrics(source)
    if not values["plausible"]:
        raise SuspectAudioRange(
            "Suspect audio range: "
            f"{values['audio_seconds']:g} audio seconds, "
            f"{values['listening_seconds']:g} listening seconds, "
            f"{values['elapsed_seconds']:g} elapsed seconds. "
            f"Limit is {values['max_playback_speed']:g}x plus 5 seconds. "
            "Review the source positions and listening counter; no positions were guessed."
        )
