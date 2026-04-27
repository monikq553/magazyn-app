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

    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT;")

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

    # ISSUE ITEMS (🔥 KLUCZOWE)
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


# 🔒 ADMIN REQUIRED
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')

        if session.get('role') != 'admin':
            return "Brak dostępu"

        return f(*args, **kwargs)
    return decorated


# 🔐 LOGIN
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = db()
        cur = conn.cursor()

        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        user = cur.fetchone()

        conn.close()

        if user and check_password_hash(user[2], password):
            session['user'] = username
            session['role'] = user[3]
            return redirect('/')
        else:
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


# 📊 DASHBOARD
@app.route('/dashboard')
@login_required
def dashboard():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM products")
    total_products = cur.fetchone()[0]

    cur.execute("SELECT SUM(qty) FROM products")
    total_qty = cur.fetchone()[0] or 0

    cur.execute("SELECT name, qty FROM products ORDER BY qty DESC LIMIT 5")
    top = cur.fetchall()

    conn.close()

    names = [t[0] for t in top]
    qtys = [float(t[1]) for t in top]

    return render_template(
        "dashboard.html",
        total_products=total_products,
        total_qty=total_qty,
        names=names,
        qtys=qtys
    )


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


# ❌ USUŃ PRODUKT
@app.route('/delete_product/<int:id>', methods=['POST'])
@login_required
def delete_product(id):
    conn = db()
    cur = conn.cursor()

    cur.execute("DELETE FROM products WHERE id=%s", (id,))

    conn.commit()
    conn.close()

    return redirect(request.referrer)

# 🟢 PRZYJĘCIE
@app.route('/przyjecie')
@login_required
def przyjecie():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM products")
    products = cur.fetchall()

    # 🔥 DODAJ TO
    cur.execute("SELECT * FROM packages")
    packages = cur.fetchall()

    conn.close()

    return render_template(
        "przyjecie.html",
        products=products,
        packages=packages
    )
# 🟢 WYDANIE
@app.route('/wydanie')
@login_required
def wydanie():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM products")
    products = cur.fetchall()

    conn.close()

    return render_template("wydanie.html", products=products)

# 📥 PRZYJĘCIE - zapis dokumentu
@app.route('/receive_doc', methods=['POST'])
@login_required
def receive_doc():
    conn = db()
    cur = conn.cursor()

    kontrahent = request.form.get('kontrahent')
    date = datetime.now().strftime("%Y-%m-%d")

    # zapis dokumentu (możesz zrobić osobną tabelę później)
    cur.execute(
        "INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (date, kontrahent, "PRZYJĘCIE", "", "PZ")
    )

    doc_id = cur.fetchone()[0]

    # 🔥 pobieranie list (wiele pozycji)
    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')
    warehouses = request.form.getlist('warehouse')

    for i in range(len(product_ids)):
        pid = product_ids[i]
        warehouse = warehouses[i]

        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0

        if not pid or qty <= 0:
            continue

        # 🔥 aktualizacja magazynu
        cur.execute(
            "SELECT id FROM products WHERE id=%s AND warehouse=%s",
            (pid, warehouse)
        )
        product = cur.fetchone()

        if product:
            cur.execute(
                "UPDATE products SET qty = qty + %s WHERE id=%s",
                (qty, pid)
            )

        # 🔥 zapis pozycji (historia)
        cur.execute(
            "INSERT INTO issue_items(doc_id, product_id, qty) VALUES (%s,%s,%s)",
            (doc_id, pid, qty)
        )

    conn.commit()
    conn.close()

    return redirect('/historia')

# 🔥 ISSUE DOC (MULTI MAGAZYN)
@app.route('/issue_doc', methods=['POST'])
@login_required
def issue_doc():
    conn = db()
    cur = conn.cursor()

    kontrahent = request.form.get('kontrahent')

    date = datetime.now().strftime("%Y-%m-%d")

    # 🔥 numer auto
    cur.execute("SELECT COUNT(*) FROM issue_docs")
    count = cur.fetchone()[0] + 1
    doc_number = f"WZ/{count}/{datetime.now().year}"

    cur.execute(
        "INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (date, kontrahent, "", "", doc_number)
    )
    doc_id = cur.fetchone()[0]

    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')
    warehouses = request.form.getlist('warehouse')

    for i in range(len(product_ids)):
        if not product_ids[i]:
            continue

        pid = int(product_ids[i])
        wh = warehouses[i]

        try:
            q = float(qtys[i].replace(",", "."))
        except:
            q = 0

        if q <= 0:
            continue

        # 🔥 stan
        cur.execute(
            "SELECT qty FROM products WHERE id=%s AND warehouse=%s",
            (pid, wh)
        )
        res = cur.fetchone()

        if not res or res[0] < q:
            conn.close()
            return f"Brak stanu w magazynie {wh}"

        # zapis pozycji
        cur.execute(
            "INSERT INTO issue_items(doc_id, product_id, qty, warehouse) VALUES (%s,%s,%s,%s)",
            (doc_id, pid, q, wh)
        )

        # odejmowanie
        cur.execute(
            "UPDATE products SET qty = qty - %s WHERE id=%s AND warehouse=%s",
            (q, pid, wh)
        )

    conn.commit()
    conn.close()

    return redirect(f'/doc/{doc_id}')


# 📄 SZCZEGÓŁ
@app.route('/doc/<int:id>')
@login_required
def doc_detail(id):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM issue_docs WHERE id=%s", (id,))
    doc = cur.fetchone()

    cur.execute("""
        SELECT p.name, i.qty, i.warehouse
        FROM issue_items i
        JOIN products p ON p.id = i.product_id
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

    cur.execute("SELECT * FROM issue_docs ORDER BY date DESC, id DESC")
    docs = cur.fetchall()

    conn.close()

    days = {}
    for d in docs:
        days.setdefault(d[1], []).append(d)

    return render_template("historia.html", days=days)


# 👥 USERS
@app.route('/users')
@admin_required
def users():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT id, username, role FROM users")
    users = cur.fetchall()

    conn.close()

    return render_template("users.html", users=users)


# ➕ ADD USER
@app.route('/add_user', methods=['POST'])
@admin_required
def add_user():
    conn = db()
    cur = conn.cursor()

    username = request.form['username']
    password = generate_password_hash(request.form['password'])
    role = request.form['role']

    cur.execute(
        "INSERT INTO users(username, password, role) VALUES (%s,%s,%s)",
        (username, password, role)
    )

    conn.commit()
    conn.close()

    return redirect('/users')


# 🗑 DELETE USER
@app.route('/delete_user/<int:id>', methods=['POST'])
@admin_required
def delete_user(id):
    conn = db()
    cur = conn.cursor()

    if id == 1:
        return "Nie można usunąć admina"

    cur.execute("SELECT username FROM users WHERE id=%s", (id,))
    user = cur.fetchone()

    if user and user[0] == session.get('user'):
        return "Nie możesz usunąć siebie"

    cur.execute("DELETE FROM users WHERE id=%s", (id,))

    conn.commit()
    conn.close()

    return redirect('/users')


# 🚀 START
if __name__ == '__main__':
    app.run()
