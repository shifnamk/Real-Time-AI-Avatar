"""
Generate idle animation for avatar agent
Run this once to create the base idle animation with mouth closed
"""

import os
import sys
import numpy as np
import soundfile as sf
from pathlib import Path
import cv2

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from musetalk_renderer import MuseTalkRenderer, FrameExtractor
from motion_selector import MotionSelector
from utils import SAMPLE_RATE

# Configuration
DURATION = 3.0  # seconds of idle animation
OUTPUT_DIR = Path(__file__).parent / "idle_animation"
MUSETALK_PATH = "../MuseTalk"  # Adjust to your MuseTalk path
CUSTOMVIDEO_ROOT = "../data/customvideo"  # Your motion clips directory
USE_WSL = os.name == 'nt'


def generate_idle_animation():
    """Generate idle animation by selecting closed-mouth frames from motion clips"""
    
    print("="*60)
    print("GENERATING IDLE ANIMATION")
    print("="*60)
    
    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    frames_dir = OUTPUT_DIR / "frames"
    frames_dir.mkdir(exist_ok=True)
    # Clear existing frames to avoid mixing old/new output
    for existing in frames_dir.glob("*.png"):
        try:
            existing.unlink()
        except Exception:
            pass
    
    # 1. Select neutral motion clip
    print("\n[1/2] Selecting neutral motion clip...")
    motion_selector = MotionSelector(CUSTOMVIDEO_ROOT)
    
    # Try to get neutral/normal (most neutral expression)
    motion_frames_dir = (
        motion_selector.get_motion_frames_dir("neutral", "normal") or
        motion_selector.get_motion_frames_dir("calm", "normal") or
        motion_selector.get_any_motion_frames_dir()
    )
    
    if not motion_frames_dir:
        print("  [ERROR] No motion clips found!")
        print(f"  Please ensure motion clips exist in: {CUSTOMVIDEO_ROOT}")
        return False
    
    print(f"  [OK] Using motion: {motion_frames_dir.parent.name}")
    
    # 2. Create natural idle animation with subtle movements
    print("\n[2/2] Creating natural idle animation...")
    
    # Get all frame files from motion clip
    frame_files = sorted(motion_frames_dir.glob("*.png"))
    
    if not frame_files:
        print(f"  [ERROR] No frames found in {motion_frames_dir}")
        return False
    
    # Strategy: Use frames with minimal mouth movement (closed/nearly closed)
    # Typically the first 20-30% of neutral clips have closed mouth with natural micro-movements
    
    # Calculate how many frames to use (first 25% of clip for subtle movements)
    num_source_frames = max(15, len(frame_files) // 4)
    num_source_frames = min(num_source_frames, len(frame_files))
    
    print(f"  [INFO] Using first {num_source_frames} frames from {len(frame_files)} total")
    
    # Load selected frames
    source_frames = []
    for i in range(num_source_frames):
        frame = cv2.imread(str(frame_files[i]))
        if frame is not None:
            source_frames.append(frame)
    
    if not source_frames:
        print(f"  [ERROR] Could not load any frames")
        return False
    
    # Create looping animation by repeating the selected frames with ping-pong to avoid jumps
    target_frames = int(DURATION * 25)  # 25 FPS * duration
    selected_frames = []

    if len(source_frames) == 1:
        loop_sequence = source_frames * target_frames
    else:
        forward = source_frames
        backward = source_frames[-2:0:-1]  # exclude endpoints to prevent duplicate pauses
        loop_sequence = forward + backward

    print(f"  [INFO] Total frames in ping-pong loop: {len(loop_sequence)}")

    for i in range(target_frames):
        frame = loop_sequence[i % len(loop_sequence)].copy()
        selected_frames.append(frame)

        output_path = frames_dir / f"{i:05d}.png"
        cv2.imwrite(str(output_path), frame)

    print(f"  [OK] Created {len(selected_frames)} idle frames with natural micro-movements")

    # Create metadata
    metadata = {
        "duration": DURATION,
        "fps": len(selected_frames) / DURATION,
        "frame_count": len(selected_frames),
        "motion_clip": motion_frames_dir.parent.name,
        "method": "natural_idle_ping_pong_loop",
        "source_frames_used": len(source_frames),
        "loop_sequence_length": len(loop_sequence),
        "resolution": [selected_frames[0].shape[1], selected_frames[0].shape[0]]
    }
    
    import json
    (OUTPUT_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2))
    
    print("\n" + "="*60)
    print("[SUCCESS] IDLE ANIMATION GENERATED SUCCESSFULLY")
    print(f"   Location: {OUTPUT_DIR}")
    print(f"   Frames: {len(selected_frames)}")
    print(f"   Duration: {DURATION}s")
    print(f"   Method: Natural idle with subtle movements (mouth closed)")
    print(f"   Source frames: {len(source_frames)} (first 25% of clip)")
    print("="*60 + "\n")
    
    return True


if __name__ == "__main__":
    success = generate_idle_animation()
    sys.exit(0 if success else 1)

