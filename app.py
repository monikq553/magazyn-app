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

    # PACKAGES
    cur.execute("""
    CREATE TABLE IF NOT EXISTS packages(
        id SERIAL PRIMARY KEY,
        product_id INTEGER,
        number TEXT,
        qty REAL,
        warehouse TEXT
    );
    """)

    # DOCS
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

    # ITEMS (bez kombinowania w środku)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS issue_items(
        id SERIAL PRIMARY KEY,
        doc_id INTEGER,
        product_id INTEGER,
        qty REAL,
        warehouse TEXT,
        package_id INTEGER
    );
    """)

    # 🔥 KLUCZOWE — aktualizacja starej bazy
    cur.execute("""
    ALTER TABLE issue_items
    ADD COLUMN IF NOT EXISTS package_id INTEGER;
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


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.route('/')
@login_required
def home():
    return render_template("home.html")


@app.route('/magazyny')
@login_required
def magazyny():
    return render_template("magazyny.html")


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


# 📥 PRZYJĘCIE
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


# 📥 ZAPIS PRZYJĘCIA
@app.route('/receive_doc', methods=['POST'])
@login_required
def receive_doc():
    conn = db()
    cur = conn.cursor()

    date = datetime.now().strftime("%Y-%m-%d")

    cur.execute("""
        INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (date, request.form.get('kontrahent'), "PZ", "", "PZ"))

    doc_id = cur.fetchone()[0]

    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')
    warehouses = request.form.getlist('warehouse')
    package_numbers = request.form.getlist('package_number')

    for i in range(len(product_ids)):
        if not product_ids[i]:
            continue

        pid = int(product_ids[i])
        wh = warehouses[i]

        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0

        if qty <= 0:
            continue

        # ✅ stan +
        cur.execute("""
            UPDATE products 
            SET qty = qty + %s 
            WHERE id=%s AND warehouse=%s
        """, (qty, pid, wh))

        # zapis pozycji
        cur.execute("""
            INSERT INTO issue_items(doc_id, product_id, qty, warehouse)
            VALUES (%s,%s,%s,%s)
        """, (doc_id, pid, qty, wh))

        # pakiet
        if package_numbers[i]:
            cur.execute("""
                INSERT INTO packages(product_id, number, qty, warehouse)
                VALUES (%s,%s,%s,%s)
            """, (pid, package_numbers[i], qty, wh))

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

    cur.execute("SELECT * FROM packages")
    packages = cur.fetchall()

    conn.close()

    return render_template("wydanie.html", products=products, packages=packages)


# 📤 ZAPIS WYDANIA (PRO)
@app.route('/issue_doc', methods=['POST'])
@login_required
def issue_doc():
    conn = db()
    cur = conn.cursor()

    date = datetime.now().strftime("%Y-%m-%d")
    kontrahent = request.form.get('kontrahent')

    cur.execute("SELECT COUNT(*) FROM issue_docs")
    num = cur.fetchone()[0] + 1
    doc_number = f"WZ/{num}/{datetime.now().year}"

    cur.execute("""
        INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (date, kontrahent, "", "", doc_number))

    doc_id = cur.fetchone()[0]

    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')
    warehouses = request.form.getlist('warehouse')
    package_ids = request.form.getlist('package_id')

    for i in range(len(product_ids)):
        if not product_ids[i]:
            continue

        pid = int(product_ids[i])
        wh = warehouses[i]

        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0

        if qty <= 0:
            continue

        # 🔥 SPRAWDŹ STAN
        cur.execute("""
            SELECT qty FROM products WHERE id=%s AND warehouse=%s
        """, (pid, wh))
        current = cur.fetchone()

        if not current or current[0] < qty:
            conn.close()
            return f"Brak stanu w magazynie {wh}"

        pkg = package_ids[i] if package_ids[i] else None

        # pakiet
        if pkg:
            pkg = int(pkg)

            cur.execute("""
                SELECT qty FROM packages WHERE id=%s AND warehouse=%s
            """, (pkg, wh))
            p = cur.fetchone()

            if not p or p[0] < qty:
                conn.close()
                return "Brak w paczce"

            cur.execute("""
                UPDATE packages SET qty = qty - %s WHERE id=%s
            """, (qty, pkg))

        # ✅ stan -
        cur.execute("""
            UPDATE products 
            SET qty = qty - %s 
            WHERE id=%s AND warehouse=%s
        """, (qty, pid, wh))

        cur.execute("""
            INSERT INTO issue_items(doc_id, product_id, qty, warehouse, package_id)
            VALUES (%s,%s,%s,%s,%s)
        """, (doc_id, pid, qty, wh, pkg))

    # czyść puste paczki
    cur.execute("DELETE FROM packages WHERE qty <= 0")

    conn.commit()
    conn.close()

    return redirect(f"/doc/{doc_id}")


# 📄 SZCZEGÓŁ
@app.route('/doc/<int:id>')
@login_required
def doc_detail(id):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM issue_docs WHERE id=%s", (id,))
    doc = cur.fetchone()

    cur.execute("""
        SELECT p.name, i.qty, i.warehouse, pk.number
        FROM issue_items i
        JOIN products p ON p.id = i.product_id
        LEFT JOIN packages pk ON pk.id = i.package_id
        WHERE i.doc_id=%s
    """, (id,))
    items = cur.fetchall()

    conn.close()

    return render_template("doc_detail.html", doc=doc, items=items)


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


if __name__ == '__main__':
    app.run()
