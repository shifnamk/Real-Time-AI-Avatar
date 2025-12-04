# 🎭 Real-Time AI Avatar with Voice Agent

Real-time AI avatar system with lip-sync powered by MuseTalk, integrated with OpenAI Realtime API for natural conversations.

## 🎬 Live Demo

> **🚀 See it in action! Real-time lip-sync with OpenAI voice agent**

### 🎥 Avatar Demo Video


https://github.com/user-attachments/assets/ae353c6f-cb33-48dd-b523-90fa668b5dc6



https://github.com/shifnamk/Real-Time-AI-Avatar/assets/Avatar-Demo.mp4

**🎯 What you'll see:**
- Real-time lip synchronization with MuseTalk
- Voice interaction powered by OpenAI Realtime API  
- Instant response and interruption handling
- Natural facial expressions and movements
- High-quality 48kHz audio output

**📥 Full Quality:** [Download Original (51.2 MB)](https://github.com/shifnamk/Real-Time-AI-Avatar/raw/master/Avatar.mp4)

**Key Features Demonstrated:**
- ✨ Real-time lip synchronization with MuseTalk
- 🎤 Voice interaction powered by OpenAI Realtime API  
- ⚡ Instant response and interruption handling
- 🎯 Natural facial expressions and movements
- 🔊 High-quality 48kHz audio output

---

## 🚀 Features

- **Real-time Lip Sync**: MuseTalk-powered lip synchronization at 50 FPS
- **OpenAI Realtime Voice Agent**: Local browser-based voice agent with OpenAI GPT-4o
- **GPU Accelerated**: CUDA-optimized for smooth performance
- **WebRTC Streaming**: Low-latency video/audio streaming via WebRTC data channels
- **Closed Lips on Silence**: Natural closed-lip appearance when not speaking
- **High Quality Audio**: 48kHz audio output for crystal-clear voice
- **Interruption Support**: Real-time user interruption detection
- **Low Latency**: Optimized audio transport (5-10ms via WebRTC)

## 📋 Requirements

### Hardware
- NVIDIA GPU (CUDA support required)
- 8GB+ VRAM recommended

### Software
- Windows 10/11
- Python 3.8+
- Node.js 16+
- CUDA Toolkit
- WSL2 (Ubuntu 22.04) for avatar asset creation
- OpenAI API key (for voice agent)

## 🎯 Quick Start

### 1. Install Dependencies

**Python (Backend):**
```bash
cd avatar_backup_v1
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**Node.js (Frontend):**
```bash
cd avatar-frontend
npm install
```

### 2. Configure OpenAI API Key

Edit `avatar-frontend/src/App.tsx` and add your OpenAI API key:
```typescript
const OPENAI_API_KEY = ''; // Add your OpenAI API key here
```

### 3. Start the System

**Terminal 1 - Start LiveTalking Server:**
```bash
cd avatar_agent
start_livetalking.bat
```
Wait for: `GPU cache ready: 88 frames on cuda`

**Terminal 2 - Build Frontend & Start Backend:**
```bash
cd avatar-frontend
npm run build
node avatar-backend.cjs
```

**Access:**
- Application: http://localhost:5000
- LiveTalking: http://localhost:8010

### 4. Start Conversation

1. Open http://localhost:5000 in your browser
2. Click **"Start Session"**
3. Allow microphone access
4. Start talking to the avatar!
5. The avatar will respond using OpenAI's GPT-4o voice model

### 5. Host Publicly (Optional)

#### Option A: Tailscale (Recommended for secure access)
```bash
# Install Tailscale and connect your device
tailscale serve https / http://localhost:5000
tailscale funnel --https 443 http://localhost:5000
```

#### Option B: ngrok (For temporary public access)
```bash
ngrok http 5000
```

**Note:** Public hosting requires HTTPS for microphone access. Tailscale Funnel provides automatic HTTPS certificates.

When you update frontend code, re-run `npm run build` before restarting the backend.

## 📁 Project Structure

```
avatar_backup_v1/
├── avatar-frontend/          # React frontend
│   ├── src/
│   │   ├── App.tsx           # Main app component
│   │   ├── LiveTalkingAvatar.tsx  # Avatar video component (WebRTC)
│   │   └── LocalVoiceAgentBridge.tsx  # OpenAI Realtime voice agent
│   ├── avatar-backend.cjs    # Backend proxy server
│   └── package.json          # Node.js dependencies
│
├── avatar_agent/             # LiveTalking launcher
│   └── start_livetalking.bat # Server start script
│
├── livetalking/              # MuseTalk engine
│   ├── app.py                # WebRTC server
│   ├── basereal.py           # Core renderer
│   ├── baseasr.py            # Audio processing & silence detection
│   ├── musereal.py           # MuseTalk integration
│   ├── museasr.py            # Whisper ASR
│   ├── webrtc.py             # WebRTC handler
│   ├── web/                  # Web interface
│   ├── models/               # AI models
│   │   ├── musetalkV15/      # Lip-sync model
│   │   ├── whisper/          # Speech recognition
│   │   ├── sd-vae-ft-mse/    # VAE encoder (for asset creation)
│   │   ├── dwpose/           # Pose detection (for asset creation)
│   │   └── face-parse-bisent/ # Face parsing (for asset creation)
│   ├── data/avatars/
│   │   └── calm_avatar_realtime/  # Active avatar (88 frames)
│   ├── closed_lips_humming.wav    # Silence audio (16kHz)
│   ├── closed_lips_humming_48k.wav # Silence audio (48kHz)
│   └── genavatar_musetalk.py # Avatar asset generator
│
├── mmcv/                     # Computer vision library (for asset creation)
├── MuseTalk/                 # MuseTalk library
└── requirements.txt          # Python dependencies
```

## 🎨 Creating New Avatar Assets

To create a new avatar from a video:

### 1. Prepare Video
Place your base video in a folder, e.g., `avatr_base_video/avatar_base.mp4`

Requirements:
- Single person, front-facing
- 25-30 FPS
- Clear face visibility
- Recommended: 5-10 seconds of neutral expression

### 2. Generate Avatar Assets

```bash
wsl.exe -d Ubuntu-22.04 -- bash -c "cd /mnt/c/shifna/avatar_backup_v1/livetalking && source ~/miniconda3/bin/activate nerfstream && python genavatar_musetalk.py --file ../avatr_base_video/avatar_base.mp4 --avatar_id new_avatar_name --bbox_shift 5"
```

**Parameters:**
- `--file`: Path to your base video
- `--avatar_id`: Name for the new avatar
- `--bbox_shift`: Vertical adjustment (default: 0, increase to shift down)

### 3. Update Configuration

Edit `avatar_agent/start_livetalking.bat`:
```batch
--avatar_id new_avatar_name
```

## ⚙️ Configuration

### OpenAI Voice Agent

Edit `avatar-frontend/src/App.tsx`:
```typescript
const OPENAI_API_KEY = ''; // Add your OpenAI API key here
```

Edit `avatar-frontend/src/LocalVoiceAgentBridge.tsx` to customize the AI behavior:
```typescript
// Session configuration
session: {
  modalities: ['text', 'audio'],
  instructions: 'You are a helpful voice assistant for 3 Fils Restaurants. Be conversational and brief.',
  voice: 'alloy',  // Options: alloy, echo, fable, onyx, nova, shimmer
  turn_detection: {
    type: 'server_vad',
    threshold: 0.5,
    prefix_padding_ms: 200,
    silence_duration_ms: 300,
  },
}
```

### Silence Detection

Edit `livetalking/baseasr.py`:
```python
self.silence_rms_threshold = 0.0001  # Adjust sensitivity
```

Lower value = more sensitive (detects softer sounds as speech)
Higher value = less sensitive (requires louder sounds)

### Video Quality

Edit `avatar_agent/start_livetalking.bat`:
```batch
--W 1280              # Width
--H 720               # Height
--batch_size 16       # Processing batch size
--fps 50              # Frame rate (default)
```

## 🔧 Troubleshooting

### OpenAI API connection fails
- Verify API key is correct in `App.tsx`
- Check browser console for WebSocket errors
- Ensure you have OpenAI API credits

### Avatar lips shivering/open during silence
- Check `livetalking/closed_lips_humming.wav` exists
- Verify silence detection logs: `[CLOSED LIPS]` messages
- Adjust `silence_rms_threshold` in `baseasr.py`

### GPU not detected
- Install CUDA Toolkit
- Check: `torch.cuda.is_available()` returns `True`
- Look for: `GPU cache ready: 88 frames on cuda` in logs

### Audio stuttering or queue overflow
- Check for `[AUDIO QUEUE] Full` warnings in logs
- Increase keepalive startup delay in `LocalVoiceAgentBridge.tsx`
- Verify WebRTC data channel is open (look for `[WebRTC]` in logs)

### WebRTC connection fails
- Check firewall settings
- Verify ports 8010 and 5000 are available
- Try restarting the LiveTalking server
- Check browser console for WebRTC errors

### Microphone not working
- Ensure HTTPS is used (required for microphone access)
- Check browser permissions
- Verify microphone is not in use by another application

### Interruption not working
- Check RMS threshold in `LocalVoiceAgentBridge.tsx`
- Verify `SPEECH_ENERGY_THRESHOLD` is appropriate for your microphone
- Look for `⚡ INTERRUPTION DETECTED` in browser console

## 📊 Performance

**Typical Performance (RTX 3060+):**
- Lip Sync: 50 FPS
- MuseTalk Inference: 90-105 FPS
- Video Output: 20-25 FPS
- Audio: 48kHz, 128kbps
- Audio Transport: 5-10ms (WebRTC) vs 50-70ms (HTTP)
- OpenAI Response: ~500ms (with optimized turn detection)
- End-to-End Latency: 1000-1300ms (30-35% faster than before)

## 🛠️ Tech Stack

**Backend:**
- Python 3.8+
- PyTorch (CUDA)
- MuseTalk (lip-sync)
- Whisper (speech recognition)
- aiortc (WebRTC server)

**Frontend:**
- React + TypeScript
- Vite
- WebRTC Data Channels (low-latency audio)
- Web Audio API (microphone capture)
- OpenAI Realtime API (voice agent)

**Models:**
- MuseTalk V1.5 (lip-sync)
- Whisper Tiny (ASR for transcription)
- OpenAI GPT-4o Realtime (voice agent)
- SD-VAE-FT-MSE (video encoding for asset creation)
- DWPose (face landmarks for asset creation)

## 📝 Notes

### Voice Agent Architecture
The system uses a **browser-based voice agent** that connects directly to OpenAI's Realtime API:
1. User speaks → Microphone capture (Web Audio API)
2. Audio sent to OpenAI → GPT-4o processes and responds
3. AI audio received → Sent to LiveTalking via WebRTC data channel
4. MuseTalk generates lip-sync → Video streamed back to browser

**Benefits:**
- No Python voice agent needed
- Lower latency (WebRTC data channels)
- Real-time interruption support
- Simpler architecture

### Closed Lips Implementation
When the AI is silent, the system:
1. Detects silence using RMS threshold (< 0.0001)
2. Sends near-silence audio to MuseTalk (generates natural closed lips)
3. Forces output audio to zeros (user hears nothing)
4. Continuously generates frames (adapts to head pose motion)

### Audio Pipeline
- Input: OpenAI Realtime API → 24kHz PCM16 audio
- Resampling: 24kHz → 48kHz for LiveTalking
- Transport: WebRTC data channel (5-10ms latency)
- Processing: Downsampled to 16kHz for MuseTalk
- Output: 48kHz HQ audio to user
- Dual-path during silence: Near-silence to MuseTalk, zeros to output

### Latency Optimizations
- **WebRTC Data Channel**: 85% faster than HTTP (5-10ms vs 50-70ms)
- **Reduced Turn Detection**: 300ms faster AI response
- **Keepalive Startup Delay**: Eliminates audio queue overflow
- **Total Improvement**: 30-35% faster end-to-end latency

## 📚 Documentation

- **[QUICK_START_LOCAL_VOICE_AGENT.md](QUICK_START_LOCAL_VOICE_AGENT.md)** - Detailed setup guide
- **[LATENCY_OPTIMIZATIONS.md](avatar-frontend/LATENCY_OPTIMIZATIONS.md)** - Performance optimizations
- **[INTERRUPTION_FEATURE.md](avatar-frontend/INTERRUPTION_FEATURE.md)** - Interruption implementation
- **[STUTTERING_FIX.md](avatar-frontend/STUTTERING_FIX.md)** - Audio stuttering fix
- **[DEPENDENCIES_UPDATE.md](DEPENDENCIES_UPDATE.md)** - Dependency changes

## 📄 License

See individual component licenses:
- MuseTalk: Apache 2.0
- OpenAI: Commercial API (requires API key)

## 🙏 Credits

- [MuseTalk](https://github.com/TMElyralab/MuseTalk) - Real-time lip-sync
- [OpenAI Realtime API](https://platform.openai.com/docs/guides/realtime) - Voice agent
- [Whisper](https://github.com/openai/whisper) - Speech recognition


