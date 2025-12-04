# Interruption Feature - Client-Side Voice Activity Detection

## Overview
The interruption feature allows users to interrupt the AI avatar mid-speech, just like in natural human conversation. This is implemented using client-side Voice Activity Detection (VAD) with energy-based speech detection.

## How It Works

### 1. **Speech Detection**
The system continuously monitors microphone input for user speech using RMS (Root Mean Square) energy calculation:

```typescript
// Calculate RMS energy from audio samples
function detectUserSpeech(audioData: Int16Array): boolean {
  let sum = 0;
  for (let i = 0; i < audioData.length; i++) {
    const normalized = audioData[i] / 32768;
    sum += normalized * normalized;
  }
  const rms = Math.sqrt(sum / audioData.length);
  
  // User is speaking if energy exceeds threshold
  return rms > SPEECH_ENERGY_THRESHOLD; // 0.02
}
```

### 2. **State Tracking**
The system tracks two critical states:
- **`aiIsGenerating`**: OpenAI is creating a response (after `response.created`, before `response.done`)
- **`isAISpeaking`**: AI audio is being sent to LiveTalking server

### 3. **Interruption Trigger**
When user speech is detected WHILE the AI is generating or speaking:

```typescript
if ((isAISpeaking || aiIsGenerating) && detectUserSpeech(int16Data)) {
  handleInterruption();
}
```

### 4. **Interruption Actions**
When interruption is triggered, the system:

1. **Cancels OpenAI response generation**
   ```typescript
   ws.send(JSON.stringify({
     type: 'response.cancel'
   }));
   ```

2. **Stops avatar talking** (clears LiveTalking audio queue)
   ```typescript
   fetch(`${liveTalkingUrl}/interrupt_talk`, {
     method: 'POST',
     body: JSON.stringify({ sessionid: liveTalkingSessionId })
   });
   ```

3. **Clears local audio buffer**
   ```typescript
   audioChunks.length = 0;
   isAISpeaking = false; // Resume keepalive
   ```

### 5. **Cooldown Period**
To prevent false triggers and rapid interruptions:
- **500ms cooldown** between interruptions
- Prevents echo or background noise from triggering multiple interruptions

## Configuration

### Energy Threshold
```typescript
const SPEECH_ENERGY_THRESHOLD = 0.02; // RMS threshold
```

**Adjust this value if:**
- **Too sensitive** (interrupts on background noise): Increase to `0.03` or `0.04`
- **Not sensitive enough** (doesn't detect speech): Decrease to `0.015` or `0.01`

### Cooldown Period
```typescript
const INTERRUPT_COOLDOWN_MS = 500; // 0.5 seconds
```

## LiveTalking Server Integration

The LiveTalking server already has the `/interrupt_talk` endpoint:

```python
# livetalking/app.py (line 305)
async def interrupt_talk(request):
    params = await request.json()
    sessionid = params.get('sessionid', 0)
    nerfreals[sessionid].flush_talk()  # Clear audio queue
    return web.Response(...)
```

This endpoint calls `flush_talk()` which clears the audio queue, stopping the avatar mid-speech.

## Comparison with Python Implementation

This browser implementation matches the Python `realtime_livekit_style_base.py` logic:

| Feature | Python Implementation | Browser Implementation |
|---------|----------------------|------------------------|
| **VAD Method** | WebRTC VAD + RMS energy | RMS energy only |
| **Energy Threshold** | 800 (int16 scale) | 0.02 (normalized) |
| **State Tracking** | `ai_is_generating`, `is_ai_speaking` | `aiIsGenerating`, `isAISpeaking` |
| **Interruption Action** | Stop local playback + cancel | Stop LiveTalking + cancel |
| **Cooldown** | 500ms | 500ms |

**Key Difference**: 
- Python plays AI audio locally (can stop with `sd.stop()`)
- Browser sends AI audio to LiveTalking server (must call `/interrupt_talk` to stop)

## Testing the Feature

### Test 1: Basic Interruption
1. Start session and ask: "Tell me a long story about space exploration"
2. While AI is speaking, interrupt by saying "Stop!"
3. **Expected**: Avatar stops mid-sentence, console shows:
   ```
   ⚡⚡⚡ INTERRUPTION DETECTED! User speaking during AI response
   📤 Cancelled OpenAI response generation
   🛑 Stopped avatar talking (cleared audio queue)
   ✅ Interruption handled, ready for new input
   ```

### Test 2: Multiple Interruptions
1. Start session and ask a question
2. Interrupt 3 times in a row
3. **Expected**: Only processes interruptions with 500ms gaps (cooldown working)

### Test 3: False Positive Prevention
1. Play background music or have ambient noise
2. Start session and let AI speak
3. **Expected**: Should NOT interrupt unless clear speech detected

## Troubleshooting

### Issue: Too Many False Interruptions
**Solution**: Increase `SPEECH_ENERGY_THRESHOLD` from `0.02` to `0.03` or `0.04`

### Issue: Interruption Not Detected
**Solution**: 
1. Check microphone is working (speak and watch browser console for audio levels)
2. Decrease `SPEECH_ENERGY_THRESHOLD` to `0.015`
3. Ensure using headphones (prevents AI audio echo from triggering VAD)

### Issue: Avatar Doesn't Stop
**Solution**: 
1. Check LiveTalking server logs for `/interrupt_talk` endpoint calls
2. Verify `nerfreals[sessionid].flush_talk()` is clearing the queue
3. Check network connectivity between frontend and LiveTalking server

## Console Logs

When interruption works correctly, you'll see:

```
[🤖 AI responding...]
🎵 Starting AI audio send, pausing keepalive...
🎵 [LocalVoiceAgent] Sent 25 blocks (~1.0s)

⚡⚡⚡ INTERRUPTION DETECTED! User speaking during AI response
📤 Cancelled OpenAI response generation
🛑 Stopped avatar talking (cleared audio queue)
✅ Interruption handled, ready for new input
⚡ AI audio send interrupted by user
```

## Implementation Date
November 26, 2024

## Related Files
- `avatar-frontend/src/LocalVoiceAgentBridge.tsx` - Main implementation
- `livetalking/app.py` - Server-side `/interrupt_talk` endpoint
- `realtime_livekit_style_base.py` - Python reference implementation

