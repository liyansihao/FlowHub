"""An approved asking price is an intent, not a fresh market observation."""
import math
import time


def approved_price_intent(review, now=None):
    now=time.time() if now is None else now
    try:
        finished=float(review['finished_at'])
        match=review['result'];evidence=match['evidence'];source=evidence['source'];profit=evidence['profit']
        price=float(profit['sell_price_cny']);purchase=float(match['purchase'])
        if (review['state']!='matched' or not 0<=now-finished<21600
            or source['comparebot']['decision']['outcome']!='approved'
            or not math.isfinite(price) or price<=0
            or not math.isfinite(purchase) or purchase<=0
            or price!=float(profit['input']['sell_price'])
            or purchase!=float(profit['input']['purchase_price'])
            or purchase!=float(source['selected_cost_cny'])
            or str(match['supplier_id'])!=str(source['selected_offer_id'])):
            return None
        return {'kind':'approved_listing_price','sku':str(review['candidate']['source_key']),
                'value':price,'currency':'CNY','decided_at':finished,
                'source_quote':review['candidate']['origin'].get('price_evidence')}
    except (KeyError,ValueError,TypeError,OverflowError):return None
