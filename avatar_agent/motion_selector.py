"""
Motion Selector - Selects motion clips based on emotion + intensity
Same logic as your make_avatar_plan_arabiv4565.py
"""

import os
from pathlib import Path
from glob import glob
from typing import Optional
from utils import EMOTIONS, INTENSITIES

class MotionSelector:
    """
    Selects pre-recorded motion clips based on emotion and intensity
    Matches your offline pipeline: make_avatar_plan_arabiv4565.py lines 22-48
    """
    
    def __init__(self, customvideo_root: str):
        """
        Initialize motion selector
        
        Args:
            customvideo_root: Path to customvideo directory with motion clips
                             Format: customvideo/<emotion>_<intensity>_<id>/
        """
        self.customvideo_root = Path(customvideo_root)
        
        if not self.customvideo_root.exists():
            raise ValueError(f"Customvideo root not found: {customvideo_root}")
        
        # Emotion alias mapping (same as your offline pipeline lines 23-32)
        self.emotion_alias = {
            "neutral": "neutral",
            "calm": "calm",
            "happy": "happy",
            "sad": "sad",
            "angry": "angry",
            "fearful": "fearful",
            "disgust": "disgust",
            "surprised": "surprised",
        }
        
        # Cache available motion directories
        self.motion_cache = self._scan_motion_dirs()
        
        print(f"[MotionSelector] Initialized with {len(self.motion_cache)} motion clips")
        print(f"[MotionSelector] Root: {self.customvideo_root}")
    
    def _scan_motion_dirs(self) -> dict:
        """Scan and cache all available motion directories"""
        cache = {}
        
        for emotion in EMOTIONS:
            for intensity in INTENSITIES:
                key = f"{emotion}_{intensity}"
                dirs = self._find_motion_dirs(emotion, intensity)
                if dirs:
                    cache[key] = dirs
                    print(f"[MotionSelector]   {key}: {len(dirs)} clips")
        
        return cache
    
    def _find_motion_dirs(self, emotion: str, intensity: str) -> list[Path]:
        """
        Find motion directories for emotion + intensity
        Same logic as your offline pipeline lines 35-48
        """
        # Map emotion through alias
        base_emotion = self.emotion_alias.get(emotion, "neutral")
        
        # Pattern: <emotion>_<intensity>_*
        pattern = str(self.customvideo_root / f"{base_emotion}_{intensity}_*")
        matches = sorted(glob(pattern))
        
        return [Path(m) for m in matches]
    
    def select_motion_dir(self, emotion: str, intensity: str) -> Optional[Path]:
        """
        Select a motion directory for given emotion and intensity
        
        Args:
            emotion: Emotion name (neutral, happy, sad, etc.)
            intensity: Intensity level (normal, strong)
            
        Returns:
            Path to motion directory or None if not found
        """
        key = f"{emotion}_{intensity}"
        
        # Get from cache
        dirs = self.motion_cache.get(key, [])
        
        if not dirs:
            print(f"[MotionSelector] No motion clips found for {key}")
            # Fallback to neutral_normal
            dirs = self.motion_cache.get("neutral_normal", [])
            if not dirs:
                return None
        
        # Pick first one (you can add random selection later)
        motion_dir = dirs[0]
        
        return motion_dir
    
    def get_motion_frames_dir(self, emotion: str, intensity: str) -> Optional[Path]:
        """
        Get the image frames directory for a motion clip
        
        Returns:
            Path to frames directory (motion_dir/image/)
        """
        motion_dir = self.select_motion_dir(emotion, intensity)
        
        if motion_dir is None:
            return None
        
        frames_dir = motion_dir / "image"
        
        if not frames_dir.exists():
            print(f"[MotionSelector] Frames directory not found: {frames_dir}")
            return None
        
        return frames_dir
    
    def get_available_emotions(self) -> list[str]:
        """Get list of emotions with available motion clips"""
        emotions = set()
        for key in self.motion_cache.keys():
            emotion = key.split("_")[0]
            emotions.add(emotion)
        return sorted(emotions)
    
    def get_stats(self) -> dict:
        """Get motion selector statistics"""
        total_clips = sum(len(dirs) for dirs in self.motion_cache.values())
        return {
            "total_clips": total_clips,
            "emotion_intensity_combos": len(self.motion_cache),
            "available_emotions": self.get_available_emotions()
        }

