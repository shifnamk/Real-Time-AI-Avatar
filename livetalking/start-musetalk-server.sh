#!/bin/bash
###############################################################################
# Start MuseTalk WebSocket Server
###############################################################################

echo "========================================"
echo "Starting MuseTalk WebSocket Server"
echo "========================================"
echo ""

# Activate conda environment
source ~/miniconda3/bin/activate nerfstream

# Check if avatar assets exist
AVATAR_DIR="./data/avatars/calm_avatar_realtime"
if [ ! -d "$AVATAR_DIR" ]; then
    echo "❌ Error: Avatar assets not found at $AVATAR_DIR"
    echo ""
    echo "Please generate avatar assets first:"
    echo "python genavatar_musetalk.py \\"
    echo "  --file /mnt/c/shifna/Avatar_LiveTalking/data/customvideo/calm_normal_01/image \\"
    echo "  --avatar_id calm_avatar_realtime \\"
    echo "  --version v15 \\"
    echo "  --bbox_shift 0"
    echo ""
    exit 1
fi

# Start the server
python musetalk_websocket_server.py \
  --avatar_id calm_avatar_realtime \
  --host 0.0.0.0 \
  --port 8010 \
  --batch_size 8


