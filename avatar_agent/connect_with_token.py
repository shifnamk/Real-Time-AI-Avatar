"""
Direct connection to LiveKit room using token
This bypasses the CLI and connects directly
"""

import os
import sys
import asyncio
import signal
from dotenv import load_dotenv
from livekit import rtc
from avatar_agent import AvatarPipeline, process_audio_stream

# Load environment
load_dotenv()

# Flush output immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


async def connect_to_room(url: str, token: str, room_name: str):
    """Connect to LiveKit room with token"""
    
    print("\n" + "="*60)
    print(f"AVATAR AGENT CONNECTING TO ROOM: {room_name}")
    print(f"URL: {url}")
    print("="*60 + "\n")
    
    # Initialize pipeline
    try:
        pipeline = AvatarPipeline()
    except Exception as e:
        print(f"[ERROR] Failed to initialize pipeline: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Create room
    room = rtc.Room()
    
    @room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        print(f"[PARTICIPANT] Connected: {participant.identity}")
    
    @room.on("track_subscribed")
    def on_track_subscribed(
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant
    ):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            print(f"[AUDIO] Subscribed to audio from: {participant.identity}\n")
            # Process audio stream
            audio_stream = rtc.AudioStream(track)
            asyncio.create_task(process_audio_stream(audio_stream, pipeline))
    
    # Connect to room
    print(f"[CONNECTING] Attempting to connect to LiveKit...")
    print(f"[DEBUG] URL: {url}")
    print(f"[DEBUG] Token length: {len(token)}")
    print(f"[DEBUG] Room: {room_name}")
    
    try:
        print("[DEBUG] Calling room.connect()...")
        sys.stdout.flush()
        
        # Add timeout to connection attempt
        await asyncio.wait_for(room.connect(url, token), timeout=30.0)
        print(f"[OK] Connected to room: {room_name}\n")
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
    
    # Create and publish video track
    print("[VIDEO] Creating video source and track...")
    video_source = rtc.VideoSource(512, 512)
    video_track = rtc.LocalVideoTrack.create_video_track("avatar_video", video_source)
    
    options = rtc.TrackPublishOptions()
    options.source = rtc.TrackSource.SOURCE_CAMERA
    
    print("[VIDEO] Publishing video track...")
    await room.local_participant.publish_track(video_track, options)
    print("[OK] Video track published\n")
    
    # Start processing loops
    print("[LOOPS] Starting audio and video processing loops...")
    segment_task = asyncio.create_task(pipeline.segment_processing_loop())
    video_task = asyncio.create_task(pipeline.video_streaming_loop(video_source))
    
    print("[OK] Avatar agent is now streaming!\n")
    
    # Wait for completion
    try:
        await asyncio.gather(segment_task, video_task)
    except asyncio.CancelledError:
        print("\n[SHUTDOWN] Stopping avatar agent...")
        pipeline.stop()
        await room.disconnect()
        print("[OK] Disconnected")
    except Exception as e:
        print(f"\n[ERROR] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        pipeline.stop()
        await room.disconnect()


if __name__ == "__main__":
    # Get credentials from command line or environment
    if len(sys.argv) >= 4:
        url = sys.argv[1]
        token = sys.argv[2]
        room_name = sys.argv[3]
    else:
        url = os.getenv("LIVEKIT_URL")
        token = os.getenv("LIVEKIT_TOKEN")
        room_name = os.getenv("LIVEKIT_ROOM")
    
    if not url or not token or not room_name:
        print("[ERROR] Missing credentials")
        print("Usage: python connect_with_token.py <url> <token> <room_name>")
        print("Or set LIVEKIT_URL, LIVEKIT_TOKEN, LIVEKIT_ROOM environment variables")
        sys.exit(1)
    
    # Run
    asyncio.run(connect_to_room(url, token, room_name))

