#!/usr/bin/env python3
"""
ZapChat v2 - Real-time Chat Server
Features: file sharing, contact invitations, groups, profile pictures, voice messages
Pure Python stdlib only. Data saved to zapchat.db
Run: python3 chat_server.py  ->  open http://localhost:8080
"""

import hashlib, http.server, json, mimetypes, os, random, re
import sqlite3, struct, threading, time, base64, uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE      = Path(__file__).parent
DB_FILE   = BASE / "zapchat.db"
MEDIA_DIR = BASE / "zapchat_media"
MEDIA_DIR.mkdir(exist_ok=True)

# ── Database ───────────────────────────────────────────────────────────────────
def get_db():
    c = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    c = get_db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            username      TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            display       TEXT NOT NULL,
            tag           TEXT NOT NULL,
            bio           TEXT DEFAULT 'Hey there! I am using ZapChat.',
            color         TEXT NOT NULL,
            avatar        TEXT DEFAULT '',
            joined        INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invitations (
            id        TEXT PRIMARY KEY,
            from_user TEXT NOT NULL,
            to_user   TEXT NOT NULL,
            status    TEXT DEFAULT 'pending',
            ts        INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contacts (
            user_a TEXT NOT NULL,
            user_b TEXT NOT NULL,
            PRIMARY KEY (user_a, user_b)
        );
        CREATE TABLE IF NOT EXISTS groups (
            id      TEXT PRIMARY KEY,
            name    TEXT NOT NULL,
            avatar  TEXT DEFAULT '',
            color   TEXT NOT NULL,
            owner   TEXT NOT NULL,
            created INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS group_members (
            group_id TEXT NOT NULL,
            username TEXT NOT NULL,
            role     TEXT DEFAULT 'member',
            PRIMARY KEY (group_id, username)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id        TEXT PRIMARY KEY,
            sender    TEXT NOT NULL,
            receiver  TEXT,
            group_id  TEXT,
            msg_type  TEXT DEFAULT 'text',
            text      TEXT NOT NULL,
            file_name TEXT DEFAULT '',
            ts        INTEGER NOT NULL,
            read      INTEGER DEFAULT 0
        );
    """)
    # Auto-migrate old databases: add missing columns safely
    migrations = [
        ("users",    "avatar",    "TEXT DEFAULT ''"),
        ("users",    "tag",       "TEXT DEFAULT '0000'"),
        ("messages", "msg_type",  "TEXT DEFAULT 'text'"),
        ("messages", "file_name", "TEXT DEFAULT ''"),
        ("messages", "group_id",  "TEXT"),
        ("messages", "receiver",  "TEXT"),
    ]
    col_cache = {}
    for table, col, defn in migrations:
        if table not in col_cache:
            col_cache[table] = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
        if col not in col_cache[table]:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
                print(f"  Migrated: {table}.{col} added")
                col_cache[table].add(col)
            except Exception as e:
                print(f"  Migration skip: {e}")
    c.commit(); c.close()
    print(f"  Database: {DB_FILE}")
    print(f"  Media:    {MEDIA_DIR}")

# ── Runtime state ──────────────────────────────────────────────────────────────
sessions   = {}
ws_clients = {}
lock       = threading.Lock()
COLORS = ["#00BCD4","#E91E63","#9C27B0","#FF5722","#4CAF50","#FF9800","#2196F3","#795548"]

def hash_pw(pw):  return hashlib.sha256(pw.encode()).hexdigest()
def new_token():  return str(uuid.uuid4())
def color_for(u): return COLORS[sum(ord(c) for c in u) % len(COLORS)]
def is_online(u):
    with lock: return bool(ws_clients.get(u))

def generate_tag(display_name):
    conn = get_db()
    used = {r[0] for r in conn.execute("SELECT tag FROM users WHERE LOWER(display)=?", (display_name.lower(),))}
    conn.close()
    for _ in range(2000):
        tag = f"{random.randint(1,9999):04d}"
        if tag not in used: return tag
    return str(random.randint(1000,9999))

def broadcast(username, payload):
    data = json.dumps(payload)
    with lock: conns = list(ws_clients.get(username, []))
    for conn in conns:
        try: conn.send_ws(data)
        except: pass

def broadcast_group(group_id, payload, skip=None):
    conn = get_db()
    members = [r["username"] for r in conn.execute(
        "SELECT username FROM group_members WHERE group_id=?", (group_id,))]
    conn.close()
    for m in members:
        if m != skip: broadcast(m, payload)

# ── WebSocket ──────────────────────────────────────────────────────────────────
class WS:
    def __init__(self, sock, addr): self.sock=sock; self.addr=addr; self.closed=False

    def send_ws(self, msg):
        if self.closed: return
        d = msg.encode(); n = len(d)
        if n<=125:     f=bytes([0x81,n])+d
        elif n<=65535: f=bytes([0x81,126])+struct.pack(">H",n)+d
        else:          f=bytes([0x81,127])+struct.pack(">Q",n)+d
        try: self.sock.sendall(f)
        except: self.closed=True

    def recv_ws(self):
        try:
            h=self._rx(2)
            if not h: return None
            op=h[0]&0x0F
            if op==8: return None
            masked=(h[1]&0x80)!=0
            n=h[1]&0x7F
            if n==126: n=struct.unpack(">H",self._rx(2))[0]
            elif n==127: n=struct.unpack(">Q",self._rx(8))[0]
            mask=self._rx(4) if masked else None
            data=self._rx(n)
            if masked: data=bytes(data[i]^mask[i%4] for i in range(n))
            return data.decode("utf-8") if op==1 else None
        except: return None

    def _rx(self,n):
        buf=b""
        while len(buf)<n:
            c=self.sock.recv(n-len(buf))
            if not c: raise ConnectionResetError
            buf+=c
        return buf

    def close(self):
        self.closed=True
        try: self.sock.close()
        except: pass

def handshake(sock, raw):
    text=raw.decode("utf-8",errors="replace")
    m=re.search(r"Sec-WebSocket-Key: (.+)\r\n",text)
    if not m: return False
    accept=base64.b64encode(hashlib.sha1(
        (m.group(1).strip()+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
    ).digest()).decode()
    sock.sendall((
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode())
    return True

# ── API ────────────────────────────────────────────────────────────────────────
def tok(token):
    with lock: return sessions.get(token)

def api_register(body):
    u=body.get("username","").strip().lower()
    p=body.get("password","")
    d=(body.get("display","") or u).strip()
    bio=body.get("bio","Hey there! I am using ZapChat.").strip()
    if not u or not p: return {"ok":False,"error":"Username and password required"}
    if not re.match(r"^[a-z0-9_]{3,20}$",u): return {"ok":False,"error":"Username: 3-20 chars, a-z 0-9 _"}
    conn=get_db()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username=?",(u,)).fetchone():
            return {"ok":False,"error":"Username already taken"}
        tag=generate_tag(d); color=color_for(u)
        conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?)",(u,hash_pw(p),d,tag,bio,color,"",int(time.time())))
        conn.commit()
        with lock: token=new_token(); sessions[token]=u
        return {"ok":True,"token":token,"username":u,"display":d,"tag":tag,"color":color,"avatar":""}
    finally: conn.close()

def api_login(body):
    u=body.get("username","").strip().lower(); p=body.get("password","")
    conn=get_db()
    try:
        row=conn.execute("SELECT * FROM users WHERE username=?",(u,)).fetchone()
        if not row or row["password_hash"]!=hash_pw(p): return {"ok":False,"error":"Invalid username or password"}
        with lock: token=new_token(); sessions[token]=u
        return {"ok":True,"token":token,"username":u,"display":row["display"],"tag":row["tag"],
                "color":row["color"],"bio":row["bio"],"avatar":row["avatar"] or ""}
    finally: conn.close()

def api_profile(token):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    conn=get_db()
    try:
        row=conn.execute("SELECT * FROM users WHERE username=?",(u,)).fetchone()
        if not row: return {"ok":False,"error":"Not found"}
        return {"ok":True,"username":u,"display":row["display"],"tag":row["tag"],"bio":row["bio"],
                "color":row["color"],"avatar":row["avatar"] or "","status":"online" if is_online(u) else "offline"}
    finally: conn.close()

def api_logout(token):
    with lock: sessions.pop(token,None)
    return {"ok":True}

def api_upload_avatar(token, body):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    data_url=body.get("data","")
    if not data_url.startswith("data:image"): return {"ok":False,"error":"Invalid image"}
    try:
        header,b64=data_url.split(",",1)
        ext="png" if "png" in header else "jpg"
        data=base64.b64decode(b64)
        if len(data)>5*1024*1024: return {"ok":False,"error":"Max 5MB"}
        fname=f"avatar_{u}.{ext}"
        (MEDIA_DIR/fname).write_bytes(data)
        url=f"/media/{fname}"
        conn=get_db(); conn.execute("UPDATE users SET avatar=? WHERE username=?",(url,u)); conn.commit(); conn.close()
        return {"ok":True,"avatar":url}
    except Exception as e: return {"ok":False,"error":str(e)}

def api_upload_file(token, body):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    data_url=body.get("data",""); fname=re.sub(r'[^\w.\-]','_',body.get("name","file"))[:120]
    try:
        _,b64=data_url.split(",",1)
        data=base64.b64decode(b64)
        if len(data)>50*1024*1024: return {"ok":False,"error":"Max 50MB"}
        safe=f"{uuid.uuid4().hex}_{fname}"
        (MEDIA_DIR/safe).write_bytes(data)
        return {"ok":True,"url":f"/media/{safe}","name":fname,"size":len(data)}
    except Exception as e: return {"ok":False,"error":str(e)}

def api_send_invite(token, body):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    target=body.get("username","").strip().lower()
    if target==u: return {"ok":False,"error":"Can't invite yourself"}
    conn=get_db()
    try:
        if not conn.execute("SELECT 1 FROM users WHERE username=?",(target,)).fetchone():
            return {"ok":False,"error":"User not found"}
        if conn.execute("SELECT 1 FROM contacts WHERE user_a=? AND user_b=?",(u,target)).fetchone():
            return {"ok":False,"error":"Already in contacts"}
        if conn.execute("SELECT 1 FROM invitations WHERE from_user=? AND to_user=? AND status='pending'",(u,target)).fetchone():
            return {"ok":False,"error":"Invitation already sent"}
        iid=str(uuid.uuid4())
        conn.execute("INSERT INTO invitations VALUES (?,?,?,'pending',?)",(iid,u,target,int(time.time()*1000)))
        conn.commit()
        me=conn.execute("SELECT * FROM users WHERE username=?",(u,)).fetchone()
    finally: conn.close()
    broadcast(target,{"type":"invitation","id":iid,"from":u,"display":me["display"],
                       "tag":me["tag"],"color":me["color"],"avatar":me["avatar"] or ""})
    return {"ok":True,"id":iid}

def api_answer_invite(token, body):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    iid=body.get("id",""); accept=body.get("accept",False)
    conn=get_db()
    try:
        inv=conn.execute("SELECT * FROM invitations WHERE id=? AND to_user=? AND status='pending'",(iid,u)).fetchone()
        if not inv: return {"ok":False,"error":"Invitation not found"}
        conn.execute("UPDATE invitations SET status=? WHERE id=?",("accepted" if accept else "declined",iid))
        if accept:
            conn.execute("INSERT OR IGNORE INTO contacts VALUES (?,?)",(u,inv["from_user"]))
            conn.execute("INSERT OR IGNORE INTO contacts VALUES (?,?)",(inv["from_user"],u))
        conn.commit()
        me=conn.execute("SELECT * FROM users WHERE username=?",(u,)).fetchone()
    finally: conn.close()
    if accept:
        broadcast(inv["from_user"],{"type":"invite_accepted","by":u,"display":me["display"],
                                     "tag":me["tag"],"color":me["color"],"avatar":me["avatar"] or ""})
    return {"ok":True}

def api_pending_invites(token):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    conn=get_db()
    try:
        rows=conn.execute("""SELECT i.*,u.display,u.tag,u.color,u.avatar
            FROM invitations i JOIN users u ON u.username=i.from_user
            WHERE i.to_user=? AND i.status='pending' ORDER BY i.ts DESC""",(u,)).fetchall()
        return {"ok":True,"invitations":[{"id":r["id"],"from":r["from_user"],"display":r["display"],
            "tag":r["tag"],"color":r["color"],"avatar":r["avatar"] or "","ts":r["ts"]} for r in rows]}
    finally: conn.close()

def api_contacts(token):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    conn=get_db()
    try:
        rows=conn.execute("""SELECT u.username,u.display,u.tag,u.color,u.bio,u.avatar
            FROM contacts c JOIN users u ON u.username=c.user_b WHERE c.user_a=?""",(u,)).fetchall()
        result=[]
        for r in rows:
            peer=r["username"]
            last=conn.execute("""SELECT * FROM messages WHERE group_id IS NULL
                AND ((sender=? AND receiver=?) OR (sender=? AND receiver=?))
                ORDER BY ts DESC LIMIT 1""",(u,peer,peer,u)).fetchone()
            unread=conn.execute("SELECT COUNT(*) FROM messages WHERE sender=? AND receiver=? AND read=0 AND group_id IS NULL",(peer,u)).fetchone()[0]
            result.append({"username":peer,"display":r["display"],"tag":r["tag"],"color":r["color"],
                           "bio":r["bio"],"avatar":r["avatar"] or "","status":"online" if is_online(peer) else "offline",
                           "last_message":{"text":last["text"],"ts":last["ts"],"from":last["sender"]} if last else None,
                           "unread":unread})
    finally: conn.close()
    result.sort(key=lambda x:(x["last_message"] or {}).get("ts",0),reverse=True)
    return {"ok":True,"contacts":result}

def api_messages(token, peer):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    conn=get_db()
    try:
        rows=conn.execute("""SELECT * FROM messages WHERE group_id IS NULL
            AND ((sender=? AND receiver=?) OR (sender=? AND receiver=?)) ORDER BY ts ASC""",(u,peer,peer,u)).fetchall()
        conn.execute("UPDATE messages SET read=1 WHERE sender=? AND receiver=? AND read=0 AND group_id IS NULL",(peer,u))
        conn.commit()
        return {"ok":True,"messages":[{"id":r["id"],"from":r["sender"],"to":r["receiver"],
            "msg_type":r["msg_type"],"text":r["text"],"file_name":r["file_name"],"ts":r["ts"],"read":bool(r["read"])} for r in rows]}
    finally: conn.close()

def api_send(token, body):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    to=body.get("to","").strip().lower(); text=body.get("text","").strip()
    msg_type=body.get("msg_type","text"); file_name=body.get("file_name","")
    if not text: return {"ok":False,"error":"Empty message"}
    conn=get_db()
    try:
        if not conn.execute("SELECT 1 FROM users WHERE username=?",(to,)).fetchone():
            return {"ok":False,"error":"Recipient not found"}
        mid=str(uuid.uuid4()); ts=int(time.time()*1000)
        conn.execute("INSERT INTO messages (id,sender,receiver,msg_type,text,file_name,ts,read) VALUES (?,?,?,?,?,?,?,0)",
                     (mid,u,to,msg_type,text,file_name,ts))
        conn.execute("INSERT OR IGNORE INTO contacts VALUES (?,?)",(u,to))
        conn.execute("INSERT OR IGNORE INTO contacts VALUES (?,?)",(to,u))
        conn.commit()
    finally: conn.close()
    msg={"id":mid,"from":u,"to":to,"msg_type":msg_type,"text":text,"file_name":file_name,"ts":ts,"read":False}
    broadcast(to,{"type":"message","message":msg})
    return {"ok":True,"message":msg}

def api_create_group(token, body):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    name=body.get("name","").strip(); members=body.get("members",[])
    if not name: return {"ok":False,"error":"Group name required"}
    gid=str(uuid.uuid4()); color=random.choice(COLORS)
    conn=get_db()
    try:
        conn.execute("INSERT INTO groups VALUES (?,?,?,?,?,?)",(gid,name,"",color,u,int(time.time()*1000)))
        conn.execute("INSERT INTO group_members VALUES (?,?,'owner')",(gid,u))
        for m in members:
            if m!=u and conn.execute("SELECT 1 FROM users WHERE username=?",(m,)).fetchone():
                conn.execute("INSERT OR IGNORE INTO group_members VALUES (?,?,'member')",(gid,m))
        conn.commit()
    finally: conn.close()
    group={"id":gid,"name":name,"color":color,"avatar":"","owner":u,"members":members+[u]}
    for m in members: broadcast(m,{"type":"group_added","group":group})
    return {"ok":True,"group":group}

def api_groups(token):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    conn=get_db()
    try:
        rows=conn.execute("""SELECT g.*,gm.role FROM groups g
            JOIN group_members gm ON gm.group_id=g.id WHERE gm.username=?""",(u,)).fetchall()
        result=[]
        for g in rows:
            members=[r["username"] for r in conn.execute("SELECT username FROM group_members WHERE group_id=?",(g["id"],))]
            last=conn.execute("SELECT * FROM messages WHERE group_id=? ORDER BY ts DESC LIMIT 1",(g["id"],)).fetchone()
            unread=conn.execute("SELECT COUNT(*) FROM messages WHERE group_id=? AND sender!=? AND read=0",(g["id"],u)).fetchone()[0]
            result.append({"id":g["id"],"name":g["name"],"color":g["color"],"avatar":g["avatar"] or "",
                           "owner":g["owner"],"role":g["role"],"members":members,
                           "last_message":{"text":last["text"],"ts":last["ts"],"from":last["sender"]} if last else None,
                           "unread":unread})
    finally: conn.close()
    result.sort(key=lambda x:(x["last_message"] or {}).get("ts",0),reverse=True)
    return {"ok":True,"groups":result}

def api_group_messages(token, group_id):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    conn=get_db()
    try:
        if not conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND username=?",(group_id,u)).fetchone():
            return {"ok":False,"error":"Not a member"}
        rows=conn.execute("SELECT * FROM messages WHERE group_id=? ORDER BY ts ASC",(group_id,)).fetchall()
        conn.execute("UPDATE messages SET read=1 WHERE group_id=? AND sender!=? AND read=0",(group_id,u))
        conn.commit()
        return {"ok":True,"messages":[{"id":r["id"],"from":r["sender"],"group_id":r["group_id"],
            "msg_type":r["msg_type"],"text":r["text"],"file_name":r["file_name"],"ts":r["ts"],"read":bool(r["read"])} for r in rows]}
    finally: conn.close()

def api_send_group(token, body):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    gid=body.get("group_id",""); text=body.get("text","").strip()
    msg_type=body.get("msg_type","text"); file_name=body.get("file_name","")
    if not text: return {"ok":False,"error":"Empty"}
    conn=get_db()
    try:
        if not conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND username=?",(gid,u)).fetchone():
            return {"ok":False,"error":"Not a member"}
        mid=str(uuid.uuid4()); ts=int(time.time()*1000)
        conn.execute("INSERT INTO messages (id,sender,group_id,msg_type,text,file_name,ts,read) VALUES (?,?,?,?,?,?,?,0)",
                     (mid,u,gid,msg_type,text,file_name,ts))
        conn.commit()
    finally: conn.close()
    msg={"id":mid,"from":u,"group_id":gid,"msg_type":msg_type,"text":text,"file_name":file_name,"ts":ts,"read":False}
    broadcast_group(gid,{"type":"group_message","message":msg},skip=u)
    return {"ok":True,"message":msg}

def api_search(token, query):
    u=tok(token)
    if not u: return {"ok":False,"error":"Not authenticated"}
    q=query.strip().lower()
    conn=get_db()
    try:
        rows=conn.execute("SELECT * FROM users WHERE username!=?",(u,)).fetchall()
        results=[]
        for r in rows:
            if q in r["username"] or q in r["display"].lower() or q in f"{r['display']}#{r['tag']}".lower():
                results.append({"username":r["username"],"display":r["display"],"tag":r["tag"],
                                "color":r["color"],"avatar":r["avatar"] or "",
                                "status":"online" if is_online(r["username"]) else "offline"})
    finally: conn.close()
    return {"ok":True,"results":results[:10]}

# ── HTTP Server ────────────────────────────────────────────────────────────────
HTML_FILE = BASE / "index.html"

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass

    def do_GET(self):
        p=urlparse(self.path); path=p.path; qs=parse_qs(p.query)
        token=self.headers.get("X-Token","")
        if path in ("/","/index.html"): return self._file(HTML_FILE,"text/html; charset=utf-8")
        if path.startswith("/media/"):
            fp=MEDIA_DIR/path[7:]
            if fp.exists() and fp.is_file():
                mime=mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
                return self._file(fp,mime,True)
            return self._json({"error":"Not found"},404)
        if path=="/ws":
            if self.headers.get("Upgrade","").lower()=="websocket": return self._handle_ws()
            return self._json({"ok":False,"error":"WebSocket required"},400)
        routes={
            "/api/profile":         lambda: api_profile(token),
            "/api/contacts":        lambda: api_contacts(token),
            "/api/groups":          lambda: api_groups(token),
            "/api/pending_invites": lambda: api_pending_invites(token),
            "/api/messages":        lambda: api_messages(token,qs.get("peer",[""])[0]),
            "/api/group_messages":  lambda: api_group_messages(token,qs.get("group_id",[""])[0]),
            "/api/search":          lambda: api_search(token,qs.get("q",[""])[0]),
        }
        fn=routes.get(path)
        if fn: return self._json(fn())
        self._json({"error":"Not found"},404)

    def do_POST(self):
        length=int(self.headers.get("Content-Length",0))
        body=json.loads(self.rfile.read(length) or b"{}")
        token=self.headers.get("X-Token",""); path=urlparse(self.path).path
        routes={
            "/api/register":      lambda: api_register(body),
            "/api/login":         lambda: api_login(body),
            "/api/logout":        lambda: api_logout(token),
            "/api/send":          lambda: api_send(token,body),
            "/api/send_group":    lambda: api_send_group(token,body),
            "/api/send_invite":   lambda: api_send_invite(token,body),
            "/api/answer_invite": lambda: api_answer_invite(token,body),
            "/api/create_group":  lambda: api_create_group(token,body),
            "/api/upload_file":   lambda: api_upload_file(token,body),
            "/api/upload_avatar": lambda: api_upload_avatar(token,body),
        }
        fn=routes.get(path)
        if fn: return self._json(fn())
        self._json({"error":"Not found"},404)

    def _file(self,path,mime,download=False):
        data=Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",mime)
        self.send_header("Content-Length",len(data))
        if download: self.send_header("Content-Disposition",f'inline; filename="{Path(path).name}"')
        self.end_headers(); self.wfile.write(data)

    def _json(self,data,code=200):
        payload=json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",len(payload))
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers(); self.wfile.write(payload)

    def _handle_ws(self):
        sock=self.connection; sock.setblocking(True)
        raw=("GET /ws HTTP/1.1\r\n"+"".join(f"{k}: {v}\r\n" for k,v in self.headers.items())+"\r\n").encode()
        if not handshake(sock,raw): return sock.close()
        ws=WS(sock,self.client_address); username=None
        try:
            while True:
                msg=ws.recv_ws()
                if msg is None: break
                try: data=json.loads(msg)
                except: continue
                t=data.get("type","")
                if t=="auth":
                    with lock:
                        username=sessions.get(data.get("token",""))
                        if username: ws_clients.setdefault(username,[]).append(ws)
                    ws.send_ws(json.dumps({"type":"auth_ok","username":username} if username else {"type":"auth_fail"}))
                elif t=="ping": ws.send_ws(json.dumps({"type":"pong"}))
                elif t=="typing" and username:
                    to=data.get("to",""); gid=data.get("group_id","")
                    if gid: broadcast_group(gid,{"type":"typing","from":username,"group_id":gid},skip=username)
                    elif to: broadcast(to,{"type":"typing","from":username})
        finally:
            ws.close()
            if username:
                with lock:
                    conns=ws_clients.get(username,[])
                    if ws in conns: conns.remove(ws)

class Server(http.server.HTTPServer):
    def process_request(self,req,addr):
        t=threading.Thread(target=self._h,args=(req,addr)); t.daemon=True; t.start()
    def _h(self,req,addr):
        try: self.finish_request(req,addr)
        except: pass

if __name__=="__main__":
    HOST = "0.0.0.0"
    PORT = int(os.environ.get("PORT", 8080))
    init_db()
    print(f"""
╔═════════════════════════════════════════╗
║   ZapChat v2 - Real-time Chat Server   ║
╠═════════════════════════════════════════╣
║  Open: http://localhost:{PORT}             ║
║  LAN:  http://<your-ip>:{PORT}             ║
║  Data: zapchat.db + zapchat_media/     ║
╚═════════════════════════════════════════╝
  Features: files, invites, groups, avatars, voice
""")
    s=Server((HOST,PORT),Handler)
    try: s.serve_forever()
    except KeyboardInterrupt: print("\nStopped. Data safe in zapchat.db")