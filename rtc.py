#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import uuid
import socketio
import aiohttp
from av.audio.frame import AudioFrame
from aiortc import RTCDataChannel, sdp
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaPlayer, MediaRelay, MediaStreamError, MediaStreamTrack

import pyaudio
import numpy as np

# pigpio lib
import pigpio
import psutil

sio = socketio.AsyncClient(ssl_verify=False)
ROOT = os.path.dirname(__file__)

logger = logging.getLogger("pc")
pc: RTCPeerConnection
pcs = set()
# hosted:bool
sid:str
channel: RTCDataChannel
task: asyncio.Task

webcam = None
relay = None

pi = None

with open(os.path.join(ROOT, 'config.json'),"r", encoding="utf-8") as f:
# with open(os.environ['HOME']+'/Projects/rtcServer/config.json',"r", encoding="utf-8") as f:
    config = json.load(f)
    # deviceid = json.load(devInfoRaw)["deviceid"]

@sio.event
async def connect():
    await sio.emit("find",config["deviceid"])
    print('connection established')
    # task = asyncio.create_task(offer())

@sio.event
async def create():
    await sio.emit("auth")
    # global hosted
    # hosted = True
    # print("created, Host:%s" % hosted)

@sio.event
async def join():
    await sio.emit("auth")
    # global hosted
    # hosted = False
    # print('joined, Host:%s'% hosted)

@sio.event
async def approve(id):
    global sid
    sid = id
    await sio.emit("accept",id)
    print('id: ', id)

@sio.event
async def bridge():
    global task
    try:
        if not task.cancelled():
            await shutdown()
            task.cancel()
            print(task.cancelled())
            # await sio.emit("leave")
            # await sio.emit("find","123")
        print("task restarting")
        task = asyncio.create_task(offer())
    except NameError:
        task = asyncio.create_task(offer())

# @sio.event
# async def waiting():
#     await sio.emit("find","123")

@sio.event
async def hangup():
    # global hosted
    # hosted = True
    # await shutdown()
    # task.cancel()
    print("hangup")
    
    # print("remote hanged up")
    # await sio.emit("find","123")

@sio.event
def disconnect():
    if pi is not None:
        servoControl(1500)
    print('disconnected from server')

@sio.on("servo-control")
def on_servoControl(pwm):
    # pwm = payload["pwm"]
    servoControl(pwm)

def servoControl(pwm):
    if not pi.connected:
        servoInit()
    else:
        pi.set_mode(18, pigpio.OUTPUT)
        if pwm >= 500 and pwm <= 2500:
            pi.set_servo_pulsewidth(18, pwm)
        elif "pwm" in pwm and pwm["pwm"] >= 500 and pwm["pwm"] <= 2500:
            pi.set_servo_pulsewidth(18, pwm["pwm"])
        else:
            print("pwm out of range!")
        
def checkProc(proc) -> bool:
    procs = {p.pid:p.info for p in psutil.process_iter(['name'])}
    if {'name': proc} in procs.values():
        return True
    else:
        return False
    
def servoInit():
    if not checkProc('pigpiod'):
        os.system("sudo pigpiod")
    global pi
    if pi is None:
        pi = pigpio.pi()

async def msgHandler(msg:str, pc:RTCPeerConnection):
    if isinstance(msg,dict):
        if msg["type"] == "offer":
            offer = RTCSessionDescription(sdp=msg["sdp"],type=msg["type"])
            # print(offer)
            await pc.setRemoteDescription(offer)

            # """
            # prepare local media
            # player = MediaPlayer(os.path.join(ROOT, "demo-instruct.wav"))
            isAudio = True
            try:
                microphone = MediaPlayer('sysdefault:CARD=Device', format='alsa')
            except:
                isAudio = False
            global webcam, relay
            if relay is None:
                webcam = MediaPlayer('/dev/video2', format='v4l2', options={
                    'video_size':'640x480',
                    'input_format':'h264',
                    'crf':'51',
                    'c:v':'copy',
                    'tune': 'zerolatency',
                    'preset':'ultrafast',
                    # 'bufsize':'3000k',
                    # 'rtbufsize':'3000k',
                    # 'rtbufsize':'2M',
                    # 'pix_fmt':'h264'
                }) 
                relay = MediaRelay()
            video = relay.subscribe(webcam.video,False)
            audio = relay.subscribe(microphone.audio,False) if isAudio else None

            # pc.addTrack(video)
            # pc.addTrack(playerAudio.audio)
            # recorder.addTrack(track)
            # pc.addTrack(microphone.audio)
            for t in pc.getTransceivers():
                if t.kind == "audio" and isAudio:
                    pc.addTrack(audio)
                elif t.kind == "video" and video:
                    pc.addTrack(video)
            
            # """

            # print(pc.remoteDescription)
            answer = await pc.createAnswer()
            # print(answer.sdp)
            await pc.setLocalDescription(answer)
            await sio.send(json.dumps({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}))
        elif msg["type"] == "answer": 
            answer = RTCSessionDescription(sdp=msg["sdp"],type=msg["type"])
            # print(pc.localDescription)
            await pc.setRemoteDescription(answer)

        elif msg["type"] == "candidate" and msg["candidate"]["candidate"]:
            # print("candidate",msg["candidate"])
            candidate = sdp.candidate_from_sdp(msg["candidate"]["candidate"])
            candidate.sdpMid = msg["candidate"]["sdpMid"]
            candidate.sdpMLineIndex = msg["candidate"]["sdpMLineIndex"]
            await pc.addIceCandidate(candidate)

async def shutdown():
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()

async def init_peer():
    async with aiohttp.ClientSession() as session:
        async with session.get(config["base_url"]+config["path"], ssl=False) as resp:
            assert resp.status == 200
            await sio.connect(config["base_url"])
    print("sid:",sio.sid)
    servoInit()
    await sio.wait()

async def offer():
    # params = await request.json()
    # offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    configuration = RTCConfiguration()
    # iceServer = RTCIceServer(urls="stun:stun3.l.google.com:19302")
    iceServer = RTCIceServer(urls=config["turnserver"],username=config["username"],credential=config["credential"])
    configuration.iceServers = [iceServer]
    
    global pc
    try:
        print("pc existed:",pc,pc.connectionState)
        await pc.close()
        pcs.clear()
    except NameError:
        print("new peerConnection")


    pc = RTCPeerConnection(configuration=configuration)
    pcs.add(pc)
    # print(hosted)
    pc_id = "PeerConnection(%s)" % uuid.uuid4()

    def log_info(msg, *args):
        logger.info(pc_id + " " + msg, *args)


    """
    # prepare local media
    # player = MediaPlayer(os.path.join(ROOT, "demo-instruct.wav"))
    microphone = MediaPlayer('sysdefault:CARD=Device', format='alsa')
    global webcam, relay
    if relay is None:
        webcam = MediaPlayer('/dev/video2', format='v4l2', options={
            'video_size':'640x480',
            'input_format':'h264',
            'crf':'51',
            'c:v':'copy',
            'tune': 'zerolatency',
            'preset':'ultrafast',
            # 'bufsize':'3000k',
            # 'rtbufsize':'3000k',
            # 'rtbufsize':'2M',
            # 'pix_fmt':'h264'
        }) 
        # webcam = MediaPlayer('/dev/video2')
        relay = MediaRelay()
    video = relay.subscribe(webcam.video,False)
    # audio = relay.subscribe(microphone.audio, False)

    # pc.addTrack(microphone.audio)
    pc.addTrack(video)
    pc.addTrack(microphone.audio)
    # pc.addTrack(video)
    # audio = relay.subscribe(playerAudio.audio)
    # pc.addTrack(audio)
    # recorder.addTrack(track)
    # pc.addTrack(player.audio)
    """


    # @pc.on("datachannel")
    # def on_datachannel(evt):
    #     global channel 
    #     channel = evt
    #     print("channel(%s)" % channel.label)

    #     @channel.on("message")
    #     async def on_message(message):
    #         print(message,isinstance(message, str))
    #         # if message == "shutdown":
    #         #     await shutdown()

    #         if isinstance(message, str) and message.startswith("ping"):
    #             channel.send("pong" + message[4:])
    
    #     @channel.on("close")
    #     async def on_close():
    #         print("closed datachannel")
    #         await shutdown()

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        log_info("Connection state is %s", pc.connectionState)
        print("pc con state:",pc.connectionState)
        if pc.connectionState == "failed":
            print("pc con failed")
            await pc.close()
            pcs.discard(pc)
            

    # @pc.on("icegatheringstatechange")
    # async def on_icegatheringstatechange():
    #     print("icegathering state:",pc.iceGatheringState)

    # @pc.on("iceconnectionstatechange")
    # async def on_iceconnectionstatechange():
    #     print("iceconnection state:",pc.iceConnectionState)

    # @pc.on("icecandidate")
    # async def on_icecandidate(e):
    #     print("candidate: ",e)
    #     if not e["candidate"] is None:
    #         print(e)
    #         await sio.send(json.dumps({"type": "candidate","candidate": e["candidate"]}))

    @pc.on("track")
    async def on_track(track):
        print("track",track.kind)
        log_info("Track %s received", track.kind)

        if track.kind == "audio":

            # async def playback():
            # chunk = 1024
            frame: AudioFrame = await track.recv()
            p = pyaudio.PyAudio()

            stream = p.open(format=p.get_format_from_width(2),
                            channels=len(frame.layout.channels),
                            rate=frame.rate, 
                            output=True)
            while True:
                try:
                    frame = await track.recv()
                except MediaStreamError:
                    break
                # print(frame.to_ndarray().astype(np.int16))
                stream.write(frame.to_ndarray().astype(np.int16).tobytes(),960)
            stream.stop_stream()
            stream.close()
            p.terminate()

        @track.on("ended")
        async def on_ended():
            log_info("Track %s ended", track.kind)

    @sio.event
    async def message(msg):
        await msgHandler(msg,pc)

    # handle offer
    # await pc.setRemoteDescription(offer)

    # send offer
    # if hosted:

    """
    # global channel
    channel = pc.createDataChannel("chat")
    print("channel(%s)" % channel.label)

    @channel.on("message")
    async def on_message(message):
        print(message,isinstance(message, str))
        # if message == "shutdown":
        #     await shutdown()
        # if isinstance(message, str) and message.startswith("ping"):
        if isinstance(message, str):
            channel.send("pong" + message[4:])

    @channel.on("close")
    async def on_close():
        print("closed datachannel")
        coros = [pc.close() for pc in pcs]
        await asyncio.gather(*coros)
        task.cancel()
        pcs.clear()



    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    # print(pc.localDescription.sdp)

    await sio.send(json.dumps({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}))
    """

    # return web.Response(
    #     content_type="application/json",
    #     text=json.dumps(
    #         {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    #     ),
    # )
    


if __name__ == '__main__':
    asyncio.run(init_peer())
    