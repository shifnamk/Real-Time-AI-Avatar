###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  email: lipku@foxmail.com
# 
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  
#       http://www.apache.org/licenses/LICENSE-2.0
# 
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

# server.py
from flask import Flask, render_template,send_from_directory,request, jsonify
from flask_sockets import Sockets
import base64
import json
#import gevent
#from gevent import pywsgi
#from geventwebsocket.handler import WebSocketHandler
import re
import numpy as np
from threading import Thread,Event
#import multiprocessing
import torch.multiprocessing as mp

from aiohttp import web
import aiohttp
import aiohttp_cors
from aiortc import RTCPeerConnection, RTCSessionDescription,RTCIceServer,RTCConfiguration
from aiortc.rtcrtpsender import RTCRtpSender
from webrtc import HumanPlayer
from basereal import BaseReal
from llm import llm_response

import argparse
import random
import shutil
import asyncio
import torch
from typing import Dict
from logger import logger
import gc


app = Flask(__name__)
#sockets = Sockets(app)
nerfreals:Dict[int, BaseReal] = {} #sessionid:BaseReal
opt = None
model = None
avatar = None
        

#####webrtc###############################
pcs = set()
_missing_session_counts = {}
_MISSING_SESSION_LOG_LIMIT = 5


def _log_missing_session(sessionid: int, message: str) -> None:
    count = _missing_session_counts.get(sessionid, 0)
    if count < _MISSING_SESSION_LOG_LIMIT:
        logger.warning(message)
    elif count == _MISSING_SESSION_LOG_LIMIT:
        logger.warning(
            f"{message} (further missing-session logs for {sessionid} suppressed)"
        )
    _missing_session_counts[sessionid] = count + 1


def _reset_missing_session_count(sessionid: int) -> None:
    _missing_session_counts.pop(sessionid, None)

def randN(N)->int:
    '''生成长度为 N的随机数 '''
    min = pow(10, N - 1)
    max = pow(10, N)
    return random.randint(min, max - 1)

def build_nerfreal(sessionid:int)->BaseReal:
    opt.sessionid=sessionid
    if opt.model == 'wav2lip':
        from lipreal import LipReal
        nerfreal = LipReal(opt,model,avatar)
    elif opt.model == 'musetalk':
        from musereal import MuseReal
        nerfreal = MuseReal(opt,model,avatar)
    # elif opt.model == 'ernerf':
    #     from nerfreal import NeRFReal
    #     nerfreal = NeRFReal(opt,model,avatar)
    elif opt.model == 'ultralight':
        from lightreal import LightReal
        nerfreal = LightReal(opt,model,avatar)
    return nerfreal

#@app.route('/offer', methods=['POST'])
async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    # Clean up dead sessions before checking max_session limit
    dead_sessions = [k for k, v in nerfreals.items() if v is None]
    for k in dead_sessions:
        logger.info(f"Cleaning up dead session {k} before accepting new connection")
        del nerfreals[k]
        _reset_missing_session_count(k)
    
    # Count only active (non-None) sessions
    active_session_count = sum(1 for v in nerfreals.values() if v is not None)
    if active_session_count >= opt.max_session:
        logger.info(f'Reached max active sessions ({opt.max_session})')
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": -1, "msg": "reach max session"}
            ),
        )
    
    sessionid = randN(6) #len(nerfreals)
    nerfreals[sessionid] = None
    logger.info('sessionid=%d, session num=%d',sessionid,len(nerfreals))
    nerfreal = await asyncio.get_event_loop().run_in_executor(None, build_nerfreal,sessionid)
    nerfreals[sessionid] = nerfreal
    _reset_missing_session_count(sessionid)
    
    #ice_server = RTCIceServer(urls='stun:stun.l.google.com:19302')
    ice_server = RTCIceServer(urls='stun:stun.miwifi.com:3478')
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[ice_server]))
    pcs.add(pc)

    async def _teardown_session():
        try:
            if sessionid in nerfreals and nerfreals[sessionid] is not None:
                nerfreals[sessionid].stop_stream()
        except Exception as exc:
            logger.warning(f"Failed to stop stream for session {sessionid}: {exc}")
        try:
            await pc.close()
        except Exception:
            pass
        pcs.discard(pc)
        if sessionid in nerfreals:
            del nerfreals[sessionid]
        _reset_missing_session_count(sessionid)
        gc.collect()

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.info("Connection state is %s" % pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await _teardown_session()

    @pc.on("datachannel")
    def on_datachannel(channel):
        logger.info(f"Data channel '{channel.label}' opened for session {sessionid}")
        
        # Track audio chunks for periodic logging
        if not hasattr(on_datachannel, '_chunk_counts'):
            on_datachannel._chunk_counts = {}
        on_datachannel._chunk_counts[sessionid] = 0
        
        @channel.on("message")
        def on_message(message):
            # Receive audio data from browser via WebRTC data channel
            # Message format: binary audio data (WAV format)
            if isinstance(message, bytes):
                try:
                    if sessionid in nerfreals and nerfreals[sessionid] is not None:
                        nerfreals[sessionid].put_audio_file(message)
                        # Log every 1000 chunks (~10 seconds)
                        on_datachannel._chunk_counts[sessionid] += 1
                        if on_datachannel._chunk_counts[sessionid] % 1000 == 0:
                            logger.info(f"[Session {sessionid}] Received {on_datachannel._chunk_counts[sessionid]} audio chunks via WebRTC")
                    else:
                        _log_missing_session(sessionid, f"Session {sessionid} not found for data channel audio")
                except Exception as e:
                    logger.error(f"Error processing data channel audio: {e}")
            else:
                logger.warning(f"Received non-binary message on data channel: {type(message)}")
        
        @channel.on("close")
        def on_close():
            logger.info(f"Data channel '{channel.label}' closed for session {sessionid}")
            asyncio.ensure_future(_teardown_session())

    player = HumanPlayer(nerfreals[sessionid])
    audio_sender = pc.addTrack(player.audio)
    video_sender = pc.addTrack(player.video)
    
    # CRITICAL FIX: Configure audio codec for high quality
    audio_capabilities = RTCRtpSender.getCapabilities("audio")
    audio_prefs = list(filter(lambda x: x.name == "opus", audio_capabilities.codecs))
    if audio_prefs:
        audio_transceiver = pc.getTransceivers()[0]  # Audio is first track
        audio_transceiver.setCodecPreferences(audio_prefs)
    
    # Configure video codec preferences
    capabilities = RTCRtpSender.getCapabilities("video")
    preferences = list(filter(lambda x: x.name == "H264", capabilities.codecs))
    preferences += list(filter(lambda x: x.name == "VP8", capabilities.codecs))
    preferences += list(filter(lambda x: x.name == "rtx", capabilities.codecs))
    transceiver = pc.getTransceivers()[1]  # Video is second track
    transceiver.setCodecPreferences(preferences)

    async def _boost_encoder_target(sender: RTCRtpSender, target_bps: int) -> None:
        """
        Poll until aiortc's encoder is created, then bump its target bitrate.
        Also pre-warms the encoder to eliminate startup lag.
        """
        try:
            for _ in range(200):  # poll up to ~20s
                encoder = getattr(sender, "_RTCRtpSender__encoder", None)
                if encoder and hasattr(encoder, "target_bitrate"):
                    current = getattr(encoder, "target_bitrate", None)
                    if current is not None and current < target_bps:
                        logger.info(
                            f"Boosting encoder target bitrate from {current} to {target_bps}"
                        )
                        encoder.target_bitrate = target_bps
                        
                        # Pre-warm encoder with keyframe request
                        if hasattr(encoder, "force_keyframe"):
                            try:
                                encoder.force_keyframe()
                                logger.info("Encoder pre-warmed with keyframe")
                            except:
                                pass
                    return
                await asyncio.sleep(0.1)
        except Exception as exc:
            logger.warning(f"Failed to boost encoder bitrate: {exc}")

    # OPTIMIZED: Boost audio quality for 48kHz fullband voice (128 kbps)
    async def _boost_audio_quality(sender: RTCRtpSender) -> None:
        """Configure Opus encoder for 48kHz fullband voice with high quality"""
        try:
            for _ in range(100):
                encoder = getattr(sender, "_RTCRtpSender__encoder", None)
                if encoder and hasattr(encoder, "codec"):
                    codec = encoder.codec
                    codec.bit_rate = 128000  # 128 kbps for high quality
                    codec.options["application"] = "voip"  # Optimized for voice (not music)
                    codec.options["signal"] = "voice"  # Voice signal type
                    codec.options["bandwidth"] = "fullband"  # 0-20kHz for 48kHz
                    codec.options["complexity"] = "10"  # Max quality
                    logger.info(f"Configured audio encoder: 128 kbps, 48kHz fullband voice")
                    return
                await asyncio.sleep(0.1)
        except Exception as exc:
            logger.warning(f"Failed to configure audio encoder: {exc}")
    
    asyncio.create_task(_boost_audio_quality(audio_transceiver.sender))

    # OPTIMIZED: 3 Mbps for high clarity at 1080p with H.264 hardware encoding
    asyncio.create_task(_boost_encoder_target(transceiver.sender, 3_000_000))

    await pc.setRemoteDescription(offer)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    #return jsonify({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "sessionid":sessionid}
        ),
    )

async def human(request):
    try:
        params = await request.json()

        sessionid = params.get('sessionid',0)
        if params.get('interrupt'):
            nerfreals[sessionid].flush_talk()

        if params['type']=='echo':
            nerfreals[sessionid].put_msg_txt(params['text'])
        elif params['type']=='chat':
            asyncio.get_event_loop().run_in_executor(None, llm_response, params['text'],nerfreals[sessionid])                         
            #nerfreals[sessionid].put_msg_txt(res)

        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": 0, "msg":"ok"}
            ),
        )
    except Exception as e:
        logger.exception('exception:')
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": -1, "msg": str(e)}
            ),
        )

async def interrupt_talk(request):
    try:
        params = await request.json()

        sessionid = params.get('sessionid',0)
        nerfreals[sessionid].flush_talk()
        
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": 0, "msg":"ok"}
            ),
        )
    except Exception as e:
        logger.exception('exception:')
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": -1, "msg": str(e)}
            ),
        )

async def humanaudio(request):
    try:
        form= await request.post()
        sessionid = int(form.get('sessionid',0))
        fileobj = form["file"]
        filename=fileobj.filename
        filebytes=fileobj.file.read()
        
        # Handle session mismatch - use active session if requested one doesn't exist
        if sessionid in nerfreals and nerfreals[sessionid] is not None:
            nerfreals[sessionid].put_audio_file(filebytes)
        else:
            # Find the most recent ACTIVE (non-None) session
            active_sessions = {k: v for k, v in nerfreals.items() if v is not None}
            if active_sessions:
                active_session = max(active_sessions.keys())
                _log_missing_session(sessionid, f"Session {sessionid} not found for REST audio; using active session {active_session}")
                nerfreals[active_session].put_audio_file(filebytes)
            else:
                # Clean up dead sessions immediately
                dead_sessions = [k for k, v in nerfreals.items() if v is None]
                if dead_sessions:
                    for k in dead_sessions:
                        logger.info(f"Cleaning up dead session {k} from /humanaudio")
                        del nerfreals[k]
                        _reset_missing_session_count(k)
                
                _log_missing_session(sessionid, f"No active sessions found; dropping audio for session {sessionid}")
                return web.Response(
                    content_type="application/json",
                    text=json.dumps(
                        {"code": -1, "msg": "No active sessions"}
                    ),
                )

        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": 0, "msg":"ok"}
            ),
        )
    except Exception as e:
        logger.exception('exception:')
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": -1, "msg": str(e)}
            ),
        )

async def set_audiotype(request):
    try:
        params = await request.json()

        sessionid = params.get('sessionid',0)    
        nerfreals[sessionid].set_custom_state(params['audiotype'],params['reinit'])

        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": 0, "msg":"ok"}
            ),
        )
    except Exception as e:
        logger.exception('exception:')
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": -1, "msg": str(e)}
            ),
        )

async def record(request):
    try:
        params = await request.json()

        sessionid = params.get('sessionid',0)
        if params['type']=='start_record':
            # nerfreals[sessionid].put_msg_txt(params['text'])
            nerfreals[sessionid].start_recording()
        elif params['type']=='end_record':
            nerfreals[sessionid].stop_recording()
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": 0, "msg":"ok"}
            ),
        )
    except Exception as e:
        logger.exception('exception:')
        return web.Response(
            content_type="application/json",
            text=json.dumps(
                {"code": -1, "msg": str(e)}
            ),
        )

async def is_speaking(request):
    params = await request.json()

    sessionid = params.get('sessionid',0)
    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {"code": 0, "data": nerfreals[sessionid].is_speaking()}
        ),
    )


async def on_shutdown(app):
    # close peer connections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()

async def post(url,data):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url,data=data) as response:
                return await response.text()
    except aiohttp.ClientError as e:
        logger.info(f'Error: {e}')

async def run(push_url,sessionid):
    nerfreal = await asyncio.get_event_loop().run_in_executor(None, build_nerfreal,sessionid)
    nerfreals[sessionid] = nerfreal

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.info("Connection state is %s" % pc.connectionState)
        if pc.connectionState == "failed":
            await pc.close()
            pcs.discard(pc)

    player = HumanPlayer(nerfreals[sessionid])
    audio_sender = pc.addTrack(player.audio)
    video_sender = pc.addTrack(player.video)

    # CRITICAL FIX: Configure audio codec for high quality (WHIP endpoint)
    audio_capabilities = RTCRtpSender.getCapabilities("audio")
    audio_prefs = list(filter(lambda x: x.name == "opus", audio_capabilities.codecs))
    if audio_prefs:
        audio_transceiver = pc.getTransceivers()[0]
        audio_transceiver.setCodecPreferences(audio_prefs)
    
    async def _boost_audio_quality_whip(sender: RTCRtpSender) -> None:
        """Configure Opus encoder for 48kHz fullband voice (WHIP endpoint)"""
        try:
            for _ in range(100):
                encoder = getattr(sender, "_RTCRtpSender__encoder", None)
                if encoder and hasattr(encoder, "codec"):
                    codec = encoder.codec
                    codec.bit_rate = 128000  # 128 kbps for high quality
                    codec.options["application"] = "voip"  # Optimized for voice
                    codec.options["signal"] = "voice"  # Voice signal type
                    codec.options["bandwidth"] = "fullband"  # 0-20kHz for 48kHz
                    codec.options["complexity"] = "10"  # Max quality
                    logger.info(f"[WHIP] Configured audio: 128 kbps, 48kHz fullband voice")
                    return
                await asyncio.sleep(0.1)
        except Exception as exc:
            logger.warning(f"[WHIP] Failed to configure audio encoder: {exc}")
    
    asyncio.create_task(_boost_audio_quality_whip(audio_transceiver.sender))

    # OPTIMIZED: 2 Mbps for smooth real-time encoding (was 8 Mbps)
    asyncio.create_task(_boost_encoder_target(pc.getTransceivers()[1].sender, 2_000_000))

    await pc.setLocalDescription(await pc.createOffer())
    answer = await post(push_url,pc.localDescription.sdp)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer,type='answer'))
##########################################
# os.environ['MKL_SERVICE_FORCE_INTEL'] = '1'
# os.environ['MULTIPROCESSING_METHOD'] = 'forkserver'                                                    
if __name__ == '__main__':
    mp.set_start_method('spawn')
    parser = argparse.ArgumentParser()
    
    # audio FPS
    parser.add_argument('--fps', type=int, default=50, help="audio fps,must be 50")
    # sliding window left-middle-right length (unit: 20ms)
    parser.add_argument('-l', type=int, default=10)
    parser.add_argument('-m', type=int, default=8)
    parser.add_argument('-r', type=int, default=10)

    parser.add_argument('--W', type=int, default=450, help="GUI width")
    parser.add_argument('--H', type=int, default=450, help="GUI height")

    #musetalk opt
    parser.add_argument('--avatar_id', type=str, default='avator_1', help="define which avatar in data/avatars")
    parser.add_argument('--idle_avatar_id', type=str, default=None, help="define idle avatar for silence (optional)")
    #parser.add_argument('--bbox_shift', type=int, default=5)
    parser.add_argument('--batch_size', type=int, default=16, help="infer batch")

    parser.add_argument('--customvideo_config', type=str, default='', help="custom action json")

    parser.add_argument('--tts', type=str, default='edgetts', help="tts service type") #xtts gpt-sovits cosyvoice fishtts tencent doubao indextts2 azuretts
    parser.add_argument('--REF_FILE', type=str, default="zh-CN-YunxiaNeural",help="参考文件名或语音模型ID，默认值为 edgetts的语音模型ID zh-CN-YunxiaNeural, 若--tts指定为azuretts, 可以使用Azure语音模型ID, 如zh-CN-XiaoxiaoMultilingualNeural")
    parser.add_argument('--REF_TEXT', type=str, default=None)
    parser.add_argument('--TTS_SERVER', type=str, default='http://127.0.0.1:9880') # http://localhost:9000
    # parser.add_argument('--CHARACTER', type=str, default='test')
    # parser.add_argument('--EMOTION', type=str, default='default')

    parser.add_argument('--model', type=str, default='musetalk') #musetalk wav2lip ultralight

    # LivePortrait integration options
    parser.add_argument('--use_liveportrait', action='store_true', 
                        help="Use LivePortrait renderer for enhanced head motion and quality")
    parser.add_argument('--liveportrait_reference', type=str, default=None,
                        help="Path to reference image for LivePortrait")

    parser.add_argument('--transport', type=str, default='rtcpush') #webrtc rtcpush virtualcam
    parser.add_argument('--push_url', type=str, default='http://localhost:1985/rtc/v1/whip/?app=live&stream=livestream') #rtmp://localhost/live/livestream

    parser.add_argument('--max_session', type=int, default=1)  #multi session count
    parser.add_argument('--listenport', type=int, default=8010, help="web listen port")
    parser.add_argument('--av_sync_offset_ms', type=float, default=0.0,
                        help="Audio/video sync offset in milliseconds (positive delays audio so video leads)")

    opt = parser.parse_args()
    #app.config.from_object(opt)
    #print(app.config)
    opt.customopt = []
    if opt.customvideo_config!='':
        with open(opt.customvideo_config,'r') as file:
            opt.customopt = json.load(file)

    # if opt.model == 'ernerf':       
    #     from nerfreal import NeRFReal,load_model,load_avatar
    #     model = load_model(opt)
    #     avatar = load_avatar(opt) 
    if opt.model == 'musetalk':
        from musereal import MuseReal,load_model,load_avatar,warm_up
        logger.info(opt)
        model = load_model()
        # Load avatar with idle frames (AV_base for speech, AV_idle for silence)
        avatar = load_avatar(opt.avatar_id, idle_avatar_id=opt.idle_avatar_id) 
        warm_up(opt.batch_size,model)      
    elif opt.model == 'wav2lip':
        from lipreal import LipReal,load_model,load_avatar,warm_up
        logger.info(opt)
        model = load_model("./models/wav2lip.pth")
        avatar = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size,model,256)
    elif opt.model == 'ultralight':
        from lightreal import LightReal,load_model,load_avatar,warm_up
        logger.info(opt)
        model = load_model(opt)
        avatar = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size,avatar,160)

    # if opt.transport=='rtmp':
    #     thread_quit = Event()
    #     nerfreals[0] = build_nerfreal(0)
    #     rendthrd = Thread(target=nerfreals[0].render,args=(thread_quit,))
    #     rendthrd.start()
    if opt.transport=='virtualcam':
        thread_quit = Event()
        nerfreals[0] = build_nerfreal(0)
        rendthrd = Thread(target=nerfreals[0].render,args=(thread_quit,))
        rendthrd.start()

    #############################################################################
    appasync = web.Application(client_max_size=1024**2*100)
    appasync.on_shutdown.append(on_shutdown)
    appasync.router.add_post("/offer", offer)
    appasync.router.add_post("/human", human)
    appasync.router.add_post("/humanaudio", humanaudio)
    appasync.router.add_post("/set_audiotype", set_audiotype)
    appasync.router.add_post("/record", record)
    appasync.router.add_post("/interrupt_talk", interrupt_talk)
    appasync.router.add_post("/is_speaking", is_speaking)
    appasync.router.add_static('/',path='web')

    # Configure default CORS settings.
    cors = aiohttp_cors.setup(appasync, defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
            )
        })
    # Configure CORS on all routes.
    for route in list(appasync.router.routes()):
        cors.add(route)

    pagename='webrtcapi.html'
    if opt.transport=='rtmp':
        pagename='echoapi.html'
    elif opt.transport=='rtcpush':
        pagename='rtcpushapi.html'
    logger.info('start http server; http://<serverip>:'+str(opt.listenport)+'/'+pagename)
    logger.info('WebRTC dashboard available at http://<serverip>:%s/dashboard.html', opt.listenport)
    def run_server(runner):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, '0.0.0.0', opt.listenport)
        loop.run_until_complete(site.start())
        if opt.transport=='rtcpush':
            for k in range(opt.max_session):
                push_url = opt.push_url
                if k!=0:
                    push_url = opt.push_url+str(k)
                loop.run_until_complete(run(push_url,k))
        loop.run_forever()    
    #Thread(target=run_server, args=(web.AppRunner(appasync),)).start()
    run_server(web.AppRunner(appasync))

    #app.on_shutdown.append(on_shutdown)
    #app.router.add_post("/offer", offer)

    # print('start websocket server')
    # server = pywsgi.WSGIServer(('0.0.0.0', 8000), app, handler_class=WebSocketHandler)
    # server.serve_forever()
    
    
