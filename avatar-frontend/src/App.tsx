import { useState } from 'react';
import { LiveTalkingAvatar } from './LiveTalkingAvatar';
import { LocalVoiceAgentBridge } from './LocalVoiceAgentBridge';
import './App.css';

// Configuration - Local OpenAI Realtime API
const OPENAI_API_KEY = ''; // Add your OpenAI API key here

function App() {
  const [isSessionActive, setIsSessionActive] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [liveTalkingSessionId, setLiveTalkingSessionId] = useState<number | null>(null);
  const [sessionKey, setSessionKey] = useState<number>(0);
  const [voiceAgentState, setVoiceAgentState] = useState<'connecting' | 'connected' | 'disconnected' | 'error'>('disconnected');
  const [audioDataChannel, setAudioDataChannel] = useState<RTCDataChannel | null>(null);

  const startSession = async () => {
    setIsConnecting(true);
    setError(null);

    try {
      console.log('🚀 Starting local voice agent session...');
      
      // No external API calls needed - just activate the session
      setIsSessionActive(true);
      
      // Increment session key to force LiveTalkingAvatar to remount with new connection
      setSessionKey(prev => prev + 1);

      console.log('✅ Session started - waiting for LiveTalking connection...');

    } catch (err: any) {
      console.error('❌ Failed to start session:', err);
      setError(err.message || 'Failed to start session');
    } finally {
      setIsConnecting(false);
    }
  };

  const endSession = async () => {
    console.log('🛑 endSession called');
    
    setIsSessionActive(false);
    setLiveTalkingSessionId(null);
    setError(null);
    setVoiceAgentState('disconnected');
  };

  return (
    <div className="app-container">
      <main className="main-content">
        {!isSessionActive ? (
          <div className="start-card">
            <h2 className="title">Ready to Start?</h2>
            <p className="subtitle">
              Begin your conversation with the AI avatar (Local Voice Agent)
            </p>

            {error && (
              <div className="error-box">
                <span style={{ fontSize: '1.5rem' }}>⚠️</span>
                <span style={{ flex: 1, textAlign: 'left' }}>{error}</span>
              </div>
            )}

            <button
              onClick={startSession}
              disabled={isConnecting}
              className="btn-primary"
            >
              {isConnecting ? (
                <>
                  <svg className="animate-spin" style={{ width: '1.5rem', height: '1.5rem' }} fill="none" viewBox="0 0 24 24">
                    <circle style={{ opacity: 0.25 }} cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                    <path style={{ opacity: 0.75 }} fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  <span>Connecting...</span>
                </>
              ) : (
                <>
                  <span style={{ fontSize: '1.5rem' }}>▶</span>
                  <span>Start Session</span>
                </>
              )}
            </button>
          </div>
        ) : (
          <>
            {/* Local Voice Agent Bridge - replaces LiveKit */}
            <LocalVoiceAgentBridge
              liveTalkingUrl="/livetalking"
              liveTalkingSessionId={liveTalkingSessionId}
              enabled={!!liveTalkingSessionId}
              openaiApiKey={OPENAI_API_KEY}
              onConnectionStateChange={setVoiceAgentState}
              dataChannel={audioDataChannel}
            />
            
            <div className="session-card">
              {/* LiveTalking avatar - realtime video with emotion switching */}
              <LiveTalkingAvatar 
                key={sessionKey}
                liveTalkingUrl="/livetalking"
                onSessionId={setLiveTalkingSessionId}
                onDataChannel={setAudioDataChannel}
              />

              <div className="session-info">
                <div className="status-indicator">
                  <div className="status-dot-container">
                    <span className="status-dot-ping"></span>
                    <span className="status-dot"></span>
                  </div>
                  <span>
                    {voiceAgentState === 'connected' ? 'Voice Agent Connected' : 
                     voiceAgentState === 'connecting' ? 'Connecting...' : 
                     voiceAgentState === 'error' ? 'Connection Error' : 'Live Session'}
                  </span>
                </div>
                <div className="room-info">
                  Local OpenAI Realtime
                  {liveTalkingSessionId && ` | LiveTalking: ${liveTalkingSessionId}`}
                </div>
              </div>

              <div className="controls">
                <button onClick={endSession} className="btn-danger">
                  <span style={{ fontSize: '1.5rem' }}>⏹</span>
                  <span>End Session</span>
                </button>
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  );
}

export default App;
