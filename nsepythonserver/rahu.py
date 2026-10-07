import os,sys
#os.chdir(os.path.dirname(os.path.abspath(__file__)))
#sys.path.insert(1, os.path.join(sys.path[0], '..'))

import requests
import pandas as pd
import json
import random
import datetime,time
import logging
import re
import urllib.parse

# ---------------------------------------------------------------------------
# NSE's site is behind Akamai Bot Manager, which fingerprints the TLS/JA3
# handshake. Plain `requests` (and plain `curl`) get a 403 on the homepage
# itself, regardless of User-Agent -- it is NOT a "requests is blocked in
# India" thing (see github.com/aeron7/nsepython issue #73), it is a TLS
# fingerprint check. curl_cffi impersonates a real Chrome TLS fingerprint and
# clears it. It is a hard dependency for this module now (both the old
# mode='vpn' os.popen-curl path and the old mode='local' plain-requests path
# are equally broken against the live site), so we import it eagerly and
# raise a clear, actionable error if it's missing rather than silently
# falling back to a transport that cannot work.
try:
    from curl_cffi.requests import Session as _CurlSession
    _CURL_CFFI_OK = True
except ImportError:
    _CURL_CFFI_OK = False

mode = 'vpn'  # kept for backward compatibility with code that reads rahu.mode;
              # the transport below is curl_cffi-based regardless of its value.


class NSEFetchError(Exception):
    """Raised by nsefetch() when an NSE endpoint can't be reached or doesn't
    return usable JSON, instead of the old behaviour of silently swallowing
    the error and returning {} -- which was itself the root cause of several
    confusing downstream KeyErrors reported against this library (e.g.
    nsepython #74, #75, nsepythonserver #6): callers would do payload["data"]
    on an empty {} and get a KeyError with no indication the real problem was
    an upstream 403/404/503."""
    pass


_nse_session = None
_nse_warmed = False

# Headers sent on every API call (beyond whatever curl_cffi's Chrome
# impersonation already sets for us).
api_headers = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def _get_nse_session(force_refresh: bool = False):
    """Return a warmed curl_cffi session impersonating Chrome. Warming means
    visiting a couple of real nseindia.com pages first so Akamai hands out
    its tracking cookies (_abck/ak_bmsc/bm_sv/...) before we hit the API --
    without this, API calls 403/404 even with the right TLS fingerprint."""
    global _nse_session, _nse_warmed
    if not _CURL_CFFI_OK:
        raise NSEFetchError(
            "curl_cffi is required to talk to nseindia.com -- plain `requests` "
            "(and plain `curl`) get a 403 on the homepage itself because NSE's "
            "Akamai Bot Manager fingerprints the TLS/JA3 handshake, not because "
            "of geography. Install it with: pip install curl_cffi"
        )
    if _nse_session is None or force_refresh:
        _nse_session = _CurlSession(impersonate="chrome124")
        _nse_warmed = False
    if not _nse_warmed:
        try:
            _nse_session.get("https://www.nseindia.com", timeout=20)
            time.sleep(1.2)
            _nse_session.get("https://www.nseindia.com/market-data/live-equity-market", timeout=20)
            time.sleep(1.2)
            _nse_session.get("https://www.nseindia.com/option-chain", timeout=20)
            time.sleep(1.0)
            _nse_warmed = True
        except Exception as _e:
            logging.warning(f"NSE session warm-up partial failure: {_e}")
    return _nse_session


def _equity_stockindices_fallback(session):
    """/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O is a retired
    route (404s under Akamai, confirmed live, cookies make no difference --
    it's simply gone, not JS-walled). /api/market-data-pre-open?key=FO
    carries the same F&O stock universe with the same per-stock fields, so we
    reshape its payload to look like the old equity-stockIndices response.
    This is done transparently inside nsefetch() so every existing caller
    (fnolist, nse_custom_function_secfno, nsetools_get_quote,
    nse_get_advances_declines, nse_get_top_losers/gainers) keeps working
    unchanged."""
    r = session.get(
        "https://www.nseindia.com/api/market-data-pre-open?key=FO",
        headers=api_headers, timeout=30,
    )
    if r.status_code != 200:
        raise NSEFetchError(
            f"equity-stockIndices fallback (market-data-pre-open?key=FO) failed: HTTP {r.status_code}"
        )
    raw = r.json()
    reshaped = []
    for item in raw.get("data", []):
        m = item.get("metadata", {})
        if not m.get("symbol"):
            continue
        reshaped.append({
            "symbol": m.get("symbol", ""),
            "pChange": m.get("pChange", 0),
            "lastPrice": m.get("lastPrice", 0),
            "change": m.get("change", 0),
            "previousClose": m.get("previousClose", 0),
            "yearHigh": m.get("yearHigh", 0),
            "yearLow": m.get("yearLow", 0),
            "totalTradedValue": m.get("totalTurnover", 0),
            "totalTradedVolume": m.get("finalQuantity", 0),
        })
    return {"data": reshaped}


def nsefetch(payload: str):
    """GET a JSON NSE endpoint through a warmed curl_cffi (Chrome-impersonating)
    session. Raises NSEFetchError on persistent failure instead of returning {}."""
    session = _get_nse_session()

    if "equity-stockIndices" in payload and "SECURITIES" in payload:
        return _equity_stockindices_fallback(session)

    try:
        r = session.get(payload, headers=api_headers, timeout=30)
        if r.status_code in (403, 404):
            # Session cookies may have gone stale -- re-warm once and retry
            # before giving up.
            session = _get_nse_session(force_refresh=True)
            r = session.get(payload, headers=api_headers, timeout=30)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError as e:
                raise NSEFetchError(
                    f"nsefetch {payload}: HTTP 200 but response body is not valid JSON ({e}); "
                    f"first 200 chars: {r.text[:200]!r}"
                )
        raise NSEFetchError(f"nsefetch {payload}: HTTP {r.status_code}")
    except NSEFetchError:
        raise
    except Exception as e:
        raise NSEFetchError(f"nsefetch {payload}: {e}")


headers = {
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0',
    'DNT': '1',
    'Upgrade-Insecure-Requests': '1',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.79 Safari/537.36',
    'Sec-Fetch-User': '?1',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-Mode': 'navigate',
    'Accept-Encoding': 'gzip, deflate, br',
    'Accept-Language': 'en-US,en;q=0.9,hi;q=0.8',
}

#Curl headers
curl_headers = ''' -H "authority: beta.nseindia.com" -H "cache-control: max-age=0" -H "dnt: 1" -H "upgrade-insecure-requests: 1" -H "user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.117 Safari/537.36" -H "sec-fetch-user: ?1" -H "accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9" -H "sec-fetch-site: none" -H "sec-fetch-mode: navigate" -H "accept-encoding: gzip, deflate, br" -H "accept-language: en-US,en;q=0.9,hi;q=0.8" --compressed'''

run_time=datetime.datetime.now()

#Constants
indices = ['NIFTY','FINNIFTY','BANKNIFTY']

def running_status():
    start_now=datetime.datetime.now().replace(hour=9, minute=15, second=0, microsecond=0)
    end_now=datetime.datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    return start_now<datetime.datetime.now()<end_now

#Getting FNO Symboles
def fnolist():
    positions = nsefetch('https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O')
    nselist = indices.copy()
    for x in range(len(positions['data'])):
        nselist.append(positions['data'][x]['symbol'])
    return nselist

def nsesymbolpurify(symbol):
    symbol = symbol.replace('&','%26') #URL Parse for Stocks Like M&M Finance
    return symbol

def nse_optionchain_scrapper(symbol):
    symbol = nsesymbolpurify(symbol)
    # Using getSymbolDerivativesData as it provides all expiries and strikes in one go
    url = f'https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolDerivativesData&symbol={symbol}'
    payload = nsefetch(url)
    
    # Transformation to match the "data" structure expected by pcr and other functions
    if payload and 'data' in payload:
        new_data = []
        # Group by strikePrice and expiryDate to create a combined CE/PE structure if possible,
        # or just provide the raw list if the consumers can handle it.
        # The current pcr() handles a list of entries where each has CE/PE keys OR is the entry itself.
        
        # Actually, let's restructure it to be more compatible with the expected 'data' format:
        # a list of dictionaries, each having 'strikePrice', 'expiryDate', 'CE', 'PE'.
        combined = {}
        for entry in payload['data']:
            sp = entry.get('strikePrice')
            ed = entry.get('expiryDate')
            ot = entry.get('optionType')
            if not sp or not ed or ot == 'XX': continue
            
            key = (sp, ed)
            if key not in combined:
                combined[key] = {'strikePrice': sp, 'expiryDate': ed, 'CE': None, 'PE': None}
            
            combined[key][ot] = entry
            
        payload['data'] = list(combined.values())
        
    return payload


def oi_chain_builder(symbol,expiry="latest",oi_mode="full"):

    if expiry == "latest":
        dates = expiry_list(symbol, type="list")
        if dates:
            expiry = dates[0]
        else:
            return pd.DataFrame(), 0.0, ""

    payload = nse_optionchain_scrapper(symbol)

    if(oi_mode=='compact'):
        col_names = ['CALLS_OI','CALLS_Chng in OI','CALLS_Volume','CALLS_IV','CALLS_LTP','CALLS_Net Chng','Strike Price','PUTS_OI','PUTS_Chng in OI','PUTS_Volume','PUTS_IV','PUTS_LTP','PUTS_Net Chng']
    if(oi_mode=='full'):
        col_names = ['CALLS_Chart','CALLS_OI','CALLS_Chng in OI','CALLS_Volume','CALLS_IV','CALLS_LTP','CALLS_Net Chng','CALLS_Bid Qty','CALLS_Bid Price','CALLS_Ask Price','CALLS_Ask Qty','Strike Price','PUTS_Bid Qty','PUTS_Bid Price','PUTS_Ask Price','PUTS_Ask Qty','PUTS_Net Chng','PUTS_LTP','PUTS_IV','PUTS_Volume','PUTS_Chng in OI','PUTS_OI','PUTS_Chart']
    oi_data = pd.DataFrame(columns = col_names)

    # We will populate these dynamically
    rows_list = []
    
    if 'expiryDates' not in payload:
        # Fallback for new API structure
        if(expiry=="latest"):
            expiry = expiry_list(symbol, type="list")[0]
        data_list = payload['data']
    else:
        # Legacy structure support
        if(expiry=="latest"):
            expiry = payload['records']['expiryDates'][0]
        data_list = payload['records']['data']

    for m in range(len(data_list)):
        current_expiry_str = data_list[m].get('expiryDates') or data_list[m].get('expiryDate')
        try:
            # Convert both to date objects for robust comparison
            if "-" in current_expiry_str:
                parts = current_expiry_str.split("-")
                if parts[1].isdigit(): fmt = "%d-%m-%Y"
                else: fmt = "%d-%b-%Y"
                curr_date = datetime.datetime.strptime(current_expiry_str, fmt).date()
                
                parts_exp = expiry.split("-")
                if parts_exp[1].isdigit(): fmt_exp = "%d-%m-%Y"
                else: fmt_exp = "%d-%b-%Y"
                exp_date = datetime.datetime.strptime(expiry, fmt_exp).date()
                match = (curr_date == exp_date)
            else:
                match = (current_expiry_str == expiry)
        except:
            match = (current_expiry_str == expiry)

        if match:
            oi_row = {col: 0 for col in col_names}
            oi_row['Strike Price'] = data_list[m]['strikePrice']

            for side in ['CE', 'PE']:
                prefix = f"{'CALLS' if side == 'CE' else 'PUTS'}_"
                if side in data_list[m] and data_list[m][side] is not None:
                    d = data_list[m][side]
                    oi_row[prefix + 'OI'] = d.get('openInterest', 0)
                    oi_row[prefix + 'Chng in OI'] = d.get('changeinOpenInterest', 0)
                    oi_row[prefix + 'Volume'] = d.get('totalTradedVolume', 0)
                    oi_row[prefix + 'IV'] = d.get('impliedVolatility', 0)
                    oi_row[prefix + 'LTP'] = d.get('lastPrice', 0)
                    oi_row[prefix + 'Net Chng'] = d.get('change', 0)
                    
                    if oi_mode == 'full':
                        # New API key mapping
                        oi_row[prefix + 'Bid Qty'] = d.get('buyQuantity1', d.get('bidQty', 0))
                        oi_row[prefix + 'Bid Price'] = d.get('buyPrice1', d.get('bidprice', 0))
                        oi_row[prefix + 'Ask Price'] = d.get('sellPrice1', d.get('askPrice', 0))
                        oi_row[prefix + 'Ask Qty'] = d.get('sellQuantity1', d.get('askQty', 0))
                        oi_row[prefix + 'Chart'] = 0

            rows_list.append(oi_row)

    oi_data = pd.DataFrame(rows_list)
    timestamp = payload.get('timestamp', payload.get('records', {}).get('timestamp', ''))
    underlyingValue = payload.get('underlyingValue', payload.get('records', {}).get('underlyingValue', 0))

    # getSymbolDerivativesData (the current option-chain data source, see
    # nse_optionchain_scrapper) doesn't carry underlyingValue at the top level
    # at all -- only inside each individual CE/PE leaf. Without this fallback
    # underlyingValue silently stays 0 (see github.com/aeron7/nsepython #80).
    if not underlyingValue and data_list:
        for entry in data_list:
            for side in ('CE', 'PE'):
                leaf = entry.get(side)
                if leaf:
                    uv = leaf.get('underlyingValue')
                    if uv:
                        underlyingValue = uv
                        break
            if underlyingValue:
                break

    oi_data['time_stamp'] = timestamp
    return oi_data, float(underlyingValue or 0), timestamp


def nse_quote_derivatives(symbol):
    symbol = nsesymbolpurify(symbol)
    if symbol.upper() in fnolist():
        payload = nsefetch('https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolDerivativesData&symbol='+symbol)
        return payload
    else:
        return {"error": f"{symbol} is not in derivatives list."}

def nse_quote(symbol,section=""):
    #https://forum.unofficed.com/t/nsetools-get-quote-is-not-fetching-delivery-data-and-delivery-can-you-include-this-as-part-of-feature-request/1115/4    
    symbol = nsesymbolpurify(symbol)

    if(section==""):
        if any(x in symbol for x in indices):
            payload = nsefetch('https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolDerivativesData&symbol='+symbol)
        else:
            payload = nsefetch('https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData&marketType=N&series=EQ&symbol='+symbol)
        return payload

    if(section=="trade_info"):
        # The old https://www.nseindia.com/api/quote-equity?section=trade_info
        # route is dead (confirmed HTTP 403 live, even through the fully
        # warmed curl_cffi session used everywhere else in this file). But
        # every piece of data that endpoint used to return is already present
        # in the no-section GetQuoteApi?functionName=getSymbolData response
        # fetched above -- no new network call needed, just remapping/
        # slicing fields that are already being pulled. This remaps the new
        # payload['equityResponse'][0] sub-objects (orderBook/tradeInfo/
        # priceInfo/metaData/secInfo) back into the shape old trade_info
        # callers expect (marketDeptOrderBook/securityWiseDP/preOpenMarket).
        #
        # One confirmed fidelity gap: the old preOpenMarket object was a rich
        # nested structure (per-price-level pre-open order book, ATO
        # buy/sell, etc) built from NSE's separate pre-open-market feed. The
        # new endpoint only carries a handful of scalar pre-open fields
        # flattened into metaData (iep/ieq/ic_change/ic_pchange/spoChange/
        # spoPchange/casStatus) -- these were all 0 when last tested outside
        # the 9:00-9:08 pre-open window (NSE zeroes them once continuous
        # trading starts), so the preopen-price-level array itself could not
        # be verified and is NOT fabricated here; preOpenMarket below only
        # carries the scalar fields that are genuinely available.
        base_payload = nse_quote(symbol, section="")
        try:
            eq = base_payload['equityResponse'][0]
        except (KeyError, IndexError, TypeError):
            # Index symbols (NIFTY/BANKNIFTY/FINNIFTY) go through the
            # derivatives payload shape above and have no cash-market
            # trade_info to remap -- return the raw base payload rather than
            # fabricate a shape that doesn't apply to indices.
            return base_payload

        trade_info = eq.get('tradeInfo', {}) or {}
        order_book = eq.get('orderBook', {}) or {}
        price_info = eq.get('priceInfo', {}) or {}
        meta_data = eq.get('metaData', {}) or {}
        sec_info = eq.get('secInfo', {}) or {}

        bid = [
            {"price": order_book.get(f"buyPrice{i}"), "quantity": order_book.get(f"buyQuantity{i}")}
            for i in range(1, 6)
        ]
        ask = [
            {"price": order_book.get(f"sellPrice{i}"), "quantity": order_book.get(f"sellQuantity{i}")}
            for i in range(1, 6)
        ]

        payload = {
            "noBlockDeals": True,
            "bulkBlockDeals": [],
            "marketDeptOrderBook": {
                "totalBuyQuantity": order_book.get("totalBuyQuantity"),
                "totalSellQuantity": order_book.get("totalSellQuantity"),
                "bid": bid,
                "ask": ask,
                "tradeInfo": {
                    "totalTradedVolume": trade_info.get("totalTradedVolume"),
                    "totalTradedValue": trade_info.get("totalTradedValue"),
                    "totalMarketCap": trade_info.get("totalMarketCap"),
                    "ffmc": trade_info.get("ffmc"),
                    "impactCost": trade_info.get("impactCost"),
                    "cmDailyVolatility": price_info.get("cmDailyVolatility"),
                    "cmAnnualVolatility": price_info.get("cmAnnualVolatility"),
                    "marketLot": trade_info.get("marketLot"),
                    "activeSeries": [trade_info.get("series")] if trade_info.get("series") else [],
                },
                "valueAtRisk": {
                    "securityVar": sec_info.get("securityvar"),
                    "indexVar": sec_info.get("indexvar"),
                    "varMargin": sec_info.get("varMargin"),
                    "extremeLossMargin": sec_info.get("extremelossMargin"),
                    "adhocMargin": sec_info.get("adhocMargin"),
                    "applicableMargin": trade_info.get("applicableMargin"),
                },
            },
            "securityWiseDP": {
                "quantityTraded": trade_info.get("quantitytraded"),
                "deliveryQuantity": trade_info.get("deliveryquantity"),
                "deliveryToTradedQuantity": trade_info.get("deliveryToTradedQuantity"),
                "seriesRemarks": None,
                "secWiseDelPosDate": trade_info.get("secwisedelposdate"),
            },
            # yearHigh/yearLow etc (old section="" response's priceInfo) --
            # kept here too since old trade_info responses also echoed 52wk
            # data under priceInfo.
            "priceInfo": {
                "yearHigh": price_info.get("yearHigh"),
                "yearLow": price_info.get("yearLow"),
                "yearHighDt": price_info.get("yearHightDt"),
                "yearLowDt": price_info.get("yearLowDt"),
                "tickSize": price_info.get("tickSize"),
                "priceBand": price_info.get("priceBand"),
            },
            # See the fidelity-gap note above: only the scalar pre-open
            # fields that exist in the new payload are included; the old
            # nested per-price-level preopen array is not available without
            # a separate call to the pre-open-market endpoint and is not
            # fabricated here.
            "preOpenMarket": {
                "IEP": meta_data.get("iep"),
                "finalQuantity": meta_data.get("ieq"),
                "Change": meta_data.get("ic_change"),
                "perChange": meta_data.get("ic_pchange"),
                "prevClose": meta_data.get("previousClose"),
                "preopen": [],
            },
        }
        return payload

    if(section!=""):
        payload = nsefetch('https://www.nseindia.com/api/quote-equity?symbol='+symbol+'&section='+section)
        return payload
def nse_expirydetails(payload, i=0, symbol=None):
    expiry_dates = []
    if 'records' in payload:
        expiry_dates = payload['records']['expiryDates']
    elif 'expiryDates' in payload:
        expiry_dates = payload['expiryDates']
    elif 'data' in payload:
        unique_dates = set()
        for entry in payload['data']:
            if 'expiryDate' in entry:
                unique_dates.add(entry['expiryDate'])
        expiry_dates = sorted(list(unique_dates), key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))

    # Filter future dates
    future_expiry_dates = []
    if expiry_dates:
        temp_dates = [datetime.datetime.strptime(date, "%d-%b-%Y").date() for date in expiry_dates]
        future_expiry_dates = sorted([date.strftime("%d-%b-%Y") for date in temp_dates if date >= datetime.datetime.now().date()], key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))

    # Fallback to expiry_list if i is out of range and we can determine the symbol
    if i >= len(future_expiry_dates):
        if not symbol and 'data' in payload and len(payload['data']) > 0:
            # Try to extract symbol from payload data
            first_entry = payload['data'][0]
            symbol = first_entry.get('symbol')
            if not symbol:
                if 'CE' in first_entry and first_entry['CE']:
                    symbol = first_entry['CE'].get('underlying')
                elif 'PE' in first_entry and first_entry['PE']:
                    symbol = first_entry['PE'].get('underlying')
        
        if symbol:
            dates = expiry_list(symbol, type="list")
            if dates:
                # Filter future dates from expiry_list as well
                temp_dates = [datetime.datetime.strptime(date, "%d-%b-%Y").date() for date in dates]
                future_expiry_dates = sorted([date.strftime("%d-%b-%Y") for date in temp_dates if date >= datetime.datetime.now().date()], key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))

    if i >= len(future_expiry_dates):
        return None, None

    currentExpiry = future_expiry_dates[i]
    currentExpiry_dt = datetime.datetime.strptime(currentExpiry, '%d-%b-%Y').date()
    date_today = run_time.date()
    dte = (currentExpiry_dt - date_today).days
    return currentExpiry_dt, dte
def pcr(payload, inp=0):
    ce_oi = 0
    pe_oi = 0
    
    # Identify the data and expiry dates based on structure
    if 'records' in payload:
        # Legacy structure
        data_list = payload['records']['data']
        expiry_dates = payload['records']['expiryDates']
    elif 'data' in payload:
        # New structure
        data_list = payload['data']
        # Extract unique sorted expiry dates from data
        unique_dates = set()
        for entry in data_list:
            ed = entry.get('expiryDate') or entry.get('expiryDates')
            if ed:
                unique_dates.add(ed)
        expiry_dates = sorted(list(unique_dates), key=lambda x: datetime.datetime.strptime(x, "%d-%m-%Y") if "-" in x and x.split("-")[1].isdigit() else datetime.datetime.strptime(x, "%d-%b-%Y"))
    else:
        # If payload is empty or unknown, we can't proceed without fetching
        # But we need a symbol. Try to get it from payload if possible.
        return 0.0

    if not expiry_dates or inp >= len(expiry_dates):
        # Requested index is outside the current payload's scope.
        # Check if we can fetch more data for this specific symbol.
        symbol = payload.get('symbol') or payload.get('records', {}).get('symbol')
        if not symbol and 'data' in payload and len(payload['data']) > 0:
             first = payload['data'][0]
             symbol = first.get('symbol') or (first.get('CE') and first['CE'].get('underlying'))
        
        if symbol and inp > 0:
            # Fetch all expiries to find the target one
            all_expiries = expiry_list(symbol, type="list")
            if inp < len(all_expiries):
                target = all_expiries[inp]
                # Fetch specific expiry data using getOptionChainData
                url = f'https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getOptionChainData&symbol={nsesymbolpurify(symbol)}&params=expiryDate={target}'
                new_payload = nsefetch(url)
                if new_payload and 'data' in new_payload:
                    for entry in new_payload['data']:
                        ce_oi += entry.get('CE', {}).get('openInterest', 0) if entry.get('CE') else 0
                        pe_oi += entry.get('PE', {}).get('openInterest', 0) if entry.get('PE') else 0
                    if ce_oi > 0: return pe_oi / ce_oi
        return 0.0
        
    target_expiry = expiry_dates[inp]
    
    found_data = False
    for i in data_list:
        curr_exp = i.get('expiryDate') or i.get('expiryDates')
        if curr_exp == target_expiry:
            found_data = True
            try:
                if 'CE' in i and i['CE']:
                    ce_oi += i['CE'].get('openInterest', 0)
                if 'PE' in i and i['PE']:
                    pe_oi += i['PE'].get('openInterest', 0)
            except (KeyError, TypeError):
                pass
    
    # If we didn't find any data for the target expiry in the payload,
    # it means the payload was filtered (e.g. by the scrapper). Fetch it now.
    if not found_data:
        symbol = payload.get('symbol') or payload.get('records', {}).get('symbol')
        if symbol:
            url = f'https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getOptionChainData&symbol={nsesymbolpurify(symbol)}&params=expiryDate={target_expiry}'
            new_payload = nsefetch(url)
            if new_payload and 'data' in new_payload:
                for entry in new_payload['data']:
                    ce_oi += entry.get('CE', {}).get('openInterest', 0) if entry.get('CE') else 0
                    pe_oi += entry.get('PE', {}).get('openInterest', 0) if entry.get('PE') else 0

    if ce_oi == 0:
        return 0.0
        
    return pe_oi / ce_oi

#forum.unofficed.com/t/unable-to-find-nse-quote-meta-api/702/4
#Refer https://forum.unofficed.com/t/changed-the-nse-quote-ltp-function/1276
def nse_quote_ltp(symbol,expiryDate="latest",optionType="-",strikePrice=0):
  if(optionType!="-"):
      payload = nse_quote_derivatives(symbol)
  else:
      if any(x in symbol for x in indices):
          payload = nse_quote_derivatives(symbol)
      else:
          payload = nsefetch('https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData&marketType=N&series=EQ&symbol='+symbol)

  lastPrice = 0

  if(optionType=="-"):
    if 'equityResponse' in payload and len(payload['equityResponse']) > 0:
        lastPrice = payload['equityResponse'][0]['orderBook']['lastPrice']
    elif 'data' in payload and len(payload['data']) > 0:
        # For indices, underlyingValue in derivative payload is the current index LTP
        lastPrice = payload['data'][0].get('underlyingValue')
    return lastPrice

  meta = "Options"
  if(optionType=="Fut"): meta = "Futures"
  if(optionType=="PE"):optionType="Put"
  if(optionType=="CE"):optionType="Call"

  if(expiryDate=="latest") or (expiryDate=="next"):
    i = 0 if expiryDate=="latest" else 1
    expiry_dates = []
    
    # Extract from new FNO payload structure
    if 'data' in payload:
        unique_dates = set()
        for entry in payload['data']:
            if 'expiryDate' in entry:
                it = entry.get('instrumentType', '')
                if (meta == "Futures" and "FUT" in it) or (meta == "Options" and "OPT" in it):
                    unique_dates.add(entry['expiryDate'])
        expiry_dates = sorted(list(unique_dates), key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))
    
    # Filter future dates
    future_expiry_dates = []
    if expiry_dates:
        temp_dates = [datetime.datetime.strptime(date, "%d-%b-%Y").date() for date in expiry_dates]
        future_expiry_dates = sorted([date.strftime("%d-%b-%Y") for date in temp_dates if date >= datetime.datetime.now().date()], key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))

    # Fallback to expiry_list
    if i >= len(future_expiry_dates):
        dates = expiry_list(symbol, type="list")
        if dates:
            temp_dates = [datetime.datetime.strptime(date, "%d-%b-%Y").date() for date in dates]
            future_expiry_dates = sorted([date.strftime("%d-%b-%Y") for date in temp_dates if date >= datetime.datetime.now().date()], key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))
    
    if i < len(future_expiry_dates):
        expiryDate = future_expiry_dates[i]
  

  if(optionType!="-"):
      data_list = payload.get('data', [])
      for i in data_list:
        # Check instrument type in identifier or metadata if present
        if meta == "Futures":
            is_match = "FUT" in i.get('instrumentType', '')
        else:
            is_match = "OPT" in i.get('instrumentType', '')
            
        if is_match:
          if(optionType=="Fut"):
              if(i.get('expiryDate')==expiryDate):
                lastPrice = i.get('lastPrice')
                break

          if((optionType=="Put")or(optionType=="Call")):
              # Some APIs have optionType as 'PE'/'CE' or 'Put'/'Call'
              p_opt_type = i.get('optionType')
              if p_opt_type == "PE": p_opt_type = "Put"
              if p_opt_type == "CE": p_opt_type = "Call"
              
              if (i.get("expiryDate")==expiryDate):
                if (p_opt_type==optionType):
                  # strikePrice in payload is often string with padding
                  try:
                      p_strike = float(str(i.get("strikePrice")).strip())
                  except:
                      p_strike = 0
                      
                  if (p_strike==float(strikePrice)):
                    lastPrice = i.get('lastPrice')
                    break

  return lastPrice

# print(nse_quote_ltp("RELIANCE"))
# print(nse_quote_ltp("RELIANCE","latest","Fut"))
# print(nse_quote_ltp("RELIANCE","next","Fut"))
# print(nse_quote_ltp("BANKNIFTY","latest","PE",32000))
# print(nse_quote_ltp("BANKNIFTY","next","PE",32000))
# print(nse_quote_ltp("BANKNIFTY","10-Jun-2021","PE",32000))
# print(nse_quote_ltp("BANKNIFTY","17-Jun-2021","PE",32000))
# print(nse_quote_ltp("RELIANCE","latest","PE",2300))
# print(nse_quote_ltp("RELIANCE","next","PE",2300))

def nse_quote_meta(symbol,expiryDate="latest",optionType="-",strikePrice=0):
  if(optionType!="-"):
      payload = nse_quote_derivatives(symbol)
  else:
      if any(x in symbol for x in indices):
          payload = nse_quote_derivatives(symbol)
      else:
          payload = nsefetch('https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData&marketType=N&series=EQ&symbol='+symbol)

  metadata = {}

  if(optionType=="-"):
      if 'equityResponse' in payload and len(payload['equityResponse']) > 0:
          metadata = payload['equityResponse'][0].get('metaData', {})
      return metadata

  meta = "Options"
  if(optionType=="Fut"): meta = "Futures"
  if(optionType=="PE"):optionType="Put"
  if(optionType=="CE"):optionType="Call"

  if(expiryDate=="latest") or (expiryDate=="next"):
    i = 0 if expiryDate=="latest" else 1
    expiry_dates = []
    if 'data' in payload:
        unique_dates = set()
        for entry in payload['data']:
            if 'expiryDate' in entry:
                it = entry.get('instrumentType', '')
                if (meta == "Futures" and "FUT" in it) or (meta == "Options" and "OPT" in it):
                    unique_dates.add(entry['expiryDate'])
        expiry_dates = sorted(list(unique_dates), key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))
    
    future_expiry_dates = []
    if expiry_dates:
        temp_dates = [datetime.datetime.strptime(date, "%d-%b-%Y").date() for date in expiry_dates]
        future_expiry_dates = sorted([date.strftime("%d-%b-%Y") for date in temp_dates if date >= datetime.datetime.now().date()], key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))

    if i >= len(future_expiry_dates):
        dates = expiry_list(symbol, type="list")
        if dates:
            temp_dates = [datetime.datetime.strptime(date, "%d-%b-%Y").date() for date in dates]
            future_expiry_dates = sorted([date.strftime("%d-%b-%Y") for date in temp_dates if date >= datetime.datetime.now().date()], key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))
    
    if i < len(future_expiry_dates):
        expiryDate = future_expiry_dates[i]
    
    # print(f"DEBUG: Calculated expiryDate={expiryDate}, meta={meta}, optionType={optionType}")

  if(optionType!="-"):
      data_list = payload.get('data', [])
      # print(f"DEBUG: Searching in {len(data_list)} items")
      for i in data_list:
        if meta == "Futures":
            is_match = "FUT" in i.get('instrumentType', '')
        else:
            is_match = "OPT" in i.get('instrumentType', '')
            
        if is_match:
          if(optionType=="Fut"):
              if(i.get('expiryDate')==expiryDate):
                metadata = i
                break

          if((optionType=="Put")or(optionType=="Call")):
              p_opt_type = i.get('optionType')
              if p_opt_type == "PE": p_opt_type = "Put"
              if p_opt_type == "CE": p_opt_type = "Call"
              
              if (i.get("expiryDate")==expiryDate):
                if (p_opt_type==optionType):
                  try:
                      p_strike = float(str(i.get("strikePrice")).strip())
                  except:
                      p_strike = 0
                      
                  if (p_strike==float(strikePrice)):
                    metadata = i
                    break

  return metadata

def nse_optionchain_ltp(payload,strikePrice,optionType,inp=0,intent=""):
    expiry_dates = payload['records']['expiryDates']
    expiry_dates = [datetime.datetime.strptime(date, "%d-%b-%Y").date() for date in expiry_dates]
    expiry_dates = [date.strftime("%d-%b-%Y") for date in expiry_dates if date >= datetime.datetime.now().date()]
    expiryDate=expiry_dates[inp]
    for x in range(len(payload['records']['data'])):
      if((payload['records']['data'][x]['strikePrice']==strikePrice) & (payload['records']['data'][x]['expiryDate']==expiryDate)):
          if(intent==""): return payload['records']['data'][x][optionType]['lastPrice']
          if(intent=="sell"): return payload['records']['data'][x][optionType]['bidprice']
          if(intent=="buy"): return payload['records']['data'][x][optionType]['askPrice']

def _reshape_equity_quote(raw):
    """/api/quote-equity (nse_eq's old data source) is dead (confirmed live
    403 Access Denied, Apache/WAF-style, not an Akamai JS challenge -- there
    is no cookie that fixes it). The live quote page itself now calls
    GetQuoteApi?functionName=getSymbolData instead, which carries nearly all
    the same information, just regrouped. We reshape it back into the old
    quote-equity top-level key names (info/metadata/priceInfo/securityInfo/
    tradeInfo) as closely as the new payload allows, so code written against
    the old shape (payload['priceInfo']['lastPrice'],
    payload['metadata']['pdSymbolPe'], etc.) keeps working. The full
    unmodified NextApi response is also kept under '_raw' for anyone who
    wants the new field names directly."""
    if not raw or 'equityResponse' not in raw or not raw['equityResponse']:
        return raw
    row = raw['equityResponse'][0]
    meta = row.get('metaData', {}) or {}
    sec = row.get('secInfo', {}) or {}
    trade = row.get('tradeInfo', {}) or {}
    price = row.get('priceInfo', {}) or {}
    order = row.get('orderBook', {}) or {}

    reshaped = {
        'info': {
            'symbol': meta.get('symbol'),
            'companyName': meta.get('companyName'),
            'industry': sec.get('basicIndustry'),
            'isin': meta.get('isinCode'),
            'series': meta.get('series'),
        },
        'metadata': {
            **meta,
            'pdSectorPe': sec.get('pdSectorPe'),
            'pdSymbolPe': sec.get('pdSymbolPe'),
            'pdSectorInd': sec.get('pdSectorInd'),
        },
        'priceInfo': {
            'lastPrice': order.get('lastPrice', meta.get('closePrice')),
            'change': meta.get('change'),
            'pChange': meta.get('pChange'),
            'previousClose': meta.get('previousClose'),
            'open': meta.get('open'),
            'close': meta.get('closePrice'),
            'vwap': meta.get('averagePrice'),
            'intraDayHighLow': {'min': meta.get('dayLow'), 'max': meta.get('dayHigh'),
                                 'value': order.get('lastPrice', meta.get('closePrice'))},
            'weekHighLow': {'min': price.get('yearLow'), 'max': price.get('yearHigh'),
                             'minDate': price.get('yearLowDt'), 'maxDate': price.get('yearHightDt')},
        },
        'securityInfo': sec,
        'tradeInfo': trade,
        'preOpenMarket': {},
        '_raw': raw,
    }
    return reshaped


def nse_eq(symbol):
    # /api/quote-equity is a dead endpoint (403 Access Denied, confirmed
    # live -- not fixable with cookies, it's a hard WAF block, not an Akamai
    # JS-sensor challenge). Use the NextApi endpoint the live quote page
    # itself now calls, reshaped to look like the old response.
    symbol = nsesymbolpurify(symbol)
    raw = nsefetch(
        'https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData&marketType=N&series=EQ&symbol=' + symbol
    )
    return _reshape_equity_quote(raw)


def nse_fno(symbol):
    # /api/quote-derivative is likewise dead (404, "Resource not found").
    # nse_quote_derivatives() already calls the live replacement
    # (getSymbolDerivativesData) and is kept working elsewhere in this file,
    # so delegate to it rather than duplicating/half-reshaping a different
    # response shape. Note the returned shape is the new flat
    # strike/expiry-list shape (payload['data'][i]['CE'/'PE']/...), not the
    # old nested quote-derivative shape -- NSE killed the old endpoint
    # outright, so there is no way to reproduce its exact JSON here.
    symbol = nsesymbolpurify(symbol)
    payload = nse_quote_derivatives(symbol)
    if not payload or 'data' not in payload:
        # Not an F&O symbol (or no derivatives data) -- fall back to the
        # equity quote instead of returning an empty/error payload.
        return nse_eq(symbol)
    return payload

def quote_equity(symbol):
    return nse_eq(symbol)

def quote_derivative(symbol):
    return nse_fno(symbol)

def option_chain(symbol):
    return nse_optionchain_scrapper(symbol)

def nse_holidays(type="trading"):
    if(type=="clearing"):
        payload = nsefetch('https://www.nseindia.com/api/holiday-master?type=clearing')
    if(type=="trading"):
        payload = nsefetch('https://www.nseindia.com/api/holiday-master?type=trading')
    return payload

def holiday_master(type="trading"):
    return nse_holidays(type)

def nse_results(index="equities",period="Quarterly"):
    if(index=="equities") or (index=="debt") or (index=="sme"):
        if(period=="Quarterly") or (period=="Annual")or (period=="Half-Yearly")or (period=="Others"):
            payload = nsefetch('https://www.nseindia.com/api/corporates-financial-results?index='+index+'&period='+period)
            return pd.json_normalize(payload)
        else:
            print("Give Correct Period Input")
    else:
        print("Give Correct Index Input")

def nse_events():
    output = nsefetch('https://www.nseindia.com/api/event-calendar')
    return pd.json_normalize(output)

def nse_past_results(symbol):
    symbol = nsesymbolpurify(symbol)
    return nsefetch('https://www.nseindia.com/api/results-comparision?symbol='+symbol)

def expiry_list(symbol, type=""):
    logging.info("Getting Expiry List of: " + symbol)
    symbol = nsesymbolpurify(symbol)
    url = f'https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getOptionChainDropdown&symbol={symbol}'
    payload = nsefetch(url)
    
    if not payload or 'expiryDates' not in payload:
        return [] if type == "list" else pd.DataFrame()

    expiry_dates = payload['expiryDates']
    
    # Format dates from DD-MM-YYYY to DD-Mon-YYYY
    formatted_dates = []
    for d in expiry_dates:
        try:
            dt = datetime.datetime.strptime(d, "%d-%m-%Y")
            formatted_dates.append(dt.strftime("%d-%b-%Y"))
        except:
            formatted_dates.append(d)
    
    if type == "list":
        return formatted_dates
    else:
        # If anything other than "list" is provided (like "df", "pandas", or default), return DataFrame
        return pd.DataFrame({'Date': formatted_dates})


def nse_custom_function_secfno(symbol,attribute="lastPrice"):
    positions = nsefetch('https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O')
    endp = len(positions['data'])
    for x in range(0, endp):
        if(positions['data'][x]['symbol']==symbol.upper()):
            return positions['data'][x][attribute]

def nse_blockdeal():
    payload = nsefetch('https://nseindia.com/api/block-deal')
    return payload

def nse_marketStatus():
    payload = nsefetch('https://nseindia.com/api/marketStatus')
    return payload

def nse_circular(mode="latest"):
    # The old mode="latest" path (nseindia.com/api/latest-circular, no "www.",
    # singular "latest-circular") is NOT an Akamai wall -- it comes back
    # HTTP 200 but with NSE's own "route doesn't exist" body
    # {'error': True, 'status': 500}, confirmed live. NSE renamed the
    # circulars listing page itself from /resources/circulars to
    # /resources/exchange-communication-circulars at some point; a Playwright
    # network capture on that current live page shows it calls
    # https://www.nseindia.com/api/circulars (with optional
    # fromDate/toDate=DD-MM-YYYY params, defaulting to the last 7 days when
    # omitted) for BOTH a "latest" view and a custom-range view -- there is no
    # separate "latest" endpoint anymore. The mode!="latest" branch below
    # already called this correct URL; "latest" just needs routing to the
    # same place instead of the dead legacy path.
    payload = nsefetch('https://www.nseindia.com/api/circulars')
    return payload

def nse_fiidii(mode="pandas"):
    try:
        if(mode=="pandas"):
            return pd.DataFrame(nsefetch('https://www.nseindia.com/api/fiidiiTradeReact'))
        else:
            return nsefetch('https://www.nseindia.com/api/fiidiiTradeReact')
    except:
        logger.info("Pandas is not working for some reason.")
        return nsefetch('https://www.nseindia.com/api/fiidiiTradeReact')

def nsetools_get_quote(symbol):
    payload = nsefetch('https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O')
    for m in range(len(payload['data'])):
        if(payload['data'][m]['symbol']==symbol.upper()):
            return payload['data'][m]


# iislliveblob.niftyindices.com (the old data source for the three functions
# below) is a dead host -- DNS NXDOMAIN, confirmed repeatedly live. There is
# no successor on that domain; /api/allIndices on nseindia.com itself (same
# transport/session as everything else in this file) carries the same -- in
# fact a much larger -- live index universe, so we use that instead and alias
# its "index" field to "indexName" so existing callers keep working.
def _nse_all_indices_as_indexname():
    payload = nsefetch("https://www.nseindia.com/api/allIndices")
    data = payload.get("data", [])
    for row in data:
        if "indexName" not in row:
            row["indexName"] = row.get("index", "")
    return data

def nse_index():
    payload = pd.DataFrame(_nse_all_indices_as_indexname())
    return payload

def nse_get_index_list():
    payload = pd.DataFrame(_nse_all_indices_as_indexname())
    return payload["indexName"].tolist()

def nse_get_index_quote(index):
    data = _nse_all_indices_as_indexname()
    for m in range(len(data)):
        if(data[m]["indexName"] == index.upper()):
            return data[m]

def nse_get_advances_declines(mode="pandas"):
    try:
        if(mode=="pandas"):
            positions = nsefetch('https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O')
            return pd.DataFrame(positions['data'])
        else:
            return nsefetch('https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O')
    except:
        logger.info("Pandas is not working for some reason.")
        return nsefetch('https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O')

def nse_get_top_losers():
    positions = nsefetch('https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O')
    df = pd.DataFrame(positions['data'])
    df = df.sort_values(by="pChange")
    return df.head(5)

def nse_get_top_gainers():
    positions = nsefetch('https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O')
    df = pd.DataFrame(positions['data'])
    df = df.sort_values(by="pChange" , ascending = False)
    return df.head(5)

def nse_get_fno_lot_sizes(symbol="all",mode="list"):
    # github.com/aeron7/nsepythonserver #4 reports this as broken because NSE
    # "discontinued" the report (linking FAOP61157.pdf). Live-verified that's
    # a red herring: archives.nseindia.com/content/fo/fo_mktlots.csv now
    # silently redirects to an unrelated PDF circular (stale link from NSE's
    # archives -> nsearchives host migration), but the real CSV report is
    # still published live, just moved to nsearchives.nseindia.com. That host
    # also needs curl_cffi specifically -- plain `requests`/`pd.read_csv`
    # hang against it even though they work fine against the old
    # archives.nseindia.com host for other CSVs. The column layout changed
    # too: it's now "UNDERLYING,SYMBOL,<expiry1>,<expiry2>,..." with the lot
    # size repeated across every live contract month instead of one lot-size
    # column, so we take the first non-empty month value as the current lot
    # size.
    url = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
    session = _get_nse_session()
    r = session.get(url, headers={"Accept": "text/csv"}, timeout=30)
    if r.status_code != 200:
        raise NSEFetchError(f"nse_get_fno_lot_sizes: HTTP {r.status_code} fetching {url}")
    text = r.text

    if(mode=="list"):
        res_dict = {}
        for line in text.split('\n'):
          if line != '' and re.search(',', line) and (line.casefold().find('symbol') == -1):
              cols = [x.strip() for x in line.split(',')]
              code = cols[1]
              lot = ""
              for c in cols[2:]:
                  if c != "":
                      lot = c
                      break
              if not code or lot == "":
                  continue
              try:
                  res_dict[code] = int(lot)
              except ValueError:
                  continue
        if(symbol=="all"):
            return res_dict
        if(symbol!=""):
            return res_dict[symbol.upper()]

    if(mode=="pandas"):
        import io
        payload = pd.read_csv(io.StringIO(text))
        payload.columns = [c.strip() for c in payload.columns]
        if(symbol=="all"):
            return payload
        else:
            payload = payload[(payload.iloc[:, 1].astype(str).str.strip() == symbol.upper())]
            return payload

def whoistheboss():
    return "subhash"

def indiavix():
    payload = nsefetch("https://www.nseindia.com/api/allIndices")
    for x in range(0, len(payload["data"])):
        if(payload["data"][x]["index"]=="INDIA VIX"):
            return payload["data"][x]["last"]

def index_info(index):
    payload = nsefetch("https://www.nseindia.com/api/allIndices")
    for x in range(0, len(payload["data"])):
        if(payload["data"][x]["index"]==index):
            return payload["data"][x]

import math
from scipy.stats import norm

def black_scholes_dexter(S0,X,t,σ="",r=10,q=0.0,td=365):

  if(σ==""):σ =indiavix()

  S0,X,σ,r,q,t = float(S0),float(X),float(σ/100),float(r/100),float(q/100),float(t/td)
  #https://unofficed.com/black-scholes-model-options-calculator-google-sheet/

  d1 = (math.log(S0/X)+(r-q+0.5*σ**2)*t)/(σ*math.sqrt(t))
  #stackoverflow.com/questions/34258537/python-typeerror-unsupported-operand-types-for-float-and-int

  #stackoverflow.com/questions/809362/how-to-calculate-cumulative-normal-distribution
  Nd1 = (math.exp((-d1**2)/2))/math.sqrt(2*math.pi)
  d2 = d1-σ*math.sqrt(t)
  Nd2 = norm.cdf(d2)
  call_theta =(-((S0*σ*math.exp(-q*t))/(2*math.sqrt(t))*(1/(math.sqrt(2*math.pi)))*math.exp(-(d1*d1)/2))-(r*X*math.exp(-r*t)*norm.cdf(d2))+(q*math.exp(-q*t)*S0*norm.cdf(d1)))/td
  put_theta =(-((S0*σ*math.exp(-q*t))/(2*math.sqrt(t))*(1/(math.sqrt(2*math.pi)))*math.exp(-(d1*d1)/2))+(r*X*math.exp(-r*t)*norm.cdf(-d2))-(q*math.exp(-q*t)*S0*norm.cdf(-d1)))/td
  call_premium =math.exp(-q*t)*S0*norm.cdf(d1)-X*math.exp(-r*t)*norm.cdf(d1-σ*math.sqrt(t))
  put_premium =X*math.exp(-r*t)*norm.cdf(-d2)-math.exp(-q*t)*S0*norm.cdf(-d1)
  call_delta =math.exp(-q*t)*norm.cdf(d1)
  put_delta =math.exp(-q*t)*(norm.cdf(d1)-1)
  gamma =(math.exp(-r*t)/(S0*σ*math.sqrt(t)))*(1/(math.sqrt(2*math.pi)))*math.exp(-(d1*d1)/2)
  vega = ((1/100)*S0*math.exp(-r*t)*math.sqrt(t))*(1/(math.sqrt(2*math.pi))*math.exp(-(d1*d1)/2))
  call_rho =(1/100)*X*t*math.exp(-r*t)*norm.cdf(d2)
  put_rho =(-1/100)*X*t*math.exp(-r*t)*norm.cdf(-d2)

  return call_theta,put_theta,call_premium,put_premium,call_delta,put_delta,gamma,vega,call_rho,put_rho

def equity_history_virgin(symbol,series,start_date,end_date):
    #url="https://www.nseindia.com/api/historical/cm/equity?symbol="+symbol+"&series=[%22"+series+"%22]&from="+str(start_date)+"&to="+str(end_date)+""
    # NOTE: the original /api/historical/cm/equity route is retired on the
    # live site (confirmed HTTP 503 as of 2026, even via curl_cffi). NSE's
    # replacement is /api/historicalOR/cm/equity -- same query params, same
    # response shape (payload['data'] records with CH_TIMESTAMP/
    # CH_CLOSING_PRICE/etc), confirmed live, so this is a plain host-path
    # swap with no downstream parsing changes needed.
    url = 'https://www.nseindia.com/api/historicalOR/cm/equity?symbol=' + symbol + '&series=["' + series + '"]&from=' + start_date + '&to=' + end_date

    payload = nsefetch(url)
    return pd.DataFrame.from_records(payload["data"])

# You shall see beautiful use the logger function.
def equity_history(symbol,series,start_date,end_date):
    #We are getting the input in text. So it is being converted to Datetime object from String.
    start_date = datetime.datetime.strptime(start_date, "%d-%m-%Y")
    end_date = datetime.datetime.strptime(end_date, "%d-%m-%Y")
    logging.info("Starting Date: "+str(start_date))
    logging.info("Ending Date: "+str(end_date))

    #We are calculating the difference between the days
    diff = end_date-start_date
    logging.info("Total Number of Days: "+str(diff.days))
    logging.info("Total FOR Loops in the program: "+str(int(diff.days/40)))
    logging.info("Remainder Loop: " + str(diff.days-(int(diff.days/40)*40)))


    total=pd.DataFrame()
    for i in range (0,int(diff.days/40)):

        temp_date = (start_date+datetime.timedelta(days=(40))).strftime("%d-%m-%Y")
        start_date = datetime.datetime.strftime(start_date, "%d-%m-%Y")

        logging.info("Loop = "+str(i))
        logging.info("====")
        logging.info("Starting Date: "+str(start_date))
        logging.info("Ending Date: "+str(temp_date))
        logging.info("====")

        #total=total.append(equity_history_virgin(symbol,series,start_date,temp_date))
        #total=total.concat(equity_history_virgin(symbol,series,start_date,temp_date))
        total = pd.concat([total, equity_history_virgin(symbol, series, start_date, temp_date)])


        logging.info("Length of the Table: "+ str(len(total)))

        #Preparation for the next loop
        start_date = datetime.datetime.strptime(temp_date, "%d-%m-%Y")


    start_date = datetime.datetime.strftime(start_date, "%d-%m-%Y")
    end_date = datetime.datetime.strftime(end_date, "%d-%m-%Y")

    logging.info("End Loop")
    logging.info("====")
    logging.info("Starting Date: "+str(start_date))
    logging.info("Ending Date: "+str(end_date))
    logging.info("====")

    #total=total.append(equity_history_virgin(symbol,series,start_date,end_date))
    #total=total.concat(equity_history_virgin(symbol,series,start_date,end_date))
    total = pd.concat([total, equity_history_virgin(symbol, series, start_date, end_date)])


    logging.info("Finale")
    logging.info("Length of the Total Dataset: "+ str(len(total)))
    payload = total.iloc[::-1].reset_index(drop=True)
    return payload

def derivative_history_virgin(symbol,start_date,end_date,instrumentType,expiry_date,strikePrice="",optionType=""):

    instrumentType = instrumentType.lower()

    if(instrumentType=="options"):
        instrumentType="OPTSTK"
        if("NIFTY" in symbol): instrumentType="OPTIDX"
        
    if(instrumentType=="futures"):
        instrumentType="FUTSTK"
        if("NIFTY" in symbol): instrumentType="FUTIDX"
        

    #if(((instrumentType=="OPTIDX")or (instrumentType=="OPTSTK")) and (expiry_date!="")):
    if(strikePrice!=""):
        strikePrice = "%.2f" % strikePrice
        strikePrice = str(strikePrice)

    # /api/historical/fo/derivatives is retired (HTTP 503 live); the
    # confirmed-working replacement is /api/historicalOR/fo/derivatives with
    # the same query params and response shape.
    nsefetch_url = "https://www.nseindia.com/api/historicalOR/fo/derivatives?&from="+str(start_date)+"&to="+str(end_date)+"&optionType="+optionType+"&strikePrice="+strikePrice+"&expiryDate="+expiry_date+"&instrumentType="+instrumentType+"&symbol="+symbol+""
    payload = nsefetch(nsefetch_url)
    logging.info(nsefetch_url)
    logging.info(payload)
    return pd.DataFrame.from_records(payload["data"])

def derivative_history(symbol,start_date,end_date,instrumentType,expiry_date,strikePrice="",optionType=""):
    #We are getting the input in text. So it is being converted to Datetime object from String.
    start_date = datetime.datetime.strptime(start_date, "%d-%m-%Y")
    end_date = datetime.datetime.strptime(end_date, "%d-%m-%Y")
    logging.info("Starting Date: "+str(start_date))
    logging.info("Ending Date: "+str(end_date))

    #We are calculating the difference between the days
    diff = end_date-start_date
    logging.info("Total Number of Days: "+str(diff.days))
    logging.info("Total FOR Loops in the program: "+str(int(diff.days/40)))
    logging.info("Remainder Loop: " + str(diff.days-(int(diff.days/40)*40)))


    total=pd.DataFrame()
    for i in range (0,int(diff.days/40)):

        temp_date = (start_date+datetime.timedelta(days=(40))).strftime("%d-%m-%Y")
        start_date = datetime.datetime.strftime(start_date, "%d-%m-%Y")

        logging.info("Loop = "+str(i))
        logging.info("====")
        logging.info("Starting Date: "+str(start_date))
        logging.info("Ending Date: "+str(temp_date))
        logging.info("====")

        #total=total.append(derivative_history_virgin(symbol,start_date,temp_date,instrumentType,expiry_date,strikePrice,optionType))
        #total=total.concat([total, derivative_history_virgin(symbol,start_date,temp_date,instrumentType,expiry_date,strikePrice,optionType)])
        total = pd.concat([total, derivative_history_virgin(symbol, start_date, temp_date, instrumentType, expiry_date, strikePrice, optionType)])


        logging.info("Length of the Table: "+ str(len(total)))

        #Preparation for the next loop
        start_date = datetime.datetime.strptime(temp_date, "%d-%m-%Y")


    start_date = datetime.datetime.strftime(start_date, "%d-%m-%Y")
    end_date = datetime.datetime.strftime(end_date, "%d-%m-%Y")

    logging.info("End Loop")
    logging.info("====")
    logging.info("Starting Date: "+str(start_date))
    logging.info("Ending Date: "+str(end_date))
    logging.info("====")

    #total=total.append(derivative_history_virgin(symbol,start_date,end_date,instrumentType,expiry_date,strikePrice,optionType))
    #total = total.concat([total, derivative_history_virgin(symbol,start_date,end_date,instrumentType,expiry_date,strikePrice,optionType)])
    total = pd.concat([total, derivative_history_virgin(symbol, start_date, end_date, instrumentType, expiry_date, strikePrice, optionType)])



    logging.info("Finale")
    logging.info("Length of the Total Dataset: "+ str(len(total)))
    payload = total.iloc[::-1].reset_index(drop=True)
    return payload


def expiry_history(symbol,start_date="",end_date="",type="options"):
    if(end_date==""):end_date=end_date
    # Same retirement as derivative_history_virgin()/equity_history_virgin()
    # above -- /api/historical/* is gone, /api/historicalOR/* is the working
    # replacement with an identical response shape.
    nsefetch_url = "https://www.nseindia.com/api/historicalOR/fo/derivatives/meta?&from="+start_date+"&to="+end_date+"&symbol="+symbol+""
    payload = nsefetch(nsefetch_url)

    #print(payload)

    for key, value in payload['expiryDatesByInstrument'].items():
      if type.lower() == "options" and "OPT" in key:
          payload_data = payload['expiryDatesByInstrument'][key]
          break
      elif type.lower() == "futures" and "FUT" in key:
          payload_data =  payload['expiryDatesByInstrument'][key]
          break
    
    # Convert start_date and end_date to datetime objects
    start_date = datetime.datetime.strptime(start_date, "%d-%m-%Y")
    end_date = datetime.datetime.strptime(end_date, "%d-%m-%Y")

    # Initialize an empty list to store filtered dates
    filtered_date_payload = []

    # Initialize a flag to check if the first date after end_date has been added
    added_after_end_date = False    

    # Iterate through date_payload and filter dates within the range
    for date_str in payload_data:
        date_obj = datetime.datetime.strptime(date_str, "%d-%b-%Y")
        if start_date <= date_obj <= end_date:
            filtered_date_payload.append(date_str)
        elif date_obj > end_date and not added_after_end_date:
            filtered_date_payload.append(date_str)
            added_after_end_date = True
    
    return filtered_date_payload

# # Nifty Indicies Site
#
# niftyindices.com is a completely separate host/site from nseindia.com (no
# Akamai Bot Manager symptoms observed here) -- but it was fully redesigned
# onto a different CMS at some point: the old ASP.NET WebMethods under
# `niftyindices.com/Backpage.aspx/*` (returning `{"d": "<json string>"}`) are
# gone, and POSTing to them now just returns the site's homepage HTML, which
# is exactly github.com/aeron7/nsepython issue #78's
# `JSONDecodeError: Expecting value: line 1 column 2 (char 1)`.
#
# The working replacement (confirmed live, ported from the already-fixed
# nsepython sibling repo) is `www.niftyindices.com/BackPage/*` (note: `www.`
# + `BackPage` not `Backpage.aspx`), which wants a short session warm-up
# first (visiting the historical-data report page) and returns a direct JSON
# array rather than the old `{"d": "..."}` wrapper.

niftyindices_headers = {
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'en-US,en;q=0.9,hi;q=0.8',
    'Content-Type': 'application/json; charset=UTF-8',
    'Origin': 'https://www.niftyindices.com',
    'Referer': 'https://www.niftyindices.com/reports/historical-data',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'X-Requested-With': 'XMLHttpRequest',
    'sec-ch-ua': '"Not;A=Brand";v="8", "Chromium";v="130", "Google Chrome";v="130"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
}

_niftyindices_session = None
_niftyindices_warmed = False


def _get_niftyindices_session():
    global _niftyindices_session, _niftyindices_warmed
    if _niftyindices_session is None:
        _niftyindices_session = requests.Session()
    if not _niftyindices_warmed:
        try:
            _niftyindices_session.get(
                "https://www.niftyindices.com/reports/historical-data",
                headers=niftyindices_headers, timeout=15,
            )
            _niftyindices_warmed = True
        except Exception as e:
            logging.warning("niftyindices.com session warm-up failed/partial: %s", e)
    return _niftyindices_session


def _niftyindices_fetch(endpoint, symbol, start_date, end_date):
    session = _get_niftyindices_session()
    data = {'cinfo': "{'name':'" + symbol + "','startDate':'" + start_date + "','endDate':'" + end_date + "','indexName':'" + symbol + "'}"}
    response = session.post(
        f"https://www.niftyindices.com/BackPage/{endpoint}",
        headers=niftyindices_headers, json=data, timeout=20,
    )
    text = response.text.strip()
    if text.startswith('<!DOCTYPE') or text.startswith('<html') or text == "":
        raise NSEFetchError(
            f"niftyindices.com/BackPage/{endpoint} returned HTML/empty instead of JSON "
            f"(HTTP {response.status_code}) -- the site may be down or have changed again."
        )
    try:
        payload = response.json()
    except ValueError:
        raise NSEFetchError(
            f"niftyindices.com/BackPage/{endpoint}: non-JSON body (HTTP {response.status_code})"
        )
    # Old API wrapped the payload as {"d": "<json string>"}; the new one
    # returns the array directly. Support both so this keeps working if
    # niftyindices.com ever reverts/mixes the two shapes.
    if isinstance(payload, dict) and "d" in payload:
        payload = json.loads(payload["d"])
    return pd.DataFrame.from_records(payload)


def index_history(symbol,start_date,end_date):
    return _niftyindices_fetch("getHistoricaldatatabletoString", symbol, start_date, end_date)

def index_pe_pb_div(symbol,start_date,end_date):
    return _niftyindices_fetch("getpepbHistoricaldataDBtoString", symbol, start_date, end_date)

def index_total_returns(symbol,start_date,end_date):
    return _niftyindices_fetch("getTotalReturnIndexString", symbol, start_date, end_date)

def get_bhavcopy(date):
    date = date.replace("-","")
    payload=pd.read_csv("https://archives.nseindia.com/products/content/sec_bhavdata_full_"+date+".csv")
    return payload

def get_bulkdeals():
    payload=pd.read_csv("https://archives.nseindia.com/content/equities/bulk.csv")
    return payload

def get_blockdeals():
    payload=pd.read_csv("https://archives.nseindia.com/content/equities/block.csv")
    return payload

#Request from subhash
## https://unofficed.com/how-to-find-the-beta-of-indian-stocks-using-python/
def get_beta_df_maker(symbol,days):
    if("NIFTY" in symbol):
        end_date = datetime.datetime.now().strftime("%d-%b-%Y")
        end_date = str(end_date)

        start_date = (datetime.datetime.now()- datetime.timedelta(days=days)).strftime("%d-%b-%Y")
        start_date = str(start_date)

        df2=index_history(symbol,start_date,end_date)
        df2["daily_change"]=df2["CLOSE"].astype(float).pct_change()
        df2=df2[['HistoricalDate','daily_change']]
        df2 = df2.iloc[1: , :]
        return df2
    else:
        end_date = datetime.datetime.now().strftime("%d-%m-%Y")
        end_date = str(end_date)

        start_date = (datetime.datetime.now()- datetime.timedelta(days=days)).strftime("%d-%m-%Y")
        start_date = str(start_date)

        df = equity_history(symbol,"EQ",start_date,end_date)

        df["daily_change"]=df["CH_CLOSING_PRICE"].pct_change()
        df=df[['CH_TIMESTAMP','daily_change']]
        df = df.iloc[1: , :] #thispointer.com/drop-first-row-of-pandas-dataframe-3-ways/
        return df

def getbeta(symbol,days=365,symbol2="NIFTY 50"):
    return get_beta(symbol,days,symbol2)

def get_beta(symbol,days=365,symbol2="NIFTY 50"):
    #Default is 248 days. (Input of Subhash)
    df = get_beta_df_maker(symbol,days)
    df2 = get_beta_df_maker(symbol2,days)

    x=df["daily_change"].tolist()
    y=df2["daily_change"].tolist()
    #stackoverflow.com/questions/42670055/is-there-any-better-way-to-calculate-the-covariance-of-two-lists-than-this
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    covariance = sum((a - mean_x) * (b - mean_y) for (a,b) in zip(x,y)) / len(x)

    mean = sum(y) / len(y)
    variance = sum((i - mean) ** 2 for i in y) / len(y)

    beta = covariance/variance
    return round(beta,3)

def nse_preopen(key="NIFTY",type="pandas"):
    payload = nsefetch("https://www.nseindia.com/api/market-data-pre-open?key="+key+"")
    if(type=="pandas"):
        payload = pd.DataFrame(payload['data'])
        payload  = pd.json_normalize(payload['metadata'])
        return payload
    else:
        return payload

#By Avinash https://forum.unofficed.com/t/nsepython-documentation/376/102?u=dexter
def nse_preopen_movers(key="FO",filter=1.5):
    preOpen_gainer=nse_preopen(key)
    return preOpen_gainer[preOpen_gainer['pChange'] >1.5],preOpen_gainer[preOpen_gainer['pChange'] <-1.5]

# type = "securities"
# type = "etf"
# type = "sme"
#
# sort = "volume"
# sort = "value"

def nse_most_active(type="securities",sort="value"):
    payload = nsefetch("https://www.nseindia.com/api/live-analysis-most-active-"+type+"?index="+sort+"")
    payload = pd.DataFrame(payload["data"])
    return payload


def nse_eq_symbols():
    #https://forum.unofficed.com/t/feature-request-stocklist-api/1073/11
    eq_list_pd = pd.read_csv('https://archives.nseindia.com/content/equities/EQUITY_L.csv')
    return eq_list_pd['SYMBOL'].tolist()

def nse_price_band_hitters(bandtype="both",view="AllSec"):
  payload = nsefetch("https://www.nseindia.com/api/live-analysis-price-band-hitter")
  
  #bandtype can be upper, lower, both
  #view can be AllSec,SecGtr20,SecLwr20
  return pd.DataFrame(payload[bandtype][view]["data"])

def nse_largedeals(mode="bulk_deals"):
  payload = nsefetch('https://www.nseindia.com/api/snapshot-capital-market-largedeal')
  if(mode=="bulk_deals"):
    return pd.DataFrame(payload["BULK_DEALS_DATA"])
  if(mode=="short_deals"):
    return pd.DataFrame(payload["SHORT_DEALS_DATA"])
  if(mode=="block_deals"):
    return pd.DataFrame(payload["BLOCK_DEALS_DATA"])

def nse_largedeals_historical(from_date, to_date, mode="bulk_deals"):
    # The old /api/historical/{bulk-deals,short-selling,block-deals} family is
    # retired on the live site (confirmed HTTP 503 straight from NSE's origin
    # -- not an Akamai bot-challenge: the 503 body is a tiny generic Apache
    # ErrorDocument page returned with a consistent ~20-30ms *origin* timing
    # on every single attempt, with or without warm-up/referer variations,
    # which is the signature of a dead backend route rather than a solvable
    # JS sensor wall).
    #
    # Found the real, current replacement by driving NSE's own "Bulk Deals/
    # Block Deals/ Short Selling Archives" report page
    # (https://www.nseindia.com/report-detail/display-bulk-and-block-deals)
    # with Playwright and capturing what it actually calls when you click
    # Go: `/api/historicalOR/bulk-block-short-deals?optionType=<mode>&from=
    # ..&to=..` -- same host-prefix swap pattern as equity/derivatives above,
    # just a different path and param name (`optionType=`, not a path
    # segment), confirmed live for all three modes. Response shape is the
    # same `{"data": [...]}` the old endpoint returned, just with a different
    # (current) NSE column-name scheme:
    #   bulk_deals/block_deals -> BD_DT_DATE, BD_DT_ORDER, BD_SYMBOL,
    #                              BD_SCRIP_NAME, BD_CLIENT_NAME, BD_BUY_SELL,
    #                              BD_QTY_TRD, BD_TP_WATP, BD_REMARKS
    #   short_deals            -> SS_DATE, SS_DATE_ORDER, SS_SYMBOL, SS_NAME,
    #                              SS_QTY
    if mode == "bulk_deals":
        option_type = "bulk_deals"
    elif mode == "short_deals":
        option_type = "short_selling"
    elif mode == "block_deals":
        option_type = "block_deals"
    else:
        option_type = mode

    url = ('https://www.nseindia.com/api/historicalOR/bulk-block-short-deals'
           '?optionType=' + option_type + '&from=' + from_date + '&to=' + to_date)
    logging.info("Fetching " + str(url))
    payload = nsefetch(url)
    return pd.DataFrame(payload["data"])

#https://forum.unofficed.com/t/feature-request-nse-fno-participant-wise-oi/1179/7
#print(get_fao_participant_oi("04-06-2021"))
def get_fao_participant_oi(date):
    date = date.replace("-","")
    payload=pd.read_csv("https://archives.nseindia.com/content/nsccl/fao_participant_oi_"+date+".csv")
    return payload

#https://forum.unofficed.com/t/how-to-check-if-the-market-is-open-today-or-not/1268/1
def is_market_open(segment = "FO"): #COM,CD,CB,CMOT,COM,FO,IRD,MF,NDM,NTRP,SLBS
    
    holiday_json = nse_holidays()[segment]

    # Get today's date in the format 'dd-Mon-yyyy'
    today_date = datetime.date.today().strftime('%d-%b-%Y')

    # Check if today's date is in the holiday_json. NOTE: this previously
    # returned on the FIRST holiday-list entry regardless of whether it
    # matched today, so any date after the list's first entry was reported
    # as "open" even on a real holiday -- it needs to scan every entry before
    # concluding the market is open.
    for holiday in holiday_json:
        if holiday['tradingDate'] == today_date:
            print(f"Market is closed today because of {holiday['description']}")
            return False
    print("FNO Market is open today. Have a Nice Trade!")
    return True

def nse_expirydetails_by_symbol(symbol,meta ="Futures",i=0):
    payload = nse_quote_derivatives(symbol)
    expiry_dates = []

    # Extract from new FNO payload structure
    if 'data' in payload:
        unique_dates = set()
        for entry in payload['data']:
            if 'expiryDate' in entry:
                # Filter by meta type if possible, though 'data' usually contains all
                # To be precise, we can check instrumentType
                it = entry.get('instrumentType', '')
                if (meta == "Futures" and "FUT" in it) or (meta == "Options" and "OPT" in it):
                    unique_dates.add(entry['expiryDate'])
        expiry_dates = sorted(list(unique_dates), key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))

    # Filter future dates
    future_expiry_dates = []
    if expiry_dates:
        temp_dates = [datetime.datetime.strptime(date, "%d-%b-%Y").date() for date in expiry_dates]
        future_expiry_dates = sorted([date.strftime("%d-%b-%Y") for date in temp_dates if date >= datetime.datetime.now().date()], key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))

    # Fallback to expiry_list if i is out of range
    if i >= len(future_expiry_dates):
        dates = expiry_list(symbol, type="list")
        if dates:
            temp_dates = [datetime.datetime.strptime(date, "%d-%b-%Y").date() for date in dates]
            future_expiry_dates = sorted([date.strftime("%d-%b-%Y") for date in temp_dates if date >= datetime.datetime.now().date()], key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"))

    if i >= len(future_expiry_dates):
        return None, None

    currentExpiry = future_expiry_dates[i]
    currentExpiry_dt = datetime.datetime.strptime(currentExpiry, '%d-%b-%Y').date()
    date_today = run_time.date()
    dte = (currentExpiry_dt - date_today).days
    return currentExpiry_dt, dte

def security_wise_archive(from_date, to_date, symbol, series="ALL"):
    # The old /api/historical/securityArchives route is retired on the live
    # site (confirmed HTTP 503 straight from NSE's origin -- same dead-route
    # signature as nse_largedeals_historical() above, not a solvable Akamai
    # challenge: tiny generic Apache ErrorDocument body, consistent fast
    # origin timing on every attempt regardless of warm-up/referer).
    #
    # Found the real, current replacement by driving NSE's own "Security-wise
    # Archives (Equities)" report page
    # (https://www.nseindia.com/report-detail/eq_security) with Playwright
    # and capturing what it actually calls when you click Go:
    # `/api/historicalOR/generateSecurityWiseHistoricalData?from=..&to=..&
    # symbol=..&type=..&series=..` -- same host-prefix-swap family as
    # equity_history()/derivative_history() above, just a different path and
    # `type=` instead of `dataType=`. Confirmed live: response shape is the
    # same `{"data": [...]}` with the same CH_*/COP_DELIV_* column names the
    # old endpoint used (cross-checked against equity_history()'s numbers for
    # the same symbol/dates -- exact match).
    base_url = "https://www.nseindia.com/api/historicalOR/generateSecurityWiseHistoricalData"
    url = f"{base_url}?from={from_date}&to={to_date}&symbol={symbol.upper()}&type=priceVolumeDeliverable&series={series.upper()}"
    payload = nsefetch(url)
    return pd.DataFrame(payload['data'])
