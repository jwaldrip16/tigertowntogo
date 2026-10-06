"""Readers for Data Dreamers and DeliverLogic ordering sites.
Each returns restaurant records in the same shape the Zuppler import uses."""
import re, html, json, time, urllib.request, urllib.parse, http.cookiejar
import concurrent.futures as cf, os

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()

def cents(s):
    m = re.search(r"(-?\d[\d,]*\.?\d*)", s or "")
    return int(round(float(m.group(1).replace(",", "")) * 100)) if m else 0

def to24(t):
    t = t.strip().upper().replace(".", "")
    if t in ("NOON",): return "12:00"
    if t in ("MIDNIGHT",): return "23:59"
    m = re.match(r"(\d{1,2})(?::(\d\d))?\s*(AM|PM)?", t)
    if not m: return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "PM" and h != 12: h += 12
    if ap == "AM" and h == 12: h = 0
    if h >= 24: return "23:59"
    return "%02d:%02d" % (h, mi)

def parse_ranges(txt):
    """'11 AM - 2 PM, 5 PM - 9 PM' -> ['11:00','21:00'] (first open to last close) or ''."""
    rs = re.findall(r"(\d{1,2}(?::\d\d)?\s*[AaPp]\.?[Mm]\.?|[Nn]oon|[Mm]idnight)\s*(?:-|–|to)\s*(\d{1,2}(?::\d\d)?\s*[AaPp]\.?[Mm]\.?|[Nn]oon|[Mm]idnight)", txt or "")
    if not rs: return ""
    a, b = to24(rs[0][0]), to24(rs[-1][1])
    if not a or not b: return ""
    if b <= a: b = "23:59"
    return [a, b]

class Web:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
    def get(self, url, data=None, ajax=False, tries=3, timeout=40):
        last = None
        for a in range(tries):
            try:
                h = {"User-Agent": UA}
                if ajax: h["X-Requested-With"] = "XMLHttpRequest"
                body = urllib.parse.urlencode(data).encode() if isinstance(data, (dict, list)) else data
                r = self.op.open(urllib.request.Request(url, data=body, headers=h), timeout=timeout)
                return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code == 404: raise
                last = e
                try:
                    txt = e.read().decode("utf-8", "replace")
                    if txt and e.code == 500 and "<form" in txt: return txt
                except Exception: pass
            except Exception as e:
                last = e
            time.sleep(1 + a)
        raise last

def base_of(site):
    site = (site or "").strip()
    if not site.startswith("http"): site = "https://" + site
    sp = urllib.parse.urlsplit(site)
    return sp.scheme + "://" + sp.netloc, site

def detect(site):
    """'zuppler', 'datadreamers', 'deliverlogic' or None."""
    base, full = base_of(site)
    w = Web()
    pages = []
    for u in (full, base + "/", base + "/restaurants"):
        try: pages.append(w.get(u, tries=2, timeout=25))
        except Exception: pass
    txt = " ".join(pages)
    if re.search(r"deliverlogic", txt, re.I) and re.search(r"/order/restaurant/", txt): return "deliverlogic"
    if re.search(r"dd_angular_site|datadreamers|Data Dreamers", txt) and re.search(r'href="/r/\d+/', txt): return "datadreamers"
    if re.search(r"zuppler", txt, re.I): return "zuppler"
    if re.search(r"deliverlogic", txt, re.I): return "deliverlogic"
    if re.search(r"dd_angular_site|datadreamers", txt, re.I): return "datadreamers"
    return None

# ---------------------------------------------------------------- Data Dreamers
def dd_pull(site, st, workers=6):
    base, full = base_of(site)
    w = Web()
    st.update(stage="Getting the restaurant list...")
    lst = w.get(base + "/restaurants")
    title = re.search(r"<title>(.*?)</title>", lst, re.S)
    zp = json.loads(w.get(base + "/getZipParameter.json.xsp", ajax=True))
    name = zp.get("branchName") or (clean(title.group(1)) if title else base)
    zips = [z.get("zip") for z in (zp.get("zipOption") or []) if z.get("zip")]
    links = list(dict.fromkeys(re.findall(r'href="(/r/(\d+)/[^"]+)"', lst)))
    seen, urls = set(), []
    for href, vid in links:
        if vid not in seen:
            seen.add(vid); urls.append((vid, base + href))
    # one directory block per restaurant on every page (address, phone, logo, lat/lng)
    info = {}
    for blk in re.findall(r'<div class="dd_collapsedEle">(.*?)</div>\s*</div>\s*</div>', lst, re.S):
        pass
    if not urls: raise ValueError(name + " has no restaurants listed.")
    if os.environ.get("IMPORT_TEST_LIMIT"): urls = urls[:int(os.environ["IMPORT_TEST_LIMIT"])]
    st.update(name=name, stage="Reading restaurants...", total=len(urls), done=0)
    pages = {}
    def one(v):
        vid, u = v
        try: return vid, u, w.get(u)
        except Exception: return vid, u, None
    with cf.ThreadPoolExecutor(workers) as ex:
        for vid, u, p in ex.map(one, urls):
            if p: pages[vid] = (u, p)
            st["done"] += 1
    # set a delivery zip so the site will show item add-ons
    zipc = zips[1] if len(zips) > 1 else (zips[0] if zips else "")
    w.get(base + "/getZipParameter.json.xsp?formZipCode=%s&refreshLeft=1" % zipc, ajax=True)
    rests, jobs = [], []
    for vid, (u, p) in pages.items():
        r = dd_parse(base, vid, u, p)
        if r:
            rests.append(r)
            jobs += [(vid, it["zid"]) for it in r["items"]]
    st.update(stage="Reading menu items, sizes and add-ons...", total=len(jobs), done=0)
    opts = {}
    def opt(j):
        try:
            d = json.loads(w.get(base + "/addNormalItem_app_json.xsp",
                                 {"forceOptions": "1", "columns": "1", "itemID": j[1]}, ajax=True))
            return j, d
        except Exception:
            return j, None
    with cf.ThreadPoolExecutor(workers) as ex:
        for j, d in ex.map(opt, jobs):
            opts[j] = d
            st["done"] += 1
    for r in rests:
        for it in r["items"]:
            it["groups"] = dd_groups(opts.get((r["zid"], it["zid"])))
    return {"source": name + " (Data Dreamers)", "site": base + "/", "platform": "datadreamers", "restaurants": rests}

def dd_parse(base, vid, url, p):
    m = re.search(r'id="dd_home_div"[^>]*data-dd_vendorName="([^"]*)"[^>]*', p)
    if not m: return None
    head = m.group(0)
    g = lambda k: html.unescape((re.search(k + r'="([^"]*)"', head) or [None, ""])[1]).strip()
    name = html.unescape(m.group(1)).strip()
    # this restaurant's own directory entry
    addr = phone = logo = ""; lat = lng = None
    for blk in re.split(r'<div class="dd_collapsedEle">', p)[1:]:
        if ("i%s." % vid) in blk[:600] or ("i%sm." % vid) in blk[:600]:
            st = re.search(r'streetAddress">([^<]*)', blk); ci = re.search(r'addressLocality">([^<]*)', blk)
            rg = re.search(r'addressRegion">([^<]*)', blk); pc = re.search(r'postalCode">([^<]*)', blk)
            ph = re.search(r'telephone">([^<]*)', blk); lo = re.search(r'image logo">([^<]*)', blk)
            la = re.search(r'latitude" content="([-\d.]+)', blk); ln = re.search(r'longitude" content="([-\d.]+)', blk)
            addr = ", ".join(x for x in [clean(st.group(1)) if st else "", (clean(ci.group(1)).title() if ci else "")] if x)
            if rg: addr += ", " + clean(rg.group(1)) + (" " + clean(pc.group(1)) if pc else "")
            phone = clean(ph.group(1)) if ph else ""
            logo = clean(lo.group(1)) if lo else ""
            lat = float(la.group(1)) if la else None; lng = float(ln.group(1)) if ln else None
            break
    lg = re.search(r'class="dd_logo-image-tag"[^>]*src="([^"]+)"', p)
    if lg: logo = urllib.parse.urljoin(base + "/", lg.group(1))
    hours = {str(i): "" for i in range(7)}
    for dn, rng in re.findall(r'dd_dayname">([^<]+)</div>\s*<div class="dd_opentimerange">([^<]*)', p):
        d = dn.strip().lower()
        if d in DAYS: hours[str(DAYS.index(d))] = parse_ranges(rng)
    items, sort = [], 0
    for sec in re.split(r'<section class="dd_menu-section"', p)[1:]:
        st_ = re.search(r'dd_menu-section-header-title">(.*?)</h3>', sec, re.S)
        sname = clean(st_.group(1)) if st_ else "Menu"
        for im in re.finditer(r'Lzip\((\d+),.*?<div class="dd_menu-item">(.*?)</div>\s*</a>', sec, re.S):
            body = im.group(2)
            t = re.search(r'dd_menu-item-title">(.*?)</div>', body, re.S)
            pr = re.search(r'dd_menu-item-price">(.*?)</p>', body, re.S)
            ds = re.search(r'dd_menu-item-description">(.*?)</p>', body, re.S)
            if not t: continue
            sort += 1
            items.append({"name": clean(t.group(1)), "description": clean(ds.group(1)) if ds else "",
                          "price_cents": cents(clean(pr.group(1))) if pr else 0, "section": sname, "tab": "",
                          "image": "", "sort": sort, "groups": [], "zid": int(im.group(1))})
    return {"zid": "dd" + vid, "name": name, "cuisine": g("data-dd_vendorcuisine"), "address": addr,
            "lat": lat, "lng": lng, "phone": phone, "hours": hours, "hours_raw": [], "photo": "", "logo": logo,
            "min_order_cents": None, "delivery_fee_cents": None, "eta_min": None, "prep": None,
            "paused": name.lower().startswith("zz") or "(old)" in name.lower(), "items": items,
            "description": g("data-dd_vendordescription"), "url": url}

def dd_groups(d):
    out = []
    og = ((d or {}).get("optionGroups") or {}).get("optionGroup") or []
    if isinstance(og, dict): og = [og]
    for grp in og:
        its = grp.get("groupItem") or []
        if isinstance(its, dict): its = [its]
        opts, seen = [], set()
        for o in its:
            if not o.get("isavailable", 1): continue
            k = (o.get("itemName") or "").strip()
            if not k or k.lower() in seen: continue
            seen.add(k.lower())
            opts.append({"name": k, "delta": int(round(float(o.get("itemPrice") or 0) * 100))})
        if not opts: continue
        single = grp.get("groupType") == 1
        mx = int(grp.get("groupMax") or 0)
        req = int(grp.get("groupRequired") or 0)
        out.append({"name": (grp.get("groupCaption") or grp.get("groupName") or "Options").strip(),
                    "min": 1 if (single and req) else (req if req and not single else 0),
                    "max": 1 if single else (mx if mx > 0 else len(opts)), "options": opts})
    return out

# ---------------------------------------------------------------- DeliverLogic
def dl_pull(site, st, workers=6):
    base, full = base_of(site)
    w = Web()
    st.update(stage="Getting the restaurant list...")
    lst = w.get(full)
    if "/order/restaurant/" not in lst:
        for u in (base + "/restaurants", base + "/order/restaurants", base + "/order/index"):
            try:
                t = w.get(u, tries=2)
                if "/order/restaurant/" in t: lst = t; break
            except Exception: pass
    ttl = re.search(r'<meta property="og:site_name" content="([^"]+)"', lst) or re.search(r"<title>(.*?)</title>", lst, re.S)
    name = clean(ttl.group(1)).split("|")[0].strip() if ttl else base
    links = re.findall(r'href="((?:https?://[^"/]+)?/order/restaurant/([^"/]+)/(\d+))"', lst)
    urls, seen = [], set()
    for href, slug, rid in links:
        if rid not in seen:
            seen.add(rid); urls.append((rid, urllib.parse.urljoin(base + "/", href)))
    if not urls: raise ValueError(name + " has no restaurants listed.")
    if os.environ.get("IMPORT_TEST_LIMIT"): urls = urls[:int(os.environ["IMPORT_TEST_LIMIT"])]
    st.update(name=name, stage="Reading restaurants...", total=len(urls), done=0)
    pages = {}
    def one(v):
        rid, u = v
        try: return rid, u, w.get(u)
        except Exception: return rid, u, None
    with cf.ThreadPoolExecutor(workers) as ex:
        for rid, u, p in ex.map(one, urls):
            if p: pages[rid] = (u, p)
            st["done"] += 1
    rests, jobs = [], []
    for rid, (u, p) in pages.items():
        r = dl_parse(rid, u, p)
        if r:
            rests.append(r)
            jobs += [(rid, it["zid"], it.pop("_disp")) for it in r["items"]]
    # the site only shows add-ons once a delivery address is set; use a restaurant's own address
    addr = next((r["address"] for r in rests if re.search(r"\d{5}", r["address"] or "")), "")
    if addr:
        zipc = re.findall(r"\d{5}", addr)[-1]
        try:
            w.get(base + "/order", {"address": addr, "zip_code": zipc, "ORDER_WHEN[date]": "", "ORDER_WHEN[time]": "ASAP",
                                    "ORDER_WHEN[type]": "DELIVERY", "on_checkout_page": "false",
                                    "update_order_params": "true"}, ajax=True)
        except Exception: pass
    st.update(stage="Reading menu items, sizes and add-ons...", total=len(jobs), done=0)
    opts = {}
    def opt(j):
        try: return j, w.get(base + "/order/item/%s/%s/%s" % j, ajax=True, tries=2)
        except Exception: return j, None
    with cf.ThreadPoolExecutor(workers) as ex:
        for j, d in ex.map(opt, jobs):
            opts[(j[0], j[1])] = d
            st["done"] += 1
    for r in rests:
        for it in r["items"]:
            it["groups"] = dl_groups(opts.get((r["zid"][2:], it["zid"])))
    return {"source": name + " (DeliverLogic)", "site": base + "/", "platform": "deliverlogic", "restaurants": rests}

def dl_parse(rid, url, p):
    h = re.search(r'<h4[^>]*>\s*([^<]{2,120}?)\s*</h4>\s*<div class="restaurant_menu_info"', p)
    t = re.search(r"<title>(.*?)</title>", p, re.S)
    name = clean(h.group(1)) if h else (clean(t.group(1)).split("|")[0].replace("Delivery Menu", "").strip() if t else "")
    if not name: return None
    ad = re.search(r'restaurant_menu_info-addresss">(.*?)</span>', p, re.S)
    ds = re.search(r'restaurant_menu_info-description">(.*?)</span>', p, re.S)
    hours = {str(i): "" for i in range(7)}
    hp = p.split("order_restaurant--open_hours_heading", 1)
    if len(hp) > 1:
        blk = hp[1].split("</table>", 1)[0][:6000]
        for dn, rng in re.findall(r"<span class='pull-left'>([A-Za-z]+)</span><span class='pull-right'>(.*?)</span>", blk, re.S):
            d = dn.strip().lower()
            if d in DAYS and not hours[str(DAYS.index(d))]:
                hours[str(DAYS.index(d))] = parse_ranges(clean(rng))
    logo = re.search(r'class="[^"]*restaurant[^"]*logo[^"]*"[^>]*src="([^"]+)"', p)
    items, sort, seen = [], 0, set()
    for sec in re.split(r'<div class="restaurant_heading', p)[1:]:
        sid = re.search(r'id="heading-([^"]+)"', sec[:300])
        if sid and sid.group(1).upper() == "POPULAR": continue
        sh = re.search(r"<h4[^>]*>(.*?)</h4>", sec, re.S)
        sname = clean(re.sub(r"<span.*", "", sh.group(1), flags=re.S)) if sh else "Menu"
        for im in re.finditer(r"view_restaurant_item\('[^']*',\s*'(\d+)',\s*'(\d+)',\s*'([^']+)'", sec):
            miid = im.group(2)
            if miid in seen: continue
            chunk = sec[im.end(): im.end() + 4000]
            chunk = chunk.split("view_restaurant_item(", 1)[0]
            nm = re.search(r'order_restaurant--menu_item_name">(.*?)</div>', chunk, re.S)
            pr = re.search(r'menu_item_price">(.*?)</div>', chunk, re.S)
            de = re.search(r'order_restaurant--menu_item_description">(.*?)</div>', chunk, re.S)
            ig = re.search(r'data-src="(https?://[^"]+/menuitems/[^"]+)"', chunk)
            if not nm: continue
            seen.add(miid); sort += 1
            items.append({"name": clean(nm.group(1)), "description": clean(de.group(1)) if de else "",
                          "price_cents": cents(clean(pr.group(1))) if pr else 0, "section": sname, "tab": "",
                          "image": ig.group(1) if ig else "", "sort": sort, "groups": [], "zid": int(miid),
                          "_disp": im.group(3)})
    nml = name.lower()
    return {"zid": "dl" + rid, "name": name, "cuisine": "", "address": clean(ad.group(1)) if ad else "",
            "lat": None, "lng": None, "phone": "", "hours": hours, "hours_raw": [], "photo": "",
            "logo": logo.group(1) if logo else "", "min_order_cents": None, "delivery_fee_cents": None,
            "eta_min": None, "prep": None, "paused": "catering only" in nml or "non-partner" in nml,
            "items": items, "description": clean(ds.group(1)) if ds else "", "url": url}

def dl_groups(s):
    out = []
    if not s or "<form" not in s: return out
    for fg in re.finditer(r'<div class="form-group([^"]*)">\s*<label[^>]*>(.*?)</label>(.*?)(?=<div class="form-group|</fieldset>)', s, re.S):
        label, body = clean(fg.group(2)), fg.group(3)
        if "OPTIONS" not in body: continue
        req = "required" in fg.group(1).lower() or "required" in label.lower()
        m = re.search(r"Choose\s+(?:up to\s+)?(\d+)", label, re.I)
        cnt = int(m.group(1)) if m else 0
        opts = []
        if "<select" in body:
            for v, t in re.findall(r'<option value="(\d+)"[^>]*>(.*?)</option>', body, re.S):
                opts.append(clean(t))
            single = True
        else:
            for t in re.findall(r'<label[^>]*>\s*<input[^>]*type="(?:checkbox|radio)"[^>]*>(.*?)</label>', body, re.S):
                opts.append(clean(t))
            single = 'type="radio"' in body
        res = []
        for t in opts:
            pm = re.search(r"\((?:Add|\+)\s*\$?([\d.,]+)\)", t, re.I)
            nm = re.sub(r"\s*\((?:Add|\+)\s*\$?[\d.,]+\)\s*", "", t).strip()
            if nm and nm.lower() not in [x["name"].lower() for x in res]:
                res.append({"name": nm, "delta": int(round(float(pm.group(1).replace(",", "")) * 100)) if pm else 0})
        if not res: continue
        name = re.sub(r"\s*\((?:Required|Optional)[^)]*\)\s*$", "", label, flags=re.I).strip() or "Options"
        if single:
            mn, mx = (1 if req else 0), 1
        else:
            mx = cnt if cnt else len(res)
            mn = (cnt if (req and re.search(r"Choose\s+\d", label, re.I) and not re.search(r"up to", label, re.I)) else (1 if req else 0))
        out.append({"name": name, "min": mn, "max": mx, "options": res})
    return out


# ---------------------------------------------------------------- whole-website copy
def _txt(fragment):
    t = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", fragment, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return clean(html.unescape(t))


def _meta(page, *names):
    for n in names:
        m = re.search(r'<meta[^>]+(?:name|property)=["\']%s["\'][^>]*>' % re.escape(n), page, re.I)
        if m:
            c = re.search(r'content=["\']([^"\']*)["\']', m.group(0), re.I)
            if c and c.group(1).strip():
                return clean(html.unescape(c.group(1)))
    return ""


def _abs(base, u):
    u = html.unescape((u or "").strip())
    if not u or u.startswith(("data:", "javascript:", "#")):
        return ""
    return urllib.parse.urljoin(base, u)


def _faqs(page):
    out = []
    for blk in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            j = json.loads(blk.strip())
        except Exception:
            continue
        for node in (j if isinstance(j, list) else j.get("@graph", [j])):
            if isinstance(node, dict) and node.get("@type") == "FAQPage":
                for q in node.get("mainEntity") or []:
                    a = (q.get("acceptedAnswer") or {}).get("text", "")
                    out.append((_txt(q.get("name", "")), _txt(a)))
    if out:
        return out
    for summ, body in re.findall(r"<summary[^>]*>(.*?)</summary>(.*?)</details>", page, re.S | re.I):
        out.append((_txt(summ), _txt(body)))
    if out:
        return out
    for dt_, dd in re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", page, re.S | re.I):
        out.append((_txt(dt_), _txt(dd)))
    if out:
        return out
    parts = re.split(r"(<h[2-6][^>]*>.*?</h[2-6]>)", page, flags=re.S | re.I)
    for i in range(1, len(parts) - 1, 2):
        q = _txt(parts[i])
        if q.endswith("?") and 8 <= len(q) <= 200:
            a = _txt(parts[i + 1])
            if a:
                out.append((q, a[:1500]))
    return out


def _hex6(v):
    v = v.lower()
    if len(v) == 4:
        v = "#" + "".join(c * 2 for c in v[1:])
    return v


def _colorful(v):
    r, g, b = (int(v[i:i + 2], 16) for i in (1, 3, 5))
    hi, lo = max(r, g, b), min(r, g, b)
    return hi - lo > 60 and hi > 70 and not (lo > 215)


def guess_colors(w, base, page):
    """The site's main brand colors: its theme color, then the most used bright colors in its styles."""
    from collections import Counter
    picks = []
    tc = _meta(page, "theme-color", "msapplication-TileColor")
    if re.fullmatch(r"#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}", tc or ""):
        if _colorful(_hex6(tc)):
            picks.append(_hex6(tc))
    css = " ".join(re.findall(r"<style[^>]*>(.*?)</style>", page, re.S | re.I))
    css += " ".join(re.findall(r'style=["\']([^"\']+)["\']', page, re.I))
    host = urllib.parse.urlparse(base).netloc
    for href in re.findall(r'<link[^>]+rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)', page, re.I)[:6]:
        full = _abs(base, href)
        if urllib.parse.urlparse(full).netloc != host:
            continue
        try:
            css += " " + w.get(full, tries=1, timeout=15)[:600000]
        except Exception:
            pass
    cnt = Counter()
    for m in re.findall(r"(?:color|background(?:-color)?|border(?:-color)?|fill)\s*:\s*[^;}]*?(#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b)", css):
        v = _hex6(m)
        if _colorful(v):
            cnt[v] += 1
    for v, _n in cnt.most_common(12):
        if v not in picks:
            picks.append(v)
        if len(picks) >= 3:
            break
    return picks[:3]


def site_pull(site, st=None):
    """Read a public website and pull the parts that fit the Fleet Foot Delivery customer website."""
    st = st if st is not None else {}
    w = Web()
    url = site.strip()
    if not url.startswith("http"):
        url = "https://" + url
    st.update(stage="Reading the website...")
    page = w.get(url)
    base = url
    host = urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    out = {"site": url, "pages": [url]}
    title = clean(html.unescape((re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I) or [None, ""])[1]))
    name = _meta(page, "og:site_name", "application-name", "apple-mobile-web-app-title")
    if not name and title:
        name = re.split(r"\s+[-|–:•]\s+", title)[0]
    out["business_name"] = name[:60]
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
    out["home_headline"] = (_txt(h1.group(1)) if h1 else "")[:120]
    out["home_sub"] = (_meta(page, "description", "og:description"))[:300]
    # logo
    logo = ""
    for m in re.finditer(r"<img[^>]+>", page, re.I):
        tag = m.group(0)
        if re.search(r"logo", tag, re.I):
            src = re.search(r'(?:data-src|src)=["\']([^"\']+)["\']', tag, re.I)
            if src:
                logo = _abs(base, src.group(1)); break
    if not logo:
        m = re.search(r'<link[^>]+rel=["\'](?:apple-touch-icon|icon|shortcut icon)["\'][^>]*href=["\']([^"\']+)', page, re.I)
        logo = _abs(base, m.group(1)) if m else ""
    out["logo"] = logo
    try:
        out["colors"] = guess_colors(w, base, page)
    except Exception:
        out["colors"] = []
    # big pictures
    pics = []
    og = _meta(page, "og:image", "twitter:image")
    if og:
        pics.append(_abs(base, og))
    for u in re.findall(r"background(?:-image)?\s*:\s*url\(['\"]?([^)'\"]+)", page, re.I):
        pics.append(_abs(base, u))
    for m in re.finditer(r"<img[^>]+>", page, re.I):
        tag = m.group(0)
        if re.search(r"logo|icon|step|play|store|badge|twitter|facebook|instagram|avatar|sprite|pixel", tag, re.I):
            continue
        src = re.search(r'(?:data-src|src)=["\']([^"\']+)["\']', tag, re.I)
        if src and re.search(r"\.(jpe?g|png|webp)(\?|$)", src.group(1), re.I):
            pics.append(_abs(base, src.group(1)))
    seen = []
    for p in pics:
        if p and p not in seen and p != logo:
            seen.append(p)
    out["pictures"] = seen[:8]
    # contact
    tel = re.search(r'href=["\']tel:([^"\']+)', page, re.I)
    out["phone"] = re.sub(r"\D", "", tel.group(1))[-10:] if tel else ""
    mail = re.search(r'href=["\']mailto:([^"\'?]+)', page, re.I)
    out["business_email"] = mail.group(1).strip()[:120] if mail else ""
    soc = {}
    for u in re.findall(r'href=["\'](https?://[^"\']+)', page, re.I):
        lu = u.lower()
        if "facebook.com/" in lu and "sharer" not in lu: soc.setdefault("facebook", u)
        elif "instagram.com/" in lu: soc.setdefault("instagram", u)
        elif re.search(r"//(www\.)?(twitter|x)\.com/", lu) and "intent" not in lu: soc.setdefault("x", u)
    out["socials"] = soc
    text = _txt(page)
    addr = re.search(r'itemprop=["\']streetAddress["\'][^>]*>(.*?)<', page, re.S | re.I)
    a2 = re.search(r"Address:?\s*(.{5,120}?\b[A-Z]{2}\s+\d{5})", text)
    out["business_address"] = (_txt(addr.group(1)) if addr else (a2.group(1) if a2 else ""))[:160]
    # how it works: a "How it works" heading followed by three heading + paragraph steps
    lines = [l for l in (clean(x) for x in re.sub(r"<[^>]+>", "\n", re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page, flags=re.S | re.I)).split("\n")) if l]
    lines = [html.unescape(l) for l in lines]
    for i, l in enumerate(lines):
        if l.lower().strip(" :") == "how it works" and i + 6 < len(lines):
            out["how_title"] = l.title()[:60]
            for n in range(3):
                out["how%d_t" % (n + 1)] = lines[i + 1 + n * 2][:60]
                out["how%d_p" % (n + 1)] = lines[i + 2 + n * 2][:300]
            break
    for i, l in enumerate(lines):
        if re.search(r"\b(app|pocket)\b", l, re.I) and len(l) < 80 and i + 1 < len(lines) and len(lines[i + 1]) > 30:
            out["pocket_title"], out["pocket_text"] = l[:80], lines[i + 1][:400]
            break
    # other pages on the same site: FAQ / about / contact
    links = []
    for u, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, re.S | re.I):
        full = _abs(base, u)
        if not full or urllib.parse.urlparse(full).netloc.lower().replace("www.", "") != host:
            continue
        lab = (_txt(label) + " " + u).lower()
        if re.search(r"faq|question|about|contact|help", lab) and full not in links:
            links.append(full)
    faqs = _faqs(page)
    about = ""
    for u in links[:6]:
        try:
            p = w.get(u)
        except Exception:
            continue
        out["pages"].append(u)
        if not faqs and re.search(r"faq|question|help", u, re.I):
            faqs = _faqs(p)
        if not faqs:
            faqs = _faqs(p) if re.search(r"frequently asked|faq", p, re.I) else []
        if not about and re.search(r"about", u, re.I):
            main = re.search(r"<main[^>]*>(.*?)</main>", p, re.S | re.I)
            about = _txt(main.group(1) if main else p)[:1500]
        if not out["phone"]:
            t2 = re.search(r'href=["\']tel:([^"\']+)', p, re.I)
            out["phone"] = re.sub(r"\D", "", t2.group(1))[-10:] if t2 else ""
        if not out["business_email"]:
            m2 = re.search(r'href=["\']mailto:([^"\'?]+)', p, re.I)
            out["business_email"] = m2.group(1).strip()[:120] if m2 else ""
    out["faqs"] = [(q[:200], a[:1500]) for q, a in faqs if q and a][:40]
    out["about"] = about
    st.update(stage="Done.")
    return out
