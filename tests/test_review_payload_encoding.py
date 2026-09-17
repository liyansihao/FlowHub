import asyncio
import base64
import gzip
import json

import httpx
from flowhub.review_transport import request


def test_vercel_refresh_compresses_snapshot_and_preserves_other_messages():
    async def run():
        captured=[]
        def accept(req):
            captured.append(json.loads(req.content))
            return httpx.Response(200,json={'ok':True})
        config={'FLOWHUB_REVIEW_SYNC_URL':'https://review.example/api/reviews',
                'FLOWHUB_REVIEW_SYNC_TOKEN':'synthetic-test',
                'FLOWHUB_REVIEW_PAYLOAD_ENCODING':'gzip-base64'}
        snapshot={'owner':'test','items':[{'sku':'123','title':'审核商品'}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(accept)) as client:
            await request(client,config,'POST',params={'mode':'refresh'},body=snapshot)
            ack={'id':'test','status':'applied'}
            await request(client,config,'POST',params={'mode':'ack'},body=ack)
        assert captured[0]['encoding']=='gzip-base64'
        assert json.loads(gzip.decompress(base64.b64decode(captured[0]['payload'])))==snapshot
        assert captured[1]==ack
    asyncio.run(run())
