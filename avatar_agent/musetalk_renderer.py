"""
MuseTalk Renderer - Generates lip-synced avatar frames
Integrates with MuseTalk for realtime frame generation
"""

import os
import sys
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Optional
import numpy as np
import cv2

class MuseTalkRenderer:
    """
    MuseTalk integration for lip-sync frame generation
    Based on your run_musetalk_segments.py
    """
    
    def __init__(self, musetalk_path: str, use_wsl: bool = False):
        """
        Initialize MuseTalk renderer
        
        Args:
            musetalk_path: Path to MuseTalk installation
            use_wsl: Whether to run MuseTalk in WSL (Windows only)
        """
        self.musetalk_path = Path(musetalk_path)
        self.use_wsl = use_wsl
        
        if not self.musetalk_path.exists():
            raise ValueError(f"MuseTalk path not found: {musetalk_path}")
        
        print(f"[MuseTalkRenderer] Initialized")
        print(f"[MuseTalkRenderer] Path: {self.musetalk_path}")
        print(f"[MuseTalkRenderer] WSL: {use_wsl}")
    
    def _win_to_wsl(self, win_path: str) -> str:
        """Convert Windows path to WSL path (from your run_musetalk_segments.py lines 16-26)"""
        if not self.use_wsl:
            return win_path
        
        # Convert to absolute path first
        abs_path = str(Path(win_path).resolve())
        abs_path = abs_path.replace("\\", "/")
        
        # Check if it's a Windows absolute path (e.g., C:/...)
        if len(abs_path) > 1 and abs_path[1] == ':':
            drive = abs_path[0].lower()
            rest = abs_path[2:]
            return f"/mnt/{drive}{rest}"
        
        return abs_path
    
    def render_segment(
        self,
        audio_path: Path,
        motion_frames_dir: Path,
        output_dir: Path
    ) -> Optional[Path]:
        """
        Render avatar video for an audio segment using motion frames
        
        Args:
            audio_path: Path to audio segment WAV file
            motion_frames_dir: Path to motion clip frames directory
            output_dir: Output directory for rendered video
            
        Returns:
            Path to rendered video or None if failed
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Convert paths for WSL if needed
        audio_wsl = self._win_to_wsl(str(audio_path))
        frames_wsl = self._win_to_wsl(str(motion_frames_dir))
        output_wsl = self._win_to_wsl(str(output_dir))
        musetalk_wsl = self._win_to_wsl(str(self.musetalk_path))
        
        # Create temporary YAML config (same as your run_musetalk_segments.py lines 56-63)
        yaml_content = textwrap.dedent(f"""
        input:
          video_path: {frames_wsl}
          audio_path: {audio_wsl}
        bbox_shift: 0
        result_dir: {output_wsl}
        """).lstrip()
        
        tmp_yaml = Path(tempfile.gettempdir()) / f"musetalk_{os.getpid()}.yaml"
        tmp_yaml.write_text(yaml_content, encoding="utf-8")
        tmp_yaml_wsl = self._win_to_wsl(str(tmp_yaml))
        
        try:
            # Run MuseTalk (same as your run_musetalk_segments.py lines 73-82)
            if self.use_wsl:
                cmd = f"""
                cd {musetalk_wsl} && \
                source ~/miniconda3/bin/activate nerfstream && \
                ~/miniconda3/envs/nerfstream/bin/python -m scripts.inference \
                  --inference_config {tmp_yaml_wsl} \
                  --result_dir {output_wsl} \
                  --unet_config ./models/musetalkV15/musetalk.json \
                  --vae_type sd-vae
                """
                
                result = subprocess.run(
                    ["wsl.exe", "-d", "Ubuntu-22.04", "--", "bash", "-lc", cmd],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=300
                )
            else:
                # Direct Python execution (non-WSL)
                cmd = [
                    sys.executable,
                    "-m", "scripts.inference",
                    "--inference_config", str(tmp_yaml),
                    "--result_dir", str(output_dir),
                    "--unet_config", "./models/musetalkV15/musetalk.json",
                    "--vae_type", "sd-vae"
                ]
                
                result = subprocess.run(
                    cmd,
                    cwd=str(self.musetalk_path),
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=300
                )
            
            if result.returncode != 0:
                print(f"[MuseTalkRenderer] Error (return code {result.returncode}):")
                print(f"  STDERR: {result.stderr[:500]}")
                print(f"  STDOUT: {result.stdout[:500]}")
                return None
            
            # Show output for debugging
            if result.stdout:
                print(f"[MuseTalkRenderer] Output: {result.stdout[-200:]}")
            
            # Find output video - MuseTalk v15 creates subdirectory structure
            candidates = []
            v15_dir = output_dir / "v15"
            if v15_dir.exists():
                candidates.extend(sorted(v15_dir.glob("*.mp4")))
            candidates.extend(sorted(output_dir.glob("*.mp4")))
            if not candidates:
                candidates = sorted(output_dir.rglob("*.mp4"))

            if candidates:
                print(f"  ✅ Found video: {candidates[0]}")
                return candidates[0]

            # Check for raw PNG frames as fallback
            if v15_dir.exists():
                png_frames = sorted(v15_dir.glob("*.png"))
                if png_frames:
                    print(f"  ✅ Found PNG frames in {v15_dir}")
                    return v15_dir

            
            print(f"  ⚠️ No output video found in {output_dir}")
            return None
            
        except subprocess.TimeoutExpired:
            print(f"[MuseTalkRenderer] Timeout rendering segment")
            return None
        except Exception as e:
            print(f"[MuseTalkRenderer] Render error: {e}")
            return None
        finally:
            # Cleanup temp file
            try:
                tmp_yaml.unlink(missing_ok=True)
            except:
                pass


class FrameExtractor:
    """Extract frames from MuseTalk output video for streaming"""
    
    @staticmethod
    def extract_frames(video_path: Path, output_dir: Optional[Path] = None) -> list[np.ndarray]:
        """
        Extract all frames from video
        
        Args:
            video_path: Path to video file
            output_dir: Optional directory to save frames as images
            
        Returns:
            List of frames as numpy arrays (BGR format)
        """
        if not video_path.exists():
            # Try reading raw PNG frames (MuseTalk often emits v15/frame_*.png)
            parent = video_path if video_path.is_dir() else video_path.parent
            frame_files = sorted(parent.glob("*.png"))
            frames = [cv2.imread(str(f)) for f in frame_files]
            frames = [f for f in frames if f is not None]
            if frames:
                print(f"[FrameExtractor] Loaded {len(frames)} PNG frames from {parent}")
                return frames
            return []
        
        frames = []
        cap = cv2.VideoCapture(str(video_path))
        
        if not cap.isOpened():
            print(f"[FrameExtractor] Cannot open video: {video_path}")
            return []
        
        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            frames.append(frame)
            
            # Optionally save frame
            if output_dir:
                output_dir.mkdir(parents=True, exist_ok=True)
                frame_path = output_dir / f"frame_{frame_idx:06d}.png"
                cv2.imwrite(str(frame_path), frame)
            
            frame_idx += 1
        
        cap.release()
        
        print(f"[FrameExtractor] Extracted {len(frames)} frames from {video_path.name}")
        return frames
    
    @staticmethod
    def get_video_info(video_path: Path) -> dict:
        """Get video metadata"""
        cap = cv2.VideoCapture(str(video_path))
        
        if not cap.isOpened():
            return {}
        
        info = {
            "fps": cap.get(cv2.CAP_PROP_FPS),
            "frame_count": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "duration": cap.get(cv2.CAP_PROP_FRAME_COUNT) / cap.get(cv2.CAP_PROP_FPS)
        }
        
        cap.release()
        return info

