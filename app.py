from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from io import BytesIO
import sqlite3
import json
import requests
import re
from datetime import datetime, timedelta
import secrets
import smtplib
from email.message import EmailMessage
from functools import wraps

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:
    BackgroundScheduler = None
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database", "inventory.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

class HybridRow(dict):
    def __init__(self, data, values=None):
        super().__init__(data)
        self._values = list(values if values is not None else data.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class PGCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.lastrowid = None

    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        clean = sql.lstrip().upper()

        if clean.startswith("INSERT INTO") and "RETURNING" not in clean:
            table_match = re.search(r"INSERT\s+INTO\s+(\w+)", sql, re.I)
            if table_match and table_match.group(1).lower() in {
                "users", "products", "customers", "sales", "sale_items"
            }:
                sql += " RETURNING id"
                self.cursor.execute(sql, params)
                row = self.cursor.fetchone()
                self.lastrowid = row[0] if row else None
                return self

        self.cursor.execute(sql, params)
        return self

    def _wrap_row(self, row):
        if row is None:
            return None

        # psycopg normally returns tuples. Convert them to a dict using
        # cursor.description while preserving integer indexing like sqlite3.Row.
        if isinstance(row, dict):
            data = row
            values = list(row.values())
        else:
            columns = [
                desc.name if hasattr(desc, "name") else desc[0]
                for desc in (self.cursor.description or [])
            ]
            data = dict(zip(columns, row))
            values = list(row)

        return HybridRow(data, values)

    def fetchone(self):
        return self._wrap_row(self.cursor.fetchone())

    def fetchall(self):
        return [self._wrap_row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

class PGConnection:
    def __init__(self, conn):
        self.conn = conn
    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        return PGCursor(cur).execute(sql, params)
    def executescript(self, sql):
        cur = self.conn.cursor()
        for statement in [x.strip() for x in sql.split(";") if x.strip()]:
            cur.execute(statement)
        return PGCursor(cur)
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()

def get_db():
    if USE_POSTGRES:
        import psycopg
        raw = psycopg.connect(DATABASE_URL, sslmode="require")
        return PGConnection(raw)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def db_is_postgres():
    return USE_POSTGRES

@app.context_processor
def inject_business_name():
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key='business_name'").fetchone()
        conn.close()
        return {"business_name_global": row["value"] if row else "My Shop"}
    except Exception:
        return {"business_name_global": "My Shop"}

def get_setting(key, default=""):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = get_db()
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()

def generate_proprietor_password(length=12):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789@#$%"
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def send_weekly_report():
    email_to = get_setting('proprietor_email').strip()
    if not email_to: return False, "Proprietor email is not configured."
    end = datetime.now(); start = end - timedelta(days=7)
    conn = get_db()
    rows = conn.execute("SELECT COUNT(*) orders, COALESCE(SUM(total),0) revenue, COALESCE(SUM(discount),0) discounts FROM sales WHERE sale_date >= ? AND sale_date < ?", (start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S'))).fetchone()
    top = conn.execute("SELECT p.name, SUM(si.quantity) qty, SUM(si.subtotal) revenue FROM sale_items si JOIN products p ON p.id=si.product_id JOIN sales s ON s.id=si.sale_id WHERE s.sale_date >= ? AND s.sale_date < ? GROUP BY p.id ORDER BY qty DESC LIMIT 5", (start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S'))).fetchall(); conn.close()
    business=get_setting('business_name','My Shop'); proprietor=get_setting('proprietor_name','Proprietor') or 'Proprietor'
    lines=[f"{business} — Weekly Sales Report",f"Proprietor: {proprietor}",f"Period: {start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}","",f"Orders: {rows['orders']}",f"Revenue: ₹{float(rows['revenue']):,.2f}",f"Discounts: ₹{float(rows['discounts']):,.2f}","","Top products:"]
    lines += [f"- {i['name']}: {i['qty']} sold, ₹{float(i['revenue']):,.2f}" for i in top] or ["- No sales in this period."]
    subject=f"Weekly Sales Report — {business}"; body='\n'.join(lines)
    resend_key=os.environ.get('RESEND_API_KEY','').strip(); resend_from=os.environ.get('RESEND_FROM','').strip() or get_setting('resend_from').strip()
    if resend_key and resend_from:
        try:
            r=requests.post('https://api.resend.com/emails',headers={'Authorization':f'Bearer {resend_key}','Content-Type':'application/json'},json={'from':resend_from,'to':[email_to],'subject':subject,'text':body},timeout=20)
            r.raise_for_status(); return True,'Weekly report sent via Resend.'
        except Exception as exc:
            app.logger.exception('Resend email failed'); return False, f'Resend failed: {exc}'
    host=get_setting('smtp_host').strip(); username=get_setting('smtp_username').strip(); password=get_setting('smtp_password'); sender=get_setting('smtp_from').strip() or username
    if not host: return False,'Configure Resend environment variables or local SMTP settings.'
    try: port=int(get_setting('smtp_port','587') or 587)
    except ValueError: port=587
    msg=EmailMessage(); msg['Subject']=subject; msg['From']=sender; msg['To']=email_to; msg.set_content(body)
    try:
        if port==465:
            with smtplib.SMTP_SSL(host,port,timeout=20) as server:
                if username: server.login(username,password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host,port,timeout=20) as server:
                server.ehlo(); server.starttls(); server.ehlo()
                if username: server.login(username,password)
                server.send_message(msg)
        return True,'Weekly report sent via SMTP.'
    except Exception as exc: return False,str(exc)

def start_weekly_scheduler():
    if BackgroundScheduler is None:
        app.logger.warning("APScheduler is not installed; weekly reports are disabled.")
        return None
    scheduler = BackgroundScheduler(timezone='Asia/Kolkata')
    day = get_setting('weekly_report_day', 'mon') or 'mon'
    try:
        hour = max(0, min(23, int(get_setting('weekly_report_hour', '9') or 9)))
    except ValueError:
        hour = 9
    scheduler.add_job(send_weekly_report, 'cron', day_of_week=day, hour=hour, minute=0, id='weekly_sales_report', replace_existing=True)
    scheduler.start()
    app.logger.info("Weekly sales report scheduler started: %s at %02d:00 IST", day, hour)
    return scheduler

def init_db():
    if USE_POSTGRES:
        conn = get_db()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'staff');
        CREATE TABLE IF NOT EXISTS products (id SERIAL PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, purchase_price DOUBLE PRECISION NOT NULL DEFAULT 0, selling_price DOUBLE PRECISION NOT NULL DEFAULT 0, quantity DOUBLE PRECISION NOT NULL DEFAULT 0, min_stock DOUBLE PRECISION NOT NULL DEFAULT 5, unit TEXT NOT NULL DEFAULT 'pcs', supplier TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS customers (id SERIAL PRIMARY KEY, name TEXT NOT NULL, phone TEXT DEFAULT '', email TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sales (id SERIAL PRIMARY KEY, customer_id INTEGER, user_id INTEGER, total DOUBLE PRECISION NOT NULL, discount DOUBLE PRECISION NOT NULL DEFAULT 0, customer_name TEXT DEFAULT '', customer_phone TEXT DEFAULT '', customer_email TEXT DEFAULT '', sale_date TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sale_items (id SERIAL PRIMARY KEY, sale_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity DOUBLE PRECISION NOT NULL, unit TEXT NOT NULL DEFAULT 'pcs', price DOUBLE PRECISION NOT NULL, subtotal DOUBLE PRECISION NOT NULL, actual_price DOUBLE PRECISION NOT NULL DEFAULT 0);
        """)
    else:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = get_db()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'staff');
        CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, category TEXT NOT NULL, purchase_price REAL NOT NULL DEFAULT 0, selling_price REAL NOT NULL DEFAULT 0, quantity REAL NOT NULL DEFAULT 0, min_stock REAL NOT NULL DEFAULT 5, unit TEXT NOT NULL DEFAULT 'pcs', supplier TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT DEFAULT '', email TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, user_id INTEGER, total REAL NOT NULL, discount REAL NOT NULL DEFAULT 0, customer_name TEXT DEFAULT '', customer_phone TEXT DEFAULT '', customer_email TEXT DEFAULT '', sale_date TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sale_items (id INTEGER PRIMARY KEY AUTOINCREMENT, sale_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity REAL NOT NULL, unit TEXT NOT NULL DEFAULT 'pcs', price REAL NOT NULL, subtotal REAL NOT NULL, actual_price REAL NOT NULL DEFAULT 0);
        """)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sales)").fetchall()}
        for col, definition in [("discount", "REAL NOT NULL DEFAULT 0"), ("customer_name", "TEXT DEFAULT ''"), ("customer_phone", "TEXT DEFAULT ''"), ("customer_email", "TEXT DEFAULT ''")]:
            if col not in cols: conn.execute(f"ALTER TABLE sales ADD COLUMN {col} {definition}")
        product_cols = {r[1] for r in conn.execute("PRAGMA table_info(products)").fetchall()}
        if "unit" not in product_cols: conn.execute("ALTER TABLE products ADD COLUMN unit TEXT NOT NULL DEFAULT 'pcs'")
        item_cols = {r[1] for r in conn.execute("PRAGMA table_info(sale_items)").fetchall()}
        if "unit" not in item_cols: conn.execute("ALTER TABLE sale_items ADD COLUMN unit TEXT NOT NULL DEFAULT 'pcs'")
        if "actual_price" not in item_cols:
            conn.execute("ALTER TABLE sale_items ADD COLUMN actual_price REAL NOT NULL DEFAULT 0")
            conn.execute("UPDATE sale_items SET actual_price=price WHERE actual_price=0")
    default_settings = {
        'business_name':'My Shop','business_location':'','gst_number':'','proprietor_name':'','proprietor_phone':'','proprietor_email':'',
        'smtp_host':'','smtp_port':'587','smtp_username':'','smtp_password':'','smtp_from':'','weekly_report_day':'mon','weekly_report_hour':'9',
        'resend_from':'','cron_secret':''
    }
    for k,v in default_settings.items():
        if USE_POSTGRES:
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING", (k,v))
        else:
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k,v))
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        conn.execute("INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)", ("Administrator","admin@inventory.com",generate_password_hash("admin123"),"admin"))
    conn.commit(); conn.close()

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip()
        password = request.form["password"]
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["role"] = user["role"]
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    stats = {
        "products": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        "low_stock": conn.execute("SELECT COUNT(*) FROM products WHERE quantity <= min_stock").fetchone()[0],
        "today_sales": conn.execute(
            ("SELECT COALESCE(SUM(total),0) FROM sales WHERE sale_date::date=CURRENT_DATE" if USE_POSTGRES else "SELECT COALESCE(SUM(total),0) FROM sales WHERE date(sale_date)=date('now','localtime')")
        ).fetchone()[0],
        "orders": conn.execute(
            ("SELECT COUNT(*) FROM sales WHERE sale_date::date=CURRENT_DATE" if USE_POSTGRES else "SELECT COUNT(*) FROM sales WHERE date(sale_date)=date('now','localtime')")
        ).fetchone()[0],
    }
    low_stock = conn.execute(
        "SELECT * FROM products WHERE quantity <= min_stock ORDER BY quantity ASC LIMIT 8"
    ).fetchall()
    recent_sales = conn.execute("""
        SELECT s.id, s.total, s.sale_date, COALESCE(NULLIF(s.customer_name,''), c.name,'Walk-in Customer') customer
        FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
        ORDER BY s.id DESC LIMIT 8
    """).fetchall()
    conn.close()
    return render_template("dashboard.html", stats=stats, low_stock=low_stock, recent_sales=recent_sales)

@app.route("/products")
@login_required
def products():
    q = request.args.get("q", "").strip()
    conn = get_db()
    if q:
        rows = conn.execute(
            "SELECT * FROM products WHERE name LIKE ? OR category LIKE ? ORDER BY id DESC",
            (f"%{q}%", f"%{q}%")
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("products.html", products=rows, q=q)

@app.route("/products/add", methods=["POST"])
@login_required
def add_product():
    data = request.form
    conn = get_db()
    conn.execute("""
        INSERT INTO products(name,category,purchase_price,selling_price,quantity,min_stock,unit,supplier,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, (
        data["name"], data["category"], float(data["purchase_price"]),
        float(data["selling_price"]), float(data["quantity"]),
        float(data["min_stock"]), data.get("unit","pcs"), data.get("supplier",""),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))
    conn.commit()
    conn.close()
    flash("Product added successfully.", "success")
    return redirect(url_for("products"))

@app.route("/products/edit/<int:product_id>", methods=["POST"])
@login_required
def edit_product(product_id):
    d = request.form
    conn = get_db()
    conn.execute("UPDATE products SET name=?,category=?,purchase_price=?,selling_price=?,quantity=?,min_stock=?,unit=?,supplier=? WHERE id=?", (d["name"], d["category"], float(d["purchase_price"]), float(d["selling_price"]), float(d["quantity"]), float(d["min_stock"]), d.get("unit","pcs"), d.get("supplier",""), product_id))
    conn.commit(); conn.close()
    flash("Product updated successfully.", "success")
    return redirect(url_for("products"))

@app.route("/products/delete/<int:product_id>", methods=["POST"])
@login_required
def delete_product(product_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM products WHERE id=?", (product_id,))
        conn.commit()
        flash("Product deleted.", "success")
    except sqlite3.IntegrityError:
        flash("This product has sales history and cannot be deleted.", "danger")
    conn.close()
    return redirect(url_for("products"))

@app.route("/customers")
@login_required
def customers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM customers ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("customers.html", customers=rows)

@app.route("/customers/add", methods=["POST"])
@login_required
def add_customer():
    d = request.form
    conn = get_db()
    conn.execute(
        "INSERT INTO customers(name,phone,email,created_at) VALUES(?,?,?,?)",
        (d["name"], d.get("phone",""), d.get("email",""), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()
    flash("Customer added.", "success")
    return redirect(url_for("customers"))

@app.route("/customers/edit/<int:customer_id>", methods=["POST"])
@login_required
def edit_customer(customer_id):
    d = request.form
    conn = get_db()
    conn.execute("UPDATE customers SET name=?, phone=?, email=? WHERE id=?", (d["name"].strip(), d.get("phone", "").strip(), d.get("email", "").strip(), customer_id))
    conn.commit(); conn.close()
    flash("Customer details updated.", "success")
    return redirect(url_for("customers"))

@app.route("/customers/delete/<int:customer_id>", methods=["POST"])
@login_required
def delete_customer(customer_id):
    conn = get_db()
    # Keep historical invoice data intact; detach the customer record from old sales.
    conn.execute("UPDATE sales SET customer_id=NULL WHERE customer_id=?", (customer_id,))
    conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
    conn.commit(); conn.close()
    flash("Customer deleted. Previous invoices are preserved.", "success")
    return redirect(url_for("customers"))

@app.route("/database/backup")
@login_required
def database_backup():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Local SQLite version
    if not USE_POSTGRES:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

        if not os.path.exists(DB_PATH):
            init_db()

        return send_file(
            DB_PATH,
            as_attachment=True,
            download_name=f"inventory_backup_{stamp}.db",
            mimetype="application/octet-stream"
        )

    # Online Render + Neon PostgreSQL version
    conn = get_db()

    backup = {}

    for table in [
        "settings",
        "users",
        "products",
        "customers",
        "sales",
        "sale_items"
    ]:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        backup[table] = [dict(row) for row in rows]

    conn.close()

    payload = json.dumps(
        {
            "backup_type": "Digital Inventory Sales System PostgreSQL backup",
            "created_at": datetime.now().isoformat(),
            "tables": backup
        },
        indent=2,
        default=str
    ).encode("utf-8")

    return send_file(
        BytesIO(payload),
        as_attachment=True,
        download_name=f"inventory_backup_{stamp}.json",
        mimetype="application/json"
    )

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    conn = get_db()
    generated_password = None
    if request.method == "POST":
        section = request.form.get('section', 'business')
        if section == 'business':
            name = request.form.get("business_name", "").strip() or "My Shop"
            location = request.form.get("business_location", "").strip()
            gst = request.form.get("gst_number", "").strip()
            for k,v in [('business_name',name),('business_location',location),('gst_number',gst)]:
                conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k,v))
            conn.commit()
            flash("Business details saved.", "success")
        elif section == 'proprietor':
            pname = request.form.get('proprietor_name', '').strip()
            pphone = request.form.get('proprietor_phone', '').strip()
            pemail = request.form.get('proprietor_email', '').strip().lower()
            if not pname or not pemail:
                flash("Proprietor name and email are required.", "danger")
            else:
                existing = conn.execute("SELECT id FROM users WHERE email=?", (pemail,)).fetchone()
                proprietor_id = conn.execute("SELECT id FROM users WHERE role='proprietor' ORDER BY id LIMIT 1").fetchone()
                if existing and (not proprietor_id or existing['id'] != proprietor_id['id']):
                    flash("That email is already used by another account.", "danger")
                else:
                    if proprietor_id:
                        conn.execute("UPDATE users SET name=?, email=? WHERE id=?", (pname, pemail, proprietor_id['id']))
                    else:
                        generated_password = generate_proprietor_password()
                        cur = conn.execute("INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)", (pname, pemail, generate_password_hash(generated_password), 'proprietor'))
                        proprietor_id = {'id': cur.lastrowid}
                    for k,v in [('proprietor_name',pname),('proprietor_phone',pphone),('proprietor_email',pemail)]:
                        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k,v))
                    conn.commit()
                    if generated_password:
                        flash("Proprietor account created. Save the generated password shown below.", "success")
                    else:
                        flash("Proprietor details updated.", "success")
        elif section == 'smtp':
            vals = {
                'smtp_host': request.form.get('smtp_host','').strip(),
                'smtp_port': request.form.get('smtp_port','587').strip() or '587',
                'smtp_username': request.form.get('smtp_username','').strip(),
                'smtp_password': request.form.get('smtp_password',''),
                'smtp_from': request.form.get('smtp_from','').strip(),
                'weekly_report_day': request.form.get('weekly_report_day','mon'),
                'weekly_report_hour': request.form.get('weekly_report_hour','9'),
            }
            for k,v in vals.items():
                conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k,v))
            conn.commit()
            flash("Weekly email settings saved. The report runs automatically when the app is running.", "success")
        elif section == 'test_email':
            conn.close()
            ok, msg = send_weekly_report()
            flash(msg, 'success' if ok else 'danger')
            conn = get_db()

    rows = conn.execute("SELECT key,value FROM settings").fetchall()
    settings_map = {r['key']: r['value'] for r in rows}
    proprietor = conn.execute("SELECT id,name,email FROM users WHERE role='proprietor' ORDER BY id LIMIT 1").fetchone()
    conn.close()
    return render_template("settings.html", business_name=settings_map.get('business_name','My Shop'), settings=settings_map, proprietor=proprietor, generated_password=generated_password)

@app.route("/change-password", methods=["POST"])
@login_required
def change_password():
    current = request.form.get('current_password','')
    new = request.form.get('new_password','')
    confirm = request.form.get('confirm_password','')
    if len(new) < 8:
        flash("New password must be at least 8 characters.", "danger")
        return redirect(url_for('settings'))
    if new != confirm:
        flash("New passwords do not match.", "danger")
        return redirect(url_for('settings'))
    conn = get_db()
    user = conn.execute("SELECT password FROM users WHERE id=?", (session['user_id'],)).fetchone()
    if not user or not check_password_hash(user['password'], current):
        conn.close()
        flash("Current password is incorrect.", "danger")
        return redirect(url_for('settings'))
    conn.execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(new), session['user_id']))
    conn.commit(); conn.close()
    flash("Password changed successfully.", "success")
    return redirect(url_for('settings'))

@app.route("/settings/test-weekly-email", methods=["POST"])
@login_required
def test_weekly_email():
    ok, msg = send_weekly_report()
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('settings'))

@app.route("/tasks/weekly-report", methods=["GET","POST"])
def scheduled_weekly_report():
    expected=os.environ.get('CRON_SECRET','').strip() or get_setting('cron_secret').strip()
    supplied=request.headers.get('X-Cron-Secret','') or request.args.get('token','')
    if not expected or not secrets.compare_digest(supplied, expected):
        return jsonify({"ok":False,"error":"Unauthorized"}),401
    ok,msg=send_weekly_report()
    return jsonify({"ok":ok,"message":msg}), (200 if ok else 500)

@app.route("/billing")
@login_required
def billing():
    conn = get_db()
    products = conn.execute("SELECT * FROM products WHERE quantity > 0 ORDER BY name").fetchall()
    customers = conn.execute("SELECT * FROM customers ORDER BY name").fetchall()
    conn.close()
    return render_template("billing.html", products=products, customers=customers)

@app.route("/api/product/<int:product_id>")
@login_required
def product_api(product_id):
    conn = get_db()
    p = conn.execute("SELECT id,name,selling_price,quantity,unit FROM products WHERE id=?", (product_id,)).fetchone()
    conn.close()
    if not p:
        return jsonify({"error": "Product not found"}), 404
    return jsonify(dict(p))

@app.route("/billing/create", methods=["POST"])
@login_required
def create_sale():
    customer_id = request.form.get("customer_id") or None
    new_customer_name = request.form.get("new_customer_name", "").strip()
    new_customer_phone = request.form.get("new_customer_phone", "").strip()
    new_customer_email = request.form.get("new_customer_email", "").strip()
    try:
        discount = max(0, float(request.form.get("discount", 0) or 0))
    except ValueError:
        discount = 0
    product_ids = request.form.getlist("product_id[]")
    quantities = request.form.getlist("quantity[]")
    actual_prices = request.form.getlist("actual_price[]")
    applied_prices = request.form.getlist("applied_price[]")

    if not product_ids:
        flash("Add at least one product.", "danger")
        return redirect(url_for("billing"))

    conn = get_db()
    try:
        items = []
        total = 0
        for index, (pid, qty_text) in enumerate(zip(product_ids, quantities)):
            qty = float(qty_text)
            if qty <= 0:
                raise ValueError("Quantity must be positive.")
            p = conn.execute("SELECT * FROM products WHERE id=?", (int(pid),)).fetchone()
            if not p:
                raise ValueError("Product not found.")
            if p["quantity"] < qty:
                raise ValueError(f"Insufficient stock for {p['name']}. Available: {p['quantity']}.")
            actual_price = float(actual_prices[index]) if index < len(actual_prices) and actual_prices[index] else float(p["selling_price"])
            applied_price = float(applied_prices[index]) if index < len(applied_prices) and applied_prices[index] else float(p["selling_price"])
            if actual_price < 0 or applied_price < 0 or applied_price > actual_price:
                raise ValueError("Applied price must be between ₹0 and the actual price.")
            subtotal = applied_price * qty
            total += subtotal
            items.append((p, qty, subtotal, actual_price, applied_price))

        price_discount = sum((actual - applied) * qty for _, qty, _, actual, applied in items)
        actual_total = sum(actual * qty for _, qty, _, actual, _ in items)
        discount = min(discount, total)
        final_total = total - discount
        saved_customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone() if customer_id else None
        customer_name = saved_customer["name"] if saved_customer else new_customer_name
        customer_phone = saved_customer["phone"] if saved_customer else new_customer_phone
        customer_email = saved_customer["email"] if saved_customer else new_customer_email
        if not saved_customer and customer_name:
            existing = conn.execute("SELECT * FROM customers WHERE lower(name)=lower(?) AND phone=?", (customer_name, customer_phone)).fetchone()
            if existing:
                customer_id = existing["id"]
                if customer_email and not existing["email"]:
                    conn.execute("UPDATE customers SET email=? WHERE id=?", (customer_email, customer_id))
            else:
                ccur = conn.execute("INSERT INTO customers(name,phone,email,created_at) VALUES(?,?,?,?)", (customer_name, customer_phone, customer_email, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                customer_id = ccur.lastrowid
        total_discount = price_discount + discount
        cur = conn.execute(
            "INSERT INTO sales(customer_id,user_id,total,discount,customer_name,customer_phone,customer_email,sale_date) VALUES(?,?,?,?,?,?,?,?)",
            (customer_id, session["user_id"], final_total, total_discount, customer_name, customer_phone, customer_email, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        sale_id = cur.lastrowid

        for p, qty, subtotal, actual_price, applied_price in items:
            conn.execute(
                "INSERT INTO sale_items(sale_id,product_id,quantity,unit,price,subtotal,actual_price) VALUES(?,?,?,?,?,?,?)",
                (sale_id, p["id"], qty, p["unit"], applied_price, subtotal, actual_price)
            )
            conn.execute("UPDATE products SET quantity=quantity-? WHERE id=?", (qty, p["id"]))

        conn.commit()
        return redirect(url_for("invoice", sale_id=sale_id))
    except (ValueError, TypeError) as e:
        conn.rollback()
        flash(str(e), "danger")
        return redirect(url_for("billing"))
    finally:
        conn.close()

@app.route("/invoice/<int:sale_id>")
@login_required
def invoice(sale_id):
    conn = get_db()
    sale = conn.execute("""
        SELECT s.*, COALESCE(NULLIF(s.customer_name,''), c.name, 'Walk-in Customer') customer_name,
               COALESCE(NULLIF(s.customer_phone,''), c.phone, '') phone, u.name staff
        FROM sales s
        LEFT JOIN customers c ON c.id=s.customer_id
        LEFT JOIN users u ON u.id=s.user_id
        WHERE s.id=?
    """, (sale_id,)).fetchone()
    items = conn.execute("""
        SELECT si.*, p.name FROM sale_items si
        JOIN products p ON p.id=si.product_id
        WHERE si.sale_id=?
    """, (sale_id,)).fetchall()
    conn.close()
    if not sale:
        return "Invoice not found", 404
    business_name = get_setting('business_name', 'My Shop')
    business_location = get_setting('business_location', '')
    gst_number = get_setting('gst_number', '')
    proprietor_name = get_setting('proprietor_name', '')
    proprietor_phone = get_setting('proprietor_phone', '')
    actual_subtotal = sum(float(i["actual_price"]) * int(i["quantity"]) for i in items)
    return render_template("invoice.html", sale=sale, items=items, business_name=business_name, business_location=business_location, gst_number=gst_number, proprietor_name=proprietor_name, proprietor_phone=proprietor_phone, actual_subtotal=actual_subtotal)

@app.route("/sales")
@login_required
def sales():
    conn = get_db()
    rows = conn.execute("""
        SELECT s.id, s.total, s.sale_date, COALESCE(NULLIF(s.customer_name,''), c.name,'Walk-in Customer') customer
        FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
        ORDER BY s.id DESC
    """).fetchall()
    conn.close()
    return render_template("sales.html", sales=rows)

@app.route("/reports")
@login_required
def reports():
    conn = get_db()
    total_revenue = conn.execute("SELECT COALESCE(SUM(total),0) FROM sales").fetchone()[0]
    total_orders = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    top_products = conn.execute("""
        SELECT p.name, SUM(si.quantity) qty, SUM(si.subtotal) revenue
        FROM sale_items si JOIN products p ON p.id=si.product_id
        GROUP BY p.id ORDER BY qty DESC LIMIT 5
    """).fetchall()
    daily = conn.execute("""
    SELECT LEFT(sale_date, 10) AS day, SUM(total) AS revenue
    FROM sales
    GROUP BY LEFT(sale_date, 10)
    ORDER BY day DESC
    LIMIT 7
""").fetchall()
    conn.close()
    return render_template(
        "reports.html",
        total_revenue=total_revenue,
        total_orders=total_orders,
        top_products=top_products,
        daily=list(reversed(daily))
    )

init_db()

if __name__ == "__main__":
    if not USE_POSTGRES and (not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true'):
        start_weekly_scheduler()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
