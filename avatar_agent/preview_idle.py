"""
Preview idle animation by creating a video
"""
import cv2
from pathlib import Path

# Paths
IDLE_DIR = Path(__file__).parent / "idle_animation" / "frames"
OUTPUT_VIDEO = Path(__file__).parent / "idle_animation" / "idle_preview.mp4"

# Get all frames
frame_files = sorted(IDLE_DIR.glob("*.png"))

if not frame_files:
    print("❌ No frames found!")
    exit(1)

# Read first frame to get dimensions
first_frame = cv2.imread(str(frame_files[0]))
height, width = first_frame.shape[:2]

# Create video writer
fourcc = cv2.VideoWriter_fourcc(*'mp4v')
fps = 25
out = cv2.VideoWriter(str(OUTPUT_VIDEO), fourcc, fps, (width, height))

print(f"Creating preview video from {len(frame_files)} frames...")

for frame_file in frame_files:
    frame = cv2.imread(str(frame_file))
    out.write(frame)

out.release()

print(f"✅ Preview video created: {OUTPUT_VIDEO}")
print(f"   Duration: {len(frame_files) / fps:.1f}s")
print(f"   Resolution: {width}x{height}")

