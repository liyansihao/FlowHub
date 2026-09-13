"""1688 procurement + DINO/Qwen approval; never submits a listing."""
from ..plugin_comparebot import evaluate


class ReviewModule:
    name = 'review'

    async def run(self, db, owner, sku, seller):
        return await evaluate(db, owner, sku, seller)
