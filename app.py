"""Local bakery manager. Python standard library only."""
import csv
import datetime as dt
import html
import json
import re
import sqlite3
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'bakery.db'
DAYS = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']

class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()

def connect():
    c = sqlite3.connect(DB, factory=Connection)
    c.execute('PRAGMA foreign_keys=ON')
    c.row_factory = sqlite3.Row
    return c

def product(text):
    m = re.search(r'(\d+)\s+(dozen cupcakes|cakes?)', text, re.I)
    if not m: raise ValueError('Use a positive whole number of cakes or dozens of cupcakes.')
    q = int(m[1])
    if not 1 <= q <= 100: raise ValueError('Quantity must be between 1 and 100.')
    return ('cupcakes' if 'cupcake' in m[2].lower() else 'cake'), q

def date_text(text):
    m = re.search(r'(Nov|Dec)\s+(\d+)', text)
    if not m:
        m = re.search(r'(\d+)\s+(Nov|Dec)', text)
        return dt.date(2026,11 if m[2]=='Nov' else 12,int(m[1])).isoformat()
    return dt.date(2026,11 if m[1]=='Nov' else 12,int(m[2])).isoformat()

def init():
    with connect() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,customer TEXT,product TEXT,quantity INTEGER,pickup TEXT,status TEXT);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,day TEXT,customer TEXT,kind TEXT,product TEXT,quantity INTEGER,pickup TEXT,raw TEXT,status TEXT DEFAULT 'pending',note TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS helpers(day TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,day TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS request_orders(message_id INTEGER PRIMARY KEY,order_id INTEGER UNIQUE);
        ''')
        columns={r['name'] for r in c.execute('PRAGMA table_info(messages)')}
        for column in ('order_value','revenue_change'):
            if column not in columns: c.execute('ALTER TABLE messages ADD COLUMN '+column+' INTEGER')
        normalize_days(c)
        if c.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone(): return
        seed(c)

def normalize_days(c):
    """Keep each pickup date once; orders and helper bookings reference that date."""
    c.execute('CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY)')
    c.executemany('INSERT OR IGNORE INTO days(day) VALUES(?)',
                  [((dt.date(2026,11,23)+dt.timedelta(days=i)).isoformat(),) for i in range(9)])
    for table, column in (('orders','pickup'),('helpers','day')):
        c.execute(f'INSERT OR IGNORE INTO days(day) SELECT DISTINCT {column} FROM {table}')
        if c.execute(f'PRAGMA foreign_key_list({table})').fetchone(): continue
        # Rebuild within this transaction, preserving IDs and all saved decisions.
        if table=='orders':
            c.execute("CREATE TABLE orders_linked(id INTEGER PRIMARY KEY,customer TEXT,product TEXT,quantity INTEGER,pickup TEXT NOT NULL REFERENCES days(day),status TEXT)")
        else:
            c.execute('CREATE TABLE helpers_linked(day TEXT PRIMARY KEY REFERENCES days(day))')
        c.execute(f'INSERT INTO {table}_linked SELECT * FROM {table}')
        c.execute(f'DROP TABLE {table}')
        c.execute(f'ALTER TABLE {table}_linked RENAME TO {table}')
    c.execute('CREATE INDEX IF NOT EXISTS orders_pickup ON orders(pickup)')

def seed(c):
    for r in csv.DictReader((ROOT/'existing_orders.csv').read_text().splitlines()):
        p,q=product(r['item'])
        c.execute('INSERT INTO orders VALUES(?,?,?,?,?,?)',(int(r['order'][1:]),r['customer'],p,q,date_text(r['pickup_day']),'accepted'))
    for i,day in enumerate(DAYS):
        source=(ROOT/f'InstagramOrders_{day}.html').read_text()
        for raw in re.findall(r'<p class="message">(.*?)</p>',source,re.S):
            raw=html.unescape(raw)
            name,order,pickup=[line.split(':',1)[1].strip() for line in raw.splitlines()]
            kind='cancel' if order.lower()=='cancel' else 'change' if order.lower().startswith('change') else 'new'
            p,q=('',0) if kind=='cancel' else product(order)
            c.execute('INSERT INTO messages(day,customer,kind,product,quantity,pickup,raw) VALUES(?,?,?,?,?,?,?)',((dt.date(2026,11,23)+dt.timedelta(days=i)).isoformat(),name,kind,p,q,date_text(pickup),raw))
    c.executemany('INSERT INTO settings VALUES(?,?)',[('initialized','yes'),('today','2026-11-23')])

def hours(row): return row['quantity']*(0.5 if row['product']=='cupcakes' else 2)
def today(c): return c.execute("SELECT value FROM settings WHERE key='today'").fetchone()[0]
def usage(c,day): return sum(hours(r) for r in c.execute("SELECT * FROM orders WHERE pickup=? AND status IN ('accepted','picked up')",(day,)))
def capacity(c,day): return 12 if c.execute('SELECT 1 FROM helpers WHERE day=?',(day,)).fetchone() else 8

def eligibility(c,m):
    if m['day'] > today(c): return 'This message has not arrived yet.',None
    # Notice is evaluated on the working day; message dates remain visible for audit.
    if (dt.date.fromisoformat(m['pickup'])-dt.date.fromisoformat(today(c))).days < 2: return 'Too late: two calendar days of notice are required.',None
    target=None
    if m['kind']!='new':
        matches=c.execute("SELECT * FROM orders WHERE lower(customer)=lower(?) AND pickup=? AND status='accepted'",(m['customer'],m['pickup'])).fetchall()
        if len(matches)!=1: return 'Needs review: exactly one accepted matching order is required.',None
        target=matches[0]
    required=0 if m['kind']=='cancel' else hours(m)
    if m['kind']=='cancel': return '',target
    total=usage(c,m['pickup'])-(hours(target) if target else 0)+required
    if total>capacity(c,m['pickup']): return f'Over capacity: would use {total:g} hours. Book a helper if eligible, or reject.',target
    return '',target

def value(row):
    return row['quantity']*(40 if row['product']=='cupcakes' else 120)

def message_value(c,m):
    if m['kind']=='new': return value(m),value(m)
    matches=c.execute("SELECT * FROM orders WHERE lower(customer)=lower(?) AND pickup=? AND status='accepted'",(m['customer'],m['pickup'])).fetchall()
    old=value(matches[0]) if len(matches)==1 else None
    if m['kind']=='cancel': return old, -old if old is not None else None
    return value(m), value(m)-old if old is not None else None

def fetch_orders(c):
    """Fetch each arrived new order once; requested orders reserve no hours."""
    # Reject expired requests of every kind, including previously fetched orders.
    for m in c.execute("SELECT * FROM messages WHERE day<=? AND status='pending' ORDER BY day,id",(today(c),)).fetchall():
        reason,_=eligibility(c,m)
        if reason.startswith('Too late:') and (m['kind']!='new' or c.execute('SELECT 1 FROM request_orders WHERE message_id=?',(m['id'],)).fetchone()):
            action(c,dict(action='reject',id=m['id']))
            c.execute("UPDATE messages SET note=? WHERE id=?",('Automatically rejected: '+reason,m['id']))
    for m in c.execute("SELECT * FROM messages WHERE day<=? AND kind='new' AND status='pending' ORDER BY day,id",(today(c),)).fetchall():
        if c.execute('SELECT 1 FROM request_orders WHERE message_id=?',(m['id'],)).fetchone(): continue
        reason,_=eligibility(c,m)
        status='rejected' if reason else 'requested'
        order=c.execute('INSERT INTO orders(customer,product,quantity,pickup,status) VALUES(?,?,?,?,?)',
                        (m['customer'],m['product'],m['quantity'],m['pickup'],status))
        c.execute('INSERT INTO request_orders VALUES(?,?)',(m['id'],order.lastrowid))
        if reason:
            c.execute("UPDATE messages SET status='rejected',note=?,order_value=?,revenue_change=? WHERE id=?",
                      ('Automatically rejected: Incompatible: '+reason,value(m),value(m),m['id']))
            c.execute('INSERT INTO audit(day,detail) VALUES(?,?)',(today(c),f"Automatically rejected incompatible order: {m['customer']} / {reason}"))

def state(c):
    fetch_orders(c)
    now=today(c)
    messages=[]
    for r in c.execute('SELECT * FROM messages WHERE day<=? ORDER BY day,id',(now,)):
        m=dict(r)
        if r['status']=='pending': m['order_value'],m['revenue_change']=message_value(c,r)
        elif r['order_value'] is None and r['kind']!='cancel': m['order_value']=value(r)
        reason,target=eligibility(c,r) if r['status']=='pending' else (r['note'],None)
        m['reason']=reason
        m['hours']=hours(target) if r['kind']=='cancel' and target else (None if r['kind']=='cancel' else hours(r))
        projected=usage(c,r['pickup'])-(hours(target) if target else 0)+(0 if r['kind']=='cancel' else hours(r))
        m['helper_offer']=bool(r['status']=='pending' and reason.startswith('Over capacity:') and capacity(c,r['pickup'])==8 and projected<=12)
        m['net_with_helper']=m['revenue_change']-100 if m['helper_offer'] and m['revenue_change'] is not None else None
        messages.append(m)
    calendar=[]
    for day_row in c.execute('SELECT day FROM days ORDER BY day'):
        day=day_row['day']
        rows=c.execute("SELECT * FROM orders WHERE pickup=? AND status IN ('accepted','picked up')",(day,)).fetchall()
        revenue=sum(r['quantity']*(40 if r['product']=='cupcakes' else 120) for r in rows)
        cap=capacity(c,day)
        calendar.append(dict(day=day,hours=usage(c,day),capacity=cap,revenue=revenue,helper=cap==12,net=revenue-(100 if cap==12 else 0),orders=[dict(id=r['id'],customer=r['customer'],product=r['product'],quantity=r['quantity'],status=r['status']) for r in c.execute('SELECT * FROM orders WHERE pickup=? ORDER BY id',(day,))]))
    return dict(today=now,messages=messages,calendar=calendar,audit=[dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 30')])

def action(c,a):
    now=today(c);kind=a['action']
    if kind=='reset':
        if a.get('confirm') != 'RESET': raise ValueError('Reset confirmation is required.')
        for table in ('request_orders','orders','messages','helpers','settings','audit'):
            c.execute('DELETE FROM '+table)
        seed(c)
        return
    if kind=='accept_all_next':
        if now>='2026-11-29': raise ValueError('The working week is complete.')
        fetch_orders(c)
        for m in c.execute("SELECT * FROM messages WHERE day<=? AND status='pending' ORDER BY day,id",(now,)).fetchall():
            reason,_=eligibility(c,m)
            action(c,dict(action='reject' if reason else 'accept',id=m['id']))
        action(c,dict(action='day'))
        return
    if kind=='day':
        day=a.get('day') or (dt.date.fromisoformat(now)+dt.timedelta(days=1)).isoformat()
        if day==now: raise ValueError('Choose a later working day.')
        if day<now or day>'2026-11-29': raise ValueError('Working days only move forward, through November 29.')
        dt.date.fromisoformat(day)
        if day>now and c.execute("SELECT 1 FROM messages WHERE day<=? AND status='pending'",(now,)).fetchone(): raise ValueError('Accept or reject the pending messages before advancing.')
        c.execute("UPDATE orders SET status='picked up' WHERE status='accepted' AND pickup<?",(day,))
        c.execute("UPDATE settings SET value=? WHERE key='today'",(day,));detail='Working day advanced to '+day
    elif kind=='helper':
        day=a['day']
        if (dt.date.fromisoformat(day)-dt.date.fromisoformat(now)).days<2: raise ValueError('Helpers need two calendar days of notice.')
        if not '2026-11-23'<=day<='2026-12-01': raise ValueError('Date outside this planning calendar.')
        if c.execute('SELECT 1 FROM helpers WHERE day=?',(day,)).fetchone(): raise ValueError('A helper is already booked.')
        c.execute('INSERT INTO helpers VALUES(?)',(day,));detail='Booked helper for '+day+' ($100)'
    elif kind=='helper_accept':
        m=c.execute('SELECT * FROM messages WHERE id=?',(int(a['id']),)).fetchone()
        if not m or not next((row['helper_offer'] for row in state(c)['messages'] if row['id']==m['id']),False):
            raise ValueError('A helper cannot make this request eligible.')
        # Keep both changes atomic even if a caller handles an exception itself.
        c.execute('SAVEPOINT helper_accept')
        try:
            action(c,dict(action='helper',day=m['pickup']))
            action(c,dict(action='accept',id=m['id']))
            c.execute('RELEASE helper_accept')
        except Exception:
            c.execute('ROLLBACK TO helper_accept');c.execute('RELEASE helper_accept');raise
        return
    elif kind in ('accept','reject'):
        m=c.execute('SELECT * FROM messages WHERE id=?',(int(a['id']),)).fetchone()
        if not m or m['status']!='pending' or m['day']>now: raise ValueError('Message is not available for a decision.')
        order_value,revenue_change=message_value(c,m)
        reason,target=eligibility(c,m)
        if kind=='accept':
            if reason: raise ValueError(reason)
            if m['kind']=='new':
                linked=c.execute('SELECT order_id FROM request_orders WHERE message_id=?',(m['id'],)).fetchone()
                if linked: c.execute("UPDATE orders SET status='accepted' WHERE id=?",(linked['order_id'],))
                else: c.execute("INSERT INTO orders(customer,product,quantity,pickup,status) VALUES(?,?,?,?,'accepted')",(m['customer'],m['product'],m['quantity'],m['pickup']))
            elif m['kind']=='cancel': c.execute("UPDATE orders SET status='cancelled' WHERE id=?",(target['id'],))
            else: c.execute('UPDATE orders SET product=?,quantity=? WHERE id=?',(m['product'],m['quantity'],target['id']))
        if kind=='reject' and m['kind']=='new':
            c.execute("UPDATE orders SET status='rejected' WHERE id=(SELECT order_id FROM request_orders WHERE message_id=?)",(m['id'],))
        note=reason if kind=='reject' and reason else 'Baker decision'
        c.execute('UPDATE messages SET status=?,note=?,order_value=?,revenue_change=? WHERE id=?',('accepted' if kind=='accept' else 'rejected',note,order_value,revenue_change,m['id']))
        detail=f"{kind.title()} message {m['id']}: {m['customer']} / {m['kind']} ({note})"
    else: raise ValueError('Unknown action.')
    c.execute('INSERT INTO audit(day,detail) VALUES(?,?)',(now,detail))

PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Butter & Batch · Bakery planner</title>
<style>
:root{--ink:#2f3530;--green:#345b48;--cream:#f7f4ed;--line:#deded2}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:15px system-ui,sans-serif}header{background:var(--green);color:white;padding:30px 5%;display:flex;justify-content:space-between;align-items:center}h1{font:36px Georgia;margin:4px 0}header a{color:white}main{max-width:1350px;margin:30px auto;padding:0 24px}h2{font:27px Georgia}small,.muted{color:#60685f}.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.calendar{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:22px 0}.card,.panel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:18px}.card b{display:block;margin-bottom:10px}.card strong{font-size:23px}.bar{height:7px;background:#eee;border-radius:6px;margin:12px 0}.bar i{display:block;background:#74937d;height:100%;border-radius:6px}.layout{display:grid;grid-template-columns:1.05fr 1fr;gap:24px}.message{border-top:1px solid var(--line);padding:18px 0}.message h3{margin:0 0 8px}pre{white-space:pre-wrap;font:14px system-ui;line-height:1.7}.reason{color:#a1482e}.badge{background:#edf1e8;border-radius:20px;padding:3px 10px;font-size:12px}button,select,input{font:inherit;padding:9px 12px;border:1px solid #b7c2b8;border-radius:6px;background:white}button{cursor:pointer}button.primary{background:var(--green);color:white}button:disabled{opacity:.4;cursor:not-allowed}table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:12px 6px;border-bottom:1px solid var(--line)}#notice{position:sticky;top:8px;background:#ffedcc;padding:12px;display:none;z-index:2}details{margin-top:24px}li{margin:8px 0}@media(max-width:850px){.layout{grid-template-columns:1fr}header{display:block}.panel{overflow:auto}}

main{max-width:1440px}.layout{grid-template-columns:minmax(320px,.95fr) minmax(420px,1.15fr);align-items:start;margin-top:28px}.insights{display:grid;gap:22px}.panel{padding:26px;border-radius:18px;box-shadow:0 5px 20px #283d2b05}h2{margin:0 0 12px}h3{margin:0}.section-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.section-heading h2{margin:0}.message{padding:24px 0}.message:first-child{margin-top:20px}.message:last-child{padding-bottom:0}.order-metrics{display:grid;grid-template-columns:1fr 1fr;gap:16px;background:#f7f5ee;border-radius:12px;padding:20px;margin:16px 0}.order-metrics small{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em}.order-metrics strong{display:block;font-size:32px;letter-spacing:-1px;margin-top:5px;color:var(--green)}.pickup{font-weight:650}.original{margin-top:14px;font-size:13px;color:#60685f}.original summary{cursor:pointer}.decision-buttons{display:flex;gap:10px}.decision-buttons button{min-height:45px;font-weight:650}.decision-buttons .primary{flex:1}.reject{color:#994c36;border-color:#dfc9be}.helper-offer{padding:15px;background:#f8ecdc;border-radius:10px;margin:14px 0}.helper-offer small{display:block;margin-top:6px;line-height:1.5}.calendar{display:grid;grid-template-columns:1fr;gap:0;margin:18px 0 0}.day-row{display:flex;align-items:center;justify-content:space-between;gap:10px;border-top:1px solid #eeeee6;padding:8px 0}.day-select{display:flex;flex:1;justify-content:space-between;align-items:center;text-align:left;border:0;background:transparent;padding:8px;font-size:13px;gap:10px}.day-select span{color:var(--green)}.selected .day-select{background:#edf2ea;border-radius:6px}.helper-button{font-size:11px;padding:6px 8px}.time-overview{display:flex;align-items:center;gap:25px;padding-top:25px}.donut{width:155px;height:155px;border-radius:50%;display:grid;place-items:center;flex-shrink:0}.donut>div{width:116px;height:116px;border-radius:50%;background:white;display:flex;flex-direction:column;justify-content:center;align-items:center}.donut strong{font-size:31px;color:var(--green)}.legend{display:flex;gap:15px;flex-wrap:wrap;font-size:12px;color:#60685f}.legend span{display:flex;align-items:center;gap:6px}.legend i{display:inline-block;width:10px;height:10px;border-radius:3px}.revenue-row{margin-top:19px}.revenue-label{display:flex;justify-content:space-between;font-size:12px;margin-bottom:7px}.stack-track{display:flex;height:17px;border-radius:4px;background:#f1f2eb;overflow:hidden}.stack-net{background:#345b48}.stack-cost{background:#cf805f}.revenue-row>small{display:block;margin-top:4px;font-size:11px}.chart-note{font-size:12px;line-height:1.5;margin-bottom:0}.order-book{margin-top:24px;overflow:auto}.empty{padding:25px 0;line-height:1.6}button:hover:not(:disabled){filter:brightness(.95)}button:focus-visible,select:focus-visible,input:focus-visible{outline:3px solid #b98e48;outline-offset:3px}@media(min-width:1050px){.inbox-panel{position:sticky;top:20px;max-height:calc(100vh - 40px);overflow:auto}}@media(max-width:900px){.layout{grid-template-columns:1fr}.time-overview{justify-content:center}}@media(max-width:500px){main{padding:0 12px}.panel{padding:18px}.section-heading{flex-wrap:wrap}.time-overview{gap:15px}.donut{width:125px;height:125px}.donut>div{width:94px;height:94px}.day-select{flex-wrap:wrap}.decision-buttons{flex-wrap:wrap}.order-metrics strong{font-size:28px}}
.planner-search{display:block;margin-top:24px}.planner-search input{max-width:100%;margin-top:8px}.day-group{padding-bottom:16px}.day-empty{font-size:13px;margin:8px}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}.comparison-row{display:grid;grid-template-columns:80px minmax(0,1fr) 55px;align-items:center;gap:10px;margin-top:8px;font-size:12px}.comparison-row>b{text-align:right}.message.active-order{border-left:3px solid #79a48a;padding-left:14px}.message{scroll-margin-top:20px}.revenue-row>b{font-size:13px}#timeChart,#inbox{overflow-x:clip}.orders-dropdown>summary{cursor:pointer;font-weight:650;padding:12px 0}.orders-dropdown[open]>summary{margin-bottom:8px}.decision-feedback{position:fixed;bottom:26px;left:50%;transform:translateX(-50%);z-index:100;background:#345b48;color:white;padding:14px 24px;border-radius:14px;box-shadow:0 8px 30px #0002;font-weight:650;pointer-events:none}.decision-feedback.rejected{background:#994c36}.decision-stamp{display:inline-block;padding:7px 12px;border-radius:8px;background:#e4efe6;color:#345b48;font-weight:700;margin-top:12px}.decision-stamp.rejected{background:#fae8e1;color:#994c36}</style><header><div><small style="color:#dce4d7">THE HOME BAKERY / ORDER DESK</small><h1>Butter & Batch</h1><div>A little order for your busiest baking days.</div></div><a href="/README.html" target="_blank">How to use this app ↗</a></header>
<main><div id="notice" role="alert"></div><div class="toolbar"><b>Working day</b><strong id="day"></strong><button id="advance" onclick="act({action:'day'})">Advance day</button><button onclick="act({action:'accept_all_next'})">Accept all and go next day</button><span id="advanceHint" class="muted"></span><button onclick="resetEverything()">Reset everything</button><span class="muted">November 23–29, 2026 · Pickups at 5 PM</span></div><p class="muted">Two days’ notice · 8 oven hours daily · Helper: +4 hours for $100 · Cupcakes $40/dozen (½ hour) · Cakes $120 (2 hours)</p>
<div class="layout"><section class="panel inbox-panel"><div class="section-heading"><h2>Order requests</h2><span id="pending" class="badge"></span></div><p class="muted">Review the value, time and pickup date. Incompatible new orders are rejected automatically. Accept all checks requests one by one.</p><label><input type="checkbox" id="history" onchange="render()"> Show decided messages</label><div id="inbox"></div></section><aside class="insights"><section class="panel"><div class="section-heading"><h2>Orders & days</h2><select id="chartDay" aria-label="Day for time availability" onchange="renderCharts()"></select></div><div id="timeChart"></div><details class="orders-dropdown"><summary>Show orders by day</summary><label class="planner-search">Search customer <input id="search" oninput="renderOrders()" placeholder="Customer name"></label><div class="calendar" id="calendar"></div></details></section><section class="panel"><h2>Revenue & costs</h2><p class="muted">Two bars per pickup day: order revenue and helper costs.</p><div class="legend"><span><i style="background:#345b48"></i>Order revenue</span><span><i style="background:#cf805f"></i>Helper costs</span></div><div id="revenueChart"></div><p class="muted chart-note">Only helper costs are recorded. Ingredients and other expenses are excluded; Revenue minus helper costs does not include those expenses.</p></section></aside></div><details class="panel"><summary>Decision history</summary><ul id="audit"></ul></details></main>
<script>
let data,activeOrder=null,followFrame=null;const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const date=s=>new Date(s+'T12:00:00').toLocaleDateString('en-CA',{weekday:'short',month:'short',day:'numeric'});const money=n=>'$'+n;
async function load(){data=await(await fetch('/api/state')).json();render()}
function resetEverything(){if(confirm('Reset everything? This permanently clears all decisions, helper bookings and history, restores the original orders and messages, and returns to Monday November 23. This cannot be undone.'))act({action:'reset',confirm:'RESET'})}
let actionBusy=false,feedbackTimer;
function decisionFeedback(label,rejected){
 document.getElementById('decisionFeedback')?.remove();clearTimeout(feedbackTimer);
 const n=document.createElement('div');n.id='decisionFeedback';n.className='decision-feedback'+(rejected?' rejected':'');n.setAttribute('role','status');n.textContent=(rejected?'✕ ':'✓ ')+label;document.body.append(n);
 if(!matchMedia('(prefers-reduced-motion: reduce)').matches)n.animate([{opacity:0,transform:'translate(-50%, 16px) scale(.96)'},{opacity:1,transform:'translate(-50%, 0) scale(1)'}],{duration:240,easing:'ease-out'});
 feedbackTimer=setTimeout(()=>n.remove(),3000);
}
async function animateDecision(card,rejected){
 if(!card)return;
 const stamp=document.createElement('div');stamp.className='decision-stamp'+(rejected?' rejected':'');stamp.textContent=rejected?'✕ Rejected':'✓ Accepted';card.append(stamp);
 if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;
 await card.animate([{backgroundColor:rejected?'#fae8e1':'#e4efe6',opacity:1,transform:'translateX(0)'},{backgroundColor:rejected?'#fae8e1':'#e4efe6',opacity:1,transform:'translateX(0)',offset:.5},{opacity:0,transform:'translateX(-48px)'}],{duration:950,easing:'cubic-bezier(.4,0,.2,1)',fill:'forwards'}).finished;
}
async function act(a){
 if(actionBusy)return;actionBusy=true;
 const decision=['accept','helper_accept','reject'].includes(a.action),rejected=a.action==='reject';
 const card=decision?document.querySelector(`[data-message-id="${a.id}"]`):null;
 const buttons=card?[...card.querySelectorAll('button')]:[],disabled=buttons.map(b=>b.disabled);buttons.forEach(b=>b.disabled=true);
 try{
  let r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(a)});let j=await r.json();if(!r.ok)throw Error(j.error);
  if(decision){
   const pickup=card?.dataset.pickup;
   const previous=data.calendar.find(d=>d.day===pickup);
   const before=previous?Math.max(0,previous.capacity-previous.hours):null;
   const response=await fetch('/api/state');if(!response.ok)throw Error('Could not refresh availability. Refresh the page.');
   data=await response.json();
   if(pickup)document.getElementById('chartDay').value=pickup;
   renderCharts();
   const updated=data.calendar.find(d=>d.day===pickup);
   const after=updated?Math.max(0,updated.capacity-updated.hours):null;
   const label=rejected?'Request rejected':'Request accepted';
   decisionFeedback(label+(after===null?'':` · ${date(pickup)}: ${before} → ${after} h available${before===after?' (unchanged)':''}`),rejected);
   const chart=document.getElementById('timeChart');
   if(!matchMedia('(prefers-reduced-motion: reduce)').matches){
    chart.animate([{backgroundColor:'#eaf2eb'},{backgroundColor:'transparent'}],{duration:900,easing:'ease-out'});
    const number=chart.querySelector('.donut strong');
    if(number&&before!==null&&after!==null){
     const start=performance.now();
     await new Promise(resolve=>{function tick(now){const progress=Math.min(1,(now-start)/550);number.textContent=(Math.round((before+(after-before)*progress)*10)/10)+' h';if(progress<1)requestAnimationFrame(tick);else resolve()}requestAnimationFrame(tick)});
    }
   }
   await Promise.all([animateDecision(card,rejected),new Promise(resolve=>setTimeout(resolve,1150))]);
  }
  if(a.action==='reset'){document.getElementById('search').value='';document.getElementById('history').checked=false}
  if(decision||['day','accept_all_next'].includes(a.action)){
   const response=await fetch('/api/state');if(!response.ok)throw Error('Could not refresh the page. Please refresh.');
   const next=await response.json();
   const pending=next.messages.filter(m=>m.status==='pending');
   const nextMessage=pending.find(m=>String(m.id)===activeOrder)||pending[0];
   const picker=document.getElementById('chartDay');
   const nextDay=nextMessage?.pickup||(['day','accept_all_next'].includes(a.action)?next.today:picker.value);
   const chart=document.getElementById('timeChart'),inbox=document.getElementById('inbox');
   const movingDay=picker.value!==nextDay;
   const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
   const targets=movingDay?[chart,inbox]:[inbox];
   const height=inbox.getBoundingClientRect().height;inbox.style.minHeight=height+'px';
   try{
    if(!reduced)await Promise.all(targets.map(el=>el.animate([{opacity:1,transform:'translateX(0)'},{opacity:0,transform:'translateX(-36px)'}],{duration:240,easing:'ease-in',fill:'forwards'}).finished));
    data=next;activeOrder=nextMessage?String(nextMessage.id):null;picker.value=nextDay;render();
    targets.forEach(el=>el.getAnimations().forEach(animation=>animation.cancel()));
    if(!reduced)await Promise.all(targets.map(el=>el.animate([{opacity:0,transform:'translateX(36px)'},{opacity:1,transform:'translateX(0)'}],{duration:360,easing:'cubic-bezier(.2,.7,.2,1)'}).finished));
   }finally{inbox.style.minHeight='';targets.forEach(el=>el.getAnimations().forEach(animation=>animation.cancel()))}
  }else await load();
  document.getElementById('notice').style.display='none';
 }catch(e){let n=document.getElementById('notice');n.textContent=e.message;n.style.display='block'}
 finally{actionBusy=false;buttons.forEach((b,i)=>b.disabled=disabled[i])}
}
function render(){document.getElementById('day').textContent=date(data.today);let waiting=data.messages.filter(m=>m.status==='pending').length;let ended=data.today==='2026-11-29';document.getElementById('advance').disabled=waiting>0||ended;document.getElementById('advance').textContent=ended?'Week complete':'Advance to '+date(new Date(new Date(data.today+'T12:00:00').getTime()+86400000).toISOString().slice(0,10));document.getElementById('advanceHint').textContent=waiting?'Accept or reject '+waiting+' pending message(s) first.':ended?'Reset everything to start again.':'';renderCharts();let pending=data.messages.filter(m=>m.status==='pending');document.getElementById('pending').textContent=pending.length+' pending';let ms=document.getElementById('history').checked?data.messages:pending;document.getElementById('inbox').innerHTML=ms.map(m=>`<article class="message" data-message-id="${m.id}" data-pickup="${m.pickup}" tabindex="0" onfocus="followOrder(this)" onclick="followOrder(this)"><div class="section-heading"><h3>${m.kind==='cancel'?'Cancellation':m.kind==='change'?'Order change':'New order'} · ${esc(m.customer)}</h3><span class="badge">${esc(m.kind)} · ${esc(m.status)}</span></div><div class="order-metrics"><div><small>${m.kind==='cancel'?'Order to cancel':'Order value'}</small><strong>${m.order_value===null?'Unavailable':money(m.order_value)}</strong></div><div><small>${m.kind==='cancel'?'Time released':'Baking time'}</small><strong>${m.hours===null?'—':m.hours+' h'}</strong></div></div><div class="pickup">Pickup ${date(m.pickup)} · 5 PM</div>${m.kind!=='new'&&m.revenue_change!==null?`<p class="muted">Revenue change: ${m.revenue_change>0?'+':''}${money(m.revenue_change)}</p>`:''}<details class="original"><summary>View Instagram message · ${date(m.day)}</summary><pre>${esc(m.raw)}</pre></details><p class="${m.reason?'reason':'muted'}">${esc(m.reason||'Ready to accept · within notice and capacity rules.')}</p>${m.helper_offer?`<div class="helper-offer"><b>${m.net_with_helper>=0?money(m.net_with_helper)+' extra after the helper fee':money(-m.net_with_helper)+' less after the helper fee'}</b><small>${money(m.revenue_change)} change in order revenue − $100 helper fee = ${money(m.net_with_helper)} overall change. The helper adds 4 hours for the whole pickup day.</small></div>`:''}${m.status==='pending'?`<div class="decision-buttons">${m.helper_offer?`<button class="primary" onclick="if(confirm('Hire a helper for $100 and accept this request? Change after the helper fee: ${money(m.net_with_helper)}.'))act({action:'helper_accept',id:${m.id}})">Hire helper & accept · $100</button>`:`<button class="primary" ${m.reason?'disabled':''} onclick="act({action:'accept',id:${m.id}})">${m.kind==='new'?'Accept order':'Approve '+m.kind}</button>`}<button class="reject" onclick="act({action:'reject',id:${m.id}})">Reject</button></div>`:''}</article>`).join('')||'<p class="empty">All caught up. Advance the working day to see the next messages.</p>' ;watchOrders();renderOrders();document.getElementById('audit').innerHTML=data.audit.map(a=>`<li>${esc(a.day)} · ${esc(a.detail)}</li>`).join('')||'<li>No decisions yet.</li>'}
function renderCharts(){
 const picker=document.getElementById('chartDay');const selected=picker.value||data.today;
 picker.innerHTML=data.calendar.map(d=>`<option value="${d.day}" ${d.day===selected?'selected':''}>${date(d.day)}</option>`).join('');
 const d=data.calendar.find(d=>d.day===picker.value)||data.calendar[0];
 const available=Math.max(0,d.capacity-d.hours),pct=available/d.capacity*100;
 document.getElementById('timeChart').innerHTML=`<div class="time-overview"><div class="donut" role="img" aria-label="${date(d.day)}: ${available} hours available, ${d.hours} hours booked out of ${d.capacity}" style="background:conic-gradient(#79a48a 0 ${pct}%,#e9e9df ${pct}% 100%)"><div><strong>${available} h</strong><small>available</small></div></div><div><h3>${date(d.day)}</h3><p><b>${d.hours} h</b> booked / ${d.capacity} h total</p><div class="legend"><span><i style="background:#79a48a"></i>Available</span><span><i style="background:#e9e9df"></i>Booked</span></div></div></div>`;
 renderOrders();
 const max=Math.max(100,...data.calendar.map(d=>Math.max(d.revenue,d.helper?100:0)));
 document.getElementById('revenueChart').innerHTML=data.calendar.map(d=>{const cost=d.helper?100:0;return `<div class="revenue-row"><b>${date(d.day)}</b><div class="comparison-row"><span>Revenue</span><div class="stack-track" role="img" aria-label="Revenue ${money(d.revenue)}"><div class="stack-net" style="width:${d.revenue/max*100}%"></div></div><b>${money(d.revenue)}</b></div><div class="comparison-row"><span>Helper costs</span><div class="stack-track" role="img" aria-label="Helper costs ${money(cost)}"><div class="stack-cost" style="width:${cost/max*100}%"></div></div><b>${money(cost)}</b></div></div>`}).join('');
}
function followOrder(card){
 if(!card||actionBusy)return;
 activeOrder=card.dataset.messageId;
 document.querySelectorAll('.message').forEach(el=>el.classList.toggle('active-order',el===card));
 const picker=document.getElementById('chartDay');
 if(picker.value!==card.dataset.pickup){picker.value=card.dataset.pickup;renderCharts()}
}
function followVisibleOrder(){
 const pane=document.querySelector('.inbox-panel').getBoundingClientRect();
 const top=Math.max(0,pane.top),bottom=Math.min(window.innerHeight,pane.bottom);
 let best=null,score=0;
 document.querySelectorAll('.message').forEach(card=>{const r=card.getBoundingClientRect();const visible=Math.max(0,Math.min(r.bottom,bottom)-Math.max(r.top,top));if(visible>score){score=visible;best=card}});
 if(best&&best.dataset.messageId!==activeOrder)followOrder(best);
}
function scheduleFollow(){if(followFrame===null)followFrame=requestAnimationFrame(()=>{followFrame=null;followVisibleOrder()})}
function watchOrders(){
 const cards=[...document.querySelectorAll('.message')];
 followOrder(cards.find(card=>card.dataset.messageId===activeOrder)||cards[0]);
 document.querySelector('.inbox-panel').onscroll=scheduleFollow;
}
window.addEventListener('scroll',scheduleFollow,{passive:true});
window.addEventListener('resize',scheduleFollow);
function renderOrders(){
 const q=document.getElementById('search').value.trim().toLowerCase();
 const selected=document.getElementById('chartDay').value;
 document.getElementById('calendar').innerHTML=data.calendar.filter(d=>!q||d.orders.some(o=>o.customer.toLowerCase().includes(q))).map(d=>{
 const orders=d.orders.filter(o=>o.customer.toLowerCase().includes(q));
 return `<section class="day-group"><div class="day-row ${d.day===selected?'selected':''}"><button class="day-select" onclick="document.getElementById('chartDay').value='${d.day}';renderCharts()"><b>${date(d.day)}</b><span>${Math.max(0,d.capacity-d.hours)} h free <small>/ ${d.capacity} h</small></span></button>${d.helper?'<span class="badge">Helper hired</span>':`<button class="helper-button" ${(new Date(d.day)-new Date(data.today))/86400000<2?'disabled':''} onclick="if(confirm('Book a helper for ${date(d.day)} for $100?'))act({action:'helper',day:'${d.day}'})">+ Helper · $100</button>`}</div>${orders.length?`<table><caption class="sr-only">Orders for ${date(d.day)}</caption><thead><tr><th>Order / customer</th><th>Product</th><th>Status</th></tr></thead><tbody>${orders.map(o=>`<tr><td>#${o.id} ${esc(o.customer)}</td><td>${o.quantity} ${o.product==='cake'?'cake(s)':'dozen cupcakes'}</td><td>${esc(o.status)}</td></tr>`).join('')}</tbody></table>`:'<p class="muted day-empty">No orders for this day.</p>'}</section>`;
 }).join('')||'<p class="empty">No customers match your search.</p>';
}load();
</script></html>'''

class Handler(BaseHTTPRequestHandler):
    def reply(self,code,body,typ='application/json'):
        payload=(json.dumps(body) if typ=='application/json' else body).encode()
        self.send_response(code);self.send_header('Content-Type',typ+'; charset=utf-8');self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(payload)
    def do_GET(self):
        if self.path=='/': self.reply(200,PAGE,'text/html')
        elif self.path=='/README.html': self.reply(200,(ROOT/'README.html').read_text(),'text/html')
        elif self.path=='/api/state':
            with connect() as c:
                c.execute('BEGIN IMMEDIATE')
                result=state(c)
            self.reply(200,result)
        else:self.reply(404,{'error':'Not found'})
    def do_POST(self):
        if self.path!='/api/action':self.reply(404,{'error':'Not found'});return
        if self.headers.get('Origin') not in (None,'http://127.0.0.1:8000','http://localhost:8000'):self.reply(403,{'error':'Origin not allowed'});return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<8192:raise ValueError('Invalid request size.')
            a=json.loads(self.rfile.read(length))
            with connect() as c:
                c.execute('BEGIN IMMEDIATE');action(c,a)
            self.reply(200,{'ok':True})
        except (ValueError,KeyError,TypeError,sqlite3.IntegrityError) as e:self.reply(400,{'error':str(e)})

if __name__=='__main__':
    init()
    print('Bakery app: http://127.0.0.1:8000 — keep this window open. Ctrl+C stops it.',flush=True)
    ThreadingHTTPServer(('127.0.0.1',8000),Handler).serve_forever()
