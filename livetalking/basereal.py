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

import math
import torch
import numpy as np

import subprocess
import os
import time
import cv2
import glob
import resampy
import hashlib
import pickle

import queue
from queue import Queue
from collections import deque
from threading import Thread, Event
from io import BytesIO
import soundfile as sf

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
from av import AudioFrame, VideoFrame

import av
from fractions import Fraction
from pathlib import Path

from ttsreal import EdgeTTS,SovitsTTS,XTTS,CosyVoiceTTS,FishTTS,TencentTTS,DoubaoTTS,IndexTTS2,AzureTTS
from logger import logger

from tqdm import tqdm

_CUSTOM_IMAGE_CACHE = {}
_CUSTOM_AUDIO_CACHE = {}
_FRAME_CACHE_DIR = Path(__file__).resolve().parent / "cache" / "custom_frames"
_FRAME_CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _image_cache_key(cache_key, target_size):
    if not cache_key:
        return None
    if not target_size:
        return cache_key
    return f"{cache_key}|{target_size[0]}x{target_size[1]}"

def _cache_file_path(cache_lookup_key: str) -> Path:
    digest = hashlib.sha1(cache_lookup_key.encode("utf-8")).hexdigest()
    return _FRAME_CACHE_DIR / f"{digest}.npz"

def read_imgs(img_list, cache_key=None, target_size=None):
    cache_lookup_key = _image_cache_key(cache_key, target_size)
    if cache_lookup_key and cache_lookup_key in _CUSTOM_IMAGE_CACHE:
        logger.info(f'using in-memory cached images for {cache_lookup_key}')
        return _CUSTOM_IMAGE_CACHE[cache_lookup_key]

    disk_cache_path = None
    if cache_lookup_key:
        disk_cache_path = _cache_file_path(cache_lookup_key)
        if disk_cache_path.exists():
            logger.info(f'loading frames from disk cache: {disk_cache_path.name}')
            with np.load(disk_cache_path) as data:
                frames_arr = data["frames"]
            frames = [frame for frame in frames_arr]
            _CUSTOM_IMAGE_CACHE[cache_lookup_key] = frames
            return frames

    frames = []
    logger.info('reading images...')
    for img_path in tqdm(img_list):
        frame = cv2.imread(img_path)
        if target_size:
            frame = cv2.resize(frame, target_size)
        frames.append(frame)

    if cache_lookup_key:
        _CUSTOM_IMAGE_CACHE[cache_lookup_key] = frames
        if disk_cache_path:
            try:
                frames_arr = np.stack(frames, axis=0)
                np.savez_compressed(disk_cache_path, frames=frames_arr)
                logger.info(f'saved frames to disk cache: {disk_cache_path.name}')
            except Exception as exc:
                logger.warning(f'failed to write disk cache {disk_cache_path}: {exc}')

    return frames

def play_audio(quit_event,queue):        
    import pyaudio
    p = pyaudio.PyAudio()
    stream = p.open(
        rate=16000,
        channels=1,
        format=8,
        output=True,
        output_device_index=1,
    )
    stream.start_stream()
    # while queue.qsize() <= 0:
    #     time.sleep(0.1)
    while not quit_event.is_set():
        stream.write(queue.get(block=True))
    stream.close()

class BaseReal:
    def __init__(self, opt):
        self.opt = opt
        self.sample_rate = 16000
        self.chunk = self.sample_rate // opt.fps # 320 samples per chunk (20ms * 16000 / 1000)
        self.sessionid = self.opt.sessionid
        self.target_width = getattr(opt, "W", 512)
        self.target_height = getattr(opt, "H", 512)
        
        # Initialize idle frame tracking
        self.idle_frame_idx = 0
        self.idle_frame_list_cycle = None

        if opt.tts == "edgetts":
            self.tts = EdgeTTS(opt,self)
        elif opt.tts == "gpt-sovits":
            self.tts = SovitsTTS(opt,self)
        elif opt.tts == "xtts":
            self.tts = XTTS(opt,self)
        elif opt.tts == "cosyvoice":
            self.tts = CosyVoiceTTS(opt,self)
        elif opt.tts == "fishtts":
            self.tts = FishTTS(opt,self)
        elif opt.tts == "tencent":
            self.tts = TencentTTS(opt,self)
        elif opt.tts == "doubao":
            self.tts = DoubaoTTS(opt,self)
        elif opt.tts == "indextts2":
            self.tts = IndexTTS2(opt,self)
        elif opt.tts == "azuretts":
            self.tts = AzureTTS(opt,self)

        self.speaking = False

        self.recording = False
        self._record_video_pipe = None
        self._record_audio_pipe = None
        self.width = self.height = 0

        self.curr_state=0
        self._audio_buffer = np.zeros(0, dtype=np.float32)
        self._audio_buffer_hq = np.zeros(0, dtype=np.float32)  # HQ audio buffer (original 48kHz)
        self._hq_sample_rate = 48000  # Track original sample rate
        self._silent_audio_chunk = np.zeros(self.chunk, dtype=np.float32)
        self._silent_audio_chunk_hq = np.zeros(int(self.chunk * 48000 / self.sample_rate), dtype=np.float32)  # HQ silent chunk
        self.custom_img_cycle = {}
        self.custom_img_meta = {}
        self.custom_audio_cycle = {}
        self.custom_audio_index = {}
        self.custom_index = {}
        self.custom_opt = {}
        # Store coords, masks, and latents for each custom clip
        self.custom_coords = {}
        self.custom_masks = {}
        self.custom_mask_coords = {}
        self.custom_latents = {}
        self.__loadcustom()

        # Runtime handles for webRTC integration
        self._loop = None
        self._audio_track = None
        self._video_track = None
        self._player_quit_event = None

        # Audio/video sync controls
        self.av_sync_offset_ms = getattr(opt, "av_sync_offset_ms", 0.0)
        chunk_ms = 1000.0 * self.chunk / self.sample_rate if self.sample_rate else 0.0
        offset_chunks = int(round(self.av_sync_offset_ms / chunk_ms)) if chunk_ms else 0
        self._audio_delay_chunks = max(0, offset_chunks)
        self._audio_drop_total = max(0, -offset_chunks)
        self._audio_drop_remaining = self._audio_drop_total
        self._audio_delay_buffer = deque()

        # MuseTalk idle-frame tracking to keep lips fully closed during silence
        self._musetalk_idle_frame = None
        self._silence_run = 0
        self._silence_since_idle_update = 0
        self._captured_idle_this_silence = False
        fps = getattr(opt, "fps", 50) or 50
        self._idle_capture_threshold = max(3, int(fps * 0.2))  # wait ~200 ms before freezing an idle pose
        self._idle_refresh_interval = max(fps, self._idle_capture_threshold)  # refresh idle after ~1 s of silence

    def put_msg_txt(self,msg,datainfo:dict={}):
        self.tts.put_msg_txt(msg,datainfo)
    
    def put_audio_frame(self,audio_chunk,datainfo:dict={}): #16khz 20ms pcm
        self.asr.put_audio_frame(audio_chunk,datainfo)

    def put_audio_file(self,filebyte,datainfo:dict={}): 
        input_stream = BytesIO(filebyte)
        
        # Read original HQ audio first
        stream_hq, sample_rate = sf.read(input_stream)
        if stream_hq.size == 0:
            return

        stream_hq = stream_hq.astype(np.float32)
        if stream_hq.ndim > 1:
            stream_hq = stream_hq[:, 0]
        
        # Store original sample rate
        self._hq_sample_rate = sample_rate
        
        # Downsample for MuseTalk processing
        if sample_rate != self.sample_rate:
            stream = resampy.resample(x=stream_hq, sr_orig=sample_rate, sr_new=self.sample_rate)
        else:
            stream = stream_hq.copy()

        # Accumulate both HQ and downsampled audio
        stream = np.concatenate([self._audio_buffer, stream])
        stream_hq = np.concatenate([self._audio_buffer_hq, stream_hq])
        
        total_len = stream.shape[0]
        total_len_hq = stream_hq.shape[0]
        
        # Calculate HQ chunk size (proportional to sample rate)
        chunk_size_hq = int(self.chunk * sample_rate / self.sample_rate)
        
        idx = 0
        idx_hq = 0
        
        # Send both versions in sync
        while (total_len - idx) >= self.chunk and (total_len_hq - idx_hq) >= chunk_size_hq:
            # Low-res chunk for MuseTalk (16kHz)
            chunk = stream[idx:idx+self.chunk]
            # High-res chunk for output (48kHz)
            chunk_hq = stream_hq[idx_hq:idx_hq+chunk_size_hq]
            
            # Store HQ chunk in datainfo for passthrough
            datainfo_with_hq = datainfo.copy()
            datainfo_with_hq['hq_audio'] = chunk_hq
            datainfo_with_hq['hq_sample_rate'] = sample_rate
            
            self.put_audio_frame(chunk, datainfo_with_hq)
            idx += self.chunk
            idx_hq += chunk_size_hq

        # Store leftover samples for both
        self._audio_buffer = stream[idx:] if idx < total_len else np.zeros(0, dtype=np.float32)
        self._audio_buffer_hq = stream_hq[idx_hq:] if idx_hq < total_len_hq else np.zeros(0, dtype=np.float32)
    
    def __create_bytes_stream(self,byte_stream):
        #byte_stream=BytesIO(buffer)
        stream, sample_rate = sf.read(byte_stream) # [T*sample_rate,] float64
        logger.debug(f'[AUDIO] Received stream {sample_rate}Hz: {stream.shape}')
        stream = stream.astype(np.float32)

        if stream.ndim > 1:
            logger.debug(f'[AUDIO] Multi-channel audio, using first channel')
            stream = stream[:, 0]
    
        if sample_rate != self.sample_rate and stream.shape[0]>0:
            logger.debug(f'[AUDIO] Resampling {sample_rate}Hz → {self.sample_rate}Hz')
            stream = resampy.resample(x=stream, sr_orig=sample_rate, sr_new=self.sample_rate)

        return stream

    def flush_talk(self):
        self.tts.flush_talk()
        self.asr.flush_talk()
        self._audio_buffer = np.zeros(0, dtype=np.float32)
        self._audio_buffer_hq = np.zeros(0, dtype=np.float32)
        self.reset_audio_sync_state()

    # ------------------------------------------------------------
    # Async helper utilities
    # ------------------------------------------------------------
    def _submit_to_loop(self, loop, coro, description: str, timeout: float = 0.2) -> bool:
        """
        Safely execute a coroutine on the target asyncio loop, cancelling it if
        it blocks longer than the provided timeout. This prevents pending
        Queue.put tasks from leaking when sessions shut down.
        """
        if loop is None:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError as exc:
            logger.debug(f"Loop unavailable while submitting {description}: {exc}")
            return False

        try:
            future.result(timeout)
            return True
        except FutureTimeoutError:
            future.cancel()
            logger.debug(f"Timed out submitting {description}; cancelling task")
        except Exception as exc:
            logger.debug(f"Failed to submit {description}: {exc}")
        return False

    def _send_video_frame(self, loop, track, frame, eventpoint=None) -> bool:
        if track is None:
            return False
        return self._submit_to_loop(
            loop,
            track.enqueue_frame(frame, eventpoint),
            "video frame"
        )

    def _send_audio_frame(self, loop, track, frame, eventpoint=None) -> bool:
        if track is None:
            return False
        return self._submit_to_loop(
            loop,
            track.enqueue_frame(frame, eventpoint),
            "audio frame"
        )

    def _drain_queue(self, q):
        if q is None:
            return
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass
        except Exception as exc:
            logger.debug(f"Queue drain skipped: {exc}")

    def reset_audio_sync_state(self):
        self._audio_delay_buffer.clear()
        self._audio_drop_remaining = self._audio_drop_total
        # Clear audio pacing buffer on reset
        if hasattr(self, '_audio_pacing_buffer'):
            self._audio_pacing_buffer.clear()

    def _apply_audio_sync_offset(self, frames):
        """
        Apply configured AV sync offset to a list of audio frames.
        Returns frames ready to send this iteration.
        """
        # Fast path: If offset is 0, just return all frames immediately (no sync adjustment needed)
        if self.av_sync_offset_ms == 0.0:
            return frames
        
        if not frames and not self._audio_delay_buffer:
            return []

        processed = []
        if self._audio_drop_remaining:
            for frame in frames:
                if self._audio_drop_remaining:
                    self._audio_drop_remaining -= 1
                    logger.debug(f"[SYNC] Dropping audio frame, {self._audio_drop_remaining} remaining")
                    continue
                processed.append(frame)
        else:
            processed = list(frames)

        if self._audio_delay_chunks:
            ready = []
            self._audio_delay_buffer.extend(processed)
            while len(self._audio_delay_buffer) > self._audio_delay_chunks:
                ready.append(self._audio_delay_buffer.popleft())
            logger.debug(f"[SYNC] Buffering audio: {len(self._audio_delay_buffer)} in buffer, returning {len(ready)} frames")
            return ready

        return processed

    def _batch_has_voice(self, audio_frames) -> bool:
        if not audio_frames:
            return False
        for frame in audio_frames:
            try:
                if frame[1] == 0:
                    return True
            except (IndexError, TypeError):
                continue
        return False
    
    def _batch_is_closed_lips_mode(self, audio_frames) -> bool:
        """Check if batch is in closed lips mode (type=2 - humming audio for lip-sync)"""
        if not audio_frames:
            return False
        for frame in audio_frames:
            try:
                if frame[1] == 2:
                    return True
            except (IndexError, TypeError):
                continue
        return False

    def _update_idle_frame_state(self, res_frame, speaking: bool) -> None:
        if speaking:
            self._silence_run = 0
            self._silence_since_idle_update = 0
            self._captured_idle_this_silence = False
            return

        self._silence_run += 1
        if res_frame is None:
            return

        if not self._captured_idle_this_silence:
            if self._silence_run >= self._idle_capture_threshold:
                self._musetalk_idle_frame = res_frame.copy()
                self._captured_idle_this_silence = True
                self._silence_since_idle_update = 0
        else:
            self._silence_since_idle_update += 1
            if self._silence_since_idle_update >= self._idle_refresh_interval:
                self._musetalk_idle_frame = res_frame.copy()
                self._silence_since_idle_update = 0

    def _compose_musetalk_frame(self, res_frame, idx: int, speaking: bool, closed_lips_mode: bool = False):
        """
        Compose frame by blending MuseTalk output onto base video.
        
        Args:
            res_frame: MuseTalk-generated frame
            idx: Frame index
            speaking: True if voice detected
            closed_lips_mode: True if in closed lips mode (type=2 - humming for lip-sync)
        """
        self._update_idle_frame_state(res_frame, speaking)

        # CRITICAL: In closed lips mode, ALWAYS use fresh MuseTalk frames (not cached)
        # This ensures continuous generation matching head pose
        if closed_lips_mode and res_frame is not None:
            if not hasattr(self, "_closed_lips_frame_count"):
                self._closed_lips_frame_count = 0
            self._closed_lips_frame_count += 1
            if self._closed_lips_frame_count % 100 == 1:
                logger.info(f"[CLOSED LIPS] Generating frame #{self._closed_lips_frame_count} - continuous MuseTalk processing")
            try:
                return self.paste_back_frame(res_frame, idx)
            except Exception as exc:
                logger.warning(f"paste_back_frame error in closed lips mode: {exc}")

        # Prioritise live speech frames
        if speaking and res_frame is not None:
            try:
                return self.paste_back_frame(res_frame, idx)
            except Exception as exc:
                logger.warning(f"paste_back_frame error during speech: {exc}")

        # During silence (or speech fallback), reuse the captured MuseTalk idle frame
        idle_source = None
        if self._musetalk_idle_frame is not None:
            idle_source = self._musetalk_idle_frame
        elif not speaking and res_frame is not None:
            idle_source = res_frame

        if idle_source is not None:
            try:
                return self.paste_back_frame(idle_source, idx)
            except Exception as exc:
                logger.warning(f"paste_back_frame error during silence: {exc}")

        # Fallback to base frame if nothing else is available
        return self.frame_list_cycle[idx]

    def is_speaking(self)->bool:
        return self.speaking
    
    def __loadcustom(self):
        for item in self.opt.customopt:
            logger.info(item)
            audiotype = item['audiotype']
            
            # Get clip directory (parent of imgpath which is .../clip_name/image)
            imgpath = item['imgpath']
            clip_dir = os.path.dirname(imgpath)  # Go up from 'image' to clip directory
            
            input_img_list = glob.glob(os.path.join(imgpath, '*.[jpJP][pnPN]*[gG]'))
            input_img_list = sorted(input_img_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
            cache_key = imgpath
            self.custom_img_meta[audiotype] = {
                "name": item.get("name", str(audiotype)),
                "paths": input_img_list,
                "cache_key": cache_key,
            }
            self.custom_img_cycle[audiotype] = None

            # Load coords, masks, and latents if they exist (prepared clips)
            coords_path = os.path.join(clip_dir, "coords.pkl")
            mask_coords_path = os.path.join(clip_dir, "mask_coords.pkl")
            latents_path = os.path.join(clip_dir, "latents.pt")
            mask_dir = os.path.join(clip_dir, "mask")
            
            if os.path.exists(coords_path) and os.path.exists(mask_coords_path) and os.path.exists(latents_path):
                # Load coords
                with open(coords_path, 'rb') as f:
                    self.custom_coords[audiotype] = pickle.load(f)
                
                # Load mask coords
                with open(mask_coords_path, 'rb') as f:
                    self.custom_mask_coords[audiotype] = pickle.load(f)
                
                # Load latents
                self.custom_latents[audiotype] = torch.load(latents_path)
                
                # Load masks (lazy loaded like images)
                if os.path.exists(mask_dir):
                    mask_list = glob.glob(os.path.join(mask_dir, '*.[jpJP][pnPN]*[gG]'))
                    mask_list = sorted(mask_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
                    self.custom_masks[audiotype] = None  # Will be loaded on demand
                    self.custom_img_meta[audiotype]["mask_paths"] = mask_list
                
                logger.info(f'  Loaded MuseTalk data for audiotype {audiotype} ({item.get("name", audiotype)})')
            else:
                logger.warning(f'  MuseTalk data not found for audiotype {audiotype} - will show frames without lip-sync')
                self.custom_coords[audiotype] = None
                self.custom_masks[audiotype] = None
                self.custom_mask_coords[audiotype] = None
                self.custom_latents[audiotype] = None

            # Load audio
            audio_cache_key = item['audiopath']
            if audio_cache_key in _CUSTOM_AUDIO_CACHE:
                audio_data, sample_rate = _CUSTOM_AUDIO_CACHE[audio_cache_key]
                logger.info(f'using cached audio for {audio_cache_key}')
            else:
                audio_data, sample_rate = sf.read(item['audiopath'], dtype='float32')
                _CUSTOM_AUDIO_CACHE[audio_cache_key] = (audio_data, sample_rate)

            self.custom_audio_cycle[audiotype] = audio_data
            self.custom_audio_index[audiotype] = 0
            self.custom_index[audiotype] = 0
            self.custom_opt[audiotype] = item

    def init_customindex(self):
        self.curr_state=0
        for key in self.custom_audio_index:
            self.custom_audio_index[key]=0
        for key in self.custom_index:
            self.custom_index[key]=0

    def notify(self,eventpoint):
        pass  # eventpoint passthrough for synchronization

    def start_recording(self):
        """开始录制视频"""
        if self.recording:
            return

        command = ['ffmpeg',
                    '-y', '-an',
                    '-f', 'rawvideo',
                    '-vcodec','rawvideo',
                    '-pix_fmt', 'bgr24', #像素格式
                    '-s', "{}x{}".format(self.width, self.height),
                    '-r', str(25),
                    '-i', '-',
                    '-pix_fmt', 'yuv420p', 
                    '-vcodec', "h264",
                    #'-f' , 'flv',                  
                    f'temp{self.opt.sessionid}.mp4']
        self._record_video_pipe = subprocess.Popen(command, shell=False, stdin=subprocess.PIPE)

        acommand = ['ffmpeg',
                    '-y', '-vn',
                    '-f', 's16le',
                    #'-acodec','pcm_s16le',
                    '-ac', '1',
                    '-ar', '16000',
                    '-i', '-',
                    '-acodec', 'aac',
                    #'-f' , 'wav',                  
                    f'temp{self.opt.sessionid}.aac']
        self._record_audio_pipe = subprocess.Popen(acommand, shell=False, stdin=subprocess.PIPE)

        self.recording = True
        # self.recordq_video.queue.clear()
        # self.recordq_audio.queue.clear()
        # self.container = av.open(path, mode="w")
    
        # process_thread = Thread(target=self.record_frame, args=())
        # process_thread.start()
    
    def record_video_data(self,image):
        if self.width == 0:
            print("image.shape:",image.shape)
            self.height,self.width,_ = image.shape
        if self.recording:
            self._record_video_pipe.stdin.write(image.tostring())

    def record_audio_data(self,frame):
        if self.recording:
            self._record_audio_pipe.stdin.write(frame.tostring())
    
    # def record_frame(self): 
    #     videostream = self.container.add_stream("libx264", rate=25)
    #     videostream.codec_context.time_base = Fraction(1, 25)
    #     audiostream = self.container.add_stream("aac")
    #     audiostream.codec_context.time_base = Fraction(1, 16000)
    #     init = True
    #     framenum = 0       
    #     while self.recording:
    #         try:
    #             videoframe = self.recordq_video.get(block=True, timeout=1)
    #             videoframe.pts = framenum #int(round(framenum*0.04 / videostream.codec_context.time_base))
    #             videoframe.dts = videoframe.pts
    #             if init:
    #                 videostream.width = videoframe.width
    #                 videostream.height = videoframe.height
    #                 init = False
    #             for packet in videostream.encode(videoframe):
    #                 self.container.mux(packet)
    #             for k in range(2):
    #                 audioframe = self.recordq_audio.get(block=True, timeout=1)
    #                 audioframe.pts = int(round((framenum*2+k)*0.02 / audiostream.codec_context.time_base))
    #                 audioframe.dts = audioframe.pts
    #                 for packet in audiostream.encode(audioframe):
    #                     self.container.mux(packet)
    #             framenum += 1
    #         except queue.Empty:
    #             print('record queue empty,')
    #             continue
    #         except Exception as e:
    #             print(e)
    #             #break
    #     for packet in videostream.encode(None):
    #         self.container.mux(packet)
    #     for packet in audiostream.encode(None):
    #         self.container.mux(packet)
    #     self.container.close()
    #     self.recordq_video.queue.clear()
    #     self.recordq_audio.queue.clear()
    #     print('record thread stop')
		
    def stop_recording(self):
        """停止录制视频"""
        if not self.recording:
            return
        self.recording = False 
        self._record_video_pipe.stdin.close()  #wait() 
        self._record_video_pipe.wait()
        self._record_audio_pipe.stdin.close()
        self._record_audio_pipe.wait()
        cmd_combine_audio = f"ffmpeg -y -i temp{self.opt.sessionid}.aac -i temp{self.opt.sessionid}.mp4 -c:v copy -c:a copy data/record.mp4"
        os.system(cmd_combine_audio) 
        #os.remove(output_path)

    def mirror_index(self,size, index):
        #size = len(self.coord_list_cycle)
        turn = index // size
        res = index % size
        if turn % 2 == 0:
            return res
        else:
            return size - res - 1 
    
    def get_audio_stream(self,audiotype):
        idx = self.custom_audio_index[audiotype]
        stream = self.custom_audio_cycle[audiotype][idx:idx+self.chunk]
        self.custom_audio_index[audiotype] += self.chunk
        if self.custom_audio_index[audiotype]>=self.custom_audio_cycle[audiotype].shape[0]:
            self.curr_state = 1  #当前视频不循环播放，切换到静音状态
        return stream
    
    def _ensure_custom_frames(self, audiotype: int):
        frames = self.custom_img_cycle.get(audiotype)
        if frames is not None:
            return
        meta = self.custom_img_meta.get(audiotype)
        if not meta:
            return
        target_size = (self.target_width, self.target_height)
        logger.info(f"loading custom frames for audiotype {audiotype} ({meta.get('name', audiotype)})")
        frames = read_imgs(meta["paths"], cache_key=meta["cache_key"], target_size=target_size)
        self.custom_img_cycle[audiotype] = frames
    
    def _ensure_custom_masks(self, audiotype: int):
        """Lazy load custom masks for a specific audiotype"""
        masks = self.custom_masks.get(audiotype)
        if masks is not None and masks != []:  # Already loaded
            return
        meta = self.custom_img_meta.get(audiotype)
        if not meta or "mask_paths" not in meta:
            return
        logger.info(f"loading custom masks for audiotype {audiotype} ({meta.get('name', audiotype)})")
        # Load masks from disk
        mask_frames = []
        for mask_path in meta["mask_paths"]:
            mask = cv2.imread(mask_path, cv2.IMREAD_UNCHANGED)
            if mask is None:
                logger.warning(f'failed to read mask: {mask_path}')
                continue
            if len(mask.shape) == 2:
                mask = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
            mask_frames.append(mask)
        self.custom_masks[audiotype] = mask_frames
    
    def set_custom_state(self,audiotype, reinit=True):
        print('set_custom_state:',audiotype)
        if self.custom_audio_index.get(audiotype) is None:
            return
        self.curr_state = audiotype
        self._ensure_custom_frames(audiotype)
        if reinit:
            self.custom_audio_index[audiotype] = 0
            self.custom_index[audiotype] = 0

    def process_frames(self,quit_event,loop=None,audio_track=None,video_track=None):
        enable_transition = False  # 设置为False禁用过渡效果，True启用
        
        if enable_transition:
            _last_speaking = False
            _transition_start = time.time()
            _transition_duration = 0.1  # 过渡时间
            _last_silent_frame = None  # 静音帧缓存
            _last_speaking_frame = None  # 说话帧缓存
        
        if self.opt.transport=='virtualcam':
            import pyvirtualcam
            vircam = None

            audio_tmp = queue.Queue(maxsize=3000)
            audio_thread = Thread(target=play_audio, args=(quit_event,audio_tmp,), daemon=True, name="pyaudio_stream")
            audio_thread.start()
        
        # Video output timing tracking
        video_output_count = 0
        video_output_times = []
        last_video_log_time = time.time()
        target_frame_interval = 1.0 / max(1, self.fps)
        
        if not getattr(self, "_encoder_warmup_done", False):
            warmup_count = self.fps * 5  # ~5 seconds at target FPS
            for i in range(warmup_count):
                if quit_event.is_set():
                    break
                # choose idle frame if available, else base frame 0
                if self.idle_frame_list_cycle:
                    warm_idx = i % len(self.idle_frame_list_cycle)
                    warm_frame = self.idle_frame_list_cycle[warm_idx]
                else:
                    warm_frame = self.frame_list_cycle[i % len(self.frame_list_cycle)]
                warm_image = warm_frame
                new_frame = VideoFrame.from_ndarray(warm_image, format="bgr24")
                self._send_video_frame(loop, video_track, new_frame, None)

                silent_mono = np.clip(self._silent_audio_chunk * 32767.0, -32768, 32767).astype(np.int16)
                audio_packet = AudioFrame(format='s16', layout='mono', samples=silent_mono.shape[0])
                audio_packet.planes[0].update(silent_mono.tobytes())
                audio_packet.sample_rate = 16000
                self._send_audio_frame(loop, audio_track, audio_packet, None)
                time.sleep(target_frame_interval)
            self._encoder_warmup_done = True

        while not quit_event.is_set():
            frame_start_time = time.perf_counter()
            
            try:
                # OPTIMIZED: Use get_nowait() to avoid blocking, maintaining constant FPS
                res_frame, idx, audio_frames = self.res_frame_queue.get_nowait()
                got_from_queue = True
            except queue.Empty:
                res_frame = None
                audio_frames = []
                idx = self.idle_frame_idx % len(self.frame_list_cycle)
                got_from_queue = False
            
            t1 = time.perf_counter()

            speaking = self._batch_has_voice(audio_frames)
            closed_lips_mode = self._batch_is_closed_lips_mode(audio_frames)
            
            if enable_transition:
                # 检测状态变化
                current_speaking = speaking

                if current_speaking != _last_speaking:

                    logger.info(f"状态切换：{'说话' if _last_speaking else '静音'} → {'说话' if current_speaking else '静音'}")

                    _transition_start = time.time()

                _last_speaking = current_speaking



            # ALWAYS blend MuseTalk lip-sync onto calm_avatar
            # Silent or talking - both get lip-synced frames
            idx = self.mirror_index(len(self.coord_list_cycle), idx)
            combine_frame = self._compose_musetalk_frame(res_frame, idx, speaking, closed_lips_mode)

            t2 = time.perf_counter()
            
            cv2.putText(combine_frame, "LiveTalking", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (128,128,128), 1)
            
            t4 = time.perf_counter()
            
            if self.opt.transport=='virtualcam':
                if vircam==None:
                    height, width,_= combine_frame.shape
                    vircam = pyvirtualcam.Camera(width=width, height=height, fps=25, fmt=pyvirtualcam.PixelFormat.BGR,print_fps=True)
                vircam.send(combine_frame)
            else: #webrtc
                # GPU-DIRECT ENCODING: Check if frame is GPU tensor or CPU numpy
                if hasattr(combine_frame, 'is_cuda') and combine_frame.is_cuda:
                    # GPU tensor - use GPU-direct encoder
                    if not hasattr(self, '_gpu_encoder'):
                        from gpu_encoder import GPUDirectEncoder
                        self._gpu_encoder = GPUDirectEncoder(
                            width=combine_frame.shape[1],
                            height=combine_frame.shape[0],
                            fps=self.opt.fps,
                            bitrate=3000000
                        )
                        self._gpu_encoder.initialize(combine_frame.device)
                    
                    # Encode directly from GPU (ZERO CPU TRANSFER!)
                    encoded_packet = self._gpu_encoder.encode_frame(combine_frame)
                    if encoded_packet:
                        # Send encoded packet directly to WebRTC
                        # TODO: Integrate with aiortc's packet handling
                        logger.warning("GPU-direct encoding ready but needs aiortc integration")
                        # Fallback to CPU for now
                        image = combine_frame.cpu().numpy()
                        new_frame = VideoFrame.from_ndarray(image, format="bgr24")
                        self._send_video_frame(loop, video_track, new_frame, None)
                    else:
                        # GPU encoding failed, fallback to CPU
                        image = combine_frame.cpu().numpy()
                        new_frame = VideoFrame.from_ndarray(image, format="bgr24")
                        self._send_video_frame(loop, video_track, new_frame, None)
                else:
                    # CPU numpy array - use standard VideoFrame
                    image = combine_frame
                    new_frame = VideoFrame.from_ndarray(image, format="bgr24")
                    self._send_video_frame(loop, video_track, new_frame, None)
            
            t5 = time.perf_counter()
            
            # CRITICAL FIX: Audio pacing buffer to prevent queue overflow
            # Problem: Each video frame has 2 audio chunks, but WebRTC can only send 1 per iteration
            # Solution: Buffer all audio frames and send only 1 per iteration (matches 50 FPS rate)
            
            # Initialize audio pacing buffer on first run
            if not hasattr(self, '_audio_pacing_buffer'):
                self._audio_pacing_buffer = []
            
            # Prepare audio frames and add to buffer
            if audio_frames:
                frames_to_output = self._apply_audio_sync_offset(audio_frames)
            else:
                silent_input = [(self._silent_audio_chunk.copy(), 1, None)]
                frames_to_output = self._apply_audio_sync_offset(silent_input)

            # Add all frames to buffer
            self._audio_pacing_buffer.extend(frames_to_output)
            
            # Send ONLY ONE audio frame per iteration (prevents queue overflow)
            audio_send_start = time.perf_counter()
            if self._audio_pacing_buffer:
                frame, frame_type, eventpoint = self._audio_pacing_buffer.pop(0)
                
                # ULTIMATE FIX: If closed lips mode (type=2), replace EVERYTHING with silence NOW!
                if frame_type == 2:
                    if not hasattr(self, "_silence_enforced_count"):
                        self._silence_enforced_count = 0
                    self._silence_enforced_count += 1
                    if self._silence_enforced_count % 50 == 1:
                        logger.warning(f"[SILENCE ENFORCED #{self._silence_enforced_count}] Type=2 detected - replacing ALL audio with ZEROS!")
                    
                    # Force frame to zeros (16kHz)
                    frame = np.zeros_like(frame)
                    
                    # Force eventpoint HQ audio to zeros (48kHz)
                    if isinstance(eventpoint, dict) and 'hq_audio' in eventpoint:
                        hq_len = len(eventpoint['hq_audio'])
                        eventpoint['hq_audio'] = np.zeros(hq_len, dtype=np.float32)
                    else:
                        # Create silent HQ audio
                        eventpoint = {
                            'hq_audio': np.zeros(len(frame) * 3, dtype=np.float32),
                            'hq_sample_rate': 48000
                        }
                
                # CRITICAL: Check if HQ audio is available and swap it in
                hq_audio = None
                hq_sr = self.sample_rate  # default to 16kHz
                output_frame_array = np.asarray(frame)  # default to downsampled (now zeros for type=2)
                
                # eventpoint is actually the datainfo dict containing HQ audio
                if eventpoint and isinstance(eventpoint, dict):
                    # CRITICAL: Check for silent_output flag (closed lips mode)
                    silent_output = eventpoint.get('silent_output', False)
                    
                    if silent_output:
                        # Force silence output (closed lips mode - don't play humming!)
                        hq_audio = eventpoint.get('hq_audio')
                        hq_sample_rate = eventpoint.get('hq_sample_rate', 48000)
                        
                        if hq_audio is not None:
                            output_frame_array = np.asarray(hq_audio)  # Use silent HQ audio!
                            hq_sr = hq_sample_rate
                        else:
                            # Fallback: create silence at 48kHz
                            output_frame_array = np.zeros(len(frame) * 3, dtype=np.float32)
                            hq_sr = 48000
                    else:
                        # Normal mode: use HQ audio if available
                        hq_audio = eventpoint.get('hq_audio')
                        hq_sample_rate = eventpoint.get('hq_sample_rate')
                        
                        if hq_audio is not None and hq_sample_rate is not None:
                            output_frame_array = np.asarray(hq_audio)  # Use HQ audio!
                            hq_sr = hq_sample_rate
                        else:
                            if not hasattr(self, '_hq_swap_debug_count'):
                                self._hq_swap_debug_count = 0
                            self._hq_swap_debug_count += 1
                            if self._hq_swap_debug_count <= 5:  # Log first 5 failures
                                logger.warning(f"[HQ AUDIO SWAP] No HQ audio available, using downsampled (eventpoint keys: {list(eventpoint.keys()) if eventpoint else 'None'})")
                else:
                    if not hasattr(self, '_hq_swap_warn_count'):
                        self._hq_swap_warn_count = 0
                    self._hq_swap_warn_count += 1
                    if self._hq_swap_warn_count <= 5:  # Log first 5 times only
                        logger.warning(f"[HQ AUDIO SWAP] eventpoint is not dict: {type(eventpoint)}")
                
                # Use downsampled for debug logging (to match processing)
                frame_array = np.asarray(frame)
                if frame_type == 0:
                    debug_count = getattr(self, "_audio_debug_count", 0) + 1
                    self._audio_debug_count = debug_count
                    if debug_count % 200 == 0:
                        peak = float(np.max(np.abs(frame_array)))
                        rms = float(np.sqrt(np.mean(frame_array ** 2)))
                        logger.info(
                            "[AUDIO DEBUG] chunk=%d peak=%.4f rms=%.4f | Buffer: %d frames | Output: %dHz",
                            debug_count,
                            peak,
                            rms,
                            len(self._audio_pacing_buffer),
                            hq_sr,
                        )
                    # else leave attribute set
                elif frame_type == 2:
                    # Closed lips mode - verify output is silent (already forced to zeros above)
                    if not hasattr(self, "_closed_lips_audio_verify_count"):
                        self._closed_lips_audio_verify_count = 0
                    self._closed_lips_audio_verify_count += 1
                    if self._closed_lips_audio_verify_count % 50 == 1:
                        output_rms = float(np.sqrt(np.mean(output_frame_array ** 2)))
                        logger.info(
                            "[SILENCE VERIFY #{0}] Output RMS={1:.10f} (Should be 0.0!)".format(
                                self._closed_lips_audio_verify_count, output_rms
                            )
                        )
                
                # Convert HQ audio to PCM for output (already forced to zeros above if type=2)
                pcm_mono = np.clip(output_frame_array * 32767.0, -32768, 32767).astype(np.int16)
                if self.opt.transport == 'virtualcam':
                    audio_tmp.put(pcm_mono.tobytes())
                else:
                    new_frame = AudioFrame(format='s16', layout='mono', samples=pcm_mono.shape[0])
                    new_frame.planes[0].update(pcm_mono.tobytes())
                    new_frame.sample_rate = hq_sr  # Use HQ sample rate (48kHz)!
                    self._send_audio_frame(loop, audio_track, new_frame, eventpoint)
                self.record_audio_data(pcm_mono)
            
            # NOW do recording and logging (after both video and audio are sent)
            self.record_video_data(combine_frame)
            
            # Track video output performance
            frame_total_time = time.perf_counter() - frame_start_time
            video_output_times.append(frame_total_time)
            video_output_count += 1
            
            # Log every 2 seconds
            if time.time() - last_video_log_time >= 2.0:
                if len(video_output_times) > 0:
                    avg_time = sum(video_output_times) / len(video_output_times)
                    output_fps = 1.0 / avg_time if avg_time > 0 else 0
                    
                    # Calculate stage times
                    queue_time = (t1 - frame_start_time) * 1000
                    prep_time = (t2 - t1) * 1000
                    blend_time = (t3 - t2) * 1000
                    text_time = (t4 - t3) * 1000
                    send_time = (t5 - t4) * 1000
                    
                    logger.info(f"[VIDEO OUTPUT] FPS: {output_fps:.1f} | "
                               f"Frames: {video_output_count} | "
                               f"Avg: {avg_time*1000:.1f}ms | "
                               f"Stages: queue:{queue_time:.1f}ms prep:{prep_time:.1f}ms "
                               f"blend:{blend_time:.1f}ms text:{text_time:.1f}ms send:{send_time:.1f}ms")
                    
                    video_output_times.clear()
                    last_video_log_time = time.time()
            
            # Log A/V sync timing every 50 frames
            if not hasattr(self, '_av_sync_frame_count'):
                self._av_sync_frame_count = 0
                self._av_sync_video_time = 0
                self._av_sync_audio_time = 0
            
            self._av_sync_frame_count += 1
            self._av_sync_video_time = t5  # Video send time (from line 718)
            self._av_sync_audio_time = audio_send_start
            
            if self._av_sync_frame_count % 50 == 0:
                time_diff_ms = (self._av_sync_audio_time - self._av_sync_video_time) * 1000
                logger.info(f"[A/V SYNC DEBUG] Frame {self._av_sync_frame_count}: "
                           f"Video->Audio delay: {time_diff_ms:.2f}ms | "
                           f"{'AUDIO BEHIND' if time_diff_ms > 0 else 'AUDIO AHEAD'} | "
                           f"Offset setting: {self.av_sync_offset_ms}ms")
            if self.opt.transport=='virtualcam':
                vircam.sleep_until_next_frame()
            else:
                # OPTIMIZED: Precise frame pacing to maintain constant FPS
                # Calculate time spent and sleep exactly the remaining time
                elapsed = time.perf_counter() - frame_start_time
                sleep_time = target_frame_interval - elapsed
                if sleep_time > 0.001:  # Only sleep if more than 1ms remaining
                    time.sleep(sleep_time)
                elif sleep_time < -0.01:  # Log if we're running more than 10ms behind
                    pass  # Frame took too long, skip sleep and continue
            
        if self.opt.transport=='virtualcam':
            audio_thread.join()
            vircam.close()
        logger.info('basereal process_frames thread stop') 
    
    # def process_custom(self,audiotype:int,idx:int):
    #     if self.curr_state!=audiotype: #从推理切到口播
    #         if idx in self.switch_pos:  #在卡点位置可以切换
    #             self.curr_state=audiotype
    #             self.custom_index=0
    #     else:
    #         self.custom_index+=1
    def _ensure_custom_masks(self, audiotype: int):
        """Lazy load custom masks for a specific audiotype"""
        masks = self.custom_masks.get(audiotype)
        if masks is not None and masks != []:  # Already loaded
            return
        meta = self.custom_img_meta.get(audiotype)
        if not meta or "mask_paths" not in meta:
            return
        logger.info(f"loading custom masks for audiotype {audiotype} ({meta.get('name', audiotype)})")
        # Load masks from disk
        mask_frames = []
        for mask_path in meta["mask_paths"]:
            mask = cv2.imread(mask_path, cv2.IMREAD_UNCHANGED)
            if mask is None:
                logger.warning(f'failed to read mask: {mask_path}')
                continue
            if len(mask.shape) == 2:
                mask = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
            mask_frames.append(mask)
        self.custom_masks[audiotype] = mask_frames
    
    def set_custom_state(self,audiotype, reinit=True):

        print('set_custom_state:',audiotype)

        if self.custom_audio_index.get(audiotype) is None:

            return

        self.curr_state = audiotype

        self._ensure_custom_frames(audiotype)
        if reinit:

            self.custom_audio_index[audiotype] = 0

            self.custom_index[audiotype] = 0



    def process_frames(self,quit_event,loop=None,audio_track=None,video_track=None):

        enable_transition = False  # 设置为False禁用过渡效果，True启用

        

        if enable_transition:

            _last_speaking = False

            _transition_start = time.time()

            _transition_duration = 0.1  # 过渡时间

            _last_silent_frame = None  # 静音帧缓存

            _last_speaking_frame = None  # 说话帧缓存

        

        if self.opt.transport=='virtualcam':

            import pyvirtualcam

            vircam = None



            audio_tmp = queue.Queue(maxsize=3000)

            audio_thread = Thread(target=play_audio, args=(quit_event,audio_tmp,), daemon=True, name="pyaudio_stream")

            audio_thread.start()

        

        while not quit_event.is_set():

            try:

                res_frame,idx,audio_frames = self.res_frame_queue.get(block=True, timeout=1)

            except queue.Empty:

                continue

            

            speaking = self._batch_has_voice(audio_frames)
            closed_lips_mode = self._batch_is_closed_lips_mode(audio_frames)

            if enable_transition:

                # 检测状态变化

                current_speaking = speaking



                if current_speaking != _last_speaking:

                    logger.info(f"状态切换：{'说话' if _last_speaking else '静音'} → {'说话' if current_speaking else '静音'}")

                    _transition_start = time.time()

                _last_speaking = current_speaking



            # ALWAYS blend MuseTalk lip-sync onto calm_avatar
            # Silent or talking - both get lip-synced frames
            idx = self.mirror_index(len(self.coord_list_cycle), idx)
            combine_frame = self._compose_musetalk_frame(res_frame, idx, speaking, closed_lips_mode)

            # Ensure combine_frame is a contiguous numpy array for cv2.putText
            if combine_frame is not None:
                if not isinstance(combine_frame, np.ndarray):
                    combine_frame = np.asarray(combine_frame)
                if not combine_frame.flags['C_CONTIGUOUS']:
                    combine_frame = np.ascontiguousarray(combine_frame)
                cv2.putText(combine_frame, "LiveTalking", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (128,128,128), 1)

            if self.opt.transport=='virtualcam':

                if vircam==None:

                    height, width,_= combine_frame.shape

                    vircam = pyvirtualcam.Camera(width=width, height=height, fps=25, fmt=pyvirtualcam.PixelFormat.BGR,print_fps=True)

                vircam.send(combine_frame)

            else: #webrtc

                image = combine_frame
                new_frame = VideoFrame.from_ndarray(image, format="bgr24")
                self._send_video_frame(loop, video_track, new_frame, None)

            self.record_video_data(combine_frame)



            for audio_frame in audio_frames:

                frame,type,eventpoint = audio_frame

                frame_array = np.asarray(frame)
                if type == 0:
                    debug_count = getattr(self, "_audio_debug_count", 0) + 1
                    self._audio_debug_count = debug_count
                    if debug_count % 200 == 0:
                        peak = float(np.max(np.abs(frame_array)))
                        rms = float(np.sqrt(np.mean(frame_array ** 2)))
                        logger.info(
                            "[AUDIO DEBUG] chunk=%d peak=%.4f rms=%.4f",
                            debug_count,
                            peak,
                            rms,
                        )
                pcm_mono = np.clip(frame_array * 32767.0, -32768, 32767).astype(np.int16)

                if self.opt.transport=='virtualcam':

                    audio_tmp.put(pcm_mono.tobytes()) #TODO

                else: #webrtc

                    new_frame = AudioFrame(format='s16', layout='mono', samples=pcm_mono.shape[0])
                    new_frame.planes[0].update(pcm_mono.tobytes())
                    new_frame.sample_rate=16000
                    self._send_audio_frame(loop, audio_track, new_frame, eventpoint)

                self.record_audio_data(pcm_mono)
