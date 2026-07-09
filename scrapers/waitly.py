"""
Scraper til my.waitly.dk
Én browser-session: logger ind én gang og skraber begge sider.
"""
import re
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SOURCE_ID   = 'waitly'
SOURCE_NAME = 'Waitly'

_MONTHS_DK = {
    'januar':1,'februar':2,'marts':3,'april':4,'maj':5,'juni':6,
    'juli':7,'august':8,'september':9,'oktober':10,'november':11,'december':12
}

def _deadline_expired(s: str) -> bool:
    m = re.search(r'(\d+)\.\s+(\w+)\s+(\d{4})(?:\s+kl[.:]\s*(\d{1,2})[.:](\d{2}))?', s or '', re.I)
    if not m:
        return False
    mon = _MONTHS_DK.get(m.group(2).lower())
    if not mon:
        return False
    d = datetime(int(m.group(3)), mon, int(m.group(1)),
                 int(m.group(4) or 23), int(m.group(5) or 59))
    return d < datetime.now()


def scrape_all(config: dict) -> tuple[list[dict], list[dict]]:
    """
    Logger ind og returnerer (listings, waitlists) i én session.
    Kræver config-nøgler: email, password
    """
    email    = config.get('email', '')
    password = config.get('password', '')
    if not email or not password:
        raise RuntimeError('Waitly: mangler email eller adgangskode i konfigurationen.')

    listings  = []
    waitlists = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
        )
        page = browser.new_page()
        try:
            # ── Login ─────────────────────────────────────────────────────────
            print(f'[{SOURCE_ID}] Navigerer til my.waitly.dk ...')
            page.goto('https://my.waitly.dk', timeout=30_000)
            page.wait_for_load_state('networkidle', timeout=15_000)
            print(f'[{SOURCE_ID}] Side indlæst. URL: {page.url}')

            if page.query_selector('input[name="email"]'):
                print(f'[{SOURCE_ID}] Login-formular fundet — logger ind ...')
                page.fill('input[name="email"]', email)
                page.fill('input[name="password"]', password)
                page.click('button[type="submit"]')
                try:
                    page.wait_for_function(
                        '() => !document.querySelector("input[name=\'password\']")',
                        timeout=15_000,
                    )
                    print(f'[{SOURCE_ID}] Login OK. URL: {page.url}')
                except PWTimeout:
                    print(f'[{SOURCE_ID}] Login timeout. URL: {page.url}')
                    raise RuntimeError('Waitly: login fejlede – kontrollér dine oplysninger.')
            else:
                print(f'[{SOURCE_ID}] Ingen login-formular — allerede logget ind eller redirect.')

            # ── Venteliste-pladser (overblik / dashboard) ──────────────────
            page.wait_for_load_state('networkidle', timeout=10_000)
            page.wait_for_timeout(1_500)
            print(f'[{SOURCE_ID}] Scraper ventelister. URL: {page.url}')
            waitlists = _scrape_waitlists(page)
            print(f'[{SOURCE_ID}] Ventelister fundet: {len(waitlists)}')

            # ── Boliger til salg (tilbud) ──────────────────────────────────
            page.goto('https://my.waitly.dk/offers', timeout=30_000)
            page.wait_for_load_state('networkidle', timeout=15_000)
            # Waitly er en Vue-SPA: vent på at bolig-kortene faktisk er tegnet,
            # ikke bare at siden er loadet. Ellers læser vi en tom side.
            try:
                page.wait_for_selector('a[href^="/offers/"]', timeout=15_000)
            except PWTimeout:
                print(f'[{SOURCE_ID}] Ingen bolig-links dukkede op (måske reelt 0, eller login/side-fejl).')
            page.wait_for_timeout(1_500)
            print(f'[{SOURCE_ID}] Scraper boliger. URL: {page.url}')
            listings = _scrape_listings(page)
            print(f'[{SOURCE_ID}] Boliger fundet: {len(listings)}')

        finally:
            browser.close()

    return listings, waitlists


# ── Intern hjælpefunktioner ────────────────────────────────────────────────────

def _scrape_waitlists(page) -> list[dict]:
    results = []
    # Struktur: div.grid.grid-cols-1 > div.order-1 (navn+slutdato) + div.order-2 (plads+status)
    for row in page.query_selector_all('.grid.grid-cols-1'):
        try:
            order1 = row.query_selector('.order-1')
            order2 = row.query_selector('.order-2')
            if not order1 or not order2:
                continue

            # Navn: første p.font-bold i order-1
            name_el = order1.query_selector('p.font-bold')
            if not name_el:
                continue
            name = name_el.inner_text().strip()

            # Slutdato: p uden font-bold i order-1
            end_el   = order1.query_selector('p:not(.font-bold)')
            end_date = end_el.inner_text().strip().replace('Slutdato:', '').strip() if end_el else ''

            # Plads: p.font-bold i order-2 → "14 af 15"
            pos_el   = order2.query_selector('p.font-bold')
            if not pos_el:
                continue
            pos_text = pos_el.inner_text().strip()
            parts    = pos_text.split(' af ')
            if len(parts) != 2 or not parts[0].strip().isdigit():
                continue
            position = parts[0].strip()
            total    = parts[1].strip()

            # Status: success/error/warning farve-klasse, fallback til tekstsøgning
            status_el = order2.query_selector('p.text-success-500, p.text-error-500, p.text-warning-500')
            if status_el:
                status = status_el.inner_text().strip()
            else:
                status = next(
                    (p.inner_text().strip()
                     for p in order2.query_selector_all('p')
                     if any(w in p.inner_text() for w in ('Aktiv', 'Inaktiv', 'aktiv'))),
                    '',
                )

            results.append({
                'id':       f'{SOURCE_ID}:waitlist:{name}',
                'source':   SOURCE_NAME,
                'name':     name,
                'position': position,
                'total':    total,
                'status':   status,
                'end_date': end_date,
            })
        except Exception as e:
            print(f'[{SOURCE_ID}] waitlist row error: {e}')

    return results


def _scrape_listing_details(page, url: str) -> dict:
    """Besøg en boligside og hent pris, månedlig ydelse, værelser, størrelse, etage."""
    try:
        page.goto(url, timeout=20_000)
        page.wait_for_load_state('networkidle', timeout=10_000)
        page.wait_for_timeout(800)
        raw = page.evaluate('''() => {
            const out = {};
            document.querySelectorAll(".my-3").forEach(row => {
                const spans = row.querySelectorAll(":scope > div:first-child span");
                const label = [...spans].find(s => !s.className.includes("iconify"))
                                        ?.textContent?.trim()?.replace(":","");
                const cells = row.querySelectorAll(":scope > div:last-child span");
                const value = cells[0]?.textContent?.trim();
                const unit  = cells[1]?.textContent?.trim();
                if (label && value) out[label] = unit ? value + " " + unit : value;
            });
            return out;
        }''')
        return {
            'price':           raw.get('Pris', ''),
            'recurring_price': raw.get('Månedlig pris', ''),
            'rooms':           raw.get('Værelser', ''),
            'size':            raw.get('Størrelse', ''),
            'floor':           raw.get('Etage', ''),
        }
    except Exception as e:
        print(f'[{SOURCE_ID}] detail fejl for {url}: {e}')
        return {}


def _scrape_listings(page) -> list[dict]:
    results = []
    for link in page.query_selector_all('a[href^="/offers/"]'):
        try:
            href = link.get_attribute('href') or ''
            uid  = href.rsplit('/', 1)[-1]
            if not uid or len(uid) < 10:
                continue

            card = page.query_selector(f'[data-slot="body"]:has(a[href="{href}"])')
            if not card:
                continue

            h1    = card.query_selector('h1')
            title = h1.inner_text().strip() if h1 else ''

            association = list_type = published = address = deadline = ''
            for p_el in card.query_selector_all('p'):
                text = p_el.inner_text().strip()
                if not text:
                    continue
                if text.startswith('Liste:'):
                    list_type = text.removeprefix('Liste:').strip()
                elif text.startswith('Udgivet:'):
                    published = text.removeprefix('Udgivet:').strip()
                elif text.startswith('Adresse:'):
                    address = text.removeprefix('Adresse:').strip()
                elif text.startswith('Deadline:'):
                    deadline = text.removeprefix('Deadline:').strip()
                elif not text.startswith('Tilmelding:') and not association:
                    association = text

            if not deadline:
                tl = card.query_selector('.timelineEvent')
                if tl:
                    for dp in tl.query_selector_all('p'):
                        t = dp.inner_text().strip()
                        if 'Deadline:' in t:
                            deadline = t.replace('Deadline:', '').strip()

            if uid and title:
                results.append({
                    'id':          f'{SOURCE_ID}:{uid}',
                    'source':      SOURCE_NAME,
                    'title':       title,
                    'association': association,
                    'list_type':   list_type,
                    'published':   published,
                    'address':     address,
                    'deadline':    deadline,
                    'url':         f'https://my.waitly.dk{href}',
                })
        except Exception as e:
            print(f'[{SOURCE_ID}] listing error: {e}')

    active = [i for i in results if not _deadline_expired(i.get('deadline', ''))]
    skipped = len(results) - len(active)
    print(f'[{SOURCE_ID}] Henter detaljer for {len(active)} boliger ({skipped} sprunget over – deadline udløbet)...')
    for item in active:
        details = _scrape_listing_details(page, item['url'])
        item.update(details)
        print(f'[{SOURCE_ID}]   {item["title"][:40]} → {item.get("price","-")} / {item.get("size","-")}')

    return results
