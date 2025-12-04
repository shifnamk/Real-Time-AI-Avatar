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
import os
import soundfile as sf

import queue
from queue import Queue
import torch.multiprocessing as mp

try:
    from .basereal import BaseReal  # type: ignore
    from .logger import logger  # type: ignore
except ImportError:  # pragma: no cover
    from basereal import BaseReal  # type: ignore
    from logger import logger  # type: ignore


class BaseASR:
    def __init__(self, opt, parent:BaseReal = None):
        self.opt = opt
        self.parent = parent

        self.fps = opt.fps # 20 ms per frame
        self.sample_rate = 16000
        self.chunk = self.sample_rate // self.fps # 320 samples per chunk (20ms * 16000 / 1000)
        self.queue = Queue()
        # Larger output queue to prevent audio starvation (500 frames = 10 seconds buffer)
        self.output_queue = mp.Queue(maxsize=500)

        self.batch_size = opt.batch_size

        self.frames = []
        self.stride_left_size = opt.l
        self.stride_right_size = opt.r
        #self.context_size = 10
        # PERFORMANCE FIX: Larger queues to prevent starvation
        # feat_queue: 16 batches buffer, output_queue: 500 frames (10s) buffer
        self.feat_queue = mp.Queue(maxsize=16)
        
        # Load humming audio for closed lips during silence
        self._load_closed_lips_audio()
        
        # Silence detection threshold (based on RMS stats: silence ~0.000001, speech ~0.05+)
        self.silence_rms_threshold = 0.0001  # Detects true silence without false triggers

        #self.warm_up()

    def _load_closed_lips_audio(self):
        """
        Load TTS humming audio for naturally closed lips during silence.
        This audio is processed by MuseTalk to generate stable closed-lip frames.
        """
        try:
            # Get the directory where this file is located
            base_dir = os.path.dirname(os.path.abspath(__file__))
            
            # Load 16kHz version for MuseTalk processing
            humming_16k_path = os.path.join(base_dir, 'closed_lips_humming.wav')
            self._closed_lips_audio_16k, _ = sf.read(humming_16k_path, dtype='float32')
            self._closed_lips_idx_16k = 0
            
            # Load 48kHz version for HQ output
            humming_48k_path = os.path.join(base_dir, 'closed_lips_humming_48k.wav')
            self._closed_lips_audio_48k, _ = sf.read(humming_48k_path, dtype='float32')
            self._closed_lips_idx_48k = 0
            
            logger.info(f"[CLOSED LIPS] Loaded humming audio for naturally closed lips during silence (16kHz: {len(self._closed_lips_audio_16k)} samples, 48kHz: {len(self._closed_lips_audio_48k)} samples)")
        except Exception as e:
            logger.warning(f"[CLOSED LIPS] Could not load humming audio: {e}")
            # Fallback to zeros
            self._closed_lips_audio_16k = np.zeros(self.chunk * 100, dtype=np.float32)
            self._closed_lips_audio_48k = np.zeros(self.chunk * 100 * 3, dtype=np.float32)
            self._closed_lips_idx_16k = 0
            self._closed_lips_idx_48k = 0

    def flush_talk(self):
        self.queue.queue.clear()

    def put_audio_frame(self,audio_chunk,datainfo:dict): #16khz 20ms pcm
        self.queue.put((audio_chunk,datainfo))

    #return frame:audio pcm; type: 0-normal speak, 1-silence; eventpoint:custom event sync with audio
    def get_audio_frame(self):        
        try:
            frame,eventpoint = self.queue.get(block=True,timeout=0.05)
            type = 0
            if hasattr(self, "_queue_starve_warn") and self._queue_starve_warn:
                self._queue_starve_warn = 0
            
            # SILENCE DETECTION: Check RMS to detect silence from voice agent
            rms = float(np.sqrt(np.mean(frame ** 2)))
            
            # DEBUG: Log RMS values to determine proper threshold
            if not hasattr(self, "_rms_log_count"):
                self._rms_log_count = 0
                self._rms_values = []  # Store for statistics
            self._rms_log_count += 1
            self._rms_values.append(rms)
            
            # Log every 25 frames (500ms) for detailed monitoring
            if self._rms_log_count % 25 == 0:
                recent_rms = self._rms_values[-25:]
                avg_rms = np.mean(recent_rms)
                max_rms = np.max(recent_rms)
                min_rms = np.min(recent_rms)
                logger.info(
                    f"[RMS DEBUG] Current={rms:.6f} | 500ms: Avg={avg_rms:.6f} Max={max_rms:.6f} Min={min_rms:.6f} | Threshold={self.silence_rms_threshold:.6f}"
                )
            
            # Keep only last 1000 values to track statistics
            if len(self._rms_values) > 1000:
                self._rms_values = self._rms_values[-1000:]
            
            # Print statistics summary every 500 frames (~10 seconds)
            if self._rms_log_count % 500 == 0:
                all_rms = np.array(self._rms_values[-500:])
                percentiles = np.percentile(all_rms, [5, 10, 25, 50, 75, 90, 95])
                logger.info(
                    f"\n{'='*80}\n"
                    f"[RMS STATS] Last 10 seconds ({len(all_rms)} frames):\n"
                    f"  Min: {np.min(all_rms):.6f}\n"
                    f"  5th percentile:  {percentiles[0]:.6f}\n"
                    f"  10th percentile: {percentiles[1]:.6f}\n"
                    f"  25th percentile: {percentiles[2]:.6f}\n"
                    f"  Median:          {percentiles[3]:.6f}\n"
                    f"  75th percentile: {percentiles[4]:.6f}\n"
                    f"  90th percentile: {percentiles[5]:.6f}\n"
                    f"  95th percentile: {percentiles[6]:.6f}\n"
                    f"  Max: {np.max(all_rms):.6f}\n"
                    f"  Mean: {np.mean(all_rms):.6f}\n"
                    f"  Current threshold: {self.silence_rms_threshold:.6f}\n"
                    f"  RECOMMENDATION: Set threshold between 5th-10th percentile for silence detection\n"
                    f"{'='*80}"
                )
            
            if rms < self.silence_rms_threshold:
                # Silence detected! 
                # Strategy: Send humming to MuseTalk for lip-sync, but keep SILENCE for audio output
                type = 2  # New type: closed lips mode
                
                # Get humming audio chunk for MuseTalk processing
                chunk_size = len(frame)
                if self._closed_lips_idx_16k + chunk_size > len(self._closed_lips_audio_16k):
                    self._closed_lips_idx_16k = 0  # Loop seamlessly
                
                humming_chunk = self._closed_lips_audio_16k[self._closed_lips_idx_16k:self._closed_lips_idx_16k + chunk_size].copy()
                self._closed_lips_idx_16k += chunk_size
                
                # CRITICAL SPLIT: Different audio for MuseTalk vs Output
                # 1. Send humming to MuseTalk (in output_queue for lip-sync processing)
                # 2. Keep silence for actual audio output (eventpoint for speaker)
                
                # Create SILENT audio for ALL output paths
                silent_frame_16k = np.zeros(chunk_size, dtype=np.float32)
                chunk_size_48k = chunk_size * 3  # 48kHz is 3x 16kHz
                silent_frame_48k = np.zeros(chunk_size_48k, dtype=np.float32)
                
                # REPLACE frame with humming for MuseTalk processing
                frame = humming_chunk
                
                # But eventpoint contains SILENT audio for output (speaker won't hear humming!)
                if isinstance(eventpoint, dict):
                    eventpoint['hq_audio'] = silent_frame_48k
                    eventpoint['hq_sample_rate'] = 48000
                    eventpoint['silent_output'] = True  # Flag to force silence
                else:
                    eventpoint = {
                        'hq_audio': silent_frame_48k,
                        'hq_sample_rate': 48000,
                        'silent_output': True
                    }
                
                # Log occasionally with audio path confirmation
                if not hasattr(self, "_closed_lips_log_count"):
                    self._closed_lips_log_count = 0
                self._closed_lips_log_count += 1
                if self._closed_lips_log_count % 100 == 1:
                    output_rms = float(np.sqrt(np.mean(silent_frame_48k ** 2)))
                    humming_rms = float(np.sqrt(np.mean(humming_chunk ** 2)))
                    logger.info(f"[CLOSED LIPS] Frame #{self._closed_lips_log_count}: Humming RMS={humming_rms:.4f}->MuseTalk | Output RMS={output_rms:.6f} (FORCED SILENT)")
            
            # eventpoint is already the datainfo dict containing HQ audio (if present)
            # No need to transform it - just pass it through
                
        except queue.Empty:
            if not hasattr(self, "_queue_starve_warn"):
                self._queue_starve_warn = 0
            self._queue_starve_warn += 1
            if self._queue_starve_warn % 100 == 0:
                logger.warning("[AUDIO PIPELINE] Input queue empty, sending humming audio to MuseTalk")
            if self.parent and self.parent.curr_state>1: #播放自定义音频
                frame = self.parent.get_audio_stream(self.parent.curr_state)
                type = self.parent.curr_state
            else:
                # Queue empty - inject humming for MuseTalk, silence for output
                type = 2
                chunk_size = self.chunk
                
                # Humming audio for MuseTalk
                if self._closed_lips_idx_16k + chunk_size > len(self._closed_lips_audio_16k):
                    self._closed_lips_idx_16k = 0
                
                frame = self._closed_lips_audio_16k[self._closed_lips_idx_16k:self._closed_lips_idx_16k + chunk_size].copy()
                self._closed_lips_idx_16k += chunk_size
                
                # SILENCE for audio output (not humming!)
                chunk_size_48k = chunk_size * 3
                silent_output = np.zeros(chunk_size_48k, dtype=np.float32)
                
                eventpoint = {
                    'hq_audio': silent_output,
                    'hq_sample_rate': 48000,
                    'silent_output': True  # Flag to force silence
                }

        return frame,type,eventpoint 

    #return frame:audio pcm; type: 0-normal speak, 1-silence; eventpoint:custom event sync with audio
    def get_audio_out(self): 
        return self.output_queue.get()
    
    def warm_up(self):
        for _ in range(self.stride_left_size + self.stride_right_size):
            audio_frame,type,eventpoint=self.get_audio_frame()
            self.frames.append(audio_frame)
            self.output_queue.put((audio_frame,type,eventpoint))
        for _ in range(self.stride_left_size):
            self.output_queue.get()

    def run_step(self):
        pass

    def get_next_feat(self,block,timeout):        
        return self.feat_queue.get(block,timeout)