"""
機票搜尋器 - 反追蹤多源版本
多來源平行搜尋：Trip.com + Skyscanner（各自獨立無痕 context）
每個 source 本身就是聚合器，合計覆蓋數百家航空公司與 OTA
"""
import asyncio
import random
import re
from flask import Flask, render_template, request, jsonify
from playwright.async_api import async_playwright

app = Flask(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

CABIN_TRIP = {"ECONOMY": "y", "PREMIUM_ECONOMY": "s", "BUSINESS": "c", "FIRST": "f"}
CABIN_SKY  = {"ECONOMY": "economy", "PREMIUM_ECONOMY": "premiumeconomy",
               "BUSINESS": "business", "FIRST": "first"}

TRACKER_DOMAINS = [
    "google-analytics", "googletagmanager", "doubleclick",
    "facebook.net", "connect.facebook", "criteo", "quantserve",
    "scorecardresearch", "adsymptotic", "amazon-adsystem",
]

# ── 共用 Playwright helper ─────────────────────────────────────────────────

async def _new_ctx(p, extra_headers=None):
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
              "--disable-dev-shm-usage"],
    )
    ctx = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        locale="en-GB",
        timezone_id="Asia/Taipei",
        viewport={"width": random.randint(1280, 1920), "height": random.randint(768, 1080)},
        extra_http_headers=extra_headers or {},
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});"
    )

    async def block_trackers(route):
        if any(t in route.request.url.lower() for t in TRACKER_DOMAINS):
            await route.abort()
        else:
            await route.continue_()

    await ctx.route("**/*", block_trackers)
    return browser, ctx


async def _load_page(page, url, selector, timeout_sel=25000, extra_wait=2000):
    """頁面載入 + 等待結果 selector"""
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_selector(selector, timeout=timeout_sel)
    except Exception:
        await page.wait_for_timeout(8000)
    await page.wait_for_timeout(extra_wait)


# ════════════════════════════════════════════════════════════════════════════
# Trip.com 搜尋
# ════════════════════════════════════════════════════════════════════════════

EXTRACT_TRIP_OW = """() => {
    const flights = [];
    document.querySelectorAll('.result-item.J_FlightItem, .m-result-list .result-item')
    .forEach(card => {
        try {
            const priceEl = card.querySelector('[class*="item-con-price"],[class*="o-price-flight"]');
            const priceNum = parseFloat((priceEl?.innerText||'').replace(/[^0-9.]/g,'')) || 0;
            const timeEls = card.querySelectorAll('[class*="airline__timers"] [class*="time"],[class*="time_c"]');
            const times = Array.from(timeEls).map(e=>e.innerText.trim()).filter(t=>/^\d{2}:\d{2}/.test(t));
            const codePairs = card.querySelectorAll('.pl-2.pr-2');
            const airlineEl = card.querySelector('[class*="flights-name"]');
            const durEl = card.querySelector('[class*="info-duration"]');
            const stopEl = card.querySelector('[class*="stop__text"]');
            const stopText = stopEl?.innerText.trim() || '';
            const stops = (stopText.toLowerCase().includes('direct')||stopText.toLowerCase().includes('nonstop'))
                ? 0 : (stopText.match(/\d+/) ? parseInt(stopText.match(/\d+/)[0]) : 1);
            if (priceNum > 0 || airlineEl?.innerText) {
                flights.push({ price:priceNum, airline:airlineEl?.innerText.trim()||'',
                    depart_time:times[0]||'', arrive_time:times[1]||'',
                    from_airport:codePairs[0]?.innerText.trim()||'',
                    to_airport:codePairs[2]?.innerText.trim()||'',
                    duration:durEl?.innerText.trim()||'', stops, stops_text:stopText });
            }
        } catch(e) {}
    });
    return flights;
}"""

EXTRACT_TRIP_RT = """() => {
    const pkgs = [];
    document.querySelectorAll('.result-item.J_FlightItem, .m-result-list .result-item')
    .forEach(card => {
        try {
            const priceEl = card.querySelector('[class*="item-con-price"],[class*="o-price-flight"]');
            const priceNum = parseFloat((priceEl?.innerText||'').replace(/[^0-9.]/g,'')) || 0;
            const timeEls = card.querySelectorAll('[class*="airline__timers"] [class*="time"],[class*="time_c"]');
            const times = Array.from(timeEls).map(e=>e.innerText.trim()).filter(t=>/^\d{2}:\d{2}/.test(t));
            const airlineEls = card.querySelectorAll('[class*="flights-name"]');
            const codePairs = card.querySelectorAll('.pl-2.pr-2');
            const durEls = card.querySelectorAll('[class*="info-duration"]');
            const stopEls = card.querySelectorAll('[class*="stop__text"]');
            function sc(el){ const t=(el?.innerText||'').toLowerCase();
                return (t.includes('direct')||t.includes('nonstop'))?0:(t.match(/\d+/)?parseInt(t.match(/\d+/)[0]):1); }
            if (priceNum > 0) pkgs.push({
                price:priceNum,
                out_depart_time:times[0]||'', out_arrive_time:times[1]||'',
                ret_depart_time:times[2]||'', ret_arrive_time:times[3]||'',
                out_airline:airlineEls[0]?.innerText.trim()||'',
                ret_airline:airlineEls[1]?.innerText.trim()||airlineEls[0]?.innerText.trim()||'',
                out_from:codePairs[0]?.innerText.trim()||'', out_to:codePairs[2]?.innerText.trim()||'',
                ret_from:codePairs[4]?.innerText.trim()||'', ret_to:codePairs[6]?.innerText.trim()||'',
                out_duration:durEls[0]?.innerText.trim()||'', ret_duration:durEls[1]?.innerText.trim()||'',
                out_stops:sc(stopEls[0]), ret_stops:sc(stopEls[1]),
                out_stops_text:stopEls[0]?.innerText.trim()||'',
                ret_stops_text:stopEls[1]?.innerText.trim()||'',
            });
        } catch(e) {}
    });
    return pkgs;
}"""


async def scrape_trip_oneway(origin, dest, date, adults, cabin):
    url = (f"https://uk.trip.com/flights/showfarefirst"
           f"?dcity={origin.lower()}&acity={dest.lower()}"
           f"&ddate={date}&triptype=ow&class={CABIN_TRIP.get(cabin,'y')}"
           f"&quantity={adults}&curr=TWD&locale=en-GB&nonstoponly=off&searchboxarg=t")
    async with async_playwright() as p:
        browser, ctx = await _new_ctx(p)
        page = await ctx.new_page()
        try:
            await _load_page(page, url, ".result-item.J_FlightItem, .m-result-list .result-item")
            raw = await page.evaluate(EXTRACT_TRIP_OW)
            if not raw:
                raw = _parse_raw_text(await page.inner_text(".m-result-list, body"), origin, dest)
            await ctx.close(); await browser.close()
            return _to_legs(raw, origin, dest, date, "Trip.com"), None
        except Exception as e:
            try: await ctx.close(); await browser.close()
            except Exception: pass
            return [], str(e)


async def scrape_trip_roundtrip(origin, dest, depart_date, return_date, adults, cabin):
    url = (f"https://uk.trip.com/flights/showfarefirst"
           f"?dcity={origin.lower()}&acity={dest.lower()}"
           f"&ddate={depart_date}&adate={return_date}&triptype=rt"
           f"&class={CABIN_TRIP.get(cabin,'y')}&quantity={adults}"
           f"&curr=TWD&locale=en-GB&nonstoponly=off&searchboxarg=t")
    async with async_playwright() as p:
        browser, ctx = await _new_ctx(p)
        page = await ctx.new_page()
        try:
            await _load_page(page, url, ".result-item.J_FlightItem, .m-result-list .result-item")
            raw = await page.evaluate(EXTRACT_TRIP_RT)
            await ctx.close(); await browser.close()
            return _to_rt_pkgs(raw, origin, dest, depart_date, return_date, "Trip.com"), None
        except Exception as e:
            try: await ctx.close(); await browser.close()
            except Exception: pass
            return [], str(e)


# ════════════════════════════════════════════════════════════════════════════
# Skyscanner 搜尋
# ════════════════════════════════════════════════════════════════════════════

EXTRACT_SKY_OW = """() => {
    const flights = [];

    // Skyscanner 用 data-testid 屬性
    const cards = document.querySelectorAll(
        '[data-testid="itinerary-card-wrapper"], ' +
        '[class*="FlightsResults"] [class*="itinerary"], ' +
        '[class*="ItineraryCard"]'
    );

    cards.forEach(card => {
        try {
            // 價格
            const priceEl = card.querySelector(
                '[data-testid="price-text"], [class*="Price_mainPrice"], [class*="price"]'
            );
            const priceNum = parseFloat((priceEl?.innerText||'').replace(/[^0-9.]/g,'')) || 0;

            // 時間（出發/抵達）
            const timeEls = card.querySelectorAll(
                '[data-testid$="-time"], [class*="LegInfo_routeTime"], [class*="legTime"]'
            );
            const times = Array.from(timeEls).map(e=>e.innerText.trim()).filter(t=>/^\d{1,2}:\d{2}/.test(t))
                .map(t => t.length === 4 ? '0'+t : t);

            // 航空公司
            const airlineEl = card.querySelector('[data-testid="carrier-name"], [class*="LogoImage"] img, [class*="carrier"] img');
            const airline = airlineEl?.getAttribute('alt') || airlineEl?.innerText?.trim() || '';

            // 飛行時間
            const durEl = card.querySelector('[data-testid="duration"], [class*="duration"], [class*="Duration"]');
            const duration = durEl?.innerText?.trim() || '';

            // 轉機
            const stopsEl = card.querySelector('[data-testid="stops-text"], [class*="StopsInfo"], [class*="stops"]');
            const stopsText = stopsEl?.innerText?.trim() || '';
            const stops = (stopsText.includes('direct') || stopsText.includes('nonstop') || stopsText === '0')
                ? 0 : (stopsText.match(/\d+/) ? parseInt(stopsText.match(/\d+/)[0]) : 1);

            // 機場代碼
            const iataEls = card.querySelectorAll('[class*="iata"], [class*="Iata"], [class*="airport-name"]');
            const from_airport = iataEls[0]?.innerText?.trim() || '';
            const to_airport   = iataEls[1]?.innerText?.trim() || '';

            if (priceNum > 0) {
                flights.push({ price:priceNum, airline,
                    depart_time:times[0]||'', arrive_time:times[1]||'',
                    from_airport, to_airport, duration, stops, stops_text:stopsText });
            }
        } catch(e) {}
    });
    return flights;
}"""

EXTRACT_SKY_RT = """() => {
    const pkgs = [];
    // Skyscanner 來回結果每張卡含兩個 leg section
    const cards = document.querySelectorAll(
        '[data-testid="itinerary-card-wrapper"], [class*="ItineraryCard"]'
    );
    cards.forEach(card => {
        try {
            const priceEl = card.querySelector('[data-testid="price-text"], [class*="Price_mainPrice"]');
            const priceNum = parseFloat((priceEl?.innerText||'').replace(/[^0-9.]/g,'')) || 0;

            const timeEls = card.querySelectorAll('[data-testid$="-time"], [class*="LegInfo_routeTime"]');
            const times = Array.from(timeEls).map(e=>e.innerText.trim()).filter(t=>/^\d{1,2}:\d{2}/.test(t))
                .map(t => t.length===4 ? '0'+t : t);

            const airlineEls = card.querySelectorAll('[data-testid="carrier-name"], [class*="LogoImage"] img');
            function airlineName(el){ return el?.getAttribute('alt')||el?.innerText?.trim()||''; }

            const durEls = card.querySelectorAll('[data-testid="duration"], [class*="duration"]');
            const stopsEls = card.querySelectorAll('[data-testid="stops-text"], [class*="StopsInfo"]');
            function sc(el){ const t=(el?.innerText||'').toLowerCase();
                return (t.includes('direct')||t==='0')?0:(t.match(/\d+/)?parseInt(t.match(/\d+/)[0]):1); }

            if (priceNum > 0) pkgs.push({
                price:priceNum,
                out_depart_time:times[0]||'', out_arrive_time:times[1]||'',
                ret_depart_time:times[2]||'', ret_arrive_time:times[3]||'',
                out_airline:airlineName(airlineEls[0]),
                ret_airline:airlineName(airlineEls[1])||airlineName(airlineEls[0]),
                out_from:'', out_to:'', ret_from:'', ret_to:'',
                out_duration:durEls[0]?.innerText?.trim()||'',
                ret_duration:durEls[1]?.innerText?.trim()||'',
                out_stops:sc(stopsEls[0]), ret_stops:sc(stopsEls[1]),
                out_stops_text:stopsEls[0]?.innerText?.trim()||'',
                ret_stops_text:stopsEls[1]?.innerText?.trim()||'',
            });
        } catch(e) {}
    });
    return pkgs;
}"""


def _sky_date(iso_date):
    """2024-06-01 → 240601 (Skyscanner URL format)"""
    return iso_date.replace("-", "")[2:]  # drop century → YYMMDD


async def scrape_sky_oneway(origin, dest, date, adults, cabin):
    cabin_sky = CABIN_SKY.get(cabin, "economy")
    sky_date  = _sky_date(date)
    url = (f"https://www.skyscanner.com/transport/flights"
           f"/{origin.lower()}/{dest.lower()}/{sky_date}/"
           f"?adults={adults}&cabinclass={cabin_sky}&currency=TWD&locale=en-GB&market=TW")
    headers = {
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://www.skyscanner.com/",
    }
    async with async_playwright() as p:
        browser, ctx = await _new_ctx(p, extra_headers=headers)
        page = await ctx.new_page()
        try:
            await _load_page(page, url,
                             "[data-testid='itinerary-card-wrapper'], [class*='ItineraryCard']",
                             timeout_sel=30000, extra_wait=3000)
            raw = await page.evaluate(EXTRACT_SKY_OW)
            await ctx.close(); await browser.close()
            return _to_legs(raw, origin, dest, date, "Skyscanner"), None
        except Exception as e:
            try: await ctx.close(); await browser.close()
            except Exception: pass
            return [], str(e)


async def scrape_sky_roundtrip(origin, dest, depart_date, return_date, adults, cabin):
    cabin_sky = CABIN_SKY.get(cabin, "economy")
    sky_out   = _sky_date(depart_date)
    sky_ret   = _sky_date(return_date)
    url = (f"https://www.skyscanner.com/transport/flights"
           f"/{origin.lower()}/{dest.lower()}/{sky_out}/{sky_ret}/"
           f"?adults={adults}&cabinclass={cabin_sky}&currency=TWD&locale=en-GB&market=TW")
    headers = {
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://www.skyscanner.com/",
    }
    async with async_playwright() as p:
        browser, ctx = await _new_ctx(p, extra_headers=headers)
        page = await ctx.new_page()
        try:
            await _load_page(page, url,
                             "[data-testid='itinerary-card-wrapper'], [class*='ItineraryCard']",
                             timeout_sel=30000, extra_wait=3000)
            raw = await page.evaluate(EXTRACT_SKY_RT)
            await ctx.close(); await browser.close()
            return _to_rt_pkgs(raw, origin, dest, depart_date, return_date, "Skyscanner"), None
        except Exception as e:
            try: await ctx.close(); await browser.close()
            except Exception: pass
            return [], str(e)


# ════════════════════════════════════════════════════════════════════════════
# 多源聚合
# ════════════════════════════════════════════════════════════════════════════

async def search_all_oneway(origin, dest, date, adults, cabin):
    results = await asyncio.gather(
        scrape_trip_oneway(origin, dest, date, adults, cabin),
        scrape_sky_oneway(origin, dest, date, adults, cabin),
        return_exceptions=True,
    )
    legs, errors = [], []
    for res in results:
        if isinstance(res, Exception):
            errors.append(str(res))
        else:
            l, e = res
            legs.extend(l)
            if e: errors.append(e)
    legs.sort(key=lambda x: x["price"] or 999999)
    return legs, errors


async def search_all_roundtrip(origin, dest, depart_date, return_date, adults, cabin):
    """出程單程 + 回程單程 + Trip.com 整票 + Skyscanner 整票，共 4 個平行搜尋"""
    results = await asyncio.gather(
        scrape_trip_oneway(origin, dest, depart_date, adults, cabin),
        scrape_trip_oneway(dest, origin, return_date, adults, cabin),
        scrape_trip_roundtrip(origin, dest, depart_date, return_date, adults, cabin),
        scrape_sky_oneway(origin, dest, depart_date, adults, cabin),
        scrape_sky_oneway(dest, origin, return_date, adults, cabin),
        scrape_sky_roundtrip(origin, dest, depart_date, return_date, adults, cabin),
        return_exceptions=True,
    )
    def extract(res):
        if isinstance(res, Exception): return [], str(res)
        return res

    (out_trip, e1), (ret_trip, e2) = extract(results[0]), extract(results[1])
    (rt_trip,  e3)                 = extract(results[2])
    (out_sky,  e4), (ret_sky,  e5) = extract(results[3]), extract(results[4])
    (rt_sky,   e6)                 = extract(results[5])

    outbound = _merge_legs(out_trip, out_sky)
    inbound  = _merge_legs(ret_trip, ret_sky)
    rt_pkgs  = _merge_rt(rt_trip, rt_sky)
    errors   = [e for e in [e1,e2,e3,e4,e5,e6] if e]

    return outbound, inbound, rt_pkgs, errors


def _merge_legs(*leg_lists):
    merged = []
    for ll in leg_lists:
        merged.extend(ll)
    # 去重（相同航空 + 起飛時間 + 日期，保留來源多的）
    seen, out = {}, []
    for leg in sorted(merged, key=lambda x: x["price"] or 999999):
        key = (leg["airline"], leg["depart_time"], leg["date"])
        if key not in seen:
            seen[key] = True
            out.append(leg)
    return out


def _merge_rt(*pkg_lists):
    merged = []
    for pl in pkg_lists:
        merged.extend(pl)
    merged.sort(key=lambda x: x["price"] or 999999)
    # 去重
    seen, out = {}, []
    for pkg in merged:
        key = (pkg.get("out_airline"), pkg.get("out_depart_time"),
               pkg.get("ret_depart_time"), pkg.get("price"))
        if key not in seen:
            seen[key] = True
            out.append(pkg)
    return out


# ════════════════════════════════════════════════════════════════════════════
# 共用轉換 helpers
# ════════════════════════════════════════════════════════════════════════════

def _to_legs(raw_list, origin, dest, date, source):
    legs = []
    for f in raw_list:
        price = f.get("price", 0)
        if price and price < 500:
            price = round(price * 40)
        legs.append({
            "price":       price,
            "currency":    "TWD",
            "source":      source,
            "airline":     f.get("airline", ""),
            "flight_no":   f.get("flight_no", ""),
            "depart_time": f.get("depart_time", ""),
            "arrive_time": f.get("arrive_time", ""),
            "from":        f.get("from_airport") or origin,
            "to":          f.get("to_airport")   or dest,
            "duration":    f.get("duration", ""),
            "stops":       f.get("stops", 0),
            "stops_text":  f.get("stops_text", ""),
            "date":        date,
        })
    legs.sort(key=lambda x: x["price"] or 999999)
    return legs


def _to_rt_pkgs(raw_list, origin, dest, depart_date, return_date, source):
    pkgs = []
    for f in raw_list:
        price = f.get("price", 0)
        if price and price < 500:
            price = round(price * 40)
        pkgs.append({
            "price":           price,
            "currency":        "TWD",
            "source":          source,
            "out_airline":     f.get("out_airline", ""),
            "ret_airline":     f.get("ret_airline", ""),
            "out_depart_time": f.get("out_depart_time", ""),
            "out_arrive_time": f.get("out_arrive_time", ""),
            "ret_depart_time": f.get("ret_depart_time", ""),
            "ret_arrive_time": f.get("ret_arrive_time", ""),
            "out_from":        f.get("out_from") or origin,
            "out_to":          f.get("out_to")   or dest,
            "ret_from":        f.get("ret_from") or dest,
            "ret_to":          f.get("ret_to")   or origin,
            "out_duration":    f.get("out_duration", ""),
            "ret_duration":    f.get("ret_duration", ""),
            "out_stops":       f.get("out_stops", 0),
            "ret_stops":       f.get("ret_stops", 0),
            "out_stops_text":  f.get("out_stops_text", ""),
            "ret_stops_text":  f.get("ret_stops_text", ""),
            "depart_date":     depart_date,
            "return_date":     return_date,
        })
    pkgs.sort(key=lambda x: x["price"] or 999999)
    return pkgs


def _parse_raw_text(text, origin, dest):
    results = []
    times  = re.findall(r'\b(\d{2}:\d{2})\b', text)
    prices = re.findall(r'TWD\s*([\d,]+)', text)
    seen = set()
    for i in range(0, len(times) - 1, 2):
        pval = float(prices[i // 2].replace(",", "")) if i // 2 < len(prices) else 0
        key = (times[i], times[i + 1])
        if key not in seen and pval > 1000:
            seen.add(key)
            results.append({"price": pval, "airline": "", "depart_time": times[i],
                             "arrive_time": times[i + 1], "from_airport": origin,
                             "to_airport": dest, "duration": "", "stops": 0, "stops_text": ""})
    return results[:15]


def build_combos(outbound, inbound, top_n=5):
    if not outbound or not inbound:
        return []
    combos = []
    for o in outbound:
        for r in inbound:
            combos.append({
                "total":     (o["price"] or 0) + (r["price"] or 0),
                "out_idx":   outbound.index(o),
                "ret_idx":   inbound.index(r),
                "out_stops": o["stops"],
                "ret_stops": r["stops"],
                "out_src":   o.get("source", ""),
                "ret_src":   r.get("source", ""),
            })
    combos.sort(key=lambda c: c["total"])
    seen, results = set(), []
    for c in combos:
        key = (c["out_idx"], c["ret_idx"])
        if key not in seen:
            seen.add(key)
            results.append(c)
        if len(results) >= top_n:
            break
    return results


# ── Flask 路由 ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search", methods=["POST"])
def search():
    body        = request.get_json(force=True)
    origin      = body.get("origin", "").strip().upper()
    destination = body.get("destination", "").strip().upper()
    depart_date = body.get("depart_date", "")
    return_date = body.get("return_date", "") or None
    adults      = int(body.get("adults", 1))
    cabin_class = body.get("cabin_class", "ECONOMY")

    if not origin or not destination or not depart_date:
        return jsonify({"error": "請填寫出發地、目的地、出發日期"}), 400

    if return_date:
        outbound, inbound, rt_pkgs, errors = asyncio.run(
            search_all_roundtrip(origin, destination, depart_date, return_date, adults, cabin_class)
        )
        combos = build_combos(outbound, inbound)
        return jsonify({
            "mode":        "roundtrip",
            "outbound":    outbound,
            "inbound":     inbound,
            "combos":      combos,
            "rt_packages": rt_pkgs,
            "out_count":   len(outbound),
            "ret_count":   len(inbound),
            "rt_count":    len(rt_pkgs),
            "sources":     ["Trip.com", "Skyscanner"],
            "errors":      errors,
        })
    else:
        legs, errors = asyncio.run(
            search_all_oneway(origin, destination, depart_date, adults, cabin_class)
        )
        if errors and not legs:
            return jsonify({"error": "; ".join(errors)}), 500
        return jsonify({
            "mode":    "oneway",
            "outbound": legs,
            "count":   len(legs),
            "sources": ["Trip.com", "Skyscanner"],
        })


@app.route("/airports")
def airports():
    q = request.args.get("q", "").lower()
    AIRPORTS = [
        ("TPE", "台灣桃園 (TPE)"), ("KHH", "高雄小港 (KHH)"), ("RMQ", "台中清泉崗 (RMQ)"),
        ("TNN", "台南 (TNN)"), ("NRT", "東京成田 (NRT)"), ("HND", "東京羽田 (HND)"),
        ("KIX", "大阪關西 (KIX)"), ("ITM", "大阪伊丹 (ITM)"), ("CTS", "北海道新千歲 (CTS)"),
        ("OKA", "沖繩那霸 (OKA)"), ("FUK", "福岡 (FUK)"), ("NGO", "名古屋 (NGO)"),
        ("ICN", "首爾仁川 (ICN)"), ("GMP", "首爾金浦 (GMP)"), ("BKK", "曼谷素萬那普 (BKK)"),
        ("DMK", "曼谷廊曼 (DMK)"), ("SIN", "新加坡樟宜 (SIN)"), ("HKG", "香港 (HKG)"),
        ("MNL", "馬尼拉 (MNL)"), ("KUL", "吉隆坡 (KUL)"), ("CGK", "雅加達 (CGK)"),
        ("SYD", "雪梨 (SYD)"), ("MEL", "墨爾本 (MEL)"), ("LAX", "洛杉磯 (LAX)"),
        ("SFO", "舊金山 (SFO)"), ("JFK", "紐約甘迺迪 (JFK)"), ("ORD", "芝加哥 (ORD)"),
        ("SEA", "西雅圖 (SEA)"), ("LHR", "倫敦希斯洛 (LHR)"), ("CDG", "巴黎戴高樂 (CDG)"),
        ("AMS", "阿姆斯特丹 (AMS)"), ("FRA", "法蘭克福 (FRA)"), ("DXB", "杜拜 (DXB)"),
        ("NOU", "努美阿 (NOU)"),
    ]
    matched = [{"code": c, "name": n} for c, n in AIRPORTS if q in c.lower() or q in n.lower()]
    return jsonify(matched[:10])


if __name__ == "__main__":
    app.run(debug=True, port=5050)
