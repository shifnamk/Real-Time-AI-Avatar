###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  email: lipku@foxmail.com
# 
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  
#       http://www.apache.org/licenses/LICENSE-2.0
# 
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

import time
import numpy as np
import threading

import queue
from queue import Queue
#import multiprocessing as mp
from baseasr import BaseASR
from musetalk.whisper.audio2feature import Audio2Feature

try:
    from logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

class MuseASR(BaseASR):
    def __init__(self, opt, parent,audio_processor:Audio2Feature):
        super().__init__(opt,parent)
        self.audio_processor = audio_processor
        
        # ASYNC OPTIMIZATION: Queue for batches awaiting Whisper processing
        # Larger queue to buffer during speech bursts
        self.processing_queue = Queue(maxsize=8)
        
        # Thread control
        self.processing_thread = None
        self.processing_stop_event = threading.Event()
        
        # Start the async Whisper processing thread
        self._start_processing_thread()
    
    def _start_processing_thread(self):
        """Start the background Whisper processing thread"""
        if self.processing_thread is not None and self.processing_thread.is_alive():
            return  # Already running
        
        self.processing_stop_event.clear()
        self.processing_thread = threading.Thread(
            target=self._whisper_processing_loop,
            name="whisper-processor",
            daemon=True
        )
        self.processing_thread.start()
        logger.info("[MuseASR] Started async Whisper processing thread")
    
    def _whisper_processing_loop(self):
        """Background thread that processes audio frames through Whisper"""
        logger.info("[MuseASR] Whisper processing thread started")
        
        while not self.processing_stop_event.is_set():
            try:
                # Get batch of frames to process (blocking with timeout)
                frame_batch = self.processing_queue.get(block=True, timeout=0.5)
                
                # Process through Whisper encoder
                start_time = time.time()
                inputs = np.concatenate(frame_batch)
                whisper_feature = self.audio_processor.audio2feat(inputs)
                
                # Create chunks for MuseTalk
                whisper_chunks = self.audio_processor.feature2chunks(
                    feature_array=whisper_feature,
                    fps=self.fps/2,
                    batch_size=self.batch_size,
                    start=self.stride_left_size/2
                )
                
                # Put results in the feature queue for MuseTalk inference
                self.feat_queue.put(whisper_chunks)
                
                # Log performance periodically
                if not hasattr(self, '_whisper_process_count'):
                    self._whisper_process_count = 0
                    self._whisper_process_time = 0.0
                
                self._whisper_process_count += 1
                self._whisper_process_time += (time.time() - start_time)
                
                if self._whisper_process_count % 50 == 0:
                    avg_time = self._whisper_process_time / self._whisper_process_count
                    logger.info(f"[MuseASR] Whisper avg processing: {avg_time*1000:.1f}ms, queue: {self.processing_queue.qsize()}/8")
                    self._whisper_process_count = 0
                    self._whisper_process_time = 0.0
                
            except queue.Empty:
                continue  # No frames to process, keep waiting
            except Exception as e:
                logger.error(f"[MuseASR] Error in Whisper processing thread: {e}")
        
        logger.info("[MuseASR] Whisper processing thread stopped")
    
    def stop_processing_thread(self):
        """Stop the background Whisper processing thread"""
        if self.processing_thread is None:
            return
        
        logger.info("[MuseASR] Stopping Whisper processing thread...")
        self.processing_stop_event.set()
        
        if self.processing_thread.is_alive():
            self.processing_thread.join(timeout=2.0)
        
        self.processing_thread = None
        logger.info("[MuseASR] Whisper processing thread stopped")
    
    def flush_talk(self):
        """Override to also clean up processing thread"""
        super().flush_talk()
        # Clear the processing queue
        try:
            while not self.processing_queue.empty():
                self.processing_queue.get_nowait()
        except queue.Empty:
            pass
    
    def __del__(self):
        """Cleanup on deletion"""
        self.stop_processing_thread()

    def run_step(self):
        """
        OPTIMIZED: Non-blocking audio frame collection.
        Frames are submitted to background Whisper thread for processing.
        """
        # Pull audio frames from WebRTC queue (fast, non-blocking)
        for _ in range(self.batch_size*2):
            audio_frame,type,eventpoint = self.get_audio_frame()
            self.frames.append(audio_frame)
            self.output_queue.put((audio_frame,type,eventpoint))
        
        # Check if we have enough context to process
        if len(self.frames) <= self.stride_left_size + self.stride_right_size:
            return
        
        # CRITICAL: Submit batch for async Whisper processing (non-blocking)
        # Make a copy of frames to process
        frame_batch = list(self.frames)
        
        try:
            # Try to put in processing queue (non-blocking)
            self.processing_queue.put_nowait(frame_batch)
        except queue.Full:
            # Processing queue is full - Whisper thread is backed up
            # Drop this batch and log warning
            logger.warning("[MuseASR] Whisper processing queue full - dropping batch (processing can't keep up)")
        
        # Discard old frames to save memory
        self.frames = self.frames[-(self.stride_left_size + self.stride_right_size):]
