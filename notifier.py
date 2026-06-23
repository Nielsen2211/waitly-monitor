import re
import requests


def notify_new_listings(listings: list[dict], config: dict):
    notif = config.get('notifications', {})
    for item in listings:
        title = f"Ny bolig: {item['title']}"
        body  = (
            f"Kilde: {item['source']}\n"
            f"Adresse: {item['address']}\n"
            f"Udgivet: {item['published']}\n"
            f"Liste: {item['list_type']}\n"
            f"Deadline: {item['deadline']}\n"
            f"Link: {item['url']}"
        )

        ntfy_topic  = notif.get('ntfy_topic', '').strip()
        ntfy_server = notif.get('ntfy_server', 'http://ntfy').rstrip('/')
        if ntfy_topic:
            try:
                price   = _fmt_price(item.get('price', ''), 1000)
                monthly = _fmt_price(item.get('recurring_price', ''), 100)
                parts = []
                if price:              parts.append(price)
                if monthly:            parts.append(f'{monthly}/md')
                if item.get('rooms'):  parts.append(f"{item['rooms']} vær.")
                if item.get('size'):   parts.append(item['size'])
                if item.get('floor'):  parts.append(item['floor'])
                ntfy_body = '\n'.join(filter(None, [
                    ' · '.join(parts) if parts else '',
                    item['address'],
                    f"Deadline: {item['deadline']}" if item.get('deadline') else '',
                ]))
                requests.post(
                    f'{ntfy_server}',
                    json={
                        'topic':    ntfy_topic,
                        'title':    item['title'],
                        'message':  ntfy_body,
                        'priority': 4,
                        'tags':     ['house'],
                        'click':    item['url'],
                        'actions':  [{'action': 'view', 'label': 'Se bolig', 'url': item['url']}],
                    },
                    timeout=10,
                )
            except Exception as e:
                print(f'[notifier] ntfy error: {e}')

        recipients = notif.get('notify_emails') or []
        if notif.get('email_enabled') and notif.get('resend_api_key') and recipients:
            try:
                _send_email_resend(
                    api_key=notif['resend_api_key'],
                    to_addrs=recipients,
                    subject=title,
                    html_body=_build_email_html(item),
                    text_body=_build_email_text(item),
                )
            except Exception as e:
                print(f'[notifier] email error: {e}')

        if (notif.get('sms_enabled')
                and notif.get('twilio_sid')
                and notif.get('twilio_token')
                and notif.get('twilio_from')
                and notif.get('phone_number')):
            try:
                sms_body = (
                    f"Ny bolig: {item['title']}\n"
                    f"Adresse: {item['address']}\n"
                    f"Deadline: {item['deadline']}\n"
                    f"{item['url']}"
                )
                _send_sms(
                    to=notif['phone_number'],
                    from_=notif['twilio_from'],
                    sid=notif['twilio_sid'],
                    token=notif['twilio_token'],
                    body=sms_body,
                )
            except Exception as e:
                print(f'[notifier] sms error: {e}')


def _send_sms(to: str, from_: str, sid: str, token: str, body: str):
    requests.post(
        f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json',
        auth=(sid, token),
        data={'To': to, 'From': from_, 'Body': body},
        timeout=15,
    ).raise_for_status()


def _send_email_resend(api_key: str, to_addrs: list, subject: str,
                       html_body: str, text_body: str = ''):
    resp = requests.post(
        'https://api.resend.com/emails',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'from': 'Bolig Monitor <bolig@rsnielsen.com>',
            'to': to_addrs,
            'subject': subject,
            'html': html_body,
            'text': text_body,
        },
        timeout=15,
    )
    resp.raise_for_status()


def _fmt_price(raw: str, round_to: int) -> str:
    if not raw:
        return ''
    m = re.search(r'[\d.,]+', raw.replace('.', '').replace(',', '.'))
    if not m:
        return raw
    try:
        n = round(float(m.group()) / round_to) * round_to
        return f"{n:,.0f}".replace(',', '.') + ' DKK'
    except ValueError:
        return raw


def _build_email_text(item: dict, test: bool = False) -> str:
    price   = _fmt_price(item.get('price', ''), 1000)
    monthly = _fmt_price(item.get('recurring_price', ''), 100)
    lines = []
    if test:
        lines.append('[ TESTMAIL — ikke en rigtig ny bolig ]\n')
    lines.append(item['title'])
    lines.append(f"{item['association']} · {item['source']}")
    lines.append('')
    if price:   lines.append(f"Pris:     {price}")
    if monthly: lines.append(f"/Måned:   {monthly}")
    if item.get('rooms'): lines.append(f"Værelser: {item['rooms']}")
    if item.get('size'):  lines.append(f"Størrelse:{item['size']}")
    if item.get('floor'): lines.append(f"Etage:    {item['floor']}")
    lines.append('')
    lines.append(f"Adresse:  {item['address']}")
    lines.append(f"Liste:    {item['list_type']}")
    lines.append(f"Udgivet:  {item['published']}")
    lines.append(f"Deadline: {item.get('deadline') or '–'}")
    lines.append('')
    lines.append(f"Se boligen: {item['url']}")
    return '\n'.join(lines)


# ── HTML helpers ──────────────────────────────────────────────────────────────

_L = 'font-size:10px;font-weight:700;color:#7c3aed;letter-spacing:.07em;text-transform:uppercase;margin-bottom:3px'
_V = 'font-size:22px;font-weight:800;color:#111827;letter-spacing:-.02em'
_PILL = 'display:inline-block;background:#f5f3ff;color:#374151;font-size:13px;font-weight:500;border-radius:999px;padding:5px 14px;margin:0 6px 6px 0'
_TL = 'padding:9px 12px 9px 28px;color:#9ca3af;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap;vertical-align:top;width:100px'
_TV = 'padding:9px 28px 9px 0;font-size:14px;color:#111827'


def _build_email_html(item: dict, test: bool = False) -> str:
    price   = _fmt_price(item.get('price', ''), 1000)
    monthly = _fmt_price(item.get('recurring_price', ''), 100)
    rooms   = item.get('rooms', '')
    size    = item.get('size', '')
    floor   = item.get('floor', '')

    # Price block
    pcells = ''
    if price:
        pcells += f'<td style="width:50%;padding:0 12px 0 0"><div style="{_L}">Pris</div><div style="{_V}">{price}</div></td>'
    if monthly:
        border = 'border-left:2px solid #f3f4f6;' if price else ''
        pcells += f'<td style="width:50%;padding:0 0 0 12px;{border}"><div style="{_L}">/Måned</div><div style="{_V}">{monthly}</div></td>'
    price_block = (
        f'<div style="padding:18px 24px 16px;border-bottom:1px solid #f3f4f6">'
        f'<table width="100%" cellpadding="0" cellspacing="0"><tr>{pcells}</tr></table></div>'
    ) if pcells else ''

    # Pills
    pills = ''
    if rooms: pills += f'<span style="{_PILL}">{rooms} vær.</span>'
    if size:  pills += f'<span style="{_PILL}">{size}</span>'
    if floor: pills += f'<span style="{_PILL}">{floor}</span>'
    pills_block = (
        f'<div style="padding:14px 24px 10px;border-bottom:1px solid #f3f4f6">{pills}</div>'
    ) if pills else ''

    # Info rows
    deadline_color = '#dc2626' if item.get('deadline') else '#6b7280'
    def row(label, value, color='#111827'):
        return f'<tr><td style="{_TL}">{label}</td><td style="{_TV};color:{color}">{value}</td></tr>'

    test_banner = (
        '<div style="background:#fef08a;color:#713f12;font-size:12px;font-weight:700;'
        'text-align:center;padding:10px 16px">&#9888;&#65039; TESTMAIL — ikke en rigtig ny bolig</div>'
    ) if test else ''

    eyebrow = 'Testmail &middot; seneste bolig i databasen' if test else 'Ny bolig til salg'

    # Preheader: hidden preview text shown in Gmail inbox snippet
    addr = item['address']
    snippet = f"{price or ''}{' · ' if price and monthly else ''}{monthly or ''} — {addr}"
    preheader = (
        f'<div style="display:none;font-size:1px;color:#ffffff;'
        f'max-height:0;overflow:hidden;opacity:0">{snippet}</div>'
    )

    H = '-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif'
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        f'<body style="margin:0;padding:24px 16px;background:#f0f0f5;font-family:{H}">'
        f'{preheader}'
        f'<div style="max-width:520px;margin:0 auto;background:#ffffff;'
        f'border-radius:14px;overflow:hidden;'
        f'border:1px solid #e5e7eb">'
        f'{test_banner}'
        f'<div style="background:#7c3aed;padding:26px 28px 22px">'
        f'<div style="color:#c4b5fd;font-size:10px;font-weight:700;'
        f'letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px">{eyebrow}</div>'
        f'<div style="color:#fff;font-size:19px;font-weight:800;'
        f'line-height:1.3;margin-bottom:5px">{item["title"]}</div>'
        f'<div style="color:#ddd6fe;font-size:13px">'
        f'{item["association"]} &middot; {item["source"]}</div>'
        f'</div>'
        f'{price_block}'
        f'{pills_block}'
        f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
        f'{row("Adresse", item["address"])}'
        f'{row("Liste", item["list_type"])}'
        f'{row("Udgivet", item["published"])}'
        f'{row("Deadline", item.get("deadline") or "&ndash;", deadline_color)}'
        f'</table>'
        f'<div style="padding:16px 28px 26px;text-align:center">'
        f'<a href="{item["url"]}" style="display:inline-block;background:#7c3aed;'
        f'color:#ffffff;text-decoration:none;padding:13px 36px;border-radius:10px;'
        f'font-size:15px;font-weight:700">'
        f'Se boligen &#8594;</a>'
        f'</div>'
        f'</div>'
        f'</body></html>'
    )
