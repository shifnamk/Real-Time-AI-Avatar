"""
Shared utilities for avatar agent
"""

import os
from pathlib import Path
from typing import Dict, List

# Emotion mapping (matches your RAVDESS dataset)
EMOTIONS = ["neutral", "calm", "happy", "sad", "angry", "fearful", "disgust", "surprised"]
INTENSITIES = ["normal", "strong"]

EMOTION_TO_ID = {
    "neutral": 0,
    "happy": 1,
    "sad": 2,
    "angry": 3,
    "fearful": 4,
    "disgust": 5,
    "surprised": 6,
    "calm": 7,
}

# Audio constants
SAMPLE_RATE = 16000
N_MELS = 80
HOP_LENGTH = 256

def get_env(key: str, default: str = "") -> str:
    """Get environment variable with default"""
    return os.getenv(key, default)

def ensure_dir(path: Path) -> Path:
    """Ensure directory exists"""
    path.mkdir(parents=True, exist_ok=True)
    return path

def format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS.ms"""
    mins = int(seconds // 60)
    secs = seconds % 60
    return f"{mins:02d}:{secs:06.3f}"

