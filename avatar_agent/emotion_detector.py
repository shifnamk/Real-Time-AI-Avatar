"""
Emotion Detector - GPT-4o-mini based emotion classification
EXACT SAME logic as your offline_brain_arabiv4565.py
"""

import json
import re
import asyncio
from typing import Tuple, Optional
from utils import EMOTIONS, INTENSITIES

class EmotionDetector:
    """
    Emotion classification using GPT-4o-mini
    Matches your offline pipeline: offline_brain_arabiv4565.py lines 23-67
    """
    
    def __init__(self, api_key: str | None = None):
        """
        Initialize emotion detector
        
        Args:
            api_key: OpenAI API key
        """
        self.api_key = api_key
        self._prompt_system = (
            "You classify emotions for an Arabic voice agent. "
            "Always produce the requested JSON exactly."
        )
        self._prompt_template = (
            "You are an emotion classifier for an Arabic virtual agent.\n\n"
            "Given the following sentence (Arabic or transliterated), return:\n"
            f"- emotion: one of {EMOTIONS}\n"
            f"- intensity: one of {INTENSITIES}\n\n"
            "Sentence:\n"
            "{text}\n\n"
            "Return ONLY strict JSON in this format (no extra text):\n"
            "{\n"
            '  "emotion": "<one of %s>",\n'
            '  "intensity": "<one of %s>"\n'
            "}"
        ) % (EMOTIONS, INTENSITIES)

        if not self.api_key:
            print("[EmotionDetector] No OPENAI_API_KEY configured; will return neutral/normal")
            self.client = None
        else:
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=self.api_key)
            except Exception as e:
                print(f"[EmotionDetector] Failed to init OpenAI client: {e}")
                self.client = None
        print("[EmotionDetector] Initialized with GPT-4o-mini" if self.client else "[EmotionDetector] Running in fallback mode")
        print("[EmotionDetector] Emotions:", EMOTIONS)
        print("[EmotionDetector] Intensities:", INTENSITIES)
    
    async def classify_emotion(self, text: str) -> Tuple[str, str]:
        """
        Classify emotion from text
        EXACT SAME PROMPT as offline_brain_arabiv4565.py lines 28-44
        
        Args:
            text: Transcribed text (Arabic or transliterated)
            
        Returns:
            (emotion, intensity) tuple
        """
        if not text or not text.strip():
            return "neutral", "normal"
        
        if not self.client:
            return "neutral", "normal"

        try:
            prompt = self._prompt_template.format(text=text)
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.2,
                messages=[
                    {"role": "system", "content": self._prompt_system},
                    {"role": "user", "content": prompt},
                ],
            )
            content = response.choices[0].message.content.strip()
            match = re.search(r"\{[\s\S]*\}", content)
            json_payload = match.group(0) if match else content
            data = json.loads(json_payload)
            emotion = data.get("emotion", "neutral")
            intensity = data.get("intensity", "normal")
        except Exception as e:
            print(f"[EmotionDetector] Classification error: {e}")
            emotion, intensity = "neutral", "normal"

        if emotion not in EMOTIONS:
            emotion = "neutral"
        if intensity not in INTENSITIES:
            intensity = "normal"

        return emotion, intensity
    
    def classify_emotion_sync(self, text: str) -> Tuple[str, str]:
        """Synchronous wrapper for classify_emotion"""
        return asyncio.run(self.classify_emotion(text))


class RealtimeEmotionTracker:
    """
    Tracks emotion over time with smoothing
    Maintains context from recent segments
    """
    
    def __init__(self, detector: EmotionDetector, context_window: int = 3):
        """
        Args:
            detector: EmotionDetector instance
            context_window: Number of recent segments to consider
        """
        self.detector = detector
        self.context_window = context_window
        
        # Current state
        self.current_emotion = "neutral"
        self.current_intensity = "normal"
        
        # History for context
        self.text_history = []
        self.emotion_history = []
        
        print(f"[EmotionTracker] Initialized with context window={context_window}")
    
    async def update(self, text: str) -> Tuple[str, str]:
        """
        Update emotion based on new transcript
        
        Args:
            text: New transcribed text
            
        Returns:
            (emotion, intensity) tuple
        """
        if not text or not text.strip():
            return self.current_emotion, self.current_intensity
        
        # Add to history
        self.text_history.append(text)
        if len(self.text_history) > self.context_window:
            self.text_history.pop(0)
        
        # Combine recent context for better accuracy
        context_text = " ".join(self.text_history[-self.context_window:])
        
        # Classify with context
        emotion, intensity = await self.detector.classify_emotion(context_text)
        
        # Update state
        self.current_emotion = emotion
        self.current_intensity = intensity
        
        # Track history
        self.emotion_history.append((emotion, intensity))
        if len(self.emotion_history) > self.context_window:
            self.emotion_history.pop(0)
        
        return emotion, intensity
    
    def get_current(self) -> Tuple[str, str]:
        """Get current emotion and intensity"""
        return self.current_emotion, self.current_intensity
    
    def get_emotion_id(self) -> int:
        """Get emotion ID for model input"""
        emotion_map = {
            "neutral": 0, "happy": 1, "sad": 2, "angry": 3,
            "fearful": 4, "disgust": 5, "surprised": 6, "calm": 7
        }
        return emotion_map.get(self.current_emotion, 0)

