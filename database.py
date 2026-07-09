import sqlite3
import os
from datetime import datetime

DATA_DIR = os.environ.get('DATA_DIR', './data')
DB_FILE  = os.path.join(DATA_DIR, 'monitor.db')


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS listings (
            id              TEXT PRIMARY KEY,
            source          TEXT,
            title           TEXT,
            association     TEXT,
            list_type       TEXT,
            published       TEXT,
            address         TEXT,
            deadline        TEXT,
            url             TEXT,
            first_seen      TEXT,
            is_new          INTEGER DEFAULT 1,
            is_active       INTEGER DEFAULT 1,
            price           TEXT,
            recurring_price TEXT,
            rooms           TEXT,
            size            TEXT,
            floor           TEXT
        )
    ''')
    for col, typ in [('is_active','INTEGER DEFAULT 1'), ('price','TEXT'),
                     ('recurring_price','TEXT'), ('rooms','TEXT'),
                     ('size','TEXT'), ('floor','TEXT'), ('reminded','TEXT')]:
        try:
            c.execute(f'ALTER TABLE listings ADD COLUMN {col} {typ}')
        except Exception:
            pass

    c.execute('''
        CREATE TABLE IF NOT EXISTS waitlists (
            id        TEXT PRIMARY KEY,
            source    TEXT,
            name      TEXT,
            position  TEXT,
            total     TEXT,
            status    TEXT,
            end_date  TEXT,
            updated   TEXT
        )
    ''')

    conn.commit()
    conn.close()


# ── Listings ──────────────────────────────────────────────────────────────────

def save_listings(items: list[dict]) -> list[dict]:
    """Gem nye boliger, returnér kun dem der er nye. Opdater detaljer for eksisterende."""
    conn = sqlite3.connect(DB_FILE)
    c    = conn.cursor()
    new  = []
    for item in items:
        c.execute('SELECT id FROM listings WHERE id = ?', (item['id'],))
        if not c.fetchone():
            c.execute(
                '''INSERT INTO listings
                   (id, source, title, association, list_type, published,
                    address, deadline, url, first_seen, is_new,
                    price, recurring_price, rooms, size, floor)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)''',
                (
                    item['id'], item['source'], item['title'], item['association'],
                    item['list_type'], item['published'], item['address'],
                    item['deadline'], item['url'], datetime.now().isoformat(),
                    item.get('price',''), item.get('recurring_price',''),
                    item.get('rooms',''), item.get('size',''), item.get('floor',''),
                )
            )
            new.append(item)
        else:
            # Opdatér kun detaljer hvis scraper'en faktisk hentede dem (ikke tomme)
            if any(item.get(k) for k in ('price', 'recurring_price', 'rooms', 'size', 'floor')):
                c.execute(
                    '''UPDATE listings SET price=?, recurring_price=?, rooms=?, size=?, floor=?
                       WHERE id=?''',
                    (item.get('price',''), item.get('recurring_price',''),
                     item.get('rooms',''), item.get('size',''), item.get('floor',''),
                     item['id'])
                )
    conn.commit()
    conn.close()
    return new


def sync_active_listings(current_ids: list[str]):
    """Markér listings der ikke længere er på siden som udgåede, aktiver genkomne.

    Sikkerhedsspærre: hvis scrapet kom TOMT tilbage (0 boliger), er det næsten
    altid en fejl (login/timeout på Waitlys side) – ikke at alle boliger er væk.
    I det tilfælde rører vi IKKE is_active, så vi ikke fejlagtigt markerer alt
    som udgået og skjuler reelle, aktive tilbud.
    """
    if not current_ids:
        print('[db] Tomt scrape – springer is_active-opdatering over (undgår at markere alt som udgået).')
        return
    conn = sqlite3.connect(DB_FILE)
    c    = conn.cursor()
    placeholders = ','.join('?' * len(current_ids))
    c.execute(f'UPDATE listings SET is_active = 0 WHERE id NOT IN ({placeholders})', current_ids)
    c.execute(f'UPDATE listings SET is_active = 1 WHERE id IN ({placeholders})', current_ids)
    conn.commit()
    conn.close()


def set_reminded(listing_id: str, reminded: str):
    """Gem hvilke deadline-påmindelser der er sendt for en bolig (kommasepareret)."""
    conn = sqlite3.connect(DB_FILE)
    c    = conn.cursor()
    c.execute('UPDATE listings SET reminded = ? WHERE id = ?', (reminded, listing_id))
    conn.commit()
    conn.close()


def get_all_listings() -> list[dict]:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM listings ORDER BY is_active DESC, first_seen DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


# ── Waitlists ─────────────────────────────────────────────────────────────────

def save_waitlists(items: list[dict]):
    """Upsert ventelistepladser — altid den nyeste data."""
    conn = sqlite3.connect(DB_FILE)
    c    = conn.cursor()
    now  = datetime.now().isoformat()
    for item in items:
        c.execute(
            '''INSERT INTO waitlists (id, source, name, position, total, status, end_date, updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 position = excluded.position,
                 total    = excluded.total,
                 status   = excluded.status,
                 end_date = excluded.end_date,
                 updated  = excluded.updated''',
            (item['id'], item['source'], item['name'], item['position'],
             item['total'], item['status'], item['end_date'], now)
        )
    conn.commit()
    conn.close()


def get_all_waitlists() -> list[dict]:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM waitlists ORDER BY name')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows
