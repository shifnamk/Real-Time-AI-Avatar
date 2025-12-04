"""
Avatar Agent - Main orchestrator for realtime emotional avatar
Connects all components: Audio → STT → Emotion → Motion → MuseTalk → Video Stream
"""

import os
import sys
import asyncio
import time
import tempfile
from pathlib import Path
from collections import deque
from dotenv import load_dotenv

# LiveKit
from livekit import rtc
from livekit.agents import JobContext, WorkerOptions, cli

# Local components
from audio_buffer import AudioBuffer, AudioSegment
from stt_engine import WhisperSTT
from emotion_detector import EmotionDetector, RealtimeEmotionTracker
from motion_selector import MotionSelector
from musetalk_renderer import MuseTalkRenderer, FrameExtractor
from utils import get_env, SAMPLE_RATE

# Load environment
load_dotenv()


class AvatarPipeline:
    """
    Complete avatar pipeline:
    Audio → Segments → Emotion → Motion → MuseTalk → Frames
    """
    
    def __init__(self):
        # Configuration
        self.whisper_model = get_env("WHISPER_MODEL", "base")
        self.openai_key = get_env("OPENAI_API_KEY")
        self.ref_image = get_env("AVATAR_REF_IMAGE", "../reference/id01.jpg")
        self.customvideo_root = get_env("CUSTOMVIDEO_ROOT", "../data/customvideo")
        self.musetalk_path = get_env("MUSETALK_PATH", "../MuseTalk")
        self.use_wsl = os.name == 'nt'  # Use WSL on Windows
        
        # Segmentation config
        segment_duration = float(get_env("SEGMENT_DURATION", "1.0"))
        segment_overlap = float(get_env("SEGMENT_OVERLAP", "0.3"))
        
        print("\n" + "="*60)
        print("AVATAR AGENT INITIALIZATION")
        print("="*60)
        
        # Initialize components
        print("\n[1/6] Initializing Audio Buffer...")
        self.audio_buffer = AudioBuffer(
            max_buffer_seconds=30.0,
            segment_duration=segment_duration,
            segment_overlap=segment_overlap
        )
        
        whisper_language = get_env("WHISPER_LANGUAGE", "")
        
        print("\n[2/6] Initializing Whisper STT...")
        self.stt = WhisperSTT(model_size=self.whisper_model, language=whisper_language or None)
        
        print("\n[3/6] Initializing Emotion Detector...")
        detector = EmotionDetector(api_key=self.openai_key)
        self.emotion_tracker = RealtimeEmotionTracker(detector, context_window=3)
        
        print("\n[4/6] Initializing Motion Selector...")
        self.motion_selector = MotionSelector(self.customvideo_root)
        print(f"Motion stats: {self.motion_selector.get_stats()}")
        
        print("\n[5/6] Initializing MuseTalk Renderer...")
        self.musetalk = MuseTalkRenderer(self.musetalk_path, use_wsl=self.use_wsl)
        
        print("\n[6/6] Loading idle animation...")
        try:
            self.idle_frames = self._load_idle_animation()
            self.idle_frame_index = 0
            print(f"  [OK] Idle animation ready: {len(self.idle_frames)} frames")
        except Exception as e:
            print(f"  [ERROR] Failed to load idle animation: {e}")
            import traceback
            traceback.print_exc()
            # Use fallback
            import numpy as np
            self.idle_frames = [np.zeros((512, 512, 3), dtype=np.uint8)]
            self.idle_frame_index = 0
        
        # Frame queue for streaming
        self.frame_queue = deque(maxlen=1000)
        self.current_segment_frames = []
        self.frame_index = 0
        
        # Processing state
        self.is_running = False
        self.segments_processed = 0
        
        # Temp directory for segment processing
        self.temp_dir = Path(tempfile.gettempdir()) / "avatar_agent"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        print("\n" + "="*60)
        print("[OK] AVATAR AGENT READY")
        print("="*60 + "\n")
    
    def _load_idle_animation(self) -> list:
        """Load pre-generated idle animation frames"""
        import cv2
        import numpy as np
        
        idle_dir = Path(__file__).parent / "idle_animation" / "frames"
        
        if not idle_dir.exists():
            print(f"  [!] Idle animation not found at: {idle_dir}")
            print(f"  [!] Run 'python generate_idle_animation.py' first!")
            # Fallback to reference image
            default_frame = cv2.imread(self.ref_image)
            if default_frame is not None:
                return [default_frame]
            return [np.zeros((512, 512, 3), dtype=np.uint8)]
        
        # Load all frames
        frames = []
        for frame_file in sorted(idle_dir.glob("*.png")):
            frame = cv2.imread(str(frame_file))
            if frame is not None:
                frames.append(frame)
        
        if not frames:
            print(f"  [!] No frames found in {idle_dir}")
            return [np.zeros((512, 512, 3), dtype=np.uint8)]
        
        print(f"  [OK] Loaded {len(frames)} pre-generated idle frames")
        return frames
    
    async def process_audio_frame(self, audio_frame: rtc.AudioFrame):
        """Receive audio from voice agent"""
        import numpy as np

        # Convert int16 audio data to float32 in range [-1, 1]
        samples = np.frombuffer(audio_frame.data, dtype=np.int16)

        if audio_frame.num_channels > 1:
            samples = samples.reshape(-1, audio_frame.num_channels).mean(axis=1)

        audio_np = samples.astype(np.float32) / 32768.0

        # Resample to match pipeline sample rate if needed
        input_rate = audio_frame.sample_rate
        target_rate = SAMPLE_RATE
        if input_rate != target_rate and len(audio_np) > 0:
            ratio = target_rate / float(input_rate)
            target_len = max(1, int(len(audio_np) * ratio))
            indices = np.linspace(0, len(audio_np) - 1, num=target_len)
            audio_np = np.interp(indices, np.arange(len(audio_np)), audio_np).astype(np.float32)

        audio_np = np.ascontiguousarray(audio_np)

        # Add to buffer (automatically creates segments)
        self.audio_buffer.add_audio(audio_np)
    
    async def segment_processing_loop(self):
        """
        Background loop: Process audio segments through pipeline
        Audio Segment → STT → Emotion → Motion → MuseTalk → Frames
        """
        self.is_running = True
        print("[SegmentProcessor] Started")
        
        while self.is_running:
            # Get pending segments
            segments = self.audio_buffer.get_pending_segments()
            
            if not segments:
                await asyncio.sleep(0.1)
                continue
            
            # Process each segment
            for segment in segments:
                await self._process_segment(segment)
            
            await asyncio.sleep(0.05)
    
    async def _process_segment(self, segment: AudioSegment):
        """Process a single audio segment through the pipeline"""
        seg_id = segment.segment_id
        print(f"\n[Pipeline] Processing segment {seg_id} ({segment.duration:.2f}s)")
        
        # Step 1: STT - Transcribe audio
        print(f"  [1/4] Transcribing...")
        text = self.stt.transcribe(segment.audio)
        
        if not text:
            print(f"  [!] No transcription")
            return
        
        print(f"  [TEXT ASCII] {text[:60].encode('ascii', 'replace').decode('ascii')}")
        
        # Step 2: Emotion Detection
        print(f"  [2/4] Detecting emotion...")
        emotion, intensity = await self.emotion_tracker.update(text)
        print(f"  [EMOTION] {emotion}/{intensity}")
        
        # Step 3: Select Motion Clip
        print(f"  [3/4] Selecting motion...")
        motion_frames_dir = self.motion_selector.get_motion_frames_dir(emotion, intensity)
        
        if not motion_frames_dir:
            print(f"  [!] No motion clips available")
            return
        
        print(f"  [MOTION] {motion_frames_dir.parent.name}")
        
        # Step 4: MuseTalk - Generate lip-synced frames
        print(f"  [4/4] Rendering with MuseTalk...")
        
        # Save audio segment to temp file
        import soundfile as sf
        audio_path = self.temp_dir / f"seg_{seg_id:04d}.wav"
        sf.write(str(audio_path), segment.audio, SAMPLE_RATE)
        
        # Render with MuseTalk
        output_dir = self.temp_dir / f"output_{seg_id:04d}"
        video_path = self.musetalk.render_segment(
            audio_path=audio_path,
            motion_frames_dir=motion_frames_dir,
            output_dir=output_dir
        )
        
        if not video_path:
            print(f"  [!] MuseTalk rendering failed")
            return
        
        # Extract frames
        frames = FrameExtractor.extract_frames(video_path)
        
        if not frames:
            print(f"  [!] No frames extracted")
            return
        
        print(f"  [OK] Generated {len(frames)} frames")
        
        # Add frames to queue
        for frame in frames:
            self.frame_queue.append(frame)
        
        self.segments_processed += 1
        
        # Cleanup
        try:
            audio_path.unlink(missing_ok=True)
        except:
            pass
    
    async def video_streaming_loop(self, video_source: rtc.VideoSource):
        """
        Stream avatar frames to LiveKit
        Publishes frames at configured FPS
        """
        fps = int(get_env("FPS", "25"))
        frame_interval = 1.0 / fps
        
        print(f"[VideoStreamer] Started at {fps} FPS")
        
        import cv2
        import numpy as np
        
        # Default frame (reference image)
        default_frame = cv2.imread(self.ref_image)
        if default_frame is None:
            # Create blank frame
            default_frame = np.zeros((512, 512, 3), dtype=np.uint8)
        else:
            default_frame = cv2.resize(default_frame, (512, 512))
        
        while self.is_running:
            start_time = time.time()
            
            # Get next frame from queue or use idle animation
            if len(self.frame_queue) > 0:
                frame_bgr = self.frame_queue.popleft()
            else:
                # Loop through idle animation frames
                frame_bgr = self.idle_frames[self.idle_frame_index]
                self.idle_frame_index = (self.idle_frame_index + 1) % len(self.idle_frames)
            
            # Resize to standard size
            frame_bgr = cv2.resize(frame_bgr, (512, 512))
            
            # Convert to RGBA for LiveKit
            frame_rgba = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGBA)
            
            # Create LiveKit video frame
            video_frame = rtc.VideoFrame(
                width=512,
                height=512,
                type=rtc.VideoBufferType.RGBA,
                data=frame_rgba.tobytes()
            )
            
            # Publish (capture_frame is NOT async)
            video_source.capture_frame(video_frame)
            
            self.frame_index += 1
            
            # Log progress
            if self.frame_index % 100 == 0:
                stats = self.audio_buffer.get_stats()
                print(f"[VideoStreamer] Frame {self.frame_index}, "
                      f"Queue: {len(self.frame_queue)}, "
                      f"Segments: {self.segments_processed}, "
                      f"Audio: {stats['total_seconds']:.1f}s")
            
            # Maintain frame rate
            elapsed = time.time() - start_time
            sleep_time = max(0, frame_interval - elapsed)
            await asyncio.sleep(sleep_time)
    
    def stop(self):
        """Stop all processing"""
        self.is_running = False
        print("\n[AvatarPipeline] Stopped")


# ============================================================================
# LIVEKIT WORKER ENTRY POINT
# ============================================================================

async def entrypoint(ctx: JobContext):
    """
    LiveKit worker entry point
    Called when avatar agent joins a room
    """
    print("\n" + "="*60)
    print(f"AVATAR AGENT JOINING ROOM: {ctx.room.name}")
    print("="*60 + "\n")
    
    # Initialize pipeline
    pipeline = AvatarPipeline()
    
    # Create and publish video track
    video_source = rtc.VideoSource(512, 512)
    video_track = rtc.LocalVideoTrack.create_video_track("avatar_video", video_source)
    
    options = rtc.TrackPublishOptions()
    options.source = rtc.TrackSource.SOURCE_CAMERA
    
    await ctx.room.local_participant.publish_track(video_track, options)
    print("✅ Video track published\n")
    
    # Subscribe to voice agent audio
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(
        track: rtc.Track,
        publication: rtc.TrackPublication,
        participant: rtc.RemoteParticipant
    ):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            print(f"🎤 Subscribed to audio from: {participant.identity}\n")
            
            # Process audio stream
            audio_stream = rtc.AudioStream(track)
            asyncio.create_task(process_audio_stream(audio_stream, pipeline))
    
    # Start processing loops
    segment_task = asyncio.create_task(pipeline.segment_processing_loop())
    video_task = asyncio.create_task(pipeline.video_streaming_loop(video_source))
    
    # Wait for completion
    try:
        await asyncio.gather(segment_task, video_task)
    except asyncio.CancelledError:
        pipeline.stop()
        print("\n[AvatarAgent] Shutdown complete")


async def process_audio_stream(stream: rtc.AudioStream, pipeline: AvatarPipeline):
    """Process incoming audio frames from voice agent"""
    async for event in stream:
        # AudioStream yields AudioFrameEvent objects; extract the raw frame
        audio_frame = getattr(event, "frame", event)
        await pipeline.process_audio_frame(audio_frame)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))

