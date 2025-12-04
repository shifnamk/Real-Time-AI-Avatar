import argparse
import os
import shutil
import sys
import tempfile

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT_DIR)

import cv2
import numpy as np
import torch

from lipreal import load_model, load_avatar, device as wav_device
from wav2lip import audio

def mirror_index(size: int, index: int) -> int:
    turn = index // size
    res = index % size
    return res if turn % 2 == 0 else size - res - 1


def paste_back_frame(frame_list, coord_list, pred_frame, idx: int):
    y1, y2, x1, x2 = coord_list[idx]
    base_frame = frame_list[idx].copy()
    resized = cv2.resize(pred_frame.astype(np.uint8), (x2 - x1, y2 - y1))
    base_frame[y1:y2, x1:x2] = resized
    cv2.putText(base_frame, "LiveTalking", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (128, 128, 128), 1)
    return base_frame


def build_mel_chunks(audio_path: str, fps: int):
    wav = audio.load_wav(audio_path, 16000)
    mel = audio.melspectrogram(wav)
    mel_step_size = 16
    mel_idx_multiplier = 80.0 / fps

    mel_chunks = []
    i = 0
    while True:
        start_idx = int(i * mel_idx_multiplier)
        if start_idx + mel_step_size > mel.shape[1]:
            mel_chunks.append(mel[:, -mel_step_size:])
            break
        mel_chunks.append(mel[:, start_idx:start_idx + mel_step_size])
        i += 1
    return mel_chunks


def generate_frames(model, avatar_data, mel_chunks, batch_size: int):
    frame_list, face_list, coord_list = avatar_data
    total_frames = len(mel_chunks)
    results = []
    index = 0

    for start in range(0, total_frames, batch_size):
        mel_batch = mel_chunks[start:start + batch_size]
        current_batch = len(mel_batch)

        img_batch = []
        for i in range(current_batch):
            idx = mirror_index(len(face_list), index + i)
            img_batch.append(face_list[idx])

        img_batch = np.asarray(img_batch)
        h = img_batch.shape[1]
        img_masked = img_batch.copy()
        img_masked[:, h // 2 :, :, :] = 0
        img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.0

        mel_batch = np.asarray(mel_batch)
        mel_batch = np.reshape(mel_batch, [mel_batch.shape[0], mel_batch.shape[1], mel_batch.shape[2], 1])

        img_batch = torch.FloatTensor(np.transpose(img_batch, (0, 3, 1, 2))).to(wav_device)
        mel_batch = torch.FloatTensor(np.transpose(mel_batch, (0, 3, 1, 2))).to(wav_device)

        with torch.no_grad():
            pred = model(mel_batch, img_batch)

        pred = pred.cpu().numpy().transpose(0, 2, 3, 1) * 255.0

        for i in range(current_batch):
            idx = mirror_index(len(coord_list), index)
            combined = paste_back_frame(frame_list, coord_list, pred[i], idx)
            results.append(combined)
            index += 1

    return results


def write_video_with_audio(frames, audio_path, output_path, fps: int):
    if not frames:
        raise RuntimeError("No frames were generated.")

    height, width = frames[0].shape[:2]
    temp_dir = tempfile.mkdtemp()
    try:
        video_path = os.path.join(temp_dir, "temp_video.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(video_path, fourcc, fps, (width, height))

        for frame in frames:
            writer.write(frame)
        writer.release()

        cmd = (
            f'ffmpeg -y -i "{audio_path}" -i "{video_path}" '
            f'-c:v copy -c:a aac -shortest "{output_path}"'
        )
        if os.system(cmd) != 0:
            raise RuntimeError("ffmpeg failed to mux audio and video")
    finally:
        shutil.rmtree(temp_dir)


def main():
    parser = argparse.ArgumentParser(description="Generate a talking avatar video using Wav2Lip assets")
    parser.add_argument("--avatar_id", required=True, help="Avatar ID under data/avatars/")
    parser.add_argument("--audio_path", required=True, help="Path to input WAV file")
    parser.add_argument("--output_path", required=True, help="Destination MP4 file")
    parser.add_argument("--batch_size", type=int, default=16)
    parser.add_argument("--fps", type=int, default=25)
    args = parser.parse_args()

    audio_path = os.path.abspath(args.audio_path)
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    avatar_path = os.path.join(ROOT_DIR, "data", "avatars", args.avatar_id)
    if not os.path.isdir(avatar_path):
        raise FileNotFoundError(f"Avatar assets not found at {avatar_path}")

    model = load_model(os.path.join(ROOT_DIR, "models", "wav2lip.pth"))
    avatar = load_avatar(args.avatar_id)

    mel_chunks = build_mel_chunks(audio_path, args.fps)
    frames = generate_frames(model, avatar, mel_chunks, args.batch_size)

    output_path = os.path.abspath(args.output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    write_video_with_audio(frames, audio_path, output_path, args.fps)


if __name__ == "__main__":
    main()

