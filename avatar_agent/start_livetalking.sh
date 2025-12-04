#!/bin/bash
# Start LiveTalking realtime server with customvideo config

cd /mnt/c/shifna/Avatar_LiveTalking/LiveTalking

echo "Starting LiveTalking MuseTalk realtime server..."
echo

# Activate conda environment (if using conda)
# source ~/miniconda3/etc/profile.d/conda.sh
# conda activate nerfstream

# Start LiveTalking with customvideo config
python app.py \
    --transport webrtc \
    --model musetalk \
    --avatar_id neutral_normal_01 \
    --customvideo_config ../data/customvideo_config.json \
    --listenport 8010 \
    --batch_size 16 \
    --W 512 \
    --H 512

