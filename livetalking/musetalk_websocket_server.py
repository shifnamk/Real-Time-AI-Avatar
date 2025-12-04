#!/usr/bin/env python3
###############################################################################
#  Real-time MuseTalk WebSocket Server
#  Handles audio streaming and generates lip-synced video frames
###############################################################################

import asyncio
import websockets
import json
import numpy as np
import torch
import base64
from io import BytesIO
from PIL import Image
import cv2
import argparse
from queue import Queue
from threading import Thread
import time

from musereal import MuseReal, load_model, load_avatar

class MuseTalkWebSocketServer:
    def __init__(self, avatar_id='calm_avatar_realtime', batch_size=8):
        print(f"🚀 Initializing MuseTalk WebSocket Server...")
        print(f"   Avatar ID: {avatar_id}")
        print(f"   Batch Size: {batch_size}")
        
        # Load MuseTalk models
        self.model = load_model()
        # load_avatar returns: frame_list_cycle, mask_list_cycle, coord_list_cycle, mask_coords_list_cycle, input_latent_list_cycle
        avatar_data = load_avatar(avatar_id)
        self.frame_list_cycle = avatar_data[0]
        self.mask_list_cycle = avatar_data[1]
        self.coord_list_cycle = avatar_data[2]
        self.mask_coords_list_cycle = avatar_data[3]
        self.input_latent_list_cycle = avatar_data[4]
        
        # Server state
        self.clients = set()
        self.batch_size = batch_size
        self.audio_queue = Queue()
        self.frame_cache = {}
        
        # Get silence frame (first frame with closed mouth)
        self.silence_frame = self.frame_list_cycle[0]
        
        print("✅ MuseTalk models loaded successfully!")
        
    async def handle_client(self, websocket, path):
        """Handle a new WebSocket client connection"""
        client_id = id(websocket)
        self.clients.add(websocket)
        print(f"✅ Client {client_id} connected. Total clients: {len(self.clients)}")
        
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    
                    if data['type'] == 'audio':
                        # Process audio and generate frame
                        await self.handle_audio(websocket, data['data'])
                        
                    elif data['type'] == 'silence':
                        # Send silence frame (closed mouth)
                        await self.send_silence_frame(websocket)
                        
                    elif data['type'] == 'ping':
                        # Respond to ping
                        await websocket.send(json.dumps({'type': 'pong'}))
                        
                except json.JSONDecodeError as e:
                    print(f"❌ JSON decode error: {e}")
                    await self.send_error(websocket, "Invalid JSON format")
                except Exception as e:
                    print(f"❌ Error processing message: {e}")
                    await self.send_error(websocket, str(e))
                    
        except websockets.exceptions.ConnectionClosed:
            print(f"🔌 Client {client_id} connection closed")
        except Exception as e:
            print(f"❌ Unexpected error with client {client_id}: {e}")
        finally:
            self.clients.discard(websocket)
            print(f"👋 Client {client_id} disconnected. Total clients: {len(self.clients)}")
    
    async def handle_audio(self, websocket, audio_data):
        """Process audio data and generate lip-synced frame"""
        try:
            # Convert audio to numpy array
            audio_array = np.array(audio_data, dtype=np.float32)
            
            # Check if audio has actual content (not silence)
            audio_energy = np.abs(audio_array).mean()
            
            if audio_energy < 0.01:  # Threshold for silence detection
                # Audio is silent, send silence frame
                await self.send_silence_frame(websocket)
            else:
                # Generate lip-synced frame
                frame = self.generate_frame(audio_array)
                await self.send_frame(websocket, frame)
                
        except Exception as e:
            print(f"❌ Error handling audio: {e}")
            await self.send_error(websocket, f"Audio processing error: {str(e)}")
    
    def generate_frame(self, audio_data):
        """
        Generate a lip-synced frame from audio data
        This is a simplified version - you'll need to adapt it to your MuseReal implementation
        """
        try:
            # For now, return a frame from the cycle
            # In a full implementation, you would:
            # 1. Convert audio to mel spectrogram
            # 2. Run through MuseTalk model
            # 3. Generate the lip-synced frame
            
            # Placeholder: return a random frame from the avatar
            frame_idx = int(time.time() * 25) % len(self.frame_list_cycle)
            frame = self.frame_list_cycle[frame_idx]
            
            return frame
            
        except Exception as e:
            print(f"❌ Error generating frame: {e}")
            return self.silence_frame
    
    async def send_silence_frame(self, websocket):
        """Send a silence frame (closed mouth)"""
        await self.send_frame(websocket, self.silence_frame)
    
    async def send_frame(self, websocket, frame):
        """Convert frame to JPEG and send as base64"""
        try:
            # Convert numpy array to PIL Image
            if isinstance(frame, np.ndarray):
                # Ensure frame is in correct format (RGB, uint8)
                if frame.dtype != np.uint8:
                    frame = (frame * 255).astype(np.uint8)
                img = Image.fromarray(frame)
            else:
                img = frame
            
            # Convert to JPEG
            buffer = BytesIO()
            img.save(buffer, format='JPEG', quality=90)
            img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
            # Send to client
            await websocket.send(json.dumps({
                'type': 'frame',
                'frame': img_base64,
                'timestamp': time.time()
            }))
            
        except Exception as e:
            print(f"❌ Error sending frame: {e}")
            await self.send_error(websocket, f"Frame sending error: {str(e)}")
    
    async def send_error(self, websocket, message):
        """Send error message to client"""
        try:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': message,
                'timestamp': time.time()
            }))
        except Exception as e:
            print(f"❌ Error sending error message: {e}")
    
    async def start(self, host='0.0.0.0', port=8010):
        """Start the WebSocket server"""
        print(f"\n{'='*60}")
        print(f"🚀 MuseTalk WebSocket Server Starting...")
        print(f"{'='*60}")
        print(f"   Host: {host}")
        print(f"   Port: {port}")
        print(f"   WebSocket URL: ws://{host}:{port}/musetalk")
        print(f"{'='*60}\n")
        
        async with websockets.serve(
            self.handle_client, 
            host, 
            port,
            subprotocols=['musetalk']
        ):
            print("✅ Server is running and ready for connections!")
            print("   Press Ctrl+C to stop the server\n")
            await asyncio.Future()  # Run forever

def main():
    parser = argparse.ArgumentParser(description='MuseTalk WebSocket Server')
    parser.add_argument('--avatar_id', type=str, default='calm_avatar_realtime',
                        help='Avatar ID to use (default: calm_avatar_realtime)')
    parser.add_argument('--host', type=str, default='0.0.0.0',
                        help='Server host (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8010,
                        help='Server port (default: 8010)')
    parser.add_argument('--batch_size', type=int, default=8,
                        help='Batch size for inference (default: 8)')
    
    args = parser.parse_args()
    
    try:
        server = MuseTalkWebSocketServer(
            avatar_id=args.avatar_id,
            batch_size=args.batch_size
        )
        asyncio.run(server.start(host=args.host, port=args.port))
    except KeyboardInterrupt:
        print("\n\n👋 Server stopped by user")
    except Exception as e:
        print(f"\n❌ Server error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()

