import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from functools import wraps

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import (Flask, jsonify, make_response, redirect,
                   render_template, request, session, url_for)

from database import get_all_listings, get_all_waitlists, init_db, save_listings, save_waitlists, sync_active_listings
from notifier import notify_new_listings
from scrapers import run_all

app = Flask(__name__)

DATA_DIR = os.environ.get('DATA_DIR', './data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
STATE_FILE  = os.path.join(DATA_DIR, 'state.json')
SECRET_FILE = os.path.join(DATA_DIR, 'secret.key')

DEFAULT_CONFIG = {
    'email': '',
    'password': '',
    'app_password': '',
    'check_interval': 60,
    'notifications': {
        'ntfy_server': 'http://ntfy',   # intern Docker-URL; ntfy.sh → https://ntfy.sh
        'ntfy_topic': '',
        'email_enabled': False,
        'resend_api_key': '',
        'notify_emails': [],
        'sms_enabled': False,
        'phone_number': '',
        'twilio_sid': '',
        'twilio_token': '',
        'twilio_from': '',
    },
}

_check_lock  = threading.Lock()
_is_checking = False
scheduler = BackgroundScheduler(
    timezone='Europe/Copenhagen',
    job_defaults={
        'coalesce': True,            # saml missede kørsler til én i stedet for at hobe op
        'max_instances': 1,          # kør aldrig to tjek samtidig
        'misfire_grace_time': 3600,  # tillad op til 1 t forsinkelse frem for at droppe jobbet
    },
)

# Containeren kører ofte på UTC – brug eksplicit dansk tid så viste tidspunkter
# (last_check, næste tjek) stemmer med APScheduler og brugerens ur.
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo('Europe/Copenhagen')
except Exception:
    LOCAL_TZ = None

def _now():
    return datetime.now(LOCAL_TZ)


# ── Secret key (persisted so sessions survive restarts) ─────────────────────

def _get_secret_key() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(SECRET_FILE):
        return open(SECRET_FILE).read().strip()
    key = secrets.token_hex(32)
    open(SECRET_FILE, 'w').write(key)
    return key

app.secret_key = _get_secret_key()
app.config['SESSION_COOKIE_SAMESITE']      = 'Lax'
app.config['SESSION_COOKIE_SECURE']        = False  # set True if always HTTPS
app.config['PERMANENT_SESSION_LIFETIME']   = timedelta(days=3650)  # ~10 år


# ── Auth helpers ─────────────────────────────────────────────────────────────

def _is_authenticated() -> bool:
    return session.get('authed') is True

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _is_authenticated():
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _is_authenticated():
            return jsonify({'error': 'Ikke logget ind'}), 401
        return f(*args, **kwargs)
    return decorated


# ── Config / State helpers ───────────────────────────────────────────────────

def load_config() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding='utf-8') as f:
            saved = json.load(f)
        cfg = {**DEFAULT_CONFIG, **saved}
        cfg['notifications'] = {**DEFAULT_CONFIG['notifications'],
                                 **saved.get('notifications', {})}
        return cfg
    return {**DEFAULT_CONFIG}


def write_config(cfg: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {'last_check': None, 'last_status': None, 'last_error': None,
            'new_count': 0, 'total_count': 0, 'next_check': None}


def write_state(state: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False)


# ── Core check logic ─────────────────────────────────────────────────────────

def do_check() -> dict:
    global _is_checking
    if not _check_lock.acquire(blocking=False):
        raise RuntimeError('Et tjek kører allerede – vent et øjeblik.')
    _is_checking = True

    state = load_state()
    cfg   = load_config()
    try:
        if not cfg.get('email') or not cfg.get('password'):
            raise RuntimeError('Ingen login-oplysninger konfigureret.')

        listings, waitlists = run_all(cfg)
        new_listings        = save_listings(listings)
        save_waitlists(waitlists)
        sync_active_listings([l['id'] for l in listings])

        if new_listings:
            notify_new_listings(new_listings, cfg)

        state.update({
            'last_check':  _now().isoformat(),
            'last_status': 'ok',
            'last_error':  None,
            'new_count':   len(new_listings),
            'total_count': len(listings),
        })
        write_state(state)
        return {'ok': True, 'new_count': len(new_listings), 'total': len(listings)}

    except Exception as exc:
        state.update({
            'last_check':  _now().isoformat(),
            'last_status': 'error',
            'last_error':  str(exc),
        })
        write_state(state)
        raise
    finally:
        _is_checking = False
        _check_lock.release()


def _safe_check():
    try:
        do_check()
    except Exception as e:
        print(f'[scheduler] {e}')


# ── Scheduler (tilbagevendende interval med ±25 % spredning) ────────────────

def _update_next_check():
    """Gem tidspunktet for næste planlagte tjek (til nedtælling i UI'et)."""
    job = scheduler.get_job('monitor_check')
    if job and job.next_run_time:
        state = load_state()
        state['next_check'] = job.next_run_time.isoformat()
        write_state(state)


def _run_scheduled_check():
    """Kørt af scheduleren ved hvert interval."""
    _safe_check()
    _update_next_check()


def schedule_checks(run_now: bool = False):
    """(Gen)planlæg det tilbagevendende tjek ud fra konfigureret interval.

    Bruger ét vedvarende IntervalTrigger-job (overlever missede kørsler) i
    stedet for at planlægge ét engangs-job ad gangen. ±25 % tilfældig spredning
    via APSchedulers jitter, så tjekkene ikke rammer præcis samme minut hver gang.
    """
    cfg      = load_config()
    interval = max(5, int(cfg.get('check_interval', 60)))
    jitter   = int(interval * 60 * 0.25)   # ±25 % i sekunder

    first = _now() + (timedelta(seconds=2) if run_now
                              else timedelta(minutes=interval))
    scheduler.add_job(
        _run_scheduled_check,
        IntervalTrigger(minutes=interval, jitter=jitter),
        id='monitor_check',
        replace_existing=True,
        next_run_time=first,
    )
    _update_next_check()
    print(f'[scheduler] Tjek hvert {interval}. min (±25 %), næste {first.strftime("%H:%M")}')


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    cfg = load_config()
    error = None

    if request.method == 'POST':
        entered   = request.form.get('password', '')
        remember  = request.form.get('remember') == 'on'
        app_pw    = cfg.get('app_password', '')

        if not app_pw:
            # Ingen kode sat endnu – første gang, slip dem ind
            session.permanent = True
            session['authed'] = True
            return redirect(url_for('index'))

        if secrets.compare_digest(entered, app_pw):
            session.permanent = True
            session['authed'] = True
            next_url = request.args.get('next') or url_for('index')
            return redirect(next_url)
        else:
            error = 'Forkert adgangskode'

    app_pw_set = bool(cfg.get('app_password'))
    return render_template('login.html', error=error, app_pw_set=app_pw_set)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Main app routes ───────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return render_template('index.html')


@app.route('/api/config', methods=['GET'])
@api_login_required
def api_get_config():
    cfg  = load_config()
    safe = {k: v for k, v in cfg.items() if k not in ('password', 'app_password')}
    safe['has_password']     = bool(cfg.get('password'))
    safe['has_app_password'] = bool(cfg.get('app_password'))
    notif = dict(safe.get('notifications', {}))
    safe['has_resend_api_key'] = bool(notif.pop('resend_api_key', None))
    safe['has_twilio_token']   = bool(notif.pop('twilio_token', None))
    # Migrate legacy single notify_email → list
    if 'notify_email' in notif and not notif.get('notify_emails'):
        notif['notify_emails'] = [notif['notify_email']] if notif['notify_email'] else []
    notif.pop('notify_email', None)
    safe['notifications'] = notif
    return jsonify(safe)


@app.route('/api/config', methods=['POST'])
@api_login_required
def api_post_config():
    data = request.get_json(force=True)
    cfg  = load_config()

    for key in ('email', 'check_interval'):
        if key in data:
            cfg[key] = data[key]
    if data.get('password'):
        cfg['password'] = data['password']
    if data.get('app_password'):
        cfg['app_password'] = data['app_password']

    if 'notifications' in data:
        nd = data['notifications']
        for key in ('ntfy_server', 'ntfy_topic', 'email_enabled', 'notify_emails',
                    'sms_enabled', 'phone_number', 'twilio_sid', 'twilio_from'):
            if key in nd:
                cfg['notifications'][key] = nd[key]
        if nd.get('resend_api_key'):
            cfg['notifications']['resend_api_key'] = nd['resend_api_key']
        if nd.get('twilio_token'):
            cfg['notifications']['twilio_token'] = nd['twilio_token']

    write_config(cfg)
    # Anvend evt. nyt interval med det samme
    if scheduler.running:
        schedule_checks()
    return jsonify({'ok': True})


@app.route('/api/test/ntfy', methods=['POST'])
@api_login_required
def api_test_ntfy():
    import requests as req
    cfg   = load_config()
    notif = cfg.get('notifications', {})
    topic  = notif.get('ntfy_topic', '').strip()
    server = notif.get('ntfy_server', 'http://ntfy').rstrip('/')
    if not topic:
        return jsonify({'ok': False, 'error': 'Intet ntfy emne (topic) konfigureret'}), 400
    listings = get_all_listings()
    item = listings[0] if listings else None
    try:
        from notifier import _fmt_price
        if item:
            price   = _fmt_price(item.get('price', ''), 1000)
            monthly = _fmt_price(item.get('recurring_price', ''), 100)
            parts = []
            if price:              parts.append(price)
            if monthly:            parts.append(f'{monthly}/md')
            if item.get('rooms'):  parts.append(f"{item['rooms']} vær.")
            if item.get('size'):   parts.append(item['size'])
            ntfy_body = '\n'.join(filter(None, [
                ' · '.join(parts) if parts else '',
                item['address'],
                'TEST - ikke en rigtig ny bolig',
            ]))
            headers = {
                'Title': f"[TEST] {item['title']}",
                'Priority': 'default',
                'Tags': 'house',
                'Click': item['url'],
                'Actions': f"view, Se bolig, {item['url']}",
            }
        else:
            ntfy_body = 'Bolig Monitor virker! Ingen boliger i databasen endnu.'
            headers = {'Title': 'Bolig Monitor - test', 'Priority': 'default', 'Tags': 'white_check_mark'}
        payload = {'topic': topic, 'message': ntfy_body}
        if item:
            payload.update({
                'title':   headers['Title'],
                'priority': 3,
                'tags':    ['house'],
                'click':   item['url'],
                'actions': [{'action': 'view', 'label': 'Se bolig', 'url': item['url']}],
            })
        else:
            payload['title'] = 'Bolig Monitor - test'
        req.post(server, json=payload, timeout=10).raise_for_status()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/test/sms', methods=['POST'])
@api_login_required
def api_test_sms():
    from notifier import _send_sms
    cfg   = load_config()
    notif = cfg.get('notifications', {})
    missing = [k for k in ('twilio_sid', 'twilio_token', 'twilio_from', 'phone_number') if not notif.get(k)]
    if missing:
        return jsonify({'ok': False, 'error': f'Mangler: {", ".join(missing)}'}), 400
    try:
        _send_sms(
            to=notif['phone_number'],
            from_=notif['twilio_from'],
            sid=notif['twilio_sid'],
            token=notif['twilio_token'],
            body='Bolig Monitor test-SMS – det virker! 🏠',
        )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/test/email', methods=['POST'])
@api_login_required
def api_test_email():
    from notifier import _send_email_resend, _build_email_html, _build_email_text
    cfg   = load_config()
    notif = cfg.get('notifications', {})
    if not notif.get('resend_api_key'):
        return jsonify({'ok': False, 'error': 'Ingen Resend API-nøgle gemt'}), 400
    recipients = notif.get('notify_emails') or []
    if not recipients:
        return jsonify({'ok': False, 'error': 'Ingen modtagere konfigureret'}), 400
    listings = get_all_listings()
    if not listings:
        return jsonify({'ok': False, 'error': 'Ingen boliger i databasen endnu'}), 400
    item = listings[0]
    try:
        _send_email_resend(
            api_key=notif['resend_api_key'],
            to_addrs=recipients,
            subject=f"[TEST] Ny bolig: {item['title']}",
            html_body=_build_email_html(item, test=True),
            text_body=_build_email_text(item, test=True),
        )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/listings')
@api_login_required
def api_listings():
    return jsonify(get_all_listings())


@app.route('/api/waitlists')
@api_login_required
def api_waitlists():
    return jsonify(get_all_waitlists())


@app.route('/api/check', methods=['POST'])
@api_login_required
def api_check():
    try:
        result = do_check()
        schedule_checks()  # nulstil nedtællingen efter manuelt tjek
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/state')
@api_login_required
def api_state():
    state = load_state()
    state['is_checking'] = _is_checking
    return jsonify(state)


# ── Startup (kører ved både gunicorn og direkte python app.py) ───────────────

def _startup():
    os.makedirs(DATA_DIR, exist_ok=True)
    init_db()
    if not scheduler.running:
        scheduler.start()
    # Tjek straks ved opstart (også efter genstart/strømafbrydelse) + sæt det
    # tilbagevendende interval. Selve tjekket kører i scheduler-tråden.
    schedule_checks(run_now=True)

_startup()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
