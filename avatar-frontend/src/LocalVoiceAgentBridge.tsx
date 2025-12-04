/**
 * Local Voice Agent Bridge Component
 * Connects to local OpenAI Realtime API and forwards AI audio to LiveTalking
 * 
 * This replaces the LiveKit voice agent with a local WebSocket-based agent
 * while keeping the LiveTalking avatar pipeline unchanged.
 */

import { useEffect, useRef, useState } from 'react';

interface LocalVoiceAgentBridgeProps {
  liveTalkingUrl: string;
  liveTalkingSessionId: number | null;
  enabled: boolean;
  openaiApiKey: string;
  onConnectionStateChange?: (state: 'connecting' | 'connected' | 'disconnected' | 'error') => void;
  dataChannel?: RTCDataChannel | null;
}

const OPENAI_WS_URL = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01';
const SAMPLE_RATE = 24000; // OpenAI Realtime uses 24kHz
const SAMPLE_RATE_OUTPUT = 48000; // LiveTalking expects 48kHz
const BLOCK_SAMPLES = 1920; // 40ms @ 48kHz for LiveTalking

// WebRTC version - fast, low-latency transport
async function sendAudioBlockWebRTC(
  samples: Int16Array,
  dataChannel: RTCDataChannel,
): Promise<void> {
  if (dataChannel.readyState !== 'open') {
    throw new Error('Data channel not open');
  }
  
  // Create WAV file (same format as HTTP, just different transport)
  const wavBuffer = createWavFile(samples, SAMPLE_RATE_OUTPUT, 1);
  
  // Send via WebRTC data channel (UDP, low latency!)
  dataChannel.send(wavBuffer);
}

// HTTP version - fallback for compatibility
async function sendAudioBlock(
  samples: Int16Array,
  sessionId: number,
  liveTalkingUrl: string,
): Promise<void> {
  // Build WAV file
  const wavBuffer = createWavFile(samples, SAMPLE_RATE_OUTPUT, 1);
  const formData = new FormData();
  formData.append('sessionid', sessionId.toString());
  formData.append(
    'file',
    new Blob([wavBuffer], { type: 'audio/wav' }),
    'agent_audio.wav',
  );

  const response = await fetch(`${liveTalkingUrl}/humanaudio`, {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`LiveTalking /humanaudio failed: ${response.status} - ${text}`);
  }
}

function createWavFile(samples: Int16Array, sampleRate: number, numChannels: number): ArrayBuffer {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);

  writeString(view, 0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeString(view, 8, 'WAVE');
  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * numChannels * 2, true);
  view.setUint16(32, numChannels * 2, true);
  view.setUint16(34, 16, true);
  writeString(view, 36, 'data');
  view.setUint32(40, samples.length * 2, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i++, offset += 2) {
    view.setInt16(offset, samples[i], true);
  }

  return buffer;
}

function writeString(view: DataView, offset: number, string: string) {
  for (let i = 0; i < string.length; i++) {
    view.setUint8(offset + i, string.charCodeAt(i));
  }
}

export function LocalVoiceAgentBridge({
  liveTalkingUrl,
  liveTalkingSessionId,
  enabled,
  openaiApiKey,
  onConnectionStateChange,
  dataChannel,
}: LocalVoiceAgentBridgeProps) {
  const wsRef = useRef<WebSocket | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const isActiveRef = useRef(false);
  const [status, setStatus] = useState<string>('Idle');

  useEffect(() => {
    if (!enabled || !liveTalkingSessionId || !openaiApiKey) {
      console.log('🔇 Local voice agent disabled or missing config');
      return;
    }

    let mounted = true;
    let ws: WebSocket | null = null;
    let audioContext: AudioContext | null = null;
    let mediaStream: MediaStream | null = null;
    let workletNode: AudioWorkletNode | null = null;

    const audioChunks: Int16Array[] = [];
    let chunksSent = 0;
    let isAISpeaking = false;
    let aiIsGenerating = false; // Track if OpenAI is generating a response
    let keepaliveInterval: NodeJS.Timeout | null = null;
    let lastInterruptTime = 0; // Cooldown to prevent rapid interruptions
    
    // Interruption detection
    const SPEECH_ENERGY_THRESHOLD = 0.02; // RMS threshold for user speech detection
    const INTERRUPT_COOLDOWN_MS = 500; // 0.5 seconds between interruptions

    // Send silence audio to keep LiveTalking generating frames when AI isn't speaking
    // The LiveTalking server will detect silence and show closed lips automatically
    async function sendSilenceKeepalive() {
      if (!liveTalkingSessionId || isAISpeaking || !mounted) return;
      
      try {
        // Send 40ms of silence (1920 samples @ 48kHz)
        const silenceBlock = new Int16Array(BLOCK_SAMPLES).fill(0);
        
        // Use WebRTC if available (fast!), otherwise HTTP (fallback)
        if (dataChannel && dataChannel.readyState === 'open') {
          await sendAudioBlockWebRTC(silenceBlock, dataChannel);
        } else {
          await sendAudioBlock(silenceBlock, liveTalkingSessionId, liveTalkingUrl);
        }
      } catch (err) {
        // Ignore errors during keepalive
      }
    }

    // Detect user speech for interruption
    function detectUserSpeech(audioData: Int16Array): boolean {
      // Calculate RMS energy
      let sum = 0;
      for (let i = 0; i < audioData.length; i++) {
        const normalized = audioData[i] / 32768;
        sum += normalized * normalized;
      }
      const rms = Math.sqrt(sum / audioData.length);
      
      // User is speaking if energy exceeds threshold
      return rms > SPEECH_ENERGY_THRESHOLD;
    }

    // Handle interruption when user speaks during AI response
    async function handleInterruption() {
      const now = Date.now();
      
      // Cooldown: don't interrupt within 0.5 seconds of last interruption
      if (now - lastInterruptTime < INTERRUPT_COOLDOWN_MS) {
        return;
      }
      
      lastInterruptTime = now;
      console.log('\n⚡⚡⚡ INTERRUPTION DETECTED! User speaking during AI response');
      
      try {
        // 1. Cancel OpenAI response generation
        if (ws && ws.readyState === WebSocket.OPEN && aiIsGenerating) {
          ws.send(JSON.stringify({
            type: 'response.cancel'
          }));
          console.log('📤 Cancelled OpenAI response generation');
          aiIsGenerating = false;
        }
        
        // 2. Stop avatar talking by calling LiveTalking interrupt endpoint
        if (liveTalkingSessionId) {
          try {
            const response = await fetch(`${liveTalkingUrl}/interrupt_talk`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ sessionid: liveTalkingSessionId })
            });
            
            if (response.ok) {
              console.log('🛑 Stopped avatar talking (cleared audio queue)');
            } else {
              console.warn('⚠️ Interrupt endpoint returned:', response.status);
            }
          } catch (err) {
            console.error('❌ Failed to call interrupt endpoint:', err);
          }
        }
        
        // 3. Clear local audio queue
        audioChunks.length = 0;
        isAISpeaking = false; // Resume keepalive
        
        console.log('✅ Interruption handled, ready for new input');
        
      } catch (err) {
        console.error('❌ Interruption handling failed:', err);
      }
    }

    async function connectToOpenAI() {
      try {
        setStatus('Connecting to OpenAI...');
        onConnectionStateChange?.('connecting');

        // Connect to OpenAI Realtime API
        ws = new WebSocket(OPENAI_WS_URL, [
          'realtime',
          `openai-insecure-api-key.${openaiApiKey}`,
          'openai-beta.realtime-v1',
        ]);

        wsRef.current = ws;

        ws.onopen = async () => {
          if (!mounted) return;
          console.log('✅ Connected to OpenAI Realtime API');
          setStatus('Configuring session...');

          // Configure session
          ws?.send(
            JSON.stringify({
              type: 'session.update',
              session: {
                modalities: ['text', 'audio'],
                instructions:
                  'You are a helpful voice assistant for 3 Fils Restaurants. Be conversational and brief.',
                voice: 'alloy',
                input_audio_format: 'pcm16',
                output_audio_format: 'pcm16',
                input_audio_transcription: {
                  model: 'whisper-1',
                },
                turn_detection: {
                  type: 'server_vad',
                  threshold: 0.5,
                  prefix_padding_ms: 200,    // Reduced from 300ms for faster response
                  silence_duration_ms: 300,  // Reduced from 500ms for faster response
                },
              },
            })
          );

          // Start capturing microphone
          try {
            mediaStream = await navigator.mediaDevices.getUserMedia({
              audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
                sampleRate: SAMPLE_RATE,
              },
            });

            mediaStreamRef.current = mediaStream;

            audioContext = new AudioContext({ sampleRate: SAMPLE_RATE });
            audioContextRef.current = audioContext;

            const source = audioContext.createMediaStreamSource(mediaStream);

            // Use AudioWorkletNode for better performance
            await audioContext.audioWorklet.addModule(
              URL.createObjectURL(
                new Blob(
                  [
                    `
class AudioCaptureProcessor extends AudioWorkletProcessor {
  process(inputs, outputs, parameters) {
    const input = inputs[0];
    if (input.length > 0) {
      const channelData = input[0];
      // Convert float32 to int16
      const int16Data = new Int16Array(channelData.length);
      for (let i = 0; i < channelData.length; i++) {
        const s = Math.max(-1, Math.min(1, channelData[i]));
        int16Data[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(int16Data);
    }
    return true;
  }
}
registerProcessor('audio-capture-processor', AudioCaptureProcessor);
`,
                  ],
                  { type: 'application/javascript' }
                )
              )
            );

            workletNode = new AudioWorkletNode(audioContext, 'audio-capture-processor');

            workletNode.port.onmessage = (event) => {
              if (!isActiveRef.current || !ws || ws.readyState !== WebSocket.OPEN) return;

              const int16Data: Int16Array = event.data;
              
              // Detect user speech for interruption
              if ((isAISpeaking || aiIsGenerating) && detectUserSpeech(int16Data)) {
                handleInterruption();
              }
              
              const base64Audio = btoa(
                String.fromCharCode.apply(null, Array.from(new Uint8Array(int16Data.buffer)))
              );

              ws.send(
                JSON.stringify({
                  type: 'input_audio_buffer.append',
                  audio: base64Audio,
                })
              );
            };

            source.connect(workletNode);
            workletNode.connect(audioContext.destination);

            isActiveRef.current = true;
            setStatus('Connected - Listening...');
            onConnectionStateChange?.('connected');
            console.log('🎤 Microphone active - speak to the avatar!');
            console.log('⏳ Waiting 2 seconds before starting keepalive (letting LiveTalking stabilize)...');

            // Delay keepalive start to prevent startup audio queue overflow
            // This gives LiveTalking time to stabilize before we start sending silence
            setTimeout(() => {
              if (mounted && isActiveRef.current) {
                keepaliveInterval = setInterval(sendSilenceKeepalive, 40); // Every 40ms
                console.log('✅ Keepalive started after stabilization');
              }
            }, 2000); // Wait 2 seconds before starting keepalive
          } catch (err) {
            console.error('❌ Microphone access denied:', err);
            setStatus('Microphone access denied');
            onConnectionStateChange?.('error');
          }
        };

        ws.onmessage = async (event) => {
          if (!mounted) return;

          const data = JSON.parse(event.data);
          const msgType = data.type;

          if (msgType === 'session.created') {
            console.log('✓ Session created');
          } else if (msgType === 'session.updated') {
            console.log('✓ Session configured');
          } else if (msgType === 'input_audio_buffer.speech_started') {
            console.log('[🎤 User speaking...]');
          } else if (msgType === 'input_audio_buffer.speech_stopped') {
            console.log('[User stopped speaking]');
          } else if (msgType === 'response.created') {
            console.log('[🤖 AI responding...]');
            audioChunks.length = 0; // Clear previous chunks
            aiIsGenerating = true; // Mark AI as generating (for interruption detection)
            // Note: Keep keepalive running while AI generates response
            // We'll pause it when we actually start sending audio to LiveTalking
          } else if (msgType === 'response.audio.delta') {
            // Receive AI audio chunk
            const deltaBase64 = data.delta;
            const audioBytes = Uint8Array.from(atob(deltaBase64), (c) => c.charCodeAt(0));
            const int16Audio = new Int16Array(audioBytes.buffer);

            // Resample from 24kHz to 48kHz for LiveTalking
            const resampled = resample24to48(int16Audio);
            audioChunks.push(resampled);
          } else if (msgType === 'response.audio_transcript.delta') {
            // Log transcript
            console.log(data.delta, { end: '' });
          } else if (msgType === 'response.done') {
            console.log('\n[✓ AI response complete]');
            aiIsGenerating = false; // AI finished generating

            // Send all accumulated audio to LiveTalking at real-time speed
            if (audioChunks.length > 0 && liveTalkingSessionId) {
              const fullAudio = concatenateInt16Arrays(audioChunks);

              // Send in 40ms blocks with throttling to prevent queue overflow
              (async () => {
                // NOW pause keepalive - we're about to send AI audio
                isAISpeaking = true;
                console.log('🎵 Starting AI audio send, pausing keepalive...');
                
                let offset = 0;
                const startTime = Date.now();
                let blockIndex = 0;
                
                while (offset < fullAudio.length && mounted && isAISpeaking) {
                  const block = fullAudio.slice(offset, offset + BLOCK_SAMPLES);
                  if (block.length > 0) {
                    try {
                      // Use WebRTC if available (fast!), otherwise HTTP (fallback)
                      if (dataChannel && dataChannel.readyState === 'open') {
                        await sendAudioBlockWebRTC(block, dataChannel);
                      } else {
                        await sendAudioBlock(block, liveTalkingSessionId, liveTalkingUrl);
                      }
                      
                      chunksSent++;
                      blockIndex++;
                      
                      if (chunksSent % 25 === 0) {
                        const transport = dataChannel?.readyState === 'open' ? 'WebRTC' : 'HTTP';
                        console.log(
                          `🎵 [${transport}] Sent ${chunksSent} blocks (~${(chunksSent * 0.04).toFixed(1)}s)`
                        );
                      }
                      
                      // Throttle: wait to maintain real-time speed (40ms per block)
                      // This prevents overwhelming the LiveTalking audio queue
                      const expectedTime = blockIndex * 40; // 40ms per block
                      const actualTime = Date.now() - startTime;
                      const delay = expectedTime - actualTime;
                      
                      if (delay > 0) {
                        await new Promise(resolve => setTimeout(resolve, delay));
                      }
                    } catch (err) {
                      console.error('❌ Failed to send audio block:', err);
                      break; // Stop sending on error
                    }
                  }
                  offset += BLOCK_SAMPLES;
                }
                
                // Check if interrupted
                if (!isAISpeaking) {
                  console.log('⚡ AI audio send interrupted by user');
                } else {
                  // AI finished speaking AND all audio sent, resume keepalive
                  isAISpeaking = false;
                  console.log('✅ AI audio fully sent, resuming keepalive');
                }
              })();

              audioChunks.length = 0; // Clear chunks
            } else {
              // No audio to send, keepalive continues normally
              console.log('ℹ️ No AI audio to send');
            }
          } else if (msgType === 'error') {
            const errorInfo = data.error || {};
            const errorMsg = errorInfo.message || JSON.stringify(data);
            const errorCode = errorInfo.code || 'unknown';
            
            // Ignore harmless errors
            if (errorCode === 'response_cancel_not_active') {
              // Response finished before cancel arrived - totally fine
              console.log('ℹ️ Response already completed (cancel not needed)');
              return;
            }
            
            console.error(`❌ OpenAI error [${errorCode}]: ${errorMsg}`);
          }
        };

        ws.onerror = (error) => {
          console.error('❌ WebSocket error:', error);
          setStatus('Connection error');
          onConnectionStateChange?.('error');
        };

        ws.onclose = () => {
          console.log('⚠️ WebSocket closed');
          setStatus('Disconnected');
          onConnectionStateChange?.('disconnected');
        };
      } catch (err) {
        console.error('❌ Failed to connect to OpenAI:', err);
        setStatus('Connection failed');
        onConnectionStateChange?.('error');
      }
    }

    connectToOpenAI();

    return () => {
      mounted = false;
      isActiveRef.current = false;

      // Stop keepalive
      if (keepaliveInterval) {
        clearInterval(keepaliveInterval);
        keepaliveInterval = null;
      }

      if (workletNode) {
        workletNode.disconnect();
      }

      if (mediaStream) {
        mediaStream.getTracks().forEach((track) => track.stop());
      }

      if (audioContext) {
        audioContext.close();
      }

      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.close();
      }

      console.log('🔇 Local voice agent disconnected');
    };
  }, [enabled, liveTalkingSessionId, openaiApiKey, liveTalkingUrl, onConnectionStateChange, dataChannel]);

  return (
    <div style={{ padding: '10px', fontSize: '12px', color: '#666' }}>
      Status: {status}
    </div>
  );
}

// Helper: Resample 24kHz to 48kHz (simple linear interpolation)
function resample24to48(input: Int16Array): Int16Array {
  const output = new Int16Array(input.length * 2);
  for (let i = 0; i < input.length - 1; i++) {
    output[i * 2] = input[i];
    output[i * 2 + 1] = Math.round((input[i] + input[i + 1]) / 2);
  }
  output[output.length - 2] = input[input.length - 1];
  output[output.length - 1] = input[input.length - 1];
  return output;
}

// Helper: Concatenate Int16Arrays
function concatenateInt16Arrays(arrays: Int16Array[]): Int16Array {
  const totalLength = arrays.reduce((sum, arr) => sum + arr.length, 0);
  const result = new Int16Array(totalLength);
  let offset = 0;
  for (const arr of arrays) {
    result.set(arr, offset);
    offset += arr.length;
  }
  return result;
}

