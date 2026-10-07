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
# Round-3 fix: this list is the ONLY thing that tells nse_quote()/
# nse_quote_ltp()/nse_quote_meta()/fnolist() that a symbol is an index
# product rather than an equity/stock -- it is not derived live from
# anything, so it silently goes stale as NSE adds new tradable index
# derivatives. Confirmed live (2026-10-07): MIDCPNIFTY (launched 2024) and
# NIFTYNXT50 both have live, actively-traded option chains right now
# (getSymbolDerivativesData returns real CE/PE records for both), but
# neither was in this list -- which made nse_quote_derivatives("MIDCPNIFTY")
# report "not in derivatives list" and nse_quote_ltp("MIDCPNIFTY") silently
# return 0 instead of the real index value, with no error either way.
# Added both below. (If NSE launches further index derivatives later, they
# will need adding here too -- there is no live endpoint this project found
# that enumerates "index symbols" the way fnolist() enumerates equity F&O
# symbols.)
indices = ['NIFTY','FINNIFTY','BANKNIFTY','MIDCPNIFTY','NIFTYNXT50']

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
    # Round-3 fix: the membership check below was already correctly
    # case-insensitive (symbol.upper() in fnolist()), but the URL fetched on
    # a pass was still built from the ORIGINAL, possibly-lowercase `symbol`.
    # The live getSymbolDerivativesData endpoint is itself case-sensitive and
    # returns a 200 OK with an empty 'data': [] list for a lowercase symbol
    # (confirmed live: nse_quote_derivatives("sbin") silently returned
    # {'data': [], 'timestamp': ''} -- a plausible-looking "no data" response,
    # not an error) -- so always fetch using the uppercased symbol.
    symbol_upper = symbol.upper()
    if symbol_upper in fnolist():
        payload = nsefetch('https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolDerivativesData&symbol='+symbol_upper)
        return payload
    else:
        return {"error": f"{symbol} is not in derivatives list."}

def nse_quote(symbol,section=""):
    #https://forum.unofficed.com/t/nsetools-get-quote-is-not-fetching-delivery-data-and-delivery-can-you-include-this-as-part-of-feature-request/1115/4    
    symbol = nsesymbolpurify(symbol)

    if(section==""):
        # Round-3 fix: this index-vs-equity routing check was case-sensitive
        # (confirmed live: nse_quote("banknifty") 404'd by being routed to
        # the equity endpoint instead of the index/derivatives one) -- check
        # against the uppercased symbol, and fetch using the uppercased
        # symbol too (the live endpoint is itself case-sensitive).
        if any(x in symbol.upper() for x in indices):
            payload = nsefetch('https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolDerivativesData&symbol='+symbol.upper())
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

    # Round-3 fix: every section value other than "trade_info" (e.g. the old
    # quote-equity top-level keys "info"/"metadata"/"priceInfo"/
    # "securityInfo"/"industryInfo"/"preOpenMarket" -- see the EquityDetails
    # shape documented in hi-imcodeman/stock-nse-india's src/interface.ts,
    # the historical TS reference for this exact API) used to fall straight
    # through to the dead `/api/quote-equity?section=X` route below and raise
    # NSEFetchError(...HTTP 403...) unconditionally -- confirmed live, the
    # route is gone for every section value, not just trade_info. Every one
    # of these sections is already reconstructable from the same no-section
    # GetQuoteApi?functionName=getSymbolData payload fetched above (that's
    # exactly what _reshape_equity_quote() below already does for nse_eq()),
    # so remap them the same way trade_info was remapped instead of hitting
    # a route that can never succeed.
    _equity_detail_sections = {
        "info", "metadata", "priceInfo", "securityInfo", "industryInfo",
        "preOpenMarket",
    }
    if section in _equity_detail_sections:
        base_payload = nse_quote(symbol, section="")
        if 'equityResponse' not in base_payload or not base_payload['equityResponse']:
            # Index/derivative symbols have no cash-market EquityDetails
            # shape to slice a section out of.
            raise NSEFetchError(
                f"nse_quote({symbol!r}, section={section!r}): no equity "
                f"'section' data exists for index/derivative symbols -- use "
                f"nse_quote_derivatives(symbol) or nse_quote(symbol) (no "
                f"section) instead."
            )
        if section == "industryInfo":
            sec_info = base_payload['equityResponse'][0].get('secInfo', {}) or {}
            return {
                "macro": sec_info.get("macro"),
                "sector": sec_info.get("sector"),
                "industry": sec_info.get("industryInfo"),
                "basicIndustry": sec_info.get("basicIndustry"),
            }
        if section == "preOpenMarket":
            # _reshape_equity_quote() hardcodes 'preOpenMarket' to {} (it's
            # built for nse_eq(), which doesn't need it) -- that would make
            # this branch repeat the exact "silently empty" failure mode
            # this round-3 fix exists to remove. Build it from metaData the
            # same way the trade_info branch above already does: only the
            # scalar pre-open fields the new payload actually carries (see
            # the fidelity-gap note on the trade_info branch above -- the
            # old nested per-price-level preopen array has no equivalent in
            # the new payload and is not fabricated here).
            meta_data = base_payload['equityResponse'][0].get('metaData', {}) or {}
            return {
                "IEP": meta_data.get("iep"),
                "finalQuantity": meta_data.get("ieq"),
                "Change": meta_data.get("ic_change"),
                "perChange": meta_data.get("ic_pchange"),
                "prevClose": meta_data.get("previousClose"),
                "preopen": [],
            }
        reshaped = _reshape_equity_quote(base_payload)
        return reshaped.get(section, {})

    if(section!=""):
        # Any section value outside the known set above: the old generic
        # /api/quote-equity?section=X passthrough is dead on the live site
        # (confirmed HTTP 403) with no known live replacement for an
        # unrecognized section name -- raise clearly instead of hitting a
        # route that can never succeed.
        raise NSEFetchError(
            f"nse_quote({symbol!r}, section={section!r}): unrecognized "
            f"section. Supported: '' (full quote), 'trade_info', and "
            f"{sorted(_equity_detail_sections)}."
        )
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
def _pcr_accumulate_oi(entries):
    """Sum CE/PE openInterest across `entries`, supporting BOTH option-chain
    shapes this module's own functions hand to pcr():
      1. "grouped" -- each entry is one (strike, expiry) with nested CE/PE
         sub-dicts (nse_optionchain_scrapper()/option_chain()'s 'data' list,
         and the legacy 'records' shape).
      2. "flat" -- each entry IS a single CE-or-PE contract leg directly,
         with its own 'optionType'/'openInterest' fields and no nested
         CE/PE at all (nse_quote_derivatives()/nse_quote() for an index --
         confirmed live: getSymbolDerivativesData returns this shape).
    Round-3 fix: the previous version only handled shape 1 -- fed shape 2 it
    silently accumulated nothing (no 'CE'/'PE' key ever present on a flat
    leg) and pcr() returned a dangerously-wrong-looking 0.0 instead of the
    real ratio. Detected per-entry (not per-payload) so a mixed/either shape
    always works."""
    ce_oi = 0
    pe_oi = 0
    for entry in entries:
        if ('CE' in entry) or ('PE' in entry):
            ce = entry.get('CE')
            pe = entry.get('PE')
            if ce:
                ce_oi += ce.get('openInterest', 0) or 0
            if pe:
                pe_oi += pe.get('openInterest', 0) or 0
        else:
            ot = entry.get('optionType')
            if ot == 'CE':
                ce_oi += entry.get('openInterest', 0) or 0
            elif ot == 'PE':
                pe_oi += entry.get('openInterest', 0) or 0
    return ce_oi, pe_oi


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
        # Genuinely unrecognized shape (neither 'records' nor 'data') --
        # raise clearly instead of silently returning a wrong-looking 0.0
        # that looks like a legitimate "all puts, no calls" ratio.
        raise NSEFetchError(
            f"pcr(): unrecognized option-chain payload shape -- expected a "
            f"'records' or 'data' key, got keys {list(payload.keys())}."
        )

    if not expiry_dates or inp >= len(expiry_dates):
        # Requested index is outside the current payload's scope.
        # Check if we can fetch more data for this specific symbol.
        symbol = payload.get('symbol') or payload.get('records', {}).get('symbol')
        if not symbol and 'data' in payload and len(payload['data']) > 0:
             first = payload['data'][0]
             # Round-3 fix: a flat per-leg entry carries 'underlying'
             # directly (no nested 'CE' sub-dict to dig it out of) -- the
             # old `first.get('CE') and first['CE'].get('underlying')` was
             # always None against that shape, so this symbol lookup (and
             # therefore the whole refetch-another-expiry fallback below)
             # silently never fired for exactly the payload shape this
             # branch exists to handle.
             symbol = (
                 first.get('symbol')
                 or first.get('underlying')
                 or (first.get('CE') and first['CE'].get('underlying'))
                 or (first.get('PE') and first['PE'].get('underlying'))
             )

        if symbol and inp > 0:
            # Fetch all expiries to find the target one
            all_expiries = expiry_list(symbol, type="list")
            if inp < len(all_expiries):
                target = all_expiries[inp]
                # Fetch specific expiry data using getOptionChainData
                url = f'https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getOptionChainData&symbol={nsesymbolpurify(symbol)}&params=expiryDate={target}'
                new_payload = nsefetch(url)
                if new_payload and 'data' in new_payload:
                    ce_oi, pe_oi = _pcr_accumulate_oi(new_payload['data'])
                    if ce_oi > 0: return pe_oi / ce_oi
        return 0.0

    target_expiry = expiry_dates[inp]

    matching_entries = [
        i for i in data_list
        if (i.get('expiryDate') or i.get('expiryDates')) == target_expiry
    ]
    found_data = len(matching_entries) > 0
    ce_oi, pe_oi = _pcr_accumulate_oi(matching_entries)

    # If we didn't find any data for the target expiry in the payload,
    # it means the payload was filtered (e.g. by the scrapper). Fetch it now.
    if not found_data:
        symbol = payload.get('symbol') or payload.get('records', {}).get('symbol')
        if not symbol and data_list:
            first = data_list[0]
            symbol = (
                first.get('symbol')
                or first.get('underlying')
                or (first.get('CE') and first['CE'].get('underlying'))
                or (first.get('PE') and first['PE'].get('underlying'))
            )
        if symbol:
            url = f'https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getOptionChainData&symbol={nsesymbolpurify(symbol)}&params=expiryDate={target_expiry}'
            new_payload = nsefetch(url)
            if new_payload and 'data' in new_payload:
                ce_oi, pe_oi = _pcr_accumulate_oi(new_payload['data'])

    if ce_oi == 0:
        return 0.0

    return pe_oi / ce_oi

#forum.unofficed.com/t/unable-to-find-nse-quote-meta-api/702/4
#Refer https://forum.unofficed.com/t/changed-the-nse-quote-ltp-function/1276
def nse_quote_ltp(symbol,expiryDate="latest",optionType="-",strikePrice=0):
  if(optionType!="-"):
      payload = nse_quote_derivatives(symbol)
  else:
      # Round-3 fix: case-sensitive index check (confirmed live:
      # nse_quote_ltp("banknifty")/("nifty") 404'd; nse_quote_ltp("MIDCPNIFTY")
      # silently returned 0 because MIDCPNIFTY wasn't in `indices` at all --
      # both causes fixed here: case-insensitive check + updated `indices`).
      if any(x in symbol.upper() for x in indices):
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
      # Round-3 fix: same case-sensitive-index-check bug as nse_quote_ltp()
      # above -- see that fix's comment.
      if any(x in symbol.upper() for x in indices):
          payload = nse_quote_derivatives(symbol)
      else:
          payload = nsefetch('https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData&marketType=N&series=EQ&symbol='+symbol)

  metadata = {}

  if(optionType=="-"):
      if 'equityResponse' in payload and len(payload['equityResponse']) > 0:
          metadata = payload['equityResponse'][0].get('metaData', {})
          return metadata
      # Round-3 fix: for an index/derivative symbol (any x in `indices`),
      # `payload` is nse_quote_derivatives()'s flat per-leg 'data' list --
      # it has no 'equityResponse' key at all, so this used to fall through
      # to the bare `metadata = {}` default and return an empty dict with no
      # indication anything went wrong (confirmed live:
      # nse_quote_meta("NIFTY") always returned {}). There's no single
      # per-symbol metaData record in that shape, but the underlying's
      # current value/timestamp -- the one genuinely symbol-level fact every
      # leg in the list agrees on -- IS available, so return that instead of
      # an empty dict.
      if 'data' in payload and len(payload['data']) > 0:
          first = payload['data'][0]
          return {
              "symbol": first.get("underlying", symbol),
              "underlyingValue": first.get("underlyingValue"),
              "timestamp": payload.get("timestamp"),
          }
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
    # Round-3 fix: this function hardcoded the legacy payload['records']
    # shape only. This module's own current live option-chain source --
    # nse_optionchain_scrapper()/option_chain() -- returns data grouped
    # under a 'data' key instead (same per-strike/expiry CE+PE grouping,
    # just a different top-level key), with strikePrice as a
    # whitespace-padded STRING (e.g. "   22600.00") instead of a number.
    # Confirmed live: calling this function with either function's own
    # output raised a bare KeyError('records') every time -- i.e. it was
    # unconditionally broken against every payload this module can actually
    # hand it today. Support both shapes, and both string/numeric strike
    # inputs.
    if 'records' in payload:
        expiry_dates_raw = payload['records']['expiryDates']
        data_list = payload['records']['data']
    elif 'data' in payload:
        data_list = payload['data']
        expiry_dates_raw = sorted(
            {d.get('expiryDate') or d.get('expiryDates') for d in data_list
             if d.get('expiryDate') or d.get('expiryDates')},
            key=lambda x: datetime.datetime.strptime(x, "%d-%b-%Y"),
        )
    else:
        raise NSEFetchError(
            f"nse_optionchain_ltp(): unrecognized option-chain payload shape "
            f"-- expected a 'records' or 'data' key, got keys "
            f"{list(payload.keys())}."
        )

    expiry_dates = [datetime.datetime.strptime(date, "%d-%b-%Y").date() for date in expiry_dates_raw]
    expiry_dates = [date.strftime("%d-%b-%Y") for date in expiry_dates if date >= datetime.datetime.now().date()]
    if inp >= len(expiry_dates):
        raise NSEFetchError(
            f"nse_optionchain_ltp(): inp={inp} is out of range -- only "
            f"{len(expiry_dates)} future expiry date(s) available in this "
            f"payload."
        )
    expiryDate=expiry_dates[inp]

    try:
        target_strike = float(str(strikePrice).strip())
    except (TypeError, ValueError):
        target_strike = strikePrice

    for entry in data_list:
        entry_expiry = entry.get('expiryDate') or entry.get('expiryDates')
        if entry_expiry != expiryDate:
            continue
        raw_strike = entry.get('strikePrice')
        try:
            entry_strike = float(str(raw_strike).strip())
        except (TypeError, ValueError):
            entry_strike = raw_strike
        if entry_strike != target_strike:
            continue
        if 'optionType' in entry and 'CE' not in entry and 'PE' not in entry:
            # Flat per-leg shape (nse_quote_derivatives()/nse_quote()'s
            # getSymbolDerivativesData output): each list entry IS one CE or
            # PE leg directly (entry['optionType'] == 'CE'/'PE', price
            # fields on the entry itself), not one entry per strike holding
            # both legs nested under entry['CE']/entry['PE']. Round-3 bug
            # (confirmed live, fixed here): entry.get(optionType) always
            # returned None for this shape since there's no such nested key
            # on a flat leg -- and the old `return None` below bailed out on
            # the FIRST same-strike/expiry entry even when it was the OTHER
            # option type's leg, instead of checking the rest.
            if entry.get('optionType') != optionType:
                continue
            leg = entry
        else:
            leg = entry.get(optionType)
        if not leg:
            continue
        if(intent==""): return leg.get('lastPrice')
        if(intent=="sell"): return leg.get('bidprice', leg.get('buyPrice1'))
        if(intent=="buy"): return leg.get('askPrice', leg.get('sellPrice1'))
    return None

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
    # Round-3 fix: these were two independent `if` statements with no
    # `else`/fallback, so any `type` other than exactly "trading"/"clearing"
    # left `payload` never assigned, and `return payload` blew up with a
    # confusing, unrelated-looking `UnboundLocalError` instead of a clear
    # "invalid type" message -- confirmed live, and confirmed live that
    # NSE's own /api/holiday-master endpoint only accepts these two type
    # values (anything else returns HTTP 200 with a zero-length body, i.e.
    # there is no third live type value being missed here).
    if(type=="clearing"):
        payload = nsefetch('https://www.nseindia.com/api/holiday-master?type=clearing')
    elif(type=="trading"):
        payload = nsefetch('https://www.nseindia.com/api/holiday-master?type=trading')
    else:
        raise ValueError(
            f"nse_holidays(type={type!r}): invalid type -- NSE's "
            f"holiday-master endpoint only supports 'trading' or 'clearing'."
        )
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

  # Round-3 fix: t=0 (expiring today -- a completely normal real-world input
  # given NSE's weekly expiries) or σ=0 makes the d1 denominator below
  # literally zero, which used to raise a raw, unexplained
  # `ZeroDivisionError: division by zero` -- confirmed live with
  # black_scholes_dexter(1000,1000,0). This is a math-domain limit of the
  # Black-Scholes formula itself (d1 is undefined at zero time-to-expiry or
  # zero volatility), not an NSE-API issue, so raise a clear, descriptive
  # error instead of the bare ZeroDivisionError.
  if t <= 0:
      raise ValueError(
          f"black_scholes_dexter(): t={t*td!r} days (={t!r} years) -- "
          f"Black-Scholes is undefined at zero/negative time-to-expiry. "
          f"For an option expiring today, use intrinsic value instead."
      )
  if σ <= 0:
      raise ValueError(
          f"black_scholes_dexter(): σ={σ*100!r}% -- Black-Scholes is "
          f"undefined at zero/negative volatility."
      )

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

    # Round-3 fix: called with its own documented defaults --
    # expiry_history(symbol), i.e. start_date=end_date="" -- this
    # unconditionally crashed with
    # `ValueError: time data '' does not match format '%d-%m-%Y'` on the next
    # two lines, confirmed live. The endpoint itself happily returns the
    # full, unfiltered expiry list when from/to are blank; honor that same
    # "no range given" intent here instead of trying to strptime an empty
    # string.
    if start_date == "" or end_date == "":
        return payload_data

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

def nse_annual_reports(symbol, year_from=None, year_to=None, index="equities"):
    """Forum feature request (forum.unofficed.com topic 1459): list a listed
    company's annual reports with direct PDF download links, optionally
    filtered to a from/to year range. Backed by
    `/api/annual-reports?index=equities&symbol=X`, confirmed live via
    curl_cffi -- NOT the Akamai-walled historical/* family, just a plain
    metadata+link listing, so this is reliable. `index` is almost always
    "equities" (NSE also recognises "debt", but that returns no rows for a
    pure-equity symbol like most NSE-listed companies). Each returned row's
    `fileName` column IS the direct downloadable PDF/zip URL on
    nsearchives.nseindia.com -- fetch it yourself (e.g. with `requests` or
    `curl`) if you want the actual file; this function returns the metadata
    + link, matching how every other *_history-style function in this
    library works, rather than silently downloading files to disk.
    `year_from`/`year_to` filter client-side on the report's `fromYr` (NSE's
    own API has no year-range parameter, it always returns the full
    available history -- commonly 15-20+ years for large-cap companies)."""
    symbol = nsesymbolpurify(symbol)
    payload = nsefetch(f"https://www.nseindia.com/api/annual-reports?index={index}&symbol={symbol}")
    rows = payload.get("data") or []
    df = pd.DataFrame.from_records(rows)
    if df.empty:
        return df
    if year_from is not None or year_to is not None:
        years = pd.to_numeric(df["fromYr"], errors="coerce")
        if year_from is not None:
            df = df[years >= int(year_from)]
            years = years[df.index]
        if year_to is not None:
            df = df[years <= int(year_to)]
    return df.reset_index(drop=True)

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

    # Round-3 fix: a too-short `days` window (or a window with too few
    # trading days, e.g. days=1) made x/y empty after the first-row drop in
    # get_beta_df_maker(), and this used to crash with a bare, unexplained
    # `ZeroDivisionError: division by zero` on the very next line -- confirmed
    # live with get_beta(symbol, days=1). Raise a clear, descriptive error
    # instead so the real cause (not enough historical data for this window)
    # is obvious.
    if not x or not y:
        raise NSEFetchError(
            f"get_beta({symbol!r}, days={days}, symbol2={symbol2!r}): not "
            f"enough historical daily-change data in this {days}-day window "
            f"to compute beta -- try a larger `days` value."
        )

    #stackoverflow.com/questions/42670055/is-there-any-better-way-to-calculate-the-covariance-of-two-lists-than-this
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    covariance = sum((a - mean_x) * (b - mean_y) for (a,b) in zip(x,y)) / len(x)

    mean = sum(y) / len(y)
    variance = sum((i - mean) ** 2 for i in y) / len(y)

    if variance == 0:
        raise NSEFetchError(
            f"get_beta({symbol!r}, days={days}, symbol2={symbol2!r}): "
            f"{symbol2!r} had zero price variance over this window -- beta "
            f"is undefined."
        )

    beta = covariance/variance
    return round(beta,3)

def nse_preopen(key="NIFTY",type="pandas"):
    payload = nsefetch("https://www.nseindia.com/api/market-data-pre-open?key="+key+"")
    if(type=="pandas"):
        # NSE's pre-open-market window for most `key` values (e.g. "NIFTY")
        # is only populated for a few minutes each morning; outside that
        # window `data` is a legitimate empty list (confirmed live:
        # {"data": [], "msg": "No Data Found"}), which used to raise a
        # confusing KeyError('metadata') trying to pull a column out of an
        # empty DataFrame. Return an empty DataFrame instead.
        if not payload.get('data'):
            return pd.DataFrame()
        payload = pd.DataFrame(payload['data'])
        payload  = pd.json_normalize(payload['metadata'])
        return payload
    else:
        return payload

#By Avinash https://forum.unofficed.com/t/nsepython-documentation/376/102?u=dexter
def nse_preopen_movers(key="FO",filter=1.5):
    # Round-3 fix: the `filter` parameter was accepted but never used -- the
    # body hardcoded the literal 1.5/-1.5 thresholds regardless of what the
    # caller passed. Confirmed live: nse_preopen_movers(filter=1.5) and
    # nse_preopen_movers(filter=50) returned byte-identical output. Use the
    # actual parameter.
    preOpen_gainer=nse_preopen(key)
    return preOpen_gainer[preOpen_gainer['pChange'] >filter],preOpen_gainer[preOpen_gainer['pChange'] <-filter]

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
    # Round-3 fix: this CSV's real line 1 is a title/caption row (e.g.
    # `"Participant wise Open Interest (no. of contracts) in Equity
    # Derivatives as on Sep 01, 2026"`) and the real column header
    # ("Client Type", "Future Index Long", ... "Total Short Contracts") is
    # line 2 -- confirmed live on multiple trading dates. Without
    # `skiprows=1`, pandas parsed the caption line as the header and shifted
    # every real column/row down by one, mislabeling every column
    # (columns came out as
    # ['Participant wise Open Interest...', ' 2026""', 'Unnamed: 2', ...]) --
    # making the returned DataFrame unusable for anything. Skip the caption
    # row so the real header is used.
    payload=pd.read_csv("https://archives.nseindia.com/content/nsccl/fao_participant_oi_"+date+".csv", skiprows=1)
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


# ---------------------------------------------------------------------------
# NSE's own OFFICIAL, free, no-auth-required MCP (Model Context Protocol)
# server -- discovered at https://www.nseindia.com/nse-mcp. Two
# streamable-HTTP endpoints, confirmed live from this exact machine (India
# IP):
#   https://mcp.nseindia.in/bhavcopy/cm/mcp  ("nse-bhavcopy-redis-mcp", 21
#     tools) -- historical/derived data: history, comparisons, valuation,
#     corporate actions, market breadth/mood, movers, ...
#   https://mcp.nseindia.in/cmmkt/mcp        ("cm-market", 15 tools) -- live
#     data: quotes, gainers/losers, live indices, segment snapshots, ...
#
# This path never touches nseindia.com at all, so it is completely
# unaffected by the Akamai Bot Manager breakage the rest of this module
# works around above -- prefer it over the nseindia.com scrape path
# whenever it covers the data you need.
#
# Raw JSON-RPC 2.0 over HTTP (the MCP "streamable-http" transport) is used
# directly rather than the `mcp` SDK: confirmed during investigation that
# the SDK's streamable_http_client list_tools() throws an internal MCPError
# against this specific server, so there is no reliable SDK path for it --
# and going raw keeps curl_cffi (already a hard dependency of this module)
# as the only transport needed, adding no new dependency.
# ---------------------------------------------------------------------------

_NSE_MCP_SERVERS = {
    "bhavcopy": "https://mcp.nseindia.in/bhavcopy/cm/mcp",
    "cmmkt": "https://mcp.nseindia.in/cmmkt/mcp",
}

_NSE_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

_NSE_MCP_CLIENT_VERSION = "2.98"

# Per-server-url cache of {headers-including-Mcp-Session-Id} so a normal run
# of several nse_mcp_* calls against the same server doesn't re-run the
# initialize handshake every single call. A lightweight cache, not a hard
# requirement -- _nse_mcp_get_session_headers() transparently re-initializes
# on demand (on a cold start, or if a cached session id is rejected).
_nse_mcp_session_headers = {}


def _nse_mcp_parse_response(r):
    """Parse one HTTP response from an MCP streamable-http endpoint. The
    body comes back as EITHER plain JSON OR SSE-framed
    ("event:message\\ndata:{...}\\n\\n") depending on server/route -- this
    handles both, trying r.json() first and falling back to concatenating
    every 'data:' line and parsing that."""
    ct = (r.headers.get("content-type") or "").lower()
    if "text/event-stream" in ct:
        lines = [ln[len("data:"):].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        if not lines:
            raise ValueError(f"no 'data:' lines in SSE response body (first 200 chars: {r.text[:200]!r})")
        return json.loads("".join(lines))
    try:
        return r.json()
    except ValueError:
        # Mislabeled content-type but actually SSE-framed under the hood --
        # fall back before giving up.
        lines = [ln[len("data:"):].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        if lines:
            return json.loads("".join(lines))
        raise


def _nse_mcp_initialize(session, server_url):
    """Run the MCP 'initialize' handshake against server_url and return the
    mcp-session-id to send back on every subsequent call to this server.
    The bhavcopy endpoint occasionally 502s on a cold first initialize
    (observed live during investigation, fine on retry) -- this retries a
    few times before giving up rather than failing on one transient 502."""
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "nsepythonserver", "version": _NSE_MCP_CLIENT_VERSION},
        },
    }
    last_err = None
    for _attempt in range(3):
        try:
            r = session.post(server_url, json=body, headers=_NSE_MCP_HEADERS, timeout=30)
        except Exception as e:
            last_err = e
            time.sleep(1.0)
            continue
        if r.status_code == 200:
            try:
                rpc = _nse_mcp_parse_response(r)
            except Exception as e:
                last_err = e
                time.sleep(1.0)
                continue
            if "error" in rpc:
                last_err = NSEFetchError(f"MCP initialize {server_url}: {rpc['error']}")
                time.sleep(1.0)
                continue
            return r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        last_err = NSEFetchError(f"MCP initialize {server_url}: HTTP {r.status_code}")
        time.sleep(1.0)
    raise NSEFetchError(f"MCP initialize {server_url} failed after retries: {last_err}")


def _nse_mcp_get_session_headers(server_url, force_refresh=False):
    """Return (curl_cffi session, headers-dict-including-Mcp-Session-Id) for
    server_url, re-running the initialize handshake only when there is no
    cached session yet (or force_refresh=True). Reuses this module's shared
    _get_nse_session() curl_cffi session rather than opening a new one --
    curl_cffi sessions are cheap to share and this keeps one consistent
    connection-pooling/TLS-fingerprint story across the whole module (these
    MCP endpoints live on a different host than nseindia.com and don't need
    the Akamai warm-up cookies that session also carries, but reusing the
    same impersonated-Chrome session object for the TCP/TLS layer costs
    nothing and is simpler than managing a second session)."""
    session = _get_nse_session()
    if force_refresh or server_url not in _nse_mcp_session_headers:
        sid = _nse_mcp_initialize(session, server_url)
        headers = dict(_NSE_MCP_HEADERS)
        if sid:
            headers["Mcp-Session-Id"] = sid
        try:
            session.post(
                server_url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers, timeout=15,
            )
        except Exception:
            pass  # fire-and-forget notification -- no response body expected
        _nse_mcp_session_headers[server_url] = headers
    return session, _nse_mcp_session_headers[server_url]


def _nse_mcp_call(server_url, tool_name, arguments=None):
    """Call one tool on an NSE MCP server (raw JSON-RPC 2.0 'tools/call'
    over the streamable-http transport) and return its actual result data --
    unwrapping the JSON-RPC envelope and the MCP 'tool result' convention
    (result.content[0].text, itself sometimes a JSON string that needs a
    second json.loads(), sometimes already plain text). Raises
    NSEFetchError on any transport/protocol failure, or if the tool's own
    payload carries a top-level 'error' field, instead of returning {} or a
    partial/wrong-looking result."""
    arguments = arguments or {}
    session, headers = _nse_mcp_get_session_headers(server_url)
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments}}

    try:
        r = session.post(server_url, json=body, headers=headers, timeout=60)
    except Exception as e:
        raise NSEFetchError(f"nse_mcp_call {tool_name} @ {server_url}: {e}")

    if r.status_code in (400, 401, 404):
        # The cached Mcp-Session-Id is likely stale/invalid -- re-initialize
        # once and retry before giving up.
        try:
            session, headers = _nse_mcp_get_session_headers(server_url, force_refresh=True)
            r = session.post(server_url, json=body, headers=headers, timeout=60)
        except Exception as e:
            raise NSEFetchError(f"nse_mcp_call {tool_name} @ {server_url}: {e}")

    if r.status_code != 200:
        raise NSEFetchError(
            f"nse_mcp_call {tool_name} @ {server_url}: HTTP {r.status_code}; "
            f"first 200 chars: {r.text[:200]!r}"
        )

    try:
        rpc = _nse_mcp_parse_response(r)
    except Exception as e:
        raise NSEFetchError(
            f"nse_mcp_call {tool_name} @ {server_url}: HTTP 200 but response body "
            f"could not be parsed as JSON or SSE ({e}); first 200 chars: {r.text[:200]!r}"
        )

    if "error" in rpc:
        raise NSEFetchError(f"nse_mcp_call {tool_name} @ {server_url}: JSON-RPC error: {rpc['error']}")

    result = rpc.get("result") or {}
    content = result.get("content") or []
    if not content or "text" not in content[0]:
        raise NSEFetchError(
            f"nse_mcp_call {tool_name} @ {server_url}: no 'content[0].text' in tool "
            f"result -- got {result!r}"
        )
    text = content[0]["text"]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = text  # plain text/markdown result, not JSON -- hand it back as-is

    if isinstance(data, dict) and data.get("error"):
        raise NSEFetchError(f"nse_mcp_call {tool_name} @ {server_url}: tool reported an error: {data['error']}")
    return data


def nse_mcp_call(server, tool_name, **kwargs):
    """Generic escape-hatch: call ANY tool (named wrapper below or not) on
    either NSE MCP server. `server` is 'bhavcopy'
    (https://mcp.nseindia.in/bhavcopy/cm/mcp, 21 historical/derived-data
    tools) or 'cmmkt' (https://mcp.nseindia.in/cmmkt/mcp, 15 live-data
    tools); `tool_name` + keyword arguments map straight onto that tool's
    own inputSchema. Use this for any tool NSE adds in the future that
    doesn't yet have a named nse_mcp_* wrapper."""
    if server not in _NSE_MCP_SERVERS:
        raise NSEFetchError(
            f"nse_mcp_call: unknown server {server!r} -- expected one of {list(_NSE_MCP_SERVERS)}"
        )
    return _nse_mcp_call(_NSE_MCP_SERVERS[server], tool_name, kwargs)


def _nse_mcp_list_tools_raw(server_url):
    session, headers = _nse_mcp_get_session_headers(server_url)
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    try:
        r = session.post(server_url, json=body, headers=headers, timeout=30)
    except Exception as e:
        raise NSEFetchError(f"nse_mcp_list_tools @ {server_url}: {e}")
    if r.status_code != 200:
        raise NSEFetchError(f"nse_mcp_list_tools @ {server_url}: HTTP {r.status_code}")
    try:
        rpc = _nse_mcp_parse_response(r)
    except Exception as e:
        raise NSEFetchError(f"nse_mcp_list_tools @ {server_url}: could not parse response ({e})")
    if "error" in rpc:
        raise NSEFetchError(f"nse_mcp_list_tools @ {server_url}: JSON-RPC error: {rpc['error']}")
    return rpc.get("result", {}).get("tools", [])


def nse_mcp_list_tools(server=""):
    """Discovery helper: return the LIVE tool list (name, description,
    inputSchema) straight from the server's own tools/list response -- for
    one server ('bhavcopy' or 'cmmkt'), or both (default, as a
    {'bhavcopy': [...], 'cmmkt': [...]} dict) when `server` is omitted.
    Never hardcodes a static copy of the list, so this stays accurate even
    if NSE changes their toolset later."""
    if server:
        if server not in _NSE_MCP_SERVERS:
            raise NSEFetchError(
                f"nse_mcp_list_tools: unknown server {server!r} -- expected one of {list(_NSE_MCP_SERVERS)}"
            )
        return _nse_mcp_list_tools_raw(_NSE_MCP_SERVERS[server])
    return {name: _nse_mcp_list_tools_raw(url) for name, url in _NSE_MCP_SERVERS.items()}


# ---------------------------------------------------------------------------
# Named wrappers -- one per tool, 21 on "bhavcopy" + 15 on "cmmkt" = 36.
# Every one is backed by NSE's own official no-auth MCP server rather than
# the Akamai-affected nseindia.com scrape path used everywhere else in this
# file, so it's a notably more reliable source for whatever data it covers.
# Parameter names/defaults are taken from each tool's own inputSchema
# (property descriptions/defaults) live off the server, not guessed.
# ---------------------------------------------------------------------------

# --- bhavcopy (https://mcp.nseindia.in/bhavcopy/cm/mcp) ---------------------

def nse_mcp_get_top_by_volume(date=None, n=10, sort_by="volume"):
    """Top N most actively traded NSE stocks on a date, sorted by 'volume'
    (traded quantity) or 'value' (turnover). Backed by NSE's own official
    no-auth MCP server, not the Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("bhavcopy", "get_top_by_volume", date=date or "today", n=n, sortBy=sort_by)
    return pd.DataFrame(result.get("stocks", []))


def nse_mcp_get_top_movers(date=None, n=10, direction="gain"):
    """Top N gaining ('gain') or losing ('loss') NSE stocks on a date, with
    OHLCV details. Backed by NSE's own official no-auth MCP server, not the
    Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("bhavcopy", "get_top_movers", date=date or "today", n=n, direction=direction)
    return pd.DataFrame(result.get("stocks", []))


def nse_mcp_lookup_symbol(query):
    """Look up NSE ticker symbols by partial name/keyword -- ticker list
    only, no price data, faster than nse_mcp_search_symbols when you just
    need the symbol. Backed by NSE's own official no-auth MCP server, not
    the Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("bhavcopy", "nse_lookup_symbol", query=query)
    return result.get("symbols", [])


def nse_mcp_get_market_mood(date=None):
    """Factual NSE market-mood read for a day: India VIX level/trend, index
    and stock advance/decline breadth, benchmark changes. Backed by NSE's
    own official no-auth MCP server, not the Akamai-affected nseindia.com
    scrape path."""
    return nse_mcp_call("bhavcopy", "get_market_mood", date=date or "today")


def nse_mcp_get_index_valuation(index_name, months=24, date=None):
    """Valuation ratios (P/E, P/B, dividend yield) of an NSE index and where
    today's value sits within its own recent range. Backed by NSE's own
    official no-auth MCP server, not the Akamai-affected nseindia.com scrape
    path."""
    return nse_mcp_call("bhavcopy", "get_index_valuation", indexName=index_name, months=months, date=date or "today")


def nse_mcp_get_market_breadth(date=None):
    """Overall NSE market breadth for a trading date -- advances, declines,
    unchanged, A/D ratio, total volume. Backed by NSE's own official
    no-auth MCP server, not the Akamai-affected nseindia.com scrape path."""
    return nse_mcp_call("bhavcopy", "get_market_breadth", date=date or "today")


def nse_mcp_get_corporate_actions(symbol, from_date=None, to_date=None):
    """Actual NSE corporate-action events (splits, bonuses, dividends) for a
    stock with exact ex-dates and adjustment factors (default window: the
    last 5 years). Backed by NSE's own official no-auth MCP server, not the
    Akamai-affected nseindia.com scrape path."""
    if to_date is None:
        to_date = datetime.date.today().strftime("%Y-%m-%d")
    if from_date is None:
        from_date = (datetime.date.today() - datetime.timedelta(days=5 * 365)).strftime("%Y-%m-%d")
    result = nse_mcp_call("bhavcopy", "get_corporate_actions", symbol=symbol, fromDate=from_date, toDate=to_date)
    return pd.DataFrame(result.get("actions", []))


def nse_mcp_compare_indices(index_names, months=6, date=None):
    """Compare 2-10 NSE indices side by side: return, annualised volatility,
    max drawdown and current valuation, ranked best to worst. Backed by
    NSE's own official no-auth MCP server, not the Akamai-affected
    nseindia.com scrape path."""
    result = nse_mcp_call("bhavcopy", "compare_indices", indexNames=index_names, months=months, date=date or "today")
    return pd.DataFrame(result.get("indices", []))


def nse_mcp_get_index_movers(date=None, period="1D", n=10, scope="equity"):
    """Top gaining and top losing NSE indices for a day or period (1D/1W/1M/
    3M/6M/1Y) -- useful for sector/theme rotation. Returns both lists.
    Backed by NSE's own official no-auth MCP server, not the
    Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("bhavcopy", "get_index_movers", date=date or "today", period=period, n=n, scope=scope)
    return {
        "gainers": pd.DataFrame(result.get("gainers", [])),
        "losers": pd.DataFrame(result.get("losers", [])),
    }


def nse_mcp_get_ltp_by_date(symbol, date=None):
    """Last traded (close) price for an NSE symbol on a specific date.
    Backed by NSE's own official no-auth MCP server, not the
    Akamai-affected nseindia.com scrape path."""
    return nse_mcp_call("bhavcopy", "get_ltp_by_date", symbol=symbol, date=date or "today")


def nse_mcp_get_bulk_quote(symbols):
    """Latest available price snapshot (OHLC, % change, volume) for
    multiple NSE stocks in one call (max 50). Backed by NSE's own official
    no-auth MCP server, not the Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("bhavcopy", "get_bulk_quote", symbols=symbols)
    return pd.DataFrame(result.get("quotes", []))


def nse_mcp_get_volume_analysis(symbol, days=30):
    """Trading-volume trend analysis for an NSE stock over N trading days --
    average/max/min volume, volume-spike days (>2x avg), recent 5-day
    trend. Backed by NSE's own official no-auth MCP server, not the
    Akamai-affected nseindia.com scrape path."""
    return nse_mcp_call("bhavcopy", "get_volume_analysis", symbol=symbol, days=days)


def nse_mcp_get_stock_history(symbol, months=3, end_date=None):
    """Daily OHLCV price history for an NSE stock (up to 3 months per call
    -- chain calls with end_date=<next_end_date from the previous response>
    for a longer span). Backed by NSE's own official no-auth MCP server,
    not the Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("bhavcopy", "get_stock_history", symbol=symbol, months=months, endDate=end_date or "today")
    return pd.DataFrame(result.get("data", []))


def nse_mcp_get_index_snapshot(date=None, filter=None):
    """End-of-day values for NSE indices on a date (all ~170 by default, or
    filtered by a case-insensitive name substring, e.g. 'bank', 'nifty
    50'). Backed by NSE's own official no-auth MCP server, not the
    Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("bhavcopy", "get_index_snapshot", date=date or "today", filter=filter or "")
    return pd.DataFrame(result.get("indices", []))


def nse_mcp_search_symbols(query):
    """Search NSE stock symbols by company name or partial symbol, with
    latest close price and % change. Backed by NSE's own official no-auth
    MCP server, not the Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("bhavcopy", "search_symbols", query=query)
    return pd.DataFrame(result.get("results", []))


def nse_mcp_get_stock_vs_index(symbol, index_name="Nifty 50", months=12, date=None):
    """Compare one NSE stock against a benchmark index over a period:
    return, outperformance in percentage points, beta and correlation (the
    stock's return is already CA-adjusted for splits/bonuses). Backed by
    NSE's own official no-auth MCP server, not the Akamai-affected
    nseindia.com scrape path."""
    return nse_mcp_call(
        "bhavcopy", "get_stock_vs_index",
        symbol=symbol, indexName=index_name, months=months, date=date or "today",
    )


def nse_mcp_compare_stocks(symbols, months=6):
    """Compare multiple NSE stocks (max 10) side by side over a period: %
    return ranked best to worst, plus max drawdown per stock. Backed by
    NSE's own official no-auth MCP server, not the Akamai-affected
    nseindia.com scrape path."""
    result = nse_mcp_call("bhavcopy", "compare_stocks", symbols=symbols, months=months)
    return pd.DataFrame(result.get("stocks", []))


def nse_mcp_get_index_history(index_name, months=3, end_date=None):
    """Daily history for an NSE index -- OHLC, % change, volume, turnover,
    P/E, P/B, dividend yield (up to 12 months per call -- chain calls with
    end_date=<next_end_date from the previous response> for a longer
    span). Backed by NSE's own official no-auth MCP server, not the
    Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("bhavcopy", "get_index_history", indexName=index_name, months=months, endDate=end_date or "today")
    return pd.DataFrame(result.get("data", []))


def nse_mcp_moving_average(symbol, days=20):
    """Simple moving average (SMA) of close prices for a stock over the
    last N trading days (common periods: 20/50/200). Backed by NSE's own
    official no-auth MCP server, not the Akamai-affected nseindia.com
    scrape path."""
    return nse_mcp_call("bhavcopy", "moving_average", symbol=symbol, days=days)


def nse_mcp_get_52_week_high_low(symbol):
    """52-week high/low for an NSE stock, with dates, position within the
    range, and % distance from each extreme. Backed by NSE's own official
    no-auth MCP server, not the Akamai-affected nseindia.com scrape path."""
    return nse_mcp_call("bhavcopy", "get_52_week_high_low", symbol=symbol)


def nse_mcp_get_index_performance(index_name, date=None):
    """Price performance of one NSE index: 1D change plus 1W/1M/3M/6M/1Y/2Y
    returns, and 52-week high/low with dates and distance from each. Backed
    by NSE's own official no-auth MCP server, not the Akamai-affected
    nseindia.com scrape path."""
    return nse_mcp_call("bhavcopy", "get_index_performance", indexName=index_name, date=date or "today")


# --- cmmkt (https://mcp.nseindia.in/cmmkt/mcp) ------------------------------

def nse_mcp_cm_get_live_market_data(index="gainers"):
    """Live NSE market data for a variation type -- 'gainers' or 'loosers'
    (sic, NSE's own spelling), refreshed every 5 minutes. Backed by NSE's
    own official no-auth MCP server, not the Akamai-affected nseindia.com
    scrape path."""
    return nse_mcp_call("cmmkt", "cm_get_live_market_data", index=index)


def nse_mcp_cm_get_equity_stocks(limit=100, symbol_filter=None):
    """Latest live data for NSE Capital Market EQUITY-series stocks (EQ,
    BE, BL, BT, IL, IQ) -- symbol, LTP, OHLC, change, volume, 52w range.
    Backed by NSE's own official no-auth MCP server, not the
    Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("cmmkt", "cm_get_equity_stocks", limit=limit, symbolFilter=symbol_filter or "")
    return pd.DataFrame(result.get("stocks", []))


def nse_mcp_nse_get_losers(limit=10):
    """Top N NSE stocks by % loss, flattened across all indices and sorted
    ascending -- ideal for risk alerts. Backed by NSE's own official
    no-auth MCP server, not the Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("cmmkt", "nse_get_losers", limit=limit)
    return pd.DataFrame(result.get("stocks", []))


def nse_mcp_cm_get_call_auction_stocks(limit=100, symbol_filter=None):
    """Latest live data for NSE Call Auction session stocks (series CA,
    CB). Backed by NSE's own official no-auth MCP server, not the
    Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("cmmkt", "cm_get_call_auction_stocks", limit=limit, symbolFilter=symbol_filter or "")
    return pd.DataFrame(result.get("stocks", []))


def nse_mcp_cm_get_bond_stocks(limit=100, symbol_filter=None):
    """Latest live data for NSE bonds/debt instruments (series N1-N9, NE,
    NF, NU, Z1-Z5, ZA, ZP, ZU, GB, SG, TB, ...). Backed by NSE's own
    official no-auth MCP server, not the Akamai-affected nseindia.com
    scrape path."""
    result = nse_mcp_call("cmmkt", "cm_get_bond_stocks", limit=limit, symbolFilter=symbol_filter or "")
    return pd.DataFrame(result.get("stocks", []))


def nse_mcp_cm_get_live_gainers():
    """Raw NSE gainers data grouped by index segment (NIFTY, BANKNIFTY,
    NIFTYNEXT50, allSec, ...), NOT sorted by % change -- use
    nse_mcp_nse_get_market_movers for ranked gainers/losers. Backed by
    NSE's own official no-auth MCP server, not the Akamai-affected
    nseindia.com scrape path."""
    return nse_mcp_call("cmmkt", "cm_get_live_gainers")


def nse_mcp_nse_get_gainers(limit=10):
    """Top N NSE stocks by % gain, flattened across all indices and sorted
    descending -- ideal for dashboard display. Backed by NSE's own official
    no-auth MCP server, not the Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("cmmkt", "nse_get_gainers", limit=limit)
    return pd.DataFrame(result.get("stocks", []))


def nse_mcp_cm_get_data_status():
    """Freshness of NSE live gainers/losers market data -- when it was last
    crawled from NSE. Backed by NSE's own official no-auth MCP server, not
    the Akamai-affected nseindia.com scrape path."""
    return nse_mcp_call("cmmkt", "cm_get_data_status")


def nse_mcp_cm_get_stock_quote(symbol):
    """Latest live quote for a specific NSE CM stock by exact symbol (any
    segment -- equity, SME, bonds, call auction): LTP, OHLC, change, volume,
    52w range, series, timestamp. Backed by NSE's own official no-auth MCP
    server, not the Akamai-affected nseindia.com scrape path."""
    return nse_mcp_call("cmmkt", "cm_get_stock_quote", symbol=symbol)


def nse_mcp_cm_get_index_quote(index_name):
    """Full live quote for one NSE index by its exact live name (e.g.
    'NIFTY 50', 'NIFTY BANK', 'INDIA VIX'): last value, change, day's
    OHLC, 52-week range, and 1W/1M/1Y comparisons. Backed by NSE's own
    official no-auth MCP server, not the Akamai-affected nseindia.com
    scrape path."""
    return nse_mcp_call("cmmkt", "cm_get_index_quote", indexName=index_name)


def nse_mcp_cm_get_sme_stocks(limit=100, symbol_filter=None):
    """Latest live data for NSE SME (Small & Medium Enterprises) stocks
    (series SM, ST). Backed by NSE's own official no-auth MCP server, not
    the Akamai-affected nseindia.com scrape path."""
    result = nse_mcp_call("cmmkt", "cm_get_sme_stocks", limit=limit, symbolFilter=symbol_filter or "")
    return pd.DataFrame(result.get("stocks", []))


def nse_mcp_cm_get_live_losers():
    """Raw NSE losers data grouped by index segment (NIFTY, BANKNIFTY,
    NIFTYNEXT50, allSec, ...), NOT sorted by % change -- use
    nse_mcp_nse_get_market_movers for ranked gainers/losers. Backed by
    NSE's own official no-auth MCP server, not the Akamai-affected
    nseindia.com scrape path."""
    return nse_mcp_call("cmmkt", "cm_get_live_losers")


def nse_mcp_cm_get_live_indices(group=None, name_filter=None):
    """Latest live values of NSE indices (~140, across derivatives/broad/
    sectoral/strategy/thematic/fixed_income groups): last value, change,
    day's OHL. Backed by NSE's own official no-auth MCP server, not the
    Akamai-affected nseindia.com scrape path."""
    return nse_mcp_call("cmmkt", "cm_get_live_indices", group=group or "", nameFilter=name_filter or "")


def nse_mcp_nse_get_market_movers(index_name=None, limit=10):
    """PRIMARY tool for "today's top gainers/losers": ranked gainer and
    loser lists (by % change) across all NSE securities by default, or
    filtered to NIFTY/BANKNIFTY/NIFTYNEXT50. Returns both lists. Backed by
    NSE's own official no-auth MCP server, not the Akamai-affected
    nseindia.com scrape path."""
    result = nse_mcp_call("cmmkt", "nse_get_market_movers", indexName=index_name or "", limit=limit)
    return {
        "gainers": pd.DataFrame(result.get("gainers", [])),
        "losers": pd.DataFrame(result.get("losers", [])),
    }


def nse_mcp_cm_get_allstocks_status():
    """Freshness of the NSE all-stocks data cache -- last crawl time,
    availability, segment-wise stock counts. Backed by NSE's own official
    no-auth MCP server, not the Akamai-affected nseindia.com scrape path."""
    return nse_mcp_call("cmmkt", "cm_get_allstocks_status")
