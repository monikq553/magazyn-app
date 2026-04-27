import os
from flask import Flask, render_template, request, redirect, session
from functools import wraps
import psycopg2
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

app = Flask(__name__)
app.secret_key = "supersecretkey"


# 🔥 DB
def db():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))


# 🔥 INIT DB
def init_db():
    conn = db()
    cur = conn.cursor()

    # USERS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT
    );
    """)

    # PRODUCTS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id SERIAL PRIMARY KEY,
        name TEXT,
        qty REAL,
        unit TEXT,
        warehouse TEXT,
        price_netto REAL DEFAULT 0,
        vat REAL DEFAULT 0
    );
    """)

    # 🔥 PACKAGES (NOWE)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS packages(
        id SERIAL PRIMARY KEY,
        product_id INTEGER,
        number TEXT,
        qty REAL
    );
    """)

    # ISSUE DOCS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS issue_docs(
        id SERIAL PRIMARY KEY,
        date TEXT,
        kontrahent TEXT,
        warehouse TEXT,
        image TEXT,
        doc_number TEXT
    );
    """)

    # ISSUE ITEMS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS issue_items(
        id SERIAL PRIMARY KEY,
        doc_id INTEGER,
        product_id INTEGER,
        qty REAL,
        warehouse TEXT
    );
    """)

    # ADMIN RESET
    cur.execute("DELETE FROM users WHERE username='admin'")
    cur.execute(
        "INSERT INTO users(username, password, role) VALUES (%s,%s,%s)",
        ("admin", generate_password_hash("1234"), "admin")
    )

    conn.commit()
    conn.close()


init_db()


# 🔒 LOGIN REQUIRED
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


# 🔐 LOGIN
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        conn = db()
        cur = conn.cursor()

        cur.execute("SELECT * FROM users WHERE username=%s", (request.form['username'],))
        user = cur.fetchone()
        conn.close()

        if user and check_password_hash(user[2], request.form['password']):
            session['user'] = user[1]
            session['role'] = user[3]
            return redirect('/')
        return "Błędne dane"

    return render_template("login.html")


# 🔓 LOGOUT
@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# 🟢 HOME
@app.route('/')
@login_required
def home():
    return render_template("home.html")


# 🟢 MAGAZYNY
@app.route('/magazyny')
@login_required
def magazyny():
    return render_template("magazyny.html")


# 🟢 MAGAZYN
@app.route('/magazyn/<name>')
@login_required
def magazyn(name):
    conn = db()
    cur = conn.cursor()

    if name == "Wszystko":
        cur.execute("SELECT * FROM products")
    else:
        cur.execute("SELECT * FROM products WHERE warehouse=%s", (name,))

    products = cur.fetchall()
    conn.close()

    return render_template("index.html", products=products, warehouse=name)


# 📥 PRZYJĘCIE (VIEW)
@app.route('/przyjecie')
@login_required
def przyjecie():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM products")
    products = cur.fetchall()

    cur.execute("SELECT * FROM packages")
    packages = cur.fetchall()

    conn.close()

    return render_template("przyjecie.html", products=products, packages=packages)


# 📥 PRZYJĘCIE (LOGIKA)
@app.route('/receive_doc', methods=['POST'])
@login_required
def receive_doc():
    conn = db()
    cur = conn.cursor()

    date = datetime.now().strftime("%Y-%m-%d")

    cur.execute(
        "INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (date, request.form.get('kontrahent'), "PZ", "", "PZ")
    )
    doc_id = cur.fetchone()[0]

    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')
    warehouses = request.form.getlist('warehouse')
    package_numbers = request.form.getlist('package_number')

    for i in range(len(product_ids)):
        if not product_ids[i]:
            continue

        pid = int(product_ids[i])

        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0

        warehouse = warehouses[i]
        package = package_numbers[i]

        if qty <= 0:
            continue

        # 🔥 update produktu
        cur.execute(
            "UPDATE products SET qty = qty + %s WHERE id=%s",
            (qty, pid)
        )

        # 🔥 zapis pozycji
        cur.execute(
            "INSERT INTO issue_items(doc_id, product_id, qty, warehouse) VALUES (%s,%s,%s,%s)",
            (doc_id, pid, qty, warehouse)
        )

        # 🔥 PACZKA
        if package:
            cur.execute(
                "INSERT INTO packages(product_id, number, qty) VALUES (%s,%s,%s)",
                (pid, package, qty)
            )

    conn.commit()
    conn.close()

    return redirect('/historia')


# 📤 WYDANIE
@app.route('/wydanie')
@login_required
def wydanie():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM products")
    products = cur.fetchall()

    conn.close()

    return render_template("wydanie.html", products=products)


# 📊 HISTORIA
@app.route('/historia')
@login_required
def historia():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM issue_docs ORDER BY id DESC")
    docs = cur.fetchall()
    conn.close()

    days = {}
    for d in docs:
        days.setdefault(d[1], []).append(d)

    return render_template("historia.html", days=days)


# 🚀 START
if __name__ == '__main__':
    app.run()
