"""
LiveTalking Bridge Agent
Connects LiveKit audio → STT → Emotion → LiveTalking realtime avatar

This agent:
1. Subscribes to voice agent audio in LiveKit room
2. Runs Whisper STT + GPT-4o-mini emotion detection
3. Switches LiveTalking motion clips via /set_audiotype API
4. Forwards audio to LiveTalking for realtime lip-sync
"""

import os
import sys
import json
import asyncio
from io import BytesIO

import aiohttp
import numpy as np
import soundfile as sf
from pathlib import Path
from dotenv import load_dotenv
from livekit import rtc

# Local components
from stt_engine import WhisperSTT
from emotion_detector import EmotionDetector
from motion_selector import MotionSelector
from utils import get_env, SAMPLE_RATE

load_dotenv()

# LiveTalking server config
LIVETALKING_URL = get_env("LIVETALKING_URL", "http://localhost:8010")

class LiveTalkingBridge:
    """
    Bridge between LiveKit and LiveTalking
    """
    
    def __init__(self, room_name: str):
        print("\n" + "="*60)
        print("LIVETALKING BRIDGE INITIALIZATION")
        print("="*60)
        
        self.room_name = room_name
        self.session_warning_logged = False
        self.http_session: aiohttp.ClientSession | None = None
        self.bridge_identity: str = ""
        self.allowed_agent_prefixes = ("agent-", "voice-agent-", "assistant-")

        self.session_file_path = Path("runtime") / f"{self.room_name}.json"
        self.session_file_path.parent.mkdir(parents=True, exist_ok=True)

        # Configuration
        self.whisper_model = get_env("WHISPER_MODEL", "base")
        self.openai_key = get_env("OPENAI_API_KEY")
        self.customvideo_root = get_env("CUSTOMVIDEO_ROOT", "../data/customvideo")
        whisper_language = get_env("WHISPER_LANGUAGE", "")
        
        # Initialize components
        print("\n[1/3] Initializing Whisper STT...")
        self.stt = WhisperSTT(model_size=self.whisper_model, language=whisper_language or None)
        
        print("\n[2/3] Initializing Emotion Detector...")
        self.emotion_detector = EmotionDetector(api_key=self.openai_key)
        
        print("\n[3/3] Initializing Motion Selector...")
        self.motion_selector = MotionSelector(self.customvideo_root)
        print(f"Motion stats: {self.motion_selector.get_stats()}")
        
        # Load customvideo config to map emotion → audiotype
        config_path = Path("../data/customvideo_config_fixed.json")
        with open(config_path, 'r') as f:
            self.customvideo_config = json.load(f)
        
        # Build emotion_intensity → audiotype mapping
        self.emotion_map = {}
        for clip in self.customvideo_config:
            name = clip["name"]  # e.g., "angry_normal_01"
            audiotype = clip["audiotype"]
            # Extract emotion_intensity (e.g., "angry_normal")
            parts = name.rsplit("_", 1)  # Split from right: ["angry_normal", "01"]
            if len(parts) == 2:
                emotion_intensity = parts[0]
                if emotion_intensity not in self.emotion_map:
                    self.emotion_map[emotion_intensity] = []
                self.emotion_map[emotion_intensity].append(audiotype)
        
        print(f"[OK] Loaded {len(self.emotion_map)} emotion_intensity mappings")
        
        # State
        self.audio_buffer = []
        self.buffer_duration = 0.0
        self.segment_duration = 2.0  # Process every 2 seconds
        self.current_emotion = "neutral"
        self.current_intensity = "normal"
        self.livetalking_session = None
        self.session_task: asyncio.Task | None = None
        self.audio_send_failures = 0
        
        print("\n" + "="*60)
        print("BRIDGE READY")
        print("="*60 + "\n")
    
    def _get_audiotype(self, emotion: str, intensity: str) -> int:
        """
        Get audiotype ID for emotion + intensity
        Returns first matching audiotype or default neutral
        """
        key = f"{emotion}_{intensity}"
        audiotypes = self.emotion_map.get(key, [])
        
        if not audiotypes:
            # Fallback to neutral_normal
            audiotypes = self.emotion_map.get("neutral_normal", [90])
        
        return audiotypes[0]  # Return first clip
    
    async def _set_livetalking_emotion(self, emotion: str, intensity: str):
        """
        Call LiveTalking /set_audiotype API to switch motion clip
        """
        if not self.livetalking_session:
            if not self.session_warning_logged:
                print(f"[!] LiveTalking session not ready; skipping emotion switch")
                self.session_warning_logged = True
            return
        
        self.session_warning_logged = False
        
        audiotype = self._get_audiotype(emotion, intensity)
        
        print(f"[EMOTION] {emotion}/{intensity} -> audiotype {audiotype}")
        
        try:
            session = await self._ensure_http_session()
            async with session.post(
                f"{LIVETALKING_URL}/set_audiotype",
                json={
                    "sessionid": self.livetalking_session,
                    "audiotype": audiotype,
                    "reinit": False  # Don't reinit, just switch
                },
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    print(f"  [OK] Switched to audiotype {audiotype}")
                else:
                    print(f"  [ERROR] Failed to switch: {resp.status}")
        except Exception as e:
            print(f"[ERROR] Failed to call /set_audiotype: {e}")
    
    async def process_audio_chunk(self, audio_np: np.ndarray):
        """
        Process incoming audio chunk:
        1. Buffer audio
        2. When buffer >= segment_duration, run STT + emotion detection
        3. Switch LiveTalking motion clip
        """
        # Add to buffer
        self.audio_buffer.append(audio_np)
        self.buffer_duration += len(audio_np) / SAMPLE_RATE
        
        # Process when buffer is full
        if self.buffer_duration >= self.segment_duration:
            # Concatenate buffer
            segment = np.concatenate(self.audio_buffer)
            
            # Send audio to LiveTalking for lip sync
            await self._send_audio_to_livetalking(segment)
            
            # Clear buffer
            self.audio_buffer = []
            self.buffer_duration = 0.0
            
            # Run STT
            text = self.stt.transcribe(segment)
            
            if text:
                safe_preview = text.encode("ascii", "replace").decode("ascii")
                print(f"\n[SEGMENT] Transcribed: {safe_preview[:60]}...")
                
                # Run emotion detection
                emotion, intensity = await self.emotion_detector.classify_emotion(text)
                
                print(f"[EMOTION] Detected: {emotion}/{intensity}")
                
                # Switch LiveTalking clip if emotion changed
                if emotion != self.current_emotion or intensity != self.current_intensity:
                    self.current_emotion = emotion
                    self.current_intensity = intensity
                    await self._set_livetalking_emotion(emotion, intensity)
            else:
                print(f"[SEGMENT] No transcription (silent or too short)")

    async def wait_for_session(self):
        """
        Monitor LiveTalking session file created by backend.
        Updates session id whenever the file changes.
        """
        print(f"[SESSION] Waiting for LiveTalking session file at {self.session_file_path}")
        last_mtime = None
        while True:
            if self.session_file_path.exists():
                try:
                    stat = self.session_file_path.stat()
                    if last_mtime is not None and stat.st_mtime == last_mtime:
                        await asyncio.sleep(0.5)
                        continue

                    data = json.loads(self.session_file_path.read_text().strip() or "{}")
                    session_id = data.get("sessionId") or data.get("sessionid")
                    if session_id:
                        session_id = int(session_id)
                        if self.livetalking_session != session_id:
                            self.livetalking_session = session_id
                            self.session_warning_logged = False
                            print(f"[SESSION] LiveTalking session set to {self.livetalking_session}")
                    last_mtime = stat.st_mtime
                except Exception as e:
                    print(f"[SESSION] Failed to read session file: {e}")
            await asyncio.sleep(0.5)

    async def _ensure_http_session(self) -> aiohttp.ClientSession:
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession()
        return self.http_session

    async def _send_audio_to_livetalking(self, audio_np: np.ndarray):
        """
        Send audio segment to LiveTalking /humanaudio endpoint
        """
        if not self.livetalking_session:
            if not self.session_warning_logged:
                print("[AUDIO] LiveTalking session not ready; audio segment not forwarded yet")
                self.session_warning_logged = True
            return

        self.session_warning_logged = False

        audio_np = np.clip(audio_np, -1.0, 1.0)

        buffer = BytesIO()
        try:
            sf.write(buffer, audio_np, SAMPLE_RATE, format='wav')
        except Exception as e:
            print(f"[AUDIO] Failed to encode audio segment: {e}")
            return

        buffer.seek(0)

        form = aiohttp.FormData()
        form.add_field('sessionid', str(self.livetalking_session))
        form.add_field('file', buffer.getvalue(), filename='segment.wav', content_type='audio/wav')

        try:
            session = await self._ensure_http_session()
            async with session.post(
                f"{LIVETALKING_URL}/humanaudio",
                data=form,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    self.audio_send_failures += 1
                    if self.audio_send_failures <= 5 or self.audio_send_failures % 10 == 0:
                        print(f"[AUDIO] Failed to push audio to LiveTalking ({resp.status})")
                else:
                    if self.audio_send_failures:
                        print("[AUDIO] Audio forwarding recovered")
                    self.audio_send_failures = 0
        except Exception as e:
            self.audio_send_failures += 1
            if self.audio_send_failures <= 5 or self.audio_send_failures % 10 == 0:
                print(f"[AUDIO] Error sending audio to LiveTalking: {e}")

async def connect_to_room(url: str, token: str, room_name: str):
    """
    Connect to LiveKit room and bridge audio to LiveTalking
    """
    # Configure stdout for immediate flushing
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    
    print(f"\n[BRIDGE] Initializing...")
    
    # Initialize bridge
    try:
        bridge = LiveTalkingBridge(room_name)
    except Exception as e:
        print(f"[ERROR] Failed to initialize bridge: {e}")
        import traceback
        traceback.print_exc()
        return
    
    room = rtc.Room()
    bridge.session_task = asyncio.create_task(bridge.wait_for_session())
    
    @room.on("track_subscribed")
    def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        print(f"[TRACK] Subscribed to {participant.identity}: {track.kind}")
        
        identity = participant.identity or ""

        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return

        if identity == bridge.bridge_identity:
            print(f"[AUDIO] Skipping local bridge audio from {identity}")
            return

        if identity.startswith("user-"):
            print(f"[AUDIO] Ignoring user microphone track from {identity}")
            return

        if not identity.startswith(bridge.allowed_agent_prefixes):
            print(f"[AUDIO] Ignoring non-agent audio track from {identity}")
            return

        print(f"[AUDIO] Starting audio processing from {identity}")
        asyncio.create_task(process_audio_stream(track, bridge))
    
    @room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        print(f"[PARTICIPANT] Connected: {participant.identity}")
    
    @room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        print(f"[PARTICIPANT] Disconnected: {participant.identity}")
    
    print(f"[CONNECTING] Attempting to connect to LiveKit...")
    print(f"[DEBUG] URL: {url}")
    print(f"[DEBUG] Token length: {len(token)}")
    print(f"[DEBUG] Room: {room_name}")
    
    try:
        print("[DEBUG] Calling room.connect()...")
        sys.stdout.flush()
        await asyncio.wait_for(room.connect(url, token), timeout=30.0)
        print(f"[OK] Connected to room: {room_name}\n")
        bridge.bridge_identity = room.local_participant.identity or ""
        print(f"[SESSION] Bridge identity: {bridge.bridge_identity}")
        sys.stdout.flush()
    except asyncio.TimeoutError:
        print(f"[ERROR] Connection timeout after 30 seconds")
        print(f"[ERROR] Could not connect to {url}")
        sys.stdout.flush()
        await asyncio.sleep(5)
        return
    except Exception as e:
        print(f"[ERROR] Failed to connect: {e}")
        print(f"[ERROR] Exception type: {type(e).__name__}")
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        await asyncio.sleep(5)
        return
    
    print("[OK] Bridge is running. Waiting for audio...")
    
    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Disconnecting...")
        await room.disconnect()
    finally:
        if bridge.session_task:
            bridge.session_task.cancel()
            try:
                await bridge.session_task
            except asyncio.CancelledError:
                pass
        if bridge.http_session and not bridge.http_session.closed:
            await bridge.http_session.close()


async def process_audio_stream(track: rtc.Track, bridge: LiveTalkingBridge):
    """
    Process incoming audio frames from voice agent
    """
    import resampy
    
    audio_stream = rtc.AudioStream(track)
    
    async for event in audio_stream:
        # event is AudioFrameEvent, need to access .frame
        audio_frame = event.frame
        
        # Convert to numpy float32
        audio_data = audio_frame.data.tobytes()
        audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        
        # Resample if necessary and convert to mono
        if audio_frame.sample_rate != SAMPLE_RATE:
            audio_np = resampy.resample(audio_np, audio_frame.sample_rate, SAMPLE_RATE)
        
        if audio_frame.num_channels > 1:
            # Convert to mono by averaging channels
            audio_np = audio_np.reshape(-1, audio_frame.num_channels).mean(axis=1)
        
        # Process audio chunk
        await bridge.process_audio_chunk(audio_np)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python livetalking_bridge.py <livekit_url> <token> <room>")
        sys.exit(1)
    
    url = sys.argv[1]
    token = sys.argv[2]
    room = sys.argv[3]
    
    asyncio.run(connect_to_room(url, token, room))

