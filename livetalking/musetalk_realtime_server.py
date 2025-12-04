#!/usr/bin/env python3
###############################################################################
#  Real-time MuseTalk WebSocket Server using MuseReal
#  Integrates with LiveKit voice agent for true real-time lip-sync
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
from queue import Queue, Empty
from threading import Thread, Event
import time
import multiprocessing as mp

from musereal import MuseReal, load_model, load_avatar, warm_up
from museasr import MuseASR
from logger import logger

class Namespace:
    """Simple namespace for options"""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

class MuseTalkRealtimeServer:
    def __init__(self, avatar_id='calm_avatar_realtime', batch_size=8, fps=25):
        logger.info(f"🚀 Initializing MuseTalk Real-time Server...")
        logger.info(f"   Avatar ID: {avatar_id}")
        logger.info(f"   Batch Size: {batch_size}")
        logger.info(f"   FPS: {fps}")
        
        # Create options namespace
        self.opt = Namespace(
            fps=fps,
            batch_size=batch_size,
            avatar_id=avatar_id,
            sessionid=0,
            l=10,  # left context
            m=8,   # middle context
            r=10,  # right context
        )
        
        # Load models
        logger.info("Loading MuseTalk models...")
        self.model = load_model()
        self.avatar = load_avatar(avatar_id)
        
        # Warm up
        logger.info("Warming up model...")
        warm_up(batch_size, self.model)
        
        # Create MuseReal instance
        self.muse_real = MuseReal(self.opt, self.model, self.avatar)
        
        # Server state
        self.clients = {}  # websocket -> client_data
        self.frame_queues = {}  # websocket -> queue
        
        logger.info("✅ MuseTalk Real-time Server initialized!")
        
    async def handle_client(self, websocket, path):
        """Handle a new WebSocket client connection"""
        client_id = id(websocket)
        logger.info(f"✅ Client {client_id} connected")
        
        # Initialize client
        self.clients[websocket] = {
            'id': client_id,
            'audio_buffer': [],
            'frame_count': 0,
        }
        self.frame_queues[websocket] = Queue(maxsize=30)
        
        # Start frame sender for this client
        sender_task = asyncio.create_task(self.frame_sender(websocket))
        
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    
                    if data['type'] == 'audio':
                        # Process audio chunk
                        await self.handle_audio(websocket, data['data'])
                        
                    elif data['type'] == 'silence':
                        # Send silence frame
                        await self.send_silence_frame(websocket)
                        
                    elif data['type'] == 'ping':
                        await websocket.send(json.dumps({'type': 'pong'}))
                        
                except json.JSONDecodeError as e:
                    logger.error(f"JSON decode error: {e}")
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"🔌 Client {client_id} connection closed")
        except Exception as e:
            logger.error(f"Unexpected error with client {client_id}: {e}")
        finally:
            # Cleanup
            sender_task.cancel()
            if websocket in self.clients:
                del self.clients[websocket]
            if websocket in self.frame_queues:
                del self.frame_queues[websocket]
            logger.info(f"👋 Client {client_id} disconnected. Active clients: {len(self.clients)}")
    
    async def handle_audio(self, websocket, audio_data):
        """Process audio data and queue frame generation"""
        try:
            # Convert to numpy array
            audio_array = np.array(audio_data, dtype=np.float32)
            
            # Check if audio has content
            audio_energy = np.abs(audio_array).mean()
            
            if audio_energy < 0.01:
                # Silent audio
                await self.send_silence_frame(websocket)
            else:
                # Push audio to ASR for processing
                # Note: This is simplified - you'd need to integrate with MuseASR properly
                # For now, generate a frame from the cycle
                frame = self.get_next_frame()
                self.frame_queues[websocket].put(frame)
                
        except Exception as e:
            logger.error(f"Error handling audio: {e}")
    
    def get_next_frame(self):
        """Get next frame from the avatar cycle"""
        # This is a placeholder - in real implementation, 
        # you'd use MuseReal's inference pipeline
        frame_list = self.avatar[0]  # frame_list_cycle
        idx = int(time.time() * 25) % len(frame_list)
        return frame_list[idx]
    
    async def send_silence_frame(self, websocket):
        """Send a silence frame (first frame with closed mouth)"""
        frame_list = self.avatar[0]
        silence_frame = frame_list[0]
        self.frame_queues[websocket].put(silence_frame)
    
    async def frame_sender(self, websocket):
        """Continuously send frames from queue to client"""
        try:
            while True:
                try:
                    # Get frame from queue (non-blocking with timeout)
                    frame = self.frame_queues[websocket].get(timeout=0.05)
                    await self.send_frame(websocket, frame)
                except Empty:
                    # No frame available, send silence
                    await asyncio.sleep(0.04)  # ~25 FPS
                except Exception as e:
                    logger.error(f"Error in frame sender: {e}")
                    break
        except asyncio.CancelledError:
            logger.info("Frame sender cancelled")
    
    async def send_frame(self, websocket, frame):
        """Convert frame to JPEG and send as base64"""
        try:
            # Convert numpy array to PIL Image
            if isinstance(frame, np.ndarray):
                if frame.dtype != np.uint8:
                    frame = (frame * 255).astype(np.uint8)
                # Convert BGR to RGB if needed
                if len(frame.shape) == 3 and frame.shape[2] == 3:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(frame)
            else:
                img = frame
            
            # Convert to JPEG
            buffer = BytesIO()
            img.save(buffer, format='JPEG', quality=85)
            img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
            # Send to client
            await websocket.send(json.dumps({
                'type': 'frame',
                'frame': img_base64,
                'timestamp': time.time()
            }))
            
            # Update stats
            if websocket in self.clients:
                self.clients[websocket]['frame_count'] += 1
            
        except Exception as e:
            logger.error(f"Error sending frame: {e}")
    
    async def start(self, host='0.0.0.0', port=8010):
        """Start the WebSocket server"""
        logger.info(f"\n{'='*60}")
        logger.info(f"🚀 MuseTalk Real-time WebSocket Server Starting...")
        logger.info(f"{'='*60}")
        logger.info(f"   Host: {host}")
        logger.info(f"   Port: {port}")
        logger.info(f"   WebSocket URL: ws://{host}:{port}/musetalk")
        logger.info(f"{'='*60}\n")
        
        async with websockets.serve(
            self.handle_client, 
            host, 
            port,
            ping_interval=20,
            ping_timeout=10,
        ):
            logger.info("✅ Server is running and ready for connections!")
            logger.info("   Press Ctrl+C to stop the server\n")
            await asyncio.Future()  # Run forever

def main():
    parser = argparse.ArgumentParser(description='MuseTalk Real-time WebSocket Server')
    parser.add_argument('--avatar_id', type=str, default='calm_avatar_realtime',
                        help='Avatar ID to use')
    parser.add_argument('--host', type=str, default='0.0.0.0',
                        help='Server host')
    parser.add_argument('--port', type=int, default=8010,
                        help='Server port')
    parser.add_argument('--batch_size', type=int, default=8,
                        help='Batch size for inference')
    parser.add_argument('--fps', type=int, default=25,
                        help='Target FPS')
    
    args = parser.parse_args()
    
    try:
        # Set multiprocessing start method
        mp.set_start_method('spawn', force=True)
        
        server = MuseTalkRealtimeServer(
            avatar_id=args.avatar_id,
            batch_size=args.batch_size,
            fps=args.fps
        )
        asyncio.run(server.start(host=args.host, port=args.port))
    except KeyboardInterrupt:
        logger.info("\n\n👋 Server stopped by user")
    except Exception as e:
        logger.error(f"\n❌ Server error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()


