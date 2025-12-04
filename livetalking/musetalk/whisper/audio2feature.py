import os
from .whisper import load_model
from .whisper.audio import log_mel_spectrogram, pad_or_trim, N_FRAMES
import soundfile as sf
import numpy as np
import torch
import time
import sys
sys.path.append("..")

class Audio2Feature():
    def __init__(self, 
                 whisper_model_type="tiny",
                 model_path="./models/whisper/tiny.pt"):
        self.whisper_model_type = whisper_model_type
        self.model = load_model(model_path) #

    def get_sliced_feature(self,
                           feature_array, 
                           vid_idx, 
                           audio_feat_length=[2,2],
                           fps=25):
        """
        Get sliced features based on a given index
        :param feature_array: 
        :param start_idx: the start index of the feature
        :param audio_feat_length:
        :return: 
        """
        length = len(feature_array)
        selected_feature = []
        selected_idx = []
        
        center_idx = int(vid_idx*50/fps) 
        left_idx = center_idx-audio_feat_length[0]*2
        right_idx = center_idx + (audio_feat_length[1]+1)*2
        
        for idx in range(left_idx,right_idx):
            idx = max(0, idx)
            idx = min(length-1, idx)
            x = feature_array[idx]
            selected_feature.append(x)
            selected_idx.append(idx)
        
        selected_feature = np.concatenate(selected_feature, axis=0)
        selected_feature = selected_feature.reshape(-1, 384)# 50*384
        return selected_feature,selected_idx

    def get_sliced_feature_sparse(self,feature_array, vid_idx, audio_feat_length= [2,2],fps = 25):
        """
        Get sliced features based on a given index
        :param feature_array: 
        :param start_idx: the start index of the feature
        :param audio_feat_length:
        :return: 
        """
        length = len(feature_array)
        selected_feature = []
        selected_idx = []

        for dt in range(-audio_feat_length[0],audio_feat_length[1]+1):
            left_idx = int((vid_idx+dt)*50/fps)
            if left_idx<1 or left_idx>length-1:
                print('test-----,left_idx=',left_idx)
                left_idx = max(0, left_idx)
                left_idx = min(length-1, left_idx)

                x = feature_array[left_idx]
                x = x[np.newaxis,:,:]
                x = np.repeat(x, 2, axis=0)
                selected_feature.append(x)
                selected_idx.append(left_idx)
                selected_idx.append(left_idx)
            else:
                x = feature_array[left_idx-1:left_idx+1]
                selected_feature.append(x)
                selected_idx.append(left_idx-1)
                selected_idx.append(left_idx)
        selected_feature = np.concatenate(selected_feature, axis=0)
        selected_feature = selected_feature.reshape(-1, 384)# 50*384
        return selected_feature,selected_idx
    

    def feature2chunks(self,feature_array,fps,batch_size,audio_feat_length = [2,2],start=0):
        whisper_chunks = []
        whisper_idx_multiplier = 50./fps 
        i = 0
        #print(f"video in {fps} FPS, audio idx in 50FPS")
        for _ in range(batch_size):
            # start_idx = int(i * whisper_idx_multiplier)
            # if start_idx>=len(feature_array):
            #     break
            selected_feature,selected_idx = self.get_sliced_feature(feature_array= feature_array,vid_idx = i+start,audio_feat_length=audio_feat_length,fps=fps)
            #print(f"i:{i},selected_idx {selected_idx}")
            whisper_chunks.append(selected_feature)
            i += 1
            

        return whisper_chunks

    def audio2feat(self,audio_path):
        """
        OPTIMIZED: Extract audio features using Whisper encoder ONLY.
        This skips the decoder (transcription) which we don't need for lip-sync.
        ~3-5x faster than full transcribe() call.
        """
        # Convert audio to mel spectrogram
        mel = log_mel_spectrogram(audio_path)
        
        # Use float16 if model supports it for faster inference
        dtype = torch.float16 if self.model.device != torch.device("cpu") else torch.float32
        
        embed_list = []
        num_frames = mel.shape[-1]
        seek = 0
        sample_skip = 3000  # Process 3000 frames at a time (30 seconds)
        
        # Process mel spectrogram in chunks, extracting encoder embeddings only
        while seek < num_frames:
            end_seek = min(seek + sample_skip, num_frames)
            
            # Pad or trim segment to expected size
            segment = pad_or_trim(mel[:, seek:seek+sample_skip], N_FRAMES)
            segment = segment.to(self.model.device).to(dtype)
            
            # Ensure correct dimensions
            if segment.ndim == 2:
                segment = segment.unsqueeze(0)
            
            # CRITICAL: Call encoder directly with include_embeddings=True
            # This skips the decoder (transcription) entirely
            with torch.no_grad():
                audio_features, encoder_embeddings = self.model.encoder(segment, include_embeddings=True)
            
            # Process embeddings to match expected format
            encoder_embeddings = encoder_embeddings.transpose(0,2,1,3)
            encoder_embeddings = encoder_embeddings.squeeze(0)
            
            # Calculate how many frames were actually processed
            frames_processed = min(sample_skip, num_frames - seek)
            emb_end_idx = int(frames_processed / 2)
            
            # Convert to numpy if it's a tensor
            if torch.is_tensor(encoder_embeddings):
                embed_list.append(encoder_embeddings[:emb_end_idx].cpu().numpy())
            else:
                embed_list.append(encoder_embeddings[:emb_end_idx])
            seek += sample_skip
        
        # Concatenate all embeddings
        concatenated_array = np.concatenate(embed_list, axis=0)
        return concatenated_array

if __name__ == "__main__":
    audio_processor = Audio2Feature(model_path="../../models/whisper/whisper_tiny.pt")
    audio_path = "./test.mp3"
    array = audio_processor.audio2feat(audio_path)
    print(array.shape)
    fps = 25
    whisper_idx_multiplier = 50./fps 

    i = 0
    print(f"video in {fps} FPS, audio idx in 50FPS")
    while 1:
        start_idx = int(i * whisper_idx_multiplier)
        selected_feature,selected_idx = audio_processor.get_sliced_feature(feature_array= array,vid_idx = i,audio_feat_length=[2,2],fps=fps)
        print(f"video idx {i},\t audio idx {selected_idx},\t shape {selected_feature.shape}")
        i += 1
        if start_idx>len(array):
            break
