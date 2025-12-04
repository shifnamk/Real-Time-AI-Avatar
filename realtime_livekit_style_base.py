"""
OpenAI Realtime API with CLIENT-SIDE VAD for Instant Interruption
Using LiveKit-style approach: detect speech locally BEFORE sending to server
"""

import asyncio
import websockets
import json
import base64
import sounddevice as sd
import numpy as np
import webrtcvad
import collections

OPENAI_API_KEY = ""  # Add your OpenAI API key here

WS_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime-mini-2025-10-06"
SAMPLE_RATE = 24000
SAMPLE_RATE_VAD = 16000  # webrtcvad requires 8k, 16k, 32k, or 48k

# Removed global state - now using shared dictionary

class ClientSideVAD:
    """Client-side Voice Activity Detection for instant interruption"""
    
    def __init__(self, aggressiveness=1):
        """
        aggressiveness: 0-3, where 3 is most aggressive (less likely to detect speech)
        We use 1 for balanced sensitivity
        """
        self.vad = webrtcvad.Vad(1)  # Mode 1: balanced
        self.frame_duration_ms = 30  # 30ms frames required by webrtcvad
        self.num_padding_frames = 3  # Frames needed to trigger
        self.ring_buffer = collections.deque(maxlen=self.num_padding_frames)
        self.triggered = False
        self.energy_threshold = 800  # RMS energy threshold (increased to reduce false positives)
        self.debug_mode = False  # Set to True to see energy levels
        
    def calculate_rms(self, audio_bytes):
        """Calculate RMS (Root Mean Square) energy of audio"""
        audio_np = np.frombuffer(audio_bytes, dtype=np.int16)
        return np.sqrt(np.mean(audio_np.astype(np.float32) ** 2))
        
    def is_speech(self, audio_chunk):
        """
        Check if audio chunk contains speech with energy filtering
        audio_chunk: raw PCM16 audio at 16kHz
        Returns: True if speech detected
        """
        try:
            # webrtcvad needs exactly 480 samples for 30ms at 16kHz
            frame_size = int(SAMPLE_RATE_VAD * self.frame_duration_ms / 1000)
            
            if len(audio_chunk) < frame_size * 2:  # *2 for int16 bytes
                return False
            
            # Take first frame_size samples
            frame = audio_chunk[:frame_size * 2]
            
            # Check energy level - user speech is typically louder than echo
            rms_energy = self.calculate_rms(frame)
            
            # Debug mode: print energy levels
            if self.debug_mode and rms_energy > 100:
                print(f"[Energy: {rms_energy:.0f}]", end=" ")
            
            # If energy is too low, it's likely just echo/noise
            if rms_energy < self.energy_threshold:
                return False
            
            is_speech = self.vad.is_speech(frame, SAMPLE_RATE_VAD)
            
            # Use ring buffer to avoid false positives
            self.ring_buffer.append(is_speech)
            
            # Trigger if 2 out of 3 frames are speech
            num_voiced = sum(self.ring_buffer)
            
            if num_voiced >= 2:
                if not self.triggered:
                    self.triggered = True
                    if self.debug_mode:
                        print(f"\n[🔊 Speech triggered at energy {rms_energy:.0f}]")
                    return True
            else:
                self.triggered = False
            
            return False
            
        except Exception as e:
            return False
    
    def reset(self):
        """Reset the VAD state"""
        self.ring_buffer.clear()
        self.triggered = False


async def send_session_config(ws):
    """Configure the session"""
    await ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": "You are a helpful voice assistant. Always respond in English. Be conversational and brief.",
            "voice": "alloy",
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500
            }
        }
    }))
    print("✓ Session configured")


async def send_audio_chunks(ws, vad, interrupt_evt, state):
    """Capture and send microphone audio with CLIENT-SIDE interruption detection"""
    import queue
    import time
    audio_queue = queue.Queue()
    vad_queue = queue.Queue()  # Separate queue for VAD processing
    last_interrupt_time = 0  # Track last interruption time
    
    def mic_callback(indata, frames, time_info, status):
        """Callback for microphone input"""
        audio_bytes = bytes(indata)
        audio_queue.put(audio_bytes)
        
        # Also queue for VAD processing (downsampled)
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        # Downsample to 16kHz for VAD
        if SAMPLE_RATE != SAMPLE_RATE_VAD:
            downsample_factor = SAMPLE_RATE // SAMPLE_RATE_VAD
            audio_downsampled = audio_int16[::downsample_factor]
            vad_queue.put(audio_downsampled.tobytes())
        else:
            vad_queue.put(audio_bytes)
    
    print("\n🎤 Microphone active - SPEAK ANYTIME!")
    print("   ⚡ CLIENT-SIDE interruption enabled (energy threshold: 800)")
    print("   💡 TIP: Speak at NORMAL volume to interrupt")
    print("   💡 Use headphones to prevent echo (highly recommended!)")
    print("   Press Ctrl+C to exit\n")
    
    # Start microphone stream with aggressive echo cancellation
    # Note: Echo cancellation effectiveness depends on your audio drivers
    stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype='int16',
        blocksize=int(SAMPLE_RATE * 0.02),  # 20ms chunks
        callback=mic_callback,
        # These settings help reduce feedback/echo
        latency='low'
    )
    stream.start()
    
    chunk_count = 0
    
    try:
        while True:
            try:
                # Process VAD queue first (for interruption detection)
                try:
                    vad_audio = vad_queue.get_nowait()
                    
                    # Check if user is speaking
                    if vad.is_speech(vad_audio):
                        current_time = time.time()
                        
                        # Cooldown period: don't interrupt within 0.5 seconds of last interruption
                        if current_time - last_interrupt_time < 0.5:
                            continue
                        
                        if state['is_ai_speaking'] or state['ai_is_generating']:
                            print("\n   ⚡⚡⚡ INTERRUPTION DETECTED! ⚡⚡⚡")
                            interrupt_evt.set()
                            last_interrupt_time = current_time
                            
                            # Stop AI playback immediately
                            try:
                                sd.stop()
                                state['is_ai_speaking'] = False
                            except:
                                pass
                            
                            # Send cancel to server ONLY if AI is generating
                            if state['ai_is_generating']:
                                try:
                                    await ws.send(json.dumps({
                                        "type": "response.cancel"
                                    }))
                                    print("   📤 Cancelled AI response generation")
                                    state['ai_is_generating'] = False
                                except Exception as e:
                                    pass  # Ignore errors if response already done
                except queue.Empty:
                    pass
                
                # Send audio to server
                audio_bytes = await asyncio.get_event_loop().run_in_executor(
                    None, audio_queue.get, True, 0.01
                )
                
                audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")
                
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": audio_base64
                }))
                
                chunk_count += 1
                
            except queue.Empty:
                await asyncio.sleep(0.001)
    finally:
        stream.stop()
        stream.close()
        print(f"\n✓ Sent {chunk_count} total audio chunks")


async def receive_events(ws, vad, interrupt_evt, state):
    """Receive and handle server events"""
    print("[Listening for events...]\n")
    
    audio_chunks = []
    current_item_id = None
    
    try:
        async for message in ws:
            data = json.loads(message)
            msg_type = data.get("type")
            
            if msg_type == "session.created":
                print("✓ Session created")
            
            elif msg_type == "session.updated":
                print("✓ Session ready\n")
            
            elif msg_type == "input_audio_buffer.speech_started":
                print("\n[🎤 Speech detected by SERVER]")
            
            elif msg_type == "input_audio_buffer.speech_stopped":
                print("[Speech stopped]")
                vad.reset()  # Reset client-side VAD
            
            elif msg_type == "response.created":
                response_data = data.get("response", {})
                current_response_id = response_data.get("id")
                print(f"\n[🤖 AI responding...]")
                audio_chunks = []
                interrupt_evt.clear()
                state['ai_is_generating'] = True  # Mark AI as generating
            
            elif msg_type == "response.output_item.added":
                output_item = data.get("item", {})
                current_item_id = output_item.get("id")
            
            elif msg_type == "response.audio.delta":
                # Check if interrupted
                if interrupt_evt.is_set():
                    continue
                
                # Collect audio chunks
                delta_b64 = data.get("delta")
                audio_bytes = base64.b64decode(delta_b64)
                audio_np = np.frombuffer(audio_bytes, dtype=np.int16)
                audio_chunks.append(audio_np)
            
            elif msg_type == "response.audio_transcript.delta":
                if not interrupt_evt.is_set():
                    print(data.get("delta", ""), end="", flush=True)
            
            elif msg_type == "response.done":
                state['ai_is_generating'] = False  # AI finished generating
                
                # Check if interrupted
                if interrupt_evt.is_set():
                    print("\n\n[⚡ Response was interrupted - skipping playback]\n")
                    audio_chunks = []
                    state['is_ai_speaking'] = False
                    continue
                
                print("\n")
                
                # Play received audio
                if audio_chunks:
                    full_audio = np.concatenate(audio_chunks)
                    
                    # Add 500ms of silence to prevent cutoff
                    silence_samples = int(SAMPLE_RATE * 0.5)
                    silence = np.zeros(silence_samples, dtype=np.int16)
                    full_audio_padded = np.concatenate([full_audio, silence])
                    
                    duration = len(full_audio) / SAMPLE_RATE
                    print(f"[▶️ Playing {duration:.2f}s...]")
                    
                    # Mark AI as speaking
                    state['is_ai_speaking'] = True
                    
                    # Play audio (non-blocking)
                    # VAD stays enabled but uses energy threshold to filter out AI echo
                    sd.play(full_audio_padded, SAMPLE_RATE, blocking=False)
                    
                    # Wait for playback to finish OR interruption
                    while state['is_ai_speaking'] and not interrupt_evt.is_set():
                        try:
                            if not sd.get_stream().active:
                                break
                        except:
                            break
                        await asyncio.sleep(0.01)  # Check very frequently
                    
                    if interrupt_evt.is_set():
                        try:
                            sd.stop()
                        except:
                            pass
                        print("⚡ Playback interrupted by user")
                    else:
                        try:
                            sd.wait()
                        except:
                            pass
                        print("✓ Playback complete")
                    
                    state['is_ai_speaking'] = False
                    vad.reset()  # Reset VAD state
                
                print("\n[Ready - speak again!]\n")
            
            elif msg_type == "error":
                error_info = data.get('error', {})
                error_msg = error_info.get('message', str(data))
                error_code = error_info.get('code', 'unknown')
                
                # Ignore harmless errors
                if error_code in ['response_cancel_not_active']:
                    # This just means response finished before cancel arrived - totally fine
                    continue
                
                print(f"\n❌ Error [{error_code}]: {error_msg}")
                
                # Don't break on certain errors, just log them
                if error_code not in ['invalid_request_error']:
                    break
    
    except websockets.exceptions.ConnectionClosed as e:
        print(f"\n⚠️ WebSocket connection closed: {e}")
    except Exception as e:
        print(f"\n❌ Unexpected error in receive_events: {e}")
        import traceback
        traceback.print_exc()


async def main():
    """Main function"""
    print("="*70)
    print("OpenAI Realtime API - LiveKit-Style Interruption")
    print("  (Client-side VAD for instant interruption)")
    print("="*70)
    print()
    
    headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        ("OpenAI-Beta", "realtime=v1")
    ]
    
    print("[Connecting to OpenAI...]")
    
    try:
        # Initialize client-side VAD
        vad = ClientSideVAD()
        interrupt_evt = asyncio.Event()
        
        # Shared state between tasks
        state = {
            'is_ai_speaking': False,      # Is AI audio currently playing?
            'ai_is_generating': False     # Is AI currently generating a response?
        }
        
        async with websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=30,
            ping_timeout=30,
            close_timeout=10
        ) as ws:
            print("✓ Connected")
            
            # Wait for session.created
            first_message = await ws.recv()
            await send_session_config(ws)
            await asyncio.sleep(0.5)
            
            # Run sending and receiving concurrently
            await asyncio.gather(
                send_audio_chunks(ws, vad, interrupt_evt, state),
                receive_events(ws, vad, interrupt_evt, state)
            )
    except KeyboardInterrupt:
        print("\n\n" + "="*70)
        print("Goodbye!")
        print("="*70)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())

