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

#from .utils import *
import subprocess
import os
import time
import torch.nn.functional as F
import cv2
import glob
import pickle
import copy

import queue
from queue import Queue
from threading import Thread, Event
import torch.multiprocessing as mp

from musetalk.utils.utils import get_file_type,get_video_fps,datagen
#from musetalk.utils.preprocessing import get_landmark_and_bbox,read_imgs,coord_placeholder
from musetalk.utils.blending import get_image_blending  # CRITICAL: Use offline-style blending with mask
from musetalk.utils.utils import load_all_model
from musetalk.whisper.audio2feature import Audio2Feature

from museasr import MuseASR
import asyncio
from av import AudioFrame, VideoFrame
from basereal import BaseReal

from tqdm import tqdm
from logger import logger

# ULTRA-FAST BLENDING: Numba JIT compilation for 3-5x speedup
from numba import jit

@jit(nopython=True, parallel=True, fastmath=True, cache=True)
def fast_alpha_blend_numba(result_roi, ori_roi, mask_roi):
    """
    Ultra-fast alpha blending using Numba JIT compilation.
    This function is compiled to machine code for maximum performance.
    
    Args:
        result_roi: MuseTalk output region (uint8)
        ori_roi: Original frame region (uint8)
        mask_roi: Blending mask (uint8, 0-255)
    
    Returns:
        Blended result (uint8)
    """
    h, w = mask_roi.shape
    output = np.empty((h, w, 3), dtype=np.uint8)
    
    # Parallel loop over pixels for maximum speed
    for i in range(h):
        for j in range(w):
            alpha = mask_roi[i, j] / 255.0
            inv_alpha = 1.0 - alpha
            
            # Blend each color channel
            for c in range(3):
                blended_value = result_roi[i, j, c] * alpha + ori_roi[i, j, c] * inv_alpha
                output[i, j, c] = int(blended_value)
    
    return output

def load_model():
    # load model weights
    vae, unet, pe = load_all_model()
    device = torch.device("cuda" if torch.cuda.is_available() else ("mps" if (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()) else "cpu"))
    timesteps = torch.tensor([0], device=device)
    pe = pe.half().to(device)
    vae.vae = vae.vae.half().to(device)
    #vae.vae.share_memory().to(device)
    unet.model = unet.model.half().to(device)
    #unet.model.share_memory()
    # Initialize audio processor and Whisper model
    audio_processor = Audio2Feature(model_path="./models/whisper/tiny.pt")
    return vae, unet, pe, timesteps, audio_processor

def load_avatar(avatar_id, idle_avatar_id=None):
    #self.video_path = '' #video_path
    #self.bbox_shift = opt.bbox_shift
    avatar_path = f"./data/avatars/{avatar_id}"
    full_imgs_path = f"{avatar_path}/full_imgs" 
    coords_path = f"{avatar_path}/coords.pkl"
    latents_out_path= f"{avatar_path}/latents.pt"
    video_out_path = f"{avatar_path}/vid_output/"
    mask_out_path =f"{avatar_path}/mask"
    mask_coords_path =f"{avatar_path}/mask_coords.pkl"
    avatar_info_path = f"{avatar_path}/avator_info.json"
    # self.avatar_info = {
    #     "avatar_id":self.avatar_id,
    #     "video_path":self.video_path,
    #     "bbox_shift":self.bbox_shift   
    # }

    input_latent_list_cycle = torch.load(latents_out_path)  #,weights_only=True
    with open(coords_path, 'rb') as f:
        coord_list_cycle = pickle.load(f)
    input_img_list = glob.glob(os.path.join(full_imgs_path, '*.[jpJP][pnPN]*[gG]'))
    input_img_list = sorted(input_img_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    frame_list_cycle = read_imgs(input_img_list)
    with open(mask_coords_path, 'rb') as f:
        mask_coords_list_cycle = pickle.load(f)
    input_mask_list = glob.glob(os.path.join(mask_out_path, '*.[jpJP][pnPN]*[gG]'))
    # Deduplicate mask files by numeric index so we only keep one entry per frame.
    mask_file_map = {}
    for mask_path in input_mask_list:
        stem = os.path.splitext(os.path.basename(mask_path))[0]
        try:
            # Preserve index even if filename is zero-padded; lstrip handles "00000001".
            index = int(stem.lstrip('0') or '0')
        except ValueError:
            logger.warning(f"Skipping mask with non-numeric name: {mask_path}")
            continue
        existing = mask_file_map.get(index)
        if not existing:
            mask_file_map[index] = mask_path
            continue
        # Prefer files with longer basename (usually zero-padded full-resolution masks).
        if len(os.path.basename(mask_path)) > len(os.path.basename(existing)):
            mask_file_map[index] = mask_path
    input_mask_list = [mask_file_map[idx] for idx in sorted(mask_file_map.keys())]
    if len(input_mask_list) != len(mask_coords_list_cycle):
        logger.warning(
            f"Mask file count ({len(input_mask_list)}) and mask_coords count ({len(mask_coords_list_cycle)}) differ; "
            "truncating to the minimum to keep lists aligned."
        )
        max_len = min(len(input_mask_list), len(mask_coords_list_cycle))
        input_mask_list = input_mask_list[:max_len]
        mask_coords_list_cycle = mask_coords_list_cycle[:max_len]
        frame_list_cycle = frame_list_cycle[:max_len]
        coord_list_cycle = coord_list_cycle[:max_len]
        input_latent_list_cycle = input_latent_list_cycle[:max_len]
        if idle_frame_list_cycle is not None:
            idle_frame_list_cycle = idle_frame_list_cycle[:max_len]
    mask_list_cycle = read_masks(input_mask_list)
    
    # NEW: Load pre-computed ROI bounds for ultra-fast blending
    mask_roi_path = f"{avatar_path}/mask_roi.pkl"
    mask_roi_list_cycle = None
    if os.path.exists(mask_roi_path):
        with open(mask_roi_path, 'rb') as f:
            mask_roi_list_cycle = pickle.load(f)
        logger.info(f"Loaded pre-computed ROI bounds for ultra-fast blending")
    else:
        logger.warning(f"ROI bounds not found, will compute at runtime (slower)")
    
    # Load idle frames if idle_avatar_id is provided
    idle_frame_list_cycle = None
    if idle_avatar_id:
        idle_avatar_path = f"./data/avatars/{idle_avatar_id}"
        idle_imgs_path = f"{idle_avatar_path}/full_imgs"
        if os.path.exists(idle_imgs_path):
            idle_img_list = glob.glob(os.path.join(idle_imgs_path, '*.[jpJP][pnPN]*[gG]'))
            idle_img_list = sorted(idle_img_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
            idle_frame_list_cycle = read_imgs(idle_img_list)
            logger.info(f"Loaded {len(idle_frame_list_cycle)} idle frames from {idle_avatar_id}")
        else:
            logger.warning(f"Idle avatar path not found: {idle_imgs_path}, will use base frames for silence")
    
    return frame_list_cycle,mask_list_cycle,coord_list_cycle,mask_coords_list_cycle,input_latent_list_cycle,idle_frame_list_cycle,mask_roi_list_cycle

@torch.no_grad()
def warm_up(batch_size,model):
    # 预热函数
    logger.info('warmup model...')
    vae, unet, pe, timesteps, audio_processor = model
    #batch_size = 16
    #timesteps = torch.tensor([0], device=unet.device)
    whisper_batch = np.ones((batch_size, 50, 384), dtype=np.uint8)
    latent_batch = torch.ones(batch_size, 8, 32, 32).to(unet.device)

    audio_feature_batch = torch.from_numpy(whisper_batch)
    audio_feature_batch = audio_feature_batch.to(device=unet.device, dtype=unet.model.dtype)
    audio_feature_batch = pe(audio_feature_batch)
    latent_batch = latent_batch.to(dtype=unet.model.dtype)
    pred_latents = unet.model(latent_batch,
                              timesteps,
                              encoder_hidden_states=audio_feature_batch).sample
    vae.decode_latents(pred_latents)

def read_imgs(img_list):
    frames = []
    logger.info('reading images...')
    for img_path in tqdm(img_list):
        frame = cv2.imread(img_path)
        frames.append(frame)
    return frames

def read_masks(img_list):
    masks = []
    logger.info('reading masks...')
    for img_path in tqdm(img_list):
        # OPTIMIZED: Read as grayscale directly for faster blending
        mask = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
        if mask is None:
            logger.warning(f'failed to read mask image: {img_path}')
            continue
        # Keep as grayscale - no conversion needed
        masks.append(mask)
    return masks

def __mirror_index(size, index):
    #size = len(self.coord_list_cycle)
    turn = index // size
    res = index % size
    if turn % 2 == 0:
        return res
    else:
        return size - res - 1 

@torch.no_grad()
def inference(render_event,batch_size,input_latent_list_cycle,audio_feat_queue,audio_out_queue,res_frame_queue,
              vae, unet, pe,timesteps): #vae, unet, pe,timesteps
    
    # vae, unet, pe = load_diffusion_model()
    # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # timesteps = torch.tensor([0], device=device)
    # pe = pe.half()
    # vae.vae = vae.vae.half()
    # unet.model = unet.model.half()
    
    length = len(input_latent_list_cycle)
    index = 0
    count=0
    counttime=0
    logger.info('start inference')
    while render_event.is_set():
        starttime=time.perf_counter()
        try:
            whisper_chunks = audio_feat_queue.get(block=True, timeout=1)
        except queue.Empty:
            continue
        is_all_silence=True
        audio_frames = []
        for _ in range(batch_size*2):
            try:
                # CRITICAL FIX: Non-blocking get with timeout to prevent audio stuttering
                frame,type,eventpoint = audio_out_queue.get(block=True, timeout=0.5)
                audio_frames.append((frame,type,eventpoint))
                if type==0:
                    is_all_silence=False
            except queue.Empty:
                # Audio queue starved - use silence frame to keep pipeline flowing
                logger.warning("[AUDIO PIPELINE] Queue starved, using silence frame")
                audio_frames.append((np.zeros(320, dtype=np.float32), 1, None))
                is_all_silence=True
        
        # Always run MuseTalk for consistent face appearance
        # Use zero features for silence to generate natural closed mouth
        t=time.perf_counter()
        
        if is_all_silence:
            # SILENCE: Use zero audio features for closed mouth
            whisper_batch = np.zeros_like(np.stack(whisper_chunks))
        else:
            # SPEECH: Use actual audio features for lip-sync
            whisper_batch = np.stack(whisper_chunks)
        
        # Run MuseTalk inference (for both silence and speech)
        latent_batch = []
        for i in range(batch_size):
            idx = __mirror_index(length,index+i)
            latent = input_latent_list_cycle[idx]
            latent_batch.append(latent)
        latent_batch = torch.cat(latent_batch, dim=0)
        
        audio_feature_batch = torch.from_numpy(whisper_batch)
        audio_feature_batch = audio_feature_batch.to(device=unet.device,
                                                        dtype=unet.model.dtype)
        audio_feature_batch = pe(audio_feature_batch)
        latent_batch = latent_batch.to(device=unet.device, dtype=unet.model.dtype)

        pred_latents = unet.model(latent_batch, 
                                    timesteps, 
                                    encoder_hidden_states=audio_feature_batch).sample
        
        recon = vae.decode_latents(pred_latents)

        counttime += (time.perf_counter() - t)
        count += batch_size
        if count>=100:
            logger.info(f"MuseTalk inference: {count/counttime:.1f} FPS")
            count=0
            counttime=0
        for i,res_frame in enumerate(recon):
            res_frame_queue.put((res_frame, __mirror_index(length,index), audio_frames[i*2:i*2+2]))
            index = index + 1            
    logger.info('musereal inference processor stop')

class MuseReal(BaseReal):
    @torch.no_grad()
    def __init__(self, opt, model, avatar):
        super().__init__(opt)
        #self.opt = opt # shared with the trainer's opt to support in-place modification of rendering parameters.
        # self.W = opt.W
        # self.H = opt.H

        self.fps = opt.fps # 20 ms per frame

        self.batch_size = opt.batch_size
        self.idx = 0
        # Restore original queue sizing tied to batch size
        self.res_frame_queue = mp.Queue(max(2, self.batch_size))

        self.vae, self.unet, self.pe, self.timesteps, self.audio_processor = model
        self.frame_list_cycle,self.mask_list_cycle,self.coord_list_cycle,self.mask_coords_list_cycle, self.input_latent_list_cycle, self.idle_frame_list_cycle, self.mask_roi_list_cycle = avatar
        
        # GPU-ACCELERATED: Pre-load all frames and masks to GPU for ultra-fast blending
        device = self.unet.device
        logger.info(f"Pre-loading {len(self.frame_list_cycle)} frames and masks to GPU...")
        
        # Get target output resolution
        target_h, target_w = self.opt.H, self.opt.W
        source_h, source_w = self.frame_list_cycle[0].shape[:2]
        
        # Check if coordinates need scaling by examining first coordinate set
        # Coordinates are at a different scale if they exceed frame dimensions
        coord_scale_needed = False
        coord_scale_h = 1.0
        coord_scale_w = 1.0
        
        if len(self.coord_list_cycle) > 0:
            first_coord = self.coord_list_cycle[0]
            logger.info(f"First coord format: {len(first_coord)} elements - {first_coord[:4] if len(first_coord) >= 4 else first_coord}")
            
            # Check if any coordinate exceeds current frame dimensions
            # Handle different coordinate formats safely
            if len(first_coord) >= 8:
                # Full format: [x1, y1, x2, y2, crop_x1, crop_y1, crop_x2, crop_y2]
                max_x = max(first_coord[0], first_coord[2], first_coord[4], first_coord[6])
                max_y = max(first_coord[1], first_coord[3], first_coord[5], first_coord[7])
            elif len(first_coord) >= 4:
                # Bbox only: [x1, y1, x2, y2]
                max_x = max(first_coord[0], first_coord[2])
                max_y = max(first_coord[1], first_coord[3])
            else:
                logger.warning(f"Unexpected coordinate format with {len(first_coord)} elements")
                max_x = source_w
                max_y = source_h
            
            if max_x > source_w or max_y > source_h:
                # Coordinates are at a larger scale than frames
                coord_scale_needed = True
                # Calculate scale factor to fit coordinates to frames
                coord_scale_w = source_w / max_x if max_x > source_w else 1.0
                coord_scale_h = source_h / max_y if max_y > source_h else 1.0
                logger.info(f"Detected coordinate scale mismatch!")
                logger.info(f"Frame size: {source_w}x{source_h}, Max coords: {max_x}x{max_y}")
                logger.info(f"Scaling coordinates by {coord_scale_w:.3f}x (width) and {coord_scale_h:.3f}x (height)")
        
        # ALSO CHECK mask_coords_list_cycle for scale mismatch
        if len(self.mask_coords_list_cycle) > 0:
            first_mask_coord = self.mask_coords_list_cycle[0]
            logger.info(f"First mask_coord format: {len(first_mask_coord)} elements - {first_mask_coord[:4] if len(first_mask_coord) >= 4 else first_mask_coord}")
            
            # Check if mask coordinates exceed frame dimensions
            if len(first_mask_coord) >= 8:
                mask_max_x = max(first_mask_coord[0], first_mask_coord[2], first_mask_coord[4], first_mask_coord[6])
                mask_max_y = max(first_mask_coord[1], first_mask_coord[3], first_mask_coord[5], first_mask_coord[7])
            elif len(first_mask_coord) >= 4:
                mask_max_x = max(first_mask_coord[0], first_mask_coord[2])
                mask_max_y = max(first_mask_coord[1], first_mask_coord[3])
            else:
                mask_max_x = source_w
                mask_max_y = source_h
            
            if mask_max_x > source_w or mask_max_y > source_h:
                # Mask coordinates are at a larger scale - update scaling factors
                mask_coord_scale_w = source_w / mask_max_x if mask_max_x > source_w else 1.0
                mask_coord_scale_h = source_h / mask_max_y if mask_max_y > source_h else 1.0
                logger.info(f"Detected MASK coordinate scale mismatch!")
                logger.info(f"Frame size: {source_w}x{source_h}, Max mask coords: {mask_max_x}x{mask_max_y}")
                logger.info(f"Scaling mask coordinates by {mask_coord_scale_w:.3f}x (width) and {mask_coord_scale_h:.3f}x (height)")
                
                # Use the most restrictive scaling (mask coords might be at different scale than bbox coords)
                if not coord_scale_needed:
                    coord_scale_needed = True
                    coord_scale_w = mask_coord_scale_w
                    coord_scale_h = mask_coord_scale_h
                else:
                    # Use minimum scale to ensure all coordinates fit
                    coord_scale_w = min(coord_scale_w, mask_coord_scale_w)
                    coord_scale_h = min(coord_scale_h, mask_coord_scale_h)
        
        # Calculate scaling factors for frame resizing
        frame_scale_h = target_h / source_h
        frame_scale_w = target_w / source_w
        needs_resize = (source_h != target_h or source_w != target_w)
        
        if needs_resize:
            logger.info(f"Resizing avatar frames from {source_w}x{source_h} to {target_w}x{target_h} for GPU cache")
        
        # Convert frames to GPU tensors (HWC -> CHW format)
        # ALSO create resized CPU versions for fallback
        self.frame_list_gpu = []
        frame_list_resized = []
        for frame in self.frame_list_cycle:
            # RESIZE to match output resolution if needed
            if needs_resize:
                import cv2
                frame = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_LINEAR)
            
            frame_tensor = torch.from_numpy(frame).permute(2, 0, 1).unsqueeze(0).float().to(device)  # [1, 3, H, W]
            self.frame_list_gpu.append(frame_tensor)
            frame_list_resized.append(frame.copy())  # Keep resized CPU version for fallback
        
        # Replace original frame_list_cycle with resized version
        self.frame_list_cycle = frame_list_resized
        
        # OPTIMIZATION: Pre-allocate output buffers to avoid frame.copy() on every blend
        self.output_buffers = [frame.copy() for frame in self.frame_list_cycle]
        logger.info(f"Pre-allocated {len(self.output_buffers)} output buffers for zero-copy blending")
        
        # SCALE COORDINATES FIRST if needed (either from frame resize or coordinate mismatch)
        # This must happen BEFORE resizing masks so masks match the scaled crop_box dimensions
        if coord_scale_needed or needs_resize:
            # Calculate total scaling factor (coordinate mismatch + frame resize)
            total_scale_w = coord_scale_w * (frame_scale_w if needs_resize else 1.0)
            total_scale_h = coord_scale_h * (frame_scale_h if needs_resize else 1.0)
            
            logger.info(f"Scaling {len(self.coord_list_cycle)} coordinate sets...")
            logger.info(f"Total scale factors: {total_scale_w:.3f}x (width), {total_scale_h:.3f}x (height)")
            
            scaled_coord_list = []
            for coords in self.coord_list_cycle:
                # Scale all coordinates based on their format
                if len(coords) >= 8:
                    # Full format: [x1, y1, x2, y2, crop_x1, crop_y1, crop_x2, crop_y2]
                    scaled = [
                        int(coords[0] * total_scale_w),  # x1
                        int(coords[1] * total_scale_h),  # y1
                        int(coords[2] * total_scale_w),  # x2
                        int(coords[3] * total_scale_h),  # y2
                        int(coords[4] * total_scale_w),  # crop_x1
                        int(coords[5] * total_scale_h),  # crop_y1
                        int(coords[6] * total_scale_w),  # crop_x2
                        int(coords[7] * total_scale_h),  # crop_y2
                    ]
                elif len(coords) >= 4:
                    # Bbox only: [x1, y1, x2, y2]
                    scaled = [
                        int(coords[0] * total_scale_w),  # x1
                        int(coords[1] * total_scale_h),  # y1
                        int(coords[2] * total_scale_w),  # x2
                        int(coords[3] * total_scale_h),  # y2
                    ]
                else:
                    # Keep as-is if unexpected format
                    scaled = coords
                scaled_coord_list.append(scaled)
            self.coord_list_cycle = scaled_coord_list
            
            # Also scale mask_coords_list_cycle
            scaled_mask_coords = []
            for mask_coords in self.mask_coords_list_cycle:
                # Scale based on format
                if len(mask_coords) >= 8:
                    # Full format: [x1, y1, x2, y2, crop_x1, crop_y1, crop_x2, crop_y2]
                    scaled = [
                        int(mask_coords[0] * total_scale_w),  # x1
                        int(mask_coords[1] * total_scale_h),  # y1
                        int(mask_coords[2] * total_scale_w),  # x2
                        int(mask_coords[3] * total_scale_h),  # y2
                        int(mask_coords[4] * total_scale_w),  # crop_x1
                        int(mask_coords[5] * total_scale_h),  # crop_y1
                        int(mask_coords[6] * total_scale_w),  # crop_x2
                        int(mask_coords[7] * total_scale_h),  # crop_y2
                    ]
                elif len(mask_coords) >= 4:
                    # Bbox only: [x1, y1, x2, y2]
                    scaled = [
                        int(mask_coords[0] * total_scale_w),  # x1
                        int(mask_coords[1] * total_scale_h),  # y1
                        int(mask_coords[2] * total_scale_w),  # x2
                        int(mask_coords[3] * total_scale_h),  # y2
                    ]
                else:
                    scaled = mask_coords
                scaled_mask_coords.append(scaled)
            self.mask_coords_list_cycle = scaled_mask_coords
            
            # Scale mask_roi_list_cycle (ROI bounds)
            scaled_roi_list = []
            for roi in self.mask_roi_list_cycle:
                # roi format: [y_min, y_max, x_min, x_max]
                scaled = [
                    int(roi[0] * total_scale_h),  # y_min
                    int(roi[1] * total_scale_h),  # y_max
                    int(roi[2] * total_scale_w),  # x_min
                    int(roi[3] * total_scale_w),  # x_max
                ]
                scaled_roi_list.append(scaled)
            self.mask_roi_list_cycle = scaled_roi_list
            
            logger.info(f"✓ Coordinates scaled successfully")
        
        # NOW resize masks to match the SCALED crop_box dimensions
        # Convert masks to GPU tensors
        self.mask_list_gpu = []
        mask_list_resized = []
        
        # Debug: Check first mask size
        if len(self.mask_list_cycle) > 0:
            first_mask_shape = self.mask_list_cycle[0].shape
            first_mask_coords = self.mask_coords_list_cycle[0]
            expected_crop_w = first_mask_coords[2] - first_mask_coords[0]
            expected_crop_h = first_mask_coords[3] - first_mask_coords[1]
            logger.info(f"First mask shape: {first_mask_shape}, Expected crop_box size (after scaling): {expected_crop_h}x{expected_crop_w}")
        
        for i, mask in enumerate(self.mask_list_cycle):
            # Get the crop_box for this frame to determine expected mask size
            mask_coords = self.mask_coords_list_cycle[i]
            if len(mask_coords) >= 4:
                crop_w = mask_coords[2] - mask_coords[0]
                crop_h = mask_coords[3] - mask_coords[1]
            else:
                # Fallback: use mask's current size
                crop_h, crop_w = mask.shape[:2]
            
            # Resize mask to match crop_box size if needed (NOT full frame size!)
            if mask.shape[0] != crop_h or mask.shape[1] != crop_w:
                import cv2
                old_shape = mask.shape
                mask = cv2.resize(mask, (crop_w, crop_h), interpolation=cv2.INTER_LINEAR)
                if i == 0:  # Log first mask resize
                    logger.info(f"Resizing masks from {old_shape} to ({crop_h}, {crop_w}) to match scaled crop_box")
            
            mask_tensor = torch.from_numpy(mask).unsqueeze(0).unsqueeze(0).float().to(device) / 255.0  # [1, 1, H, W]
            self.mask_list_gpu.append(mask_tensor)
            mask_list_resized.append(mask)
        
        # Replace original mask_list_cycle with resized version
        self.mask_list_cycle = mask_list_resized
        
        # CUDA OPTIMIZATION: Create pinned memory buffer for ultra-fast GPU→CPU transfers
        # Pinned memory allows DMA (Direct Memory Access) for 2-3x faster transfers
        # Use target resolution for pinned buffer
        self._pinned_buffer = torch.empty((target_h, target_w, 3), dtype=torch.uint8, pin_memory=True)
        
        # GPU-DIRECT ENCODING: Try to enable zero-copy NVENC encoding
        self._gpu_direct_mode = False
        try:
            import PyNvVideoCodec as nvvc
            self._gpu_direct_mode = True
            logger.info("GPU-direct mode enabled: PyNvVideoCodec detected, zero CPU transfer")
        except ImportError:
            logger.info("PyNvVideoCodec not found, falling back to pinned-memory mode")
        
        logger.info("GPU cache ready: %s frames on %s", len(self.frame_list_gpu), device)
        if not self._gpu_direct_mode:
            logger.info("Pinned memory buffer allocated: %dx%dx3 for fast GPU-to-CPU transfer", target_h, target_w)
        #self.__loadavatar()

        self.asr = MuseASR(opt,self,self.audio_processor)
        self.asr.warm_up()
        
        self.render_event = mp.Event()
        self._process_thread = None
        self._inference_thread = None

    def __del__(self):
        logger.info(f'musereal({self.sessionid}) delete')
    

    def __mirror_index(self, index):
        size = len(self.coord_list_cycle)
        turn = index // size
        res = index % size
        if turn % 2 == 0:
            return res
        else:
            return size - res - 1  

    def __warm_up(self): 
        self.asr.run_step()
        whisper_chunks = self.asr.get_next_feat()
        whisper_batch = np.stack(whisper_chunks)
        latent_batch = []
        for i in range(self.batch_size):
            idx = self.__mirror_index(self.idx+i)
            latent = self.input_latent_list_cycle[idx]
            latent_batch.append(latent)
        latent_batch = torch.cat(latent_batch, dim=0)
        logger.info('infer=======')
        # for i, (whisper_batch,latent_batch) in enumerate(gen):
        audio_feature_batch = torch.from_numpy(whisper_batch)
        audio_feature_batch = audio_feature_batch.to(device=self.unet.device,
                                                        dtype=self.unet.model.dtype)
        audio_feature_batch = self.pe(audio_feature_batch)
        latent_batch = latent_batch.to(dtype=self.unet.model.dtype)

        pred_latents = self.unet.model(latent_batch, 
                                    self.timesteps, 
                                    encoder_hidden_states=audio_feature_batch).sample
        recon = self.vae.decode_latents(pred_latents)
      

    def paste_back_frame(self,pred_frame,idx:int):
        """
        Blend MuseTalk’s 256×256 output back into a cached base frame.
        Handles resolution scaling by clamping crop/bbox coordinates and
        trimming masks so shapes always agree.
        """
        ori_frame = self.frame_list_cycle[idx]
        H, W = ori_frame.shape[:2]

        bbox = list(self.coord_list_cycle[idx])
        x1_raw, y1_raw, x2_raw, y2_raw = bbox

        crop_box = list(self.mask_coords_list_cycle[idx])
        x_s_raw, y_s_raw, x_e_raw, y_e_raw = crop_box

        mask = self.mask_list_cycle[idx]

        # Clamp crop box to frame bounds
        x_s = max(0, min(W, x_s_raw))
        y_s = max(0, min(H, y_s_raw))
        x_e = max(0, min(W, x_e_raw))
        y_e = max(0, min(H, y_e_raw))

        if x_e <= x_s or y_e <= y_s:
            logger.warning(f"[Session {self.sessionid}] crop collapsed after clamping: {crop_box} -> {(x_s, y_s, x_e, y_e)}")
            return ori_frame

        # Crop the base frame into the pre-allocated buffer
        output_buffer = self.output_buffers[idx]
        np.copyto(output_buffer[y_s:y_e, x_s:x_e], ori_frame[y_s:y_e, x_s:x_e])

        # Determine how much of the mask we need to trim off each side
        mask_x0 = int(max(0, x_s - x_s_raw))
        mask_y0 = int(max(0, y_s - y_s_raw))
        mask_x1 = int(mask.shape[1] - max(0, x_e_raw - x_e))
        mask_y1 = int(mask.shape[0] - max(0, y_e_raw - y_e))

        mask_cropped = mask[mask_y0:mask_y1, mask_x0:mask_x1]
        if mask_cropped.size == 0:
            logger.warning(f"[Session {self.sessionid}] mask vanished after trimming (idx={idx})")
            return output_buffer

        crop_w = x_e - x_s
        crop_h = y_e - y_s
        if mask_cropped.shape[0] != crop_h or mask_cropped.shape[1] != crop_w:
            mask_cropped = cv2.resize(mask_cropped, (crop_w, crop_h), interpolation=cv2.INTER_LINEAR)

        # Clamp bbox to lie inside the clamped crop
        x1 = max(x_s, min(x_e, x1_raw))
        y1 = max(y_s, min(y_e, y1_raw))
        x2 = max(x_s, min(x_e, x2_raw))
        y2 = max(y_s, min(y_e, y2_raw))
        if x2 <= x1 or y2 <= y1:
            logger.warning(f"[Session {self.sessionid}] bbox collapsed after clamping: {bbox} -> {(x1, y1, x2, y2)}")
            return output_buffer

        target_w = max(1, x2 - x1)
        target_h = max(1, y2 - y1)
        pred_frame_resized = cv2.resize(pred_frame, (target_w, target_h), interpolation=cv2.INTER_LINEAR)

        new_crop_box = (x_s, y_s, x_e, y_e)
        new_bbox = (x1, y1, x2, y2)
        result = get_image_blending(output_buffer, pred_frame_resized, new_bbox, mask_cropped, new_crop_box)
        return result
    
    def paste_back_frame_on_custom(self, pred_frame, custom_frame, frame_idx:int, bbox, mask, mask_crop_box):
        """Blend MuseTalk lip-sync onto a custom emotion frame using custom clip's coords/masks"""
        ori_frame = copy.deepcopy(custom_frame)  # Use custom frame as background
        x1, y1, x2, y2 = bbox

        # Calculate bbox dimensions
        bbox_w = x2 - x1
        bbox_h = y2 - y1
        
        # Calculate mask_crop_box dimensions
        crop_w = mask_crop_box[2] - mask_crop_box[0]
        crop_h = mask_crop_box[3] - mask_crop_box[1]
        
        # Log dimensions only once per session for custom frames
        if not hasattr(self, '_logged_custom_dimensions'):
            logger.info(f"[Session {self.sessionid}] Custom frame dimensions: bbox={bbox}, mask_crop_box={mask_crop_box}")
            self._logged_custom_dimensions = True
        
        # CRITICAL FIX: Resize pred_frame to match bbox dimensions (what get_image_blending expects)
        # NOT mask_crop_box dimensions!
        res_frame = cv2.resize(pred_frame.astype(np.uint8), (bbox_w, bbox_h))
        
        # Ensure mask matches the crop box dimensions
        if mask.shape[0] != crop_h or mask.shape[1] != crop_w:
            mask = cv2.resize(mask, (crop_w, crop_h))

        combine_frame = get_image_blending(ori_frame,res_frame,bbox,mask,mask_crop_box)
        return combine_frame
            
    def render(self,quit_event,loop=None,audio_track=None,video_track=None):
        #if self.opt.asr:
        #     self.asr.warm_up()
        self._player_quit_event = quit_event
        self._loop = loop
        self._audio_track = audio_track
        self._video_track = video_track
        self.tts.render(quit_event)
        self.init_customindex()
        self.reset_audio_sync_state()
        self._process_thread = Thread(
            target=self.process_frames,
            args=(quit_event,loop,audio_track,video_track),
            name=f"muse-process-{self.sessionid}",
            daemon=True,
        )
        self._process_thread.start()

        self.render_event.set() #start infer process render
        self._inference_thread = Thread(
            target=inference,
            args=(
                self.render_event,
                self.batch_size,
                self.input_latent_list_cycle,
                self.asr.feat_queue,
                self.asr.output_queue,
                self.res_frame_queue,
                self.vae,
                self.unet,
                self.pe,
                self.timesteps,
            ),
            name=f"muse-infer-{self.sessionid}",
            daemon=True,
        )
        self._inference_thread.start() #mp.Process
        count=0
        totaltime=0
        _starttime=time.perf_counter()
        #_totalframe=0
        while not quit_event.is_set(): #todo
            # update texture every frame
            # audio stream thread...
            t = time.perf_counter()
            
            # CRITICAL FIX: Only call run_step() when there are enough frames available
            # run_step() tries to pull batch_size*2 frames (16 with batch_size=8)
            # If we call it when frames aren't ready, it injects silence and real audio accumulates
            # This causes the queue to overflow and drop real audio frames (stuttering)
            required_frames = self.opt.batch_size * 2 + self.asr.stride_left_size + self.asr.stride_right_size
            if self.asr.queue.qsize() >= required_frames:
                self.asr.run_step()
            else:
                # Not enough frames yet, sleep briefly to avoid tight loop
                time.sleep(0.002)  # 2ms sleep to reduce CPU usage
            #self.test_step(loop,audio_track,video_track)
            # totaltime += (time.perf_counter() - t)
            # count += self.opt.batch_size
            # if count>=100:
            #     print(f"------actual avg infer fps:{count/totaltime:.4f}")
            #     count=0
            #     totaltime=0
            # CRITICAL FIX: Removed blocking sleep that caused audio stuttering
            # The sleep here blocked the entire ASR loop, causing audio frames to pile up
            # Video queue has its own backpressure mechanism - no need to block audio
            # if video_track and video_track._queue.qsize()>=0.75*self.opt.batch_size:
            #     # Only log if queue is backing up significantly
            #     if video_track._queue.qsize() > 50:
            #         logger.warning(f'[Session {self.sessionid}] Video queue backing up: {video_track._queue.qsize()} frames')
            #     time.sleep(0.04*video_track._queue.qsize()*0.8)
            # if video_track._queue.qsize()>=5:
            #     print('sleep qsize=',video_track._queue.qsize())
            #     time.sleep(0.04*video_track._queue.qsize()*0.8)
                
            # delay = _starttime+_totalframe*0.04-time.perf_counter() #40ms
            # if delay > 0:
            #     time.sleep(delay)
        self.render_event.clear() #end infer process render
        logger.info('musereal thread stop')
            
    def stop_stream(self):
        logger.info(f"Stopping MuseReal stream for session {getattr(self, 'sessionid', 'unknown')}")
        # Signal worker loops to exit
        if getattr(self, "_player_quit_event", None) is not None:
            self._player_quit_event.set()
        try:
            self.render_event.clear()
        except Exception:
            pass

        # Flush queued work
        try:
            self.tts.flush_talk()
            if hasattr(self.tts, "shutdown"):
                self.tts.shutdown()
        except Exception as exc:
            logger.debug(f"TTS shutdown error: {exc}")

        try:
            self.asr.flush_talk()
            # Stop async Whisper processing thread if present
            if hasattr(self.asr, 'stop_processing_thread'):
                self.asr.stop_processing_thread()
        except Exception as exc:
            logger.debug(f"ASR flush error: {exc}")

        # Drain pending frames/features
        for q in [
            getattr(self, "res_frame_queue", None),
            getattr(self.asr, "queue", None),
            getattr(self.asr, "output_queue", None),
            getattr(self.asr, "feat_queue", None),
        ]:
            self._drain_queue(q)

        # Join worker threads gracefully
        if getattr(self, "_inference_thread", None) and self._inference_thread.is_alive():
            self._inference_thread.join(timeout=2.0)
        self._inference_thread = None

        if getattr(self, "_process_thread", None) and self._process_thread.is_alive():
            self._process_thread.join(timeout=2.0)
        self._process_thread = None

        # Clear loop/track references
        self._loop = None
        self._audio_track = None
        self._video_track = None
        self._player_quit_event = None
        self.reset_audio_sync_state()