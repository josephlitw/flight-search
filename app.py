"""
機票搜尋器 - 反追蹤版本（Trip.com Playwright 無頭瀏覽器）
來回票雙軌設計：
  軌一 整票來回（triptype=rt）：去回一起買，通常有專屬折扣
  軌二 自選拼接（兩段獨立單程）：去程 A 航 + 回程 B 航、去直回轉皆可
  三個搜尋全部平行發出，不互等
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

CABIN_CODE = {"ECONOMY": "y", "PREMIUM_ECONOMY": "s", "BUSINESS": "c", "FIRST": "f"}

TRACKER_DOMAINS = [
    "google-analytics", "googletagmanager", "doubleclick",
    "facebook.net", "connect.facebook", "criteo", "quantserve",
    "scorecardresearch", "adsymptotic", "amazon-adsystem",
]

# 單程卡片解析
EXTRACT_JS = """() => {
    const flights = [];
    const cards = document.querySelectorAll(
        '.result-item.J_FlightItem, .m-result-list .result-item'
    );
    cards.forEach(card => {
        try {
            const priceEl = card.querySelector('[class*="item-con-price"], [class*="o-price-flight"]');
            const priceText = priceEl ? priceEl.innerText.trim() : '';
            const priceNum = parseFloat(priceText.replace(/[^0-9.]/g, '')) || 0;

            const timeEls = card.querySelectorAll('[class*="airline__timers"] [class*="time"], [class*="time_c"]');
            const times = Array.from(timeEls).map(e => e.innerText.trim()).filter(t => /^\\d{2}:\\d{2}/.test(t));

            const codePairs = card.querySelectorAll('.pl-2.pr-2');
            const fromAirport = codePairs[0] ? codePairs[0].innerText.trim() : '';
            const toAirport   = codePairs[2] ? codePairs[2].innerText.trim() : '';

            const airlineEl = card.querySelector('[class*="flights-name"]');
            const airline = airlineEl ? airlineEl.innerText.trim() : '';

            const durEl = card.querySelector('[class*="info-duration"]');
            const duration = durEl ? durEl.innerText.trim() : '';

            const stopEl = card.querySelector('[class*="stop__text"]');
            const stopText = stopEl ? stopEl.innerText.trim() : '';
            const stops = (stopText.toLowerCase().includes('direct') ||
                           stopText.toLowerCase().includes('nonstop')) ? 0
                        : (stopText.match(/\\d+/) ? parseInt(stopText.match(/\\d+/)[0]) : 1);

            if (priceNum > 0 || airline) {
                flights.push({ price: priceNum, airline, depart_time: times[0]||'',
                    arrive_time: times[1]||'', from_airport: fromAirport,
                    to_airport: toAirport, duration, stops, stops_text: stopText });
            }
        } catch(e) {}
    });
    return flights;
}"""

# 整票來回卡片解析（每張卡含去回兩段資訊）
EXTRACT_JS_RT = """() => {
    const pkgs = [];
    const cards = document.querySelectorAll(
        '.result-item.J_FlightItem, .m-result-list .result-item'
    );
    cards.forEach(card => {
        try {
            const priceEl = card.querySelector('[class*="item-con-price"], [class*="o-price-flight"]');
            const priceText = priceEl ? priceEl.innerText.trim() : '';
            const priceNum = parseFloat(priceText.replace(/[^0-9.]/g, '')) || 0;

            const timeEls = card.querySelectorAll('[class*="airline__timers"] [class*="time"], [class*="time_c"]');
            const times = Array.from(timeEls).map(e => e.innerText.trim()).filter(t => /^\\d{2}:\\d{2}/.test(t));

            const airlineEls = card.querySelectorAll('[class*="flights-name"]');
            const codePairs  = card.querySelectorAll('.pl-2.pr-2');
            const durEls     = card.querySelectorAll('[class*="info-duration"]');
            const stopEls    = card.querySelectorAll('[class*="stop__text"]');

            function stopCount(el) {
                if (!el) return 0;
                const t = el.innerText.trim().toLowerCase();
                if (t.includes('direct') || t.includes('nonstop')) return 0;
                const m = t.match(/\\d+/);
                return m ? parseInt(m[0]) : 1;
            }

            if (priceNum > 0) {
                pkgs.push({
                    price:           priceNum,
                    out_depart_time: times[0] || '',
                    out_arrive_time: times[1] || '',
                    ret_depart_time: times[2] || '',
                    ret_arrive_time: times[3] || '',
                    out_airline:     airlineEls[0] ? airlineEls[0].innerText.trim() : '',
                    ret_airline:     airlineEls[1] ? airlineEls[1].innerText.trim()
                                   : (airlineEls[0] ? airlineEls[0].innerText.trim() : ''),
                    out_from:        codePairs[0] ? codePairs[0].innerText.trim() : '',
                    out_to:          codePairs[2] ? codePairs[2].innerText.trim() : '',
                    ret_from:        codePairs[4] ? codePairs[4].innerText.trim() : '',
                    ret_to:          codePairs[6] ? codePairs[6].innerText.trim() : '',
                    out_duration:    durEls[0] ? durEls[0].innerText.trim() : '',
                    ret_duration:    durEls[1] ? durEls[1].innerText.trim() : '',
                    out_stops:       stopCount(stopEls[0]),
                    ret_stops:       stopCount(stopEls[1]),
                    out_stops_text:  stopEls[0] ? stopEls[0].innerText.trim() : '',
                    ret_stops_text:  stopEls[1] ? stopEls[1].innerText.trim() : '',
                });
            }
        } catch(e) {}
    });
    return pkgs;
}"""


async def _new_browser_ctx(p):
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
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
    )

    async def block_trackers(route):
        if any(t in route.request.url.lower() for t in TRACKER_DOMAINS):
            await route.abort()
        else:
            await route.continue_()

    await ctx.route("**/*", block_trackers)
    return browser, ctx


async def scrape_oneway(origin, dest, date, adults, cabin):
    """單程搜尋 — 每次開全新無痕 context，搜完即銷毀"""
    cabin_code = CABIN_CODE.get(cabin, "y")
    url = (
        f"https://uk.trip.com/flights/showfarefirst"
        f"?dcity={origin.lower()}&acity={dest.lower()}"
        f"&ddate={date}&triptype=ow&class={cabin_code}"
        f"&quantity={adults}&curr=TWD&locale=en-GB"
        f"&nonstoponly=off&searchboxarg=t"
    )

    async with async_playwright() as p:
        browser, ctx = await _new_browser_ctx(p)
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector(
                    ".result-item.J_FlightItem, .m-result-list .result-item",
                    timeout=25000,
                )
            except Exception:
                await page.wait_for_timeout(8000)
            await page.wait_for_timeout(2000)

            raw = await page.evaluate(EXTRACT_JS)
            if not raw:
                text = await page.inner_text(".m-result-list, body")
                raw = _parse_raw_text(text, origin, dest)

            await ctx.close()
            await browser.close()

            for f in raw:
                f["currency"] = "TWD"
                if f["price"] and f["price"] < 500:
                    f["price"] = round(f["price"] * 40)

            return _to_leg(raw, origin, dest, date), None

        except Exception as e:
            try:
                await ctx.close()
                await browser.close()
            except Exception:
                pass
            return [], str(e)


async def scrape_roundtrip(origin, dest, depart_date, return_date, adults, cabin):
    """整票來回搜尋（triptype=rt）— 每次開全新無痕 context，搜完即銷毀"""
    cabin_code = CABIN_CODE.get(cabin, "y")
    url = (
        f"https://uk.trip.com/flights/showfarefirst"
        f"?dcity={origin.lower()}&acity={dest.lower()}"
        f"&ddate={depart_date}&adate={return_date}&triptype=rt&class={cabin_code}"
        f"&quantity={adults}&curr=TWD&locale=en-GB"
        f"&nonstoponly=off&searchboxarg=t"
    )

    async with async_playwright() as p:
        browser, ctx = await _new_browser_ctx(p)
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector(
                    ".result-item.J_FlightItem, .m-result-list .result-item",
                    timeout=25000,
                )
            except Exception:
                await page.wait_for_timeout(8000)
            await page.wait_for_timeout(2000)

            raw = await page.evaluate(EXTRACT_JS_RT)

            await ctx.close()
            await browser.close()

            pkgs = []
            for f in raw:
                price = f.get("price", 0)
                if price and price < 500:
                    price = round(price * 40)
                pkgs.append({
                    "price":           price,
                    "currency":        "TWD",
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
            return pkgs, None

        except Exception as e:
            try:
                await ctx.close()
                await browser.close()
            except Exception:
                pass
            return [], str(e)


def _to_leg(raw_list, origin, dest, date):
    legs = []
    for f in raw_list:
        legs.append({
            "price":       f.get("price", 0),
            "currency":    f.get("currency", "TWD"),
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


# ── Flask 路由 ────────────────────────────────────────────────────

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
        # 三個搜尋同時平行發出：去程單程 + 回程單程 + 整票來回
        async def search_all():
            return await asyncio.gather(
                scrape_oneway(origin, destination, depart_date, adults, cabin_class),
                scrape_oneway(destination, origin, return_date, adults, cabin_class),
                scrape_roundtrip(origin, destination, depart_date, return_date, adults, cabin_class),
            )

        (out_legs, out_err), (ret_legs, ret_err), (rt_pkgs, rt_err) = asyncio.run(search_all())

        combos = build_combos(out_legs, ret_legs)
        return jsonify({
            "mode":        "roundtrip",
            "outbound":    out_legs,
            "inbound":     ret_legs,
            "combos":      combos,
            "rt_packages": rt_pkgs,
            "out_count":   len(out_legs),
            "ret_count":   len(ret_legs),
            "rt_count":    len(rt_pkgs),
            "source":      "Trip.com",
            "errors":      [e for e in [out_err, ret_err, rt_err] if e],
        })
    else:
        legs, err = asyncio.run(
            scrape_oneway(origin, destination, depart_date, adults, cabin_class)
        )
        if err and not legs:
            return jsonify({"error": err}), 500
        return jsonify({
            "mode":    "oneway",
            "outbound": legs,
            "count":   len(legs),
            "source":  "Trip.com",
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
