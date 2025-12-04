"""
STT Engine - Whisper-based speech-to-text
Same as your offline_brain_arabiv4565.py but for streaming
"""

import whisper
import torch
import numpy as np
from typing import Optional
from utils import SAMPLE_RATE

class WhisperSTT:
    """
    Whisper Speech-to-Text Engine
    Matches your offline pipeline: offline_brain_arabiv4565.py lines 14-20
    """
    
    def __init__(self, model_size: str = "base", language: Optional[str] = None):
        """
        Initialize Whisper model
        
        Args:
            model_size: tiny, base, small, medium, large
            language: ar (Arabic) or auto-detect
        """
        lang_display = language if language else "auto"
        print(f"[WhisperSTT] Loading {model_size} model (language={lang_display})...")
        self.model = whisper.load_model(model_size)
        self.language = language if language else None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[WhisperSTT] Ready on {self.device}")
    
    def transcribe(self, audio: np.ndarray) -> Optional[str]:
        """
        Transcribe audio segment to text
        
        Args:
            audio: Audio samples (float32, 16kHz)
            
        Returns:
            Transcribed text or None if failed
        """
        if len(audio) < SAMPLE_RATE * 0.3:  # Need at least 0.3 seconds
            return None
        
        try:
            # Same Whisper call as your offline pipeline
            result = self.model.transcribe(
                audio,
                language=self.language,
                fp16=(self.device == "cuda"),
                verbose=False
            )
            
            text = result.get("text", "").strip()
            
            if not text:
                # Log audio stats for debugging silent segments
                peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
                print(f"[WhisperSTT] Empty transcription (len={len(audio)}, peak={peak:.4f})")
                return None
            
            return text
            
        except Exception as e:
            print(f"[WhisperSTT] Transcription error: {e}")
            return None
    
    def transcribe_with_timestamps(self, audio: np.ndarray) -> list[dict]:
        """
        Transcribe with word-level timestamps
        
        Returns:
            List of {text, start, end} dicts
        """
        if len(audio) < SAMPLE_RATE * 0.3:
            return []
        
        try:
            result = self.model.transcribe(
                audio,
                language=self.language,
                fp16=(self.device == "cuda"),
                verbose=False,
                word_timestamps=True
            )
            
            segments = []
            for seg in result.get("segments", []):
                segments.append({
                    "text": seg.get("text", "").strip(),
                    "start": seg.get("start", 0.0),
                    "end": seg.get("end", 0.0)
                })
            
            return segments
            
        except Exception as e:
            print(f"[WhisperSTT] Timestamp transcription error: {e}")
            return []

