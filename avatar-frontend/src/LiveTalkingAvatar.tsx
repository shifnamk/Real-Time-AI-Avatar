/**
 * LiveTalking Avatar Component
 * Connects directly to LiveTalking's WebRTC stream for realtime avatar
 */

import { useEffect, useRef, useState } from 'react';

interface LiveTalkingAvatarProps {
  liveTalkingUrl: string;
  onSessionId?: (sessionId: number) => void;
  onDataChannel?: (channel: RTCDataChannel | null) => void;
}

export function LiveTalkingAvatar({ liveTalkingUrl, onSessionId, onDataChannel }: LiveTalkingAvatarProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [status, setStatus] = useState<string>('Connecting...');
  const pcRef = useRef<RTCPeerConnection | null>(null);

  useEffect(() => {
    let mounted = true;
    let pc: RTCPeerConnection | null = null;

    async function connectToLiveTalking() {
      try {
        setStatus('Creating WebRTC connection...');
        
        // Create RTCPeerConnection
        pc = new RTCPeerConnection({
          iceServers: [
            { urls: 'stun:stun.l.google.com:19302' },
            { urls: 'stun:stun.miwifi.com:3478' }
          ]
        });
        
        pcRef.current = pc;

        // Create data channel for sending audio to LiveTalking
        const dataChannel = pc.createDataChannel('audio', {
          ordered: false,      // Allow out-of-order delivery for lower latency
          maxRetransmits: 0,   // Don't retransmit lost packets (real-time audio)
        });

        dataChannel.onopen = () => {
          console.log('✅ Audio data channel opened (WebRTC transport ready)');
          if (onDataChannel) {
            onDataChannel(dataChannel);
          }
        };

        dataChannel.onclose = () => {
          console.log('❌ Audio data channel closed');
          if (onDataChannel) {
            onDataChannel(null);
          }
        };

        dataChannel.onerror = (error) => {
          console.error('❌ Data channel error:', error);
        };

        // Handle incoming tracks
        let videoTrackReceivedAt = 0;
        let audioTrackReceivedAt = 0;
        
        pc.ontrack = (event) => {
          const timestamp = performance.now();
          console.log('📹 Received track from LiveTalking:', event.track.kind, `at ${timestamp.toFixed(2)}ms`);
          
          if (event.track.kind === 'video' && videoRef.current) {
            videoTrackReceivedAt = timestamp;
            console.log('✅ Attaching video track to element');
            const video = videoRef.current;
            video.srcObject = event.streams[0];
            video.muted = false;  // Play audio through video element for best sync
            
            // Force low-latency playback for tight A/V sync
            (video as any).preservesPitch = false;
            video.playsInline = true;
            
            // Try to play immediately
            video.play().catch(err => console.warn('Autoplay blocked:', err));
            
            // Log A/V sync timing
            if (audioTrackReceivedAt > 0) {
              const diff = videoTrackReceivedAt - audioTrackReceivedAt;
              console.log(`[CLIENT A/V SYNC] Video received ${diff.toFixed(2)}ms ${diff > 0 ? 'AFTER' : 'BEFORE'} audio`);
            }
            
            // Add detailed playback monitoring
            let frameCount = 0;
            video.addEventListener('timeupdate', () => {
              frameCount++;
              if (frameCount % 50 === 0) {
                const videoTime = video.currentTime;
                const buffered = video.buffered.length > 0 ? video.buffered.end(0) - videoTime : 0;
                console.log(`[CLIENT PLAYBACK] Frame ${frameCount}: time=${videoTime.toFixed(2)}s, buffer=${(buffered*1000).toFixed(0)}ms`);
              }
            });
            
            setStatus('Connected');
          }
          
          // Audio track is already included in the video stream
          // Playing it separately would cause echo and desync
          if (event.track.kind === 'audio') {
            audioTrackReceivedAt = timestamp;
            console.log('🔊 Audio track received (will play through video element for perfect sync)');
            event.track.enabled = true;
            
            // Log A/V sync timing
            if (videoTrackReceivedAt > 0) {
              const diff = audioTrackReceivedAt - videoTrackReceivedAt;
              console.log(`[CLIENT A/V SYNC] Audio received ${diff.toFixed(2)}ms ${diff > 0 ? 'AFTER' : 'BEFORE'} video`);
            }
          }
        };

        pc.oniceconnectionstatechange = () => {
          console.log('🔌 ICE connection state:', pc?.iceConnectionState);
          if (pc?.iceConnectionState === 'connected') {
            setStatus('Connected');
          } else if (pc?.iceConnectionState === 'disconnected' || pc?.iceConnectionState === 'failed') {
            setStatus('Disconnected');
          }
        };

        // Create offer
        setStatus('Creating offer...');
        const offer = await pc.createOffer({
          offerToReceiveAudio: true,
          offerToReceiveVideo: true
        });
        
        await pc.setLocalDescription(offer);

        // Send offer to LiveTalking
        setStatus('Sending offer to LiveTalking...');
        const response = await fetch(`${liveTalkingUrl}/offer`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sdp: pc.localDescription?.sdp,
            type: pc.localDescription?.type
          })
        });

        if (!response.ok) {
          throw new Error(`LiveTalking /offer failed: ${response.status}`);
        }

        const answer = await response.json();
        console.log('✅ Received answer from LiveTalking, sessionid:', answer.sessionid);
        
        if (!mounted) return;

        // Report session ID to parent
        if (onSessionId) {
          onSessionId(answer.sessionid);
        }

        // Set remote description
        await pc.setRemoteDescription(new RTCSessionDescription({
          sdp: answer.sdp,
          type: answer.type
        }));

        setStatus('Waiting for video...');
        console.log('✅ WebRTC connection established');

      } catch (error) {
        console.error('❌ Failed to connect to LiveTalking:', error);
        setStatus(`Error: ${error instanceof Error ? error.message : 'Unknown error'}`);
      }
    }

    connectToLiveTalking();

    return () => {
      mounted = false;
      if (pc) {
        console.log('🔌 Closing LiveTalking WebRTC connection');
        pc.close();
      }
    };
  }, [liveTalkingUrl, onSessionId]);

  return (
    <div className="avatar-container">
      <video
        ref={videoRef}
        className="avatar-video"
        autoPlay
        playsInline
        style={{ display: status === 'Connected' ? 'block' : 'none' }}
        preload="none"
      />
      {status !== 'Connected' && (
        <div className="avatar-placeholder">
          <div className="pulse-loader">
            <div className="pulse-ring"></div>
            <div className="pulse-ring"></div>
            <div className="pulse-icon">
              <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
              </svg>
            </div>
          </div>
          <p style={{ fontSize: '1.25rem', fontWeight: 500 }} className="animate-pulse">
            {status}
          </p>
        </div>
      )}
    </div>
  );
}

