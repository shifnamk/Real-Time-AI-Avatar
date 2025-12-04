# Local Voice Agent Integration

This document explains how the avatar system has been updated to use a **local OpenAI Realtime API** instead of the LiveKit voice agent.

## Architecture Overview

### Previous System (LiveKit)
```
Browser → LiveKit Room → Voice Agent (cloud) → AudioBridge → LiveTalking → Avatar Video
```

### New System (Local OpenAI Realtime)
```
Browser → OpenAI Realtime WebSocket (local) → LocalVoiceAgentBridge → LiveTalking → Avatar Video
```

## Key Changes

### 1. **Frontend Changes**

#### New Component: `LocalVoiceAgentBridge.tsx`
- Connects directly to OpenAI Realtime API via WebSocket
- Captures microphone audio using `getUserMedia()`
- Sends audio to OpenAI as PCM16 base64
- Receives AI audio responses
- Forwards AI audio to LiveTalking `/humanaudio` endpoint

#### Updated: `App.tsx`
- Removed LiveKit dependencies (`LiveKitRoom`, `AudioBridge`)
- Simplified session management (no external API calls)
- Uses `LocalVoiceAgentBridge` instead

#### Updated: `package.json`
- Removed `@livekit/components-react`
- Removed `livekit-client`
- Removed `livekit-server-sdk`

### 2. **Backend Changes**

#### Simplified: `avatar-backend.cjs`
- Removed all LiveKit-specific code
- Removed voice agent API proxy
- Only proxies LiveTalking server (`/livetalking`)
- Serves static frontend build

### 3. **Unchanged Components**

✅ **LiveTalking Server** - No changes
✅ **MuseTalk Pipeline** - No changes
✅ **LiveTalkingAvatar.tsx** - No changes
✅ **WebRTC Video Streaming** - No changes
✅ **Silence Detection & Closed Lips** - No changes

## Setup Instructions

### 1. Install Dependencies

```bash
cd avatar-frontend
npm install
```

### 2. Configure OpenAI API Key

Edit `src/App.tsx` and replace the `OPENAI_API_KEY`:

```typescript
const OPENAI_API_KEY = ''; // Add your OpenAI API key here
```

⚠️ **Security Note**: For production, use environment variables instead of hardcoding the API key.

### 3. Build Frontend

```bash
npm run build
```

### 4. Start Services

#### Terminal 1: Start LiveTalking Server
```bash
cd avatar_agent
./start_livetalking.bat
```

#### Terminal 2: Start Backend Server
```bash
cd avatar-frontend
node avatar-backend.cjs
```

### 5. Access the Avatar

Open your browser and navigate to:
```
http://localhost:5000
```

Click "Start Session" to begin!

## How It Works

### Audio Flow

1. **User speaks** → Browser captures mic audio
2. **LocalVoiceAgentBridge** → Sends audio to OpenAI Realtime API
3. **OpenAI processes** → Returns AI audio response
4. **LocalVoiceAgentBridge** → Forwards AI audio to `/livetalking/humanaudio`
5. **LiveTalking** → Processes with MuseTalk for lip-sync
6. **MuseTalk** → Generates video frames
7. **WebRTC** → Streams video/audio back to browser
8. **LiveTalkingAvatar** → Displays in `<video>` element

### Audio Format Details

- **Microphone Input**: 24kHz PCM16 (OpenAI Realtime requirement)
- **AI Audio Output**: 24kHz PCM16 (from OpenAI)
- **Resampled to**: 48kHz PCM16 (for LiveTalking)
- **Sent as**: 40ms WAV blocks (1920 samples @ 48kHz)

## Advantages of Local Voice Agent

1. ✅ **No LiveKit dependency** - Simpler architecture
2. ✅ **Direct OpenAI integration** - Lower latency
3. ✅ **Browser-based** - No Python audio bridge needed
4. ✅ **Flexible** - Easy to swap voice providers
5. ✅ **Cost-effective** - No LiveKit cloud costs

## Troubleshooting

### Microphone Not Working
- Ensure you're accessing via HTTPS or `localhost`
- Check browser permissions for microphone access
- Try a different browser (Chrome/Edge recommended)

### OpenAI Connection Fails
- Verify your API key is correct
- Check console for WebSocket errors
- Ensure you have internet connectivity

### No Video/Audio
- Ensure LiveTalking server is running on port 8010
- Check browser console for WebRTC errors
- Verify backend is proxying `/livetalking` correctly

### Audio Quality Issues
- Check your microphone settings
- Ensure good internet connection to OpenAI
- Try adjusting OpenAI VAD settings in `LocalVoiceAgentBridge.tsx`

## Development

### Testing Locally

```bash
# Terminal 1: Start LiveTalking
cd avatar_agent
./start_livetalking.bat

# Terminal 2: Start backend
cd avatar-frontend
node avatar-backend.cjs

# Terminal 3: Dev mode (optional, for hot reload)
cd avatar-frontend
npm run dev
```

### Production Build

```bash
cd avatar-frontend
npm run build
node avatar-backend.cjs
```

## Configuration Options

### OpenAI Settings

Edit `LocalVoiceAgentBridge.tsx` to customize:

```typescript
session: {
  voice: 'alloy',  // Options: alloy, echo, fable, onyx, nova, shimmer
  turn_detection: {
    threshold: 0.5,           // Voice detection sensitivity
    silence_duration_ms: 500, // How long to wait before ending turn
  }
}
```

### LiveTalking Settings

Edit `avatar_agent/start_livetalking.bat`:

```batch
--batch_size 16    # Audio processing batch size
--W 1280 --H 720   # Video resolution
```

## Next Steps

- [ ] Move API key to environment variable
- [ ] Add error handling UI
- [ ] Implement connection retry logic
- [ ] Add audio level indicators
- [ ] Support multiple voice options in UI

