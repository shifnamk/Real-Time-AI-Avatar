# Quick Start: Local Voice Agent + Avatar

## 🚀 Get Started in 3 Steps

### Step 1: Configure OpenAI API Key

Edit `avatar-frontend/src/App.tsx` (line 6):

```typescript
const OPENAI_API_KEY = ''; // Add your OpenAI API key here
```

### Step 2: Start the Servers

**Terminal 1 - LiveTalking Server:**
```bash
cd avatar_agent
./start_livetalking.bat
```

Wait for: `✓ start http server; http://<serverip>:8010/webrtcapi.html`

**Terminal 2 - Frontend Backend:**
```bash
cd avatar-frontend
node avatar-backend.cjs
```

Wait for: `🎭 Avatar backend running on http://localhost:5000`

### Step 3: Open in Browser

Navigate to: **http://localhost:5000**

Click **"Start Session"** and allow microphone access.

---

## ✅ What to Expect

1. **Video loads** - You'll see the avatar video player
2. **Microphone prompt** - Browser asks for mic permission
3. **Status shows "Connected"** - Voice agent is ready
4. **Speak naturally** - The avatar will respond with lip-sync

---

## 🎯 How It Works

```
Your Voice → OpenAI Realtime API → AI Response → LiveTalking → Avatar Video
```

- **Your voice** is captured in the browser
- **OpenAI processes** it in real-time
- **AI audio** is sent to LiveTalking for lip-sync
- **Avatar video** streams back with synchronized lips

---

## 🔧 Troubleshooting

### "Microphone access denied"
- Ensure you're on `localhost` or HTTPS
- Check browser permissions
- Try Chrome/Edge (best compatibility)

### "Connection error"
- Verify OpenAI API key is correct
- Check internet connection
- Look at browser console for errors

### "No video appears"
- Ensure LiveTalking server is running (port 8010)
- Check avatar_agent terminal for errors
- Verify WebRTC connection in browser console

### "Audio is choppy"
- Check your internet speed
- Ensure LiveTalking server isn't overloaded
- Try reducing video quality in `start_livetalking.bat`

---

## 📋 System Requirements

- **OS**: Windows 10/11
- **Browser**: Chrome, Edge, or Firefox (latest)
- **GPU**: NVIDIA GPU (for MuseTalk)
- **RAM**: 8GB minimum, 16GB recommended
- **Internet**: Stable connection for OpenAI API

---

## 🎨 Customization

### Change AI Voice

Edit `avatar-frontend/src/LocalVoiceAgentBridge.tsx` (line 131):

```typescript
voice: 'alloy',  // Options: alloy, echo, fable, onyx, nova, shimmer
```

### Change AI Instructions

Edit `avatar-frontend/src/LocalVoiceAgentBridge.tsx` (line 128):

```typescript
instructions: 'Your custom instructions here...',
```

### Change Video Resolution

Edit `avatar_agent/start_livetalking.bat` (line 9):

```batch
--W 1920 --H 1080  # For 1080p (requires more GPU)
```

---

## 📚 More Information

- **Full Setup Guide**: `avatar-frontend/LOCAL_VOICE_AGENT_SETUP.md`
- **Implementation Details**: `avatar-frontend/IMPLEMENTATION_SUMMARY.md`
- **Main README**: `README.md`

---

## 🆘 Need Help?

1. Check the browser console (F12) for errors
2. Check LiveTalking terminal for Python errors
3. Review the full documentation in `LOCAL_VOICE_AGENT_SETUP.md`

---

**Status**: ✅ Ready to Use
**Last Updated**: November 26, 2025

