"""
SIMPLIFIED LiveTalking Audio Bridge
Just forwards voice agent audio to LiveTalking /humanaudio endpoint
No emotion detection, no complexity - just real-time lip-sync
"""
import asyncio
import aiohttp
import sys
import json
import os
from pathlib import Path
from livekit import rtc
import numpy as np
import logging
import io
import wave
from scipy import signal

GAIN_FACTOR = int(os.getenv('AUDIO_GAIN_FACTOR', '1500'))  # Configurable via environment variable - need 0.01-0.05 RMS for lip-sync

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

LIVETALKING_URL = "http://localhost:8010"

class SimpleLiveTalkingBridge:
    def __init__(self, room_name: str, livekit_url: str, token: str):
        self.room_name = room_name
        self.livekit_url = livekit_url
        self.token = token
        self.room = rtc.Room()
        self.session_id = None
        self.audio_buffer = bytearray()
        self._saved_debug_wav = False
        
    async def connect(self):
        """Connect to LiveKit room"""
        logger.info(f"🔗 Connecting to LiveKit room: {self.room_name}")
        
        # Wait for session file
        session_file = Path(f"runtime/{self.room_name}.json")
        logger.info(f"⏳ Waiting for LiveTalking session at {session_file}")
        
        for _ in range(30):  # Wait up to 30 seconds
            if session_file.exists():
                with open(session_file, 'r') as f:
                    data = json.load(f)
                    self.session_id = data.get('sessionId')
                    logger.info(f"✅ Got LiveTalking session: {self.session_id}")
                    break
            await asyncio.sleep(1)
        
        if not self.session_id:
            logger.error("❌ No LiveTalking session found!")
            return
        
        # Connect to LiveKit
        await self.room.connect(self.livekit_url, self.token)
        logger.info(f"✅ Connected to LiveKit")
        
        # Subscribe to NEW audio tracks (tracks that get published AFTER we connect)
        @self.room.on("track_subscribed")
        def on_track_subscribed(track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                identity = participant.identity
                logger.info(f"🎤 New audio track from: {identity}")
                
                # Only process voice agent audio (agent-*)
                if identity.startswith("agent-"):
                    logger.info(f"✅ Processing voice agent audio (new track)")
                    audio_stream = rtc.AudioStream(track)
                    asyncio.create_task(self.process_audio_stream(audio_stream))
                else:
                    logger.info(f"⏭️  Skipping user audio")
        
        # CRITICAL: Also check for EXISTING tracks (tracks that were already published before we connected)
        logger.info("🔍 Checking for existing audio tracks...")
        for participant_sid, participant in self.room.remote_participants.items():
            identity = participant.identity
            logger.info(f"   Found participant: {identity}")
            
            for track_sid, publication in participant.track_publications.items():
                if publication.kind == rtc.TrackKind.KIND_AUDIO and publication.subscribed and publication.track:
                    logger.info(f"   └─ Found existing audio track: {publication.name or 'unnamed'}")
                    
                    # Only process voice agent audio (agent-*)
                    if identity.startswith("agent-"):
                        logger.info(f"✅ Processing voice agent audio (existing track)")
                        audio_stream = rtc.AudioStream(publication.track)
                        asyncio.create_task(self.process_audio_stream(audio_stream))
                    else:
                        logger.info(f"⏭️  Skipping user audio")
        
        logger.info("🎯 Bridge ready - listening for voice agent audio...")
    
    async def process_audio_stream(self, stream: rtc.AudioStream):
        """Process audio frames from voice agent and send to LiveTalking"""
        logger.info("🎵 Audio stream started")
        
        async for event in stream:
            try:
                # AudioFrameEvent has a 'frame' attribute
                frame = event.frame
                
                # Convert audio frame to 16-bit PCM bytes
                raw_buffer = frame.data
                if isinstance(raw_buffer, memoryview):
                    raw_bytes = raw_buffer.tobytes()
                else:
                    raw_bytes = bytes(raw_buffer)

                # Determine expected byte count assuming float32 samples first
                samples_per_channel = getattr(frame, "samples_per_channel", None)
                num_channels = getattr(frame, "num_channels", 1)
                expected_float_len = None
                if samples_per_channel is not None:
                    expected_float_len = samples_per_channel * num_channels * 4

                if expected_float_len is not None and len(raw_bytes) == expected_float_len:
                    # LiveKit uses float32 PCM in range [-1, 1]
                    audio_array = np.frombuffer(raw_bytes, dtype=np.float32)
                    audio_array = np.clip(audio_array, -1.0, 1.0)
                    pcm = audio_array * 32767.0
                else:
                    # Assume data is already 16-bit PCM
                    pcm = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
                
                # Apply gain boost for debugging (helps if the incoming signal is too quiet)
                raw_rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float64))))) if pcm.size else 0.0
                boosted = pcm * GAIN_FACTOR
                boosted_clipped = np.clip(boosted, -32768, 32767)
                boosted_rms = float(np.sqrt(np.mean(np.square(boosted_clipped.astype(np.float64))))) if boosted_clipped.size else 0.0
                pcm16 = boosted_clipped.astype(np.int16)
                logger.info(
                    "[AUDIO][GAIN] rms_before=%.6f rms_after=%.6f gain=%d samples=%d",
                    raw_rms / 32767.0 if raw_rms else 0.0,
                    boosted_rms / 32767.0 if boosted_rms else 0.0,
                    GAIN_FACTOR,
                    pcm16.size
                )
                audio_data = pcm16.tobytes()
                
                # Send directly to LiveTalking
                await self.send_to_livetalking(audio_data, frame.sample_rate, frame.num_channels)
                
            except Exception as e:
                logger.error(f"[ERROR] Error processing audio: {e}")
    
    async def send_to_livetalking(self, audio_data: bytes, sample_rate: int, channels: int):
        """Send audio to LiveTalking /humanaudio endpoint"""
        try:
            # Convert bytes back to int16 array for potential resampling
            audio_int16 = np.frombuffer(audio_data, dtype=np.int16)
            
            # Resample to 16kHz if needed (MuseTalk requires 16kHz)
            TARGET_SAMPLE_RATE = 16000
            if sample_rate != TARGET_SAMPLE_RATE:
                # Calculate target number of samples for resampling
                num_samples_target = int(len(audio_int16) * TARGET_SAMPLE_RATE / sample_rate)
                
                # Resample using scipy.signal.resample (high-quality Fourier method)
                audio_resampled = signal.resample(audio_int16, num_samples_target)
                
                # Convert back to int16 and clip to valid range
                audio_int16 = np.clip(audio_resampled, -32768, 32767).astype(np.int16)
                
                logger.debug(f"Resampled audio: {sample_rate}Hz ({len(audio_data)//2} samples) -> {TARGET_SAMPLE_RATE}Hz ({len(audio_int16)} samples)")
                sample_rate = TARGET_SAMPLE_RATE
            
            # Convert resampled audio back to bytes
            audio_data = audio_int16.tobytes()
            
            # Convert raw PCM audio to WAV format
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(channels)
                wav_file.setsampwidth(2)  # 16-bit = 2 bytes
                wav_file.setframerate(sample_rate)  # Now always 16000Hz
                wav_file.writeframes(audio_data)
            
            wav_bytes = wav_buffer.getvalue()

            # Save the first boosted chunk to disk for manual inspection
            if not self._saved_debug_wav:
                try:
                    with open("debug_agent_chunk.wav", "wb") as f:
                        f.write(wav_bytes)
                    logger.info("Saved boosted audio chunk to debug_agent_chunk.wav")
                except Exception as err:
                    logger.warning(f"Failed to write debug_agent_chunk.wav: {err}")
                self._saved_debug_wav = True
            
            # LiveTalking expects multipart form data with a file upload
            data = aiohttp.FormData()
            data.add_field('sessionid', str(self.session_id))
            data.add_field('file', 
                          wav_bytes,
                          filename='audio.wav',
                          content_type='audio/wav')
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{LIVETALKING_URL}/humanaudio",
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        logger.debug(f"✅ Sent {len(wav_bytes)} bytes WAV to LiveTalking")
                    else:
                        text = await resp.text()
                        logger.warning(f"⚠️  LiveTalking returned {resp.status}: {text}")
                        
        except Exception as e:
            logger.error(f"Failed to send audio to LiveTalking: {e}")
    
    async def run(self):
        """Main run loop"""
        await self.connect()
        
        # Keep running
        while True:
            await asyncio.sleep(1)

async def main():
    if len(sys.argv) != 4:
        print("Usage: python simple_audio_bridge.py <room_name> <livekit_url> <token>")
        sys.exit(1)
    
    room_name = sys.argv[1]
    livekit_url = sys.argv[2]
    token = sys.argv[3]
    
    logger.info("="*60)
    logger.info("SIMPLE LIVETALKING AUDIO BRIDGE")
    logger.info("="*60)
    logger.info(f"Room: {room_name}")
    logger.info(f"LiveKit: {livekit_url}")
    logger.info(f"LiveTalking: {LIVETALKING_URL}")
    logger.info("="*60)
    
    bridge = SimpleLiveTalkingBridge(room_name, livekit_url, token)
    await bridge.run()

if __name__ == "__main__":
    asyncio.run(main())

