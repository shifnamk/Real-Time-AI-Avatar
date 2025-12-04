// Quick script to decode a LiveKit JWT token to extract the API key
const token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJuYW1lIjoidXNlci03MjQ1YjciLCJ2aWRlbyI6eyJyb29tSm9pbiI6dHJ1ZSwicm9vbSI6InBsYXktYTA5NGY5OTUiLCJjYW5QdWJsaXNoIjp0cnVlLCJjYW5TdWJzY3JpYmUiOnRydWUsImNhblB1Ymxpc2hEYXRhIjp0cnVlfSwic3ViIjoidXNlci03MjQ1YjciLCJpc3MiOiJBUEliOW43aThvTVBiU20iLCJuYmYiOjE3NjI3NzYxNTgsImV4cCI6MTc2Mjc5Nzc1OH0.ch_Drz9Lr8_-3OAzSgpCvMz1zlKv9vwRv72C6HRZLNo";

// Decode JWT (base64)
const parts = token.split('.');
const payload = JSON.parse(Buffer.from(parts[1], 'base64').toString());

console.log('Decoded token payload:');
console.log(JSON.stringify(payload, null, 2));
console.log('\nLiveKit API Key (iss):', payload.iss);
console.log('\n⚠️  Note: The API SECRET cannot be extracted from the token.');
console.log('    You MUST ask the voice agent team for LIVEKIT_API_SECRET');

