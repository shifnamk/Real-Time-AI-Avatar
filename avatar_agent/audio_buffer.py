"""
Audio Buffer - Accumulates streaming audio and creates segments
"""

import numpy as np
import time
from threading import Lock
from collections import deque
from typing import Optional, Tuple
from utils import SAMPLE_RATE

class AudioSegment:
    """Represents a segment of audio with metadata"""
    def __init__(self, audio: np.ndarray, start_time: float, end_time: float, segment_id: int):
        self.audio = audio
        self.start_time = start_time
        self.end_time = end_time
        self.segment_id = segment_id
        self.duration = end_time - start_time
        
    def __repr__(self):
        return f"AudioSegment(id={self.segment_id}, {self.start_time:.2f}s-{self.end_time:.2f}s, {len(self.audio)} samples)"


class AudioBuffer:
    """
    Thread-safe circular buffer for streaming audio
    Automatically creates segments for processing
    """
    
    def __init__(
        self,
        max_buffer_seconds: float = 30.0,
        segment_duration: float = 2.0,
        segment_overlap: float = 0.5
    ):
        self.max_samples = int(max_buffer_seconds * SAMPLE_RATE)
        self.segment_duration = segment_duration
        self.segment_overlap = segment_overlap
        
        # Circular buffer
        self.buffer = np.zeros(self.max_samples, dtype=np.float32)
        self.write_pos = 0
        self.total_samples_written = 0
        
        # Timing
        self.start_time = time.time()
        self.last_segment_time = 0.0
        
        # Segments queue
        self.segments_queue = deque(maxlen=100)
        self.segment_counter = 0
        
        # Thread safety
        self.lock = Lock()
        
        print(f"[AudioBuffer] Initialized: {max_buffer_seconds}s buffer, {segment_duration}s segments, {segment_overlap}s overlap")
    
    def add_audio(self, audio_data: np.ndarray) -> None:
        """
        Add new audio samples to buffer
        Automatically creates segments when enough audio accumulated
        """
        with self.lock:
            n = len(audio_data)
            
            # Write to circular buffer
            if self.write_pos + n > self.max_samples:
                # Wrap around
                first_chunk = self.max_samples - self.write_pos
                self.buffer[self.write_pos:] = audio_data[:first_chunk]
                self.buffer[:n - first_chunk] = audio_data[first_chunk:]
                self.write_pos = n - first_chunk
            else:
                self.buffer[self.write_pos:self.write_pos + n] = audio_data
                self.write_pos += n
            
            self.total_samples_written += n
            
            # Check if we should create a new segment
            self._check_create_segment()
    
    def _check_create_segment(self) -> None:
        """Check if enough audio accumulated to create a segment"""
        current_time = self.total_samples_written / SAMPLE_RATE
        
        # Create segment if enough time passed
        if current_time - self.last_segment_time >= (self.segment_duration - self.segment_overlap):
            segment = self._create_segment()
            if segment is not None:
                self.segments_queue.append(segment)
                self.last_segment_time = current_time - self.segment_overlap
    
    def _create_segment(self) -> Optional[AudioSegment]:
        """Create a segment from recent audio"""
        segment_samples = int(self.segment_duration * SAMPLE_RATE)
        
        # Need enough samples
        if self.total_samples_written < segment_samples:
            return None
        
        # Extract segment (read backwards from write position)
        audio = self._read_recent(segment_samples)
        
        if len(audio) < segment_samples:
            return None
        
        # Calculate timing
        end_time = self.total_samples_written / SAMPLE_RATE
        start_time = end_time - self.segment_duration
        
        segment = AudioSegment(
            audio=audio,
            start_time=start_time,
            end_time=end_time,
            segment_id=self.segment_counter
        )
        
        self.segment_counter += 1
        return segment
    
    def _read_recent(self, n_samples: int) -> np.ndarray:
        """Read most recent n_samples from buffer"""
        n_samples = min(n_samples, self.total_samples_written, self.max_samples)
        
        if n_samples == 0:
            return np.array([], dtype=np.float32)
        
        if self.write_pos >= n_samples:
            return self.buffer[self.write_pos - n_samples:self.write_pos].copy()
        else:
            # Wrap around
            first_part = self.buffer[self.max_samples - (n_samples - self.write_pos):]
            second_part = self.buffer[:self.write_pos]
            return np.concatenate([first_part, second_part])
    
    def get_pending_segments(self) -> list[AudioSegment]:
        """Get all pending segments and clear queue"""
        with self.lock:
            segments = list(self.segments_queue)
            self.segments_queue.clear()
            return segments
    
    def get_recent_audio(self, duration_seconds: float = 1.0) -> np.ndarray:
        """Get most recent audio for realtime processing"""
        with self.lock:
            n_samples = int(duration_seconds * SAMPLE_RATE)
            return self._read_recent(n_samples)
    
    def get_stats(self) -> dict:
        """Get buffer statistics"""
        with self.lock:
            return {
                "total_seconds": self.total_samples_written / SAMPLE_RATE,
                "pending_segments": len(self.segments_queue),
                "segments_created": self.segment_counter,
                "buffer_usage": (self.total_samples_written % self.max_samples) / self.max_samples
            }

