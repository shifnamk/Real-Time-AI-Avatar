# Stuttering Fix - Audio Queue Overflow Prevention

## Problem
The avatar video was stuttering and the LiveTalking server logs showed frequent `[AUDIO QUEUE] Full - dropped oldest frame to maintain sync` warnings.

## Root Cause
The `isAISpeaking` flag was set to `true` when OpenAI **started generating** the response (`response.created` event), but AI audio sending to LiveTalking happened later in the `response.done` event. This created a timing issue:

**Before Fix:**
1. `response.created` → `isAISpeaking = true` → Keepalive stops
2. OpenAI generates response (1-2 seconds)
3. `response.done` → Start sending AI audio (3-5 seconds with throttling)
4. **Problem**: During step 3, `isAISpeaking` was set back to `false` at the END of the async send loop
5. **Result**: Keepalive resumed WHILE AI audio was still being sent → Double send rate → Queue overflow

## Solution
Changed the timing of the `isAISpeaking` flag:

**After Fix:**
1. `response.created` → Keep keepalive running (AI is generating, but we're not sending yet)
2. OpenAI generates response (1-2 seconds) - Keepalive continues sending silence
3. `response.done` → **NOW** set `isAISpeaking = true` → Pause keepalive
4. Send AI audio blocks (3-5 seconds with throttling) - Keepalive paused
5. After all audio sent → `isAISpeaking = false` → Resume keepalive
6. **Result**: No overlap between keepalive and AI audio sending → Smooth video

## Code Changes

### File: `avatar-frontend/src/LocalVoiceAgentBridge.tsx`

**Change 1: Remove early `isAISpeaking` flag (line ~271)**
```typescript
} else if (msgType === 'response.created') {
  console.log('[🤖 AI responding...]');
  audioChunks.length = 0; // Clear previous chunks
  // REMOVED: isAISpeaking = true;
  // Note: Keep keepalive running while AI generates response
```

**Change 2: Set `isAISpeaking` when actually sending audio (line ~292)**
```typescript
} else if (msgType === 'response.done') {
  console.log('\n[✓ AI response complete]');

  if (audioChunks.length > 0 && liveTalkingSessionId) {
    const fullAudio = concatenateInt16Arrays(audioChunks);

    (async () => {
      // NOW pause keepalive - we're about to send AI audio
      isAISpeaking = true;
      console.log('🎵 Starting AI audio send, pausing keepalive...');
      
      // ... send audio blocks with throttling ...
      
      // AI finished speaking AND all audio sent, resume keepalive
      isAISpeaking = false;
      console.log('✅ AI audio fully sent, resuming keepalive');
    })();
  }
}
```

## Expected Results

After this fix, you should see:
- ✅ **No** `[AUDIO QUEUE] Full` warnings in LiveTalking logs
- ✅ Smooth video output at **20-25 FPS** consistently
- ✅ No stuttering or freezing during AI speech
- ✅ Console logs showing clear separation:
  - `🎵 Starting AI audio send, pausing keepalive...`
  - `✅ AI audio fully sent, resuming keepalive`

## Testing

1. Start LiveTalking server: `./start_livetalking.bat`
2. Start backend: `node avatar-backend.cjs`
3. Open http://localhost:5000
4. Start session and speak
5. Watch LiveTalking logs - should see NO queue overflow warnings
6. Video should remain smooth throughout AI response

## Date
November 26, 2024

