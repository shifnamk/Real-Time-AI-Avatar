/**
 * Simple Express backend for Local Voice Agent + LiveTalking Avatar
 * Run with: node avatar-backend.cjs
 */

const express = require('express');
const cors = require('cors');
const path = require('path');
const { createProxyMiddleware } = require('http-proxy-middleware');

const app = express();

const LIVETALKING_TARGET = process.env.LIVETALKING_TARGET || 'http://127.0.0.1:8010';
const frontendDistPath = path.join(__dirname, 'dist');
const frontendIndexPath = path.join(frontendDistPath, 'index.html');

// Configure CORS to allow requests from frontend
app.use(cors({
  origin: ['http://localhost:5173', 'http://localhost:5175', 'http://localhost:5000'],
  methods: ['GET', 'POST'],
  credentials: true
}));

// Proxy LiveTalking realtime server through /livetalking before body parsing
app.use('/livetalking', createProxyMiddleware({
  target: LIVETALKING_TARGET,
  changeOrigin: true,
  ws: true,
  pathRewrite: { '^/livetalking': '' },
  logLevel: 'warn'
}));

app.use(express.json());

// Serve built frontend if available
if (require('fs').existsSync(frontendDistPath)) {
  app.use(express.static(frontendDistPath));
}

// Health check endpoint
app.get('/health', (req, res) => {
  res.json({ 
    status: 'running',
    message: 'Avatar Backend Server (Local Voice Agent Mode)',
    mode: 'local_openai_realtime'
  });
});

// Catch-all route to serve index.html for SPA routing
app.use((req, res, next) => {
  // Skip API and proxy routes
  if (req.path.startsWith('/livetalking') || req.path.startsWith('/health')) {
    return next();
  }
  
  // Serve index.html for all other routes
  if (require('fs').existsSync(frontendIndexPath)) {
    res.sendFile(frontendIndexPath);
  } else {
    res.status(404).send('Frontend not built. Run: npm run build');
  }
});

const PORT = process.env.PORT || 5000;
app.listen(PORT, () => {
  console.log('🎭 Avatar backend running on http://localhost:' + PORT);
  console.log('📍 LiveTalking proxy: /livetalking -> ' + LIVETALKING_TARGET);
  console.log('🎤 Mode: Local OpenAI Realtime API (browser-based)');
});
