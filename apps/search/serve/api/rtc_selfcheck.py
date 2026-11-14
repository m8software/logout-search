"""Build-time check for the WebRTC peer image: two peers in this process open a data channel
and pass a message each way, the only thing rtc_peer.py asks of aiortc. Run by Dockerfile.rtc,
so an image whose aiortc cannot do that without FFmpeg (av_absent.py) is never built."""
import asyncio
import os
import sys

import av
from aiortc import RTCConfiguration, RTCPeerConnection


async def main():
    if os.path.basename(av.__file__) != "av.py" or "av.libs" in " ".join(sys.modules):
        raise SystemExit(f"rtc_selfcheck: the real PyAV is installed ({av.__file__}), not the stand-in")
    asker, engine = RTCPeerConnection(RTCConfiguration(iceServers=[])), RTCPeerConnection(RTCConfiguration(iceServers=[]))
    got_by_engine, got_by_asker = asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future()

    @engine.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(raw):
            got_by_engine.set_result(raw)
            channel.send("answer:" + raw)

    channel = asker.createDataChannel("search")
    channel.on("open", lambda: channel.send("q=chess"))
    channel.on("message", got_by_asker.set_result)

    await asker.setLocalDescription(await asker.createOffer())
    await engine.setRemoteDescription(asker.localDescription)
    await engine.setLocalDescription(await engine.createAnswer())
    await asker.setRemoteDescription(engine.localDescription)
    try:
        sent, back = await asyncio.wait_for(asyncio.gather(got_by_engine, got_by_asker), 30)
    finally:
        await asker.close()
        await engine.close()
    if (sent, back) != ("q=chess", "answer:q=chess"):
        raise SystemExit(f"rtc_selfcheck: wrong messages {sent!r} {back!r}")
    if "m=audio" in engine.localDescription.sdp or "m=video" in engine.localDescription.sdp:
        raise SystemExit("rtc_selfcheck: the answer offers audio or video")
    print("rtc_selfcheck: data channel open, one message each way, no FFmpeg loaded")


asyncio.run(main())
