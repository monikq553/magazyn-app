import os
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, session
from functools import wraps
from datetime import datetime
import pandas as pd
import psycopg2

app = Flask(__name__)
app.secret_key = "supersecretkey"  # 🔥 wymagane do logowania

UPLOAD_FOLDER = "static/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# 🔥 DB
def db():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))


# 🔥 INIT DB
def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS products(
    id SERIAL PRIMARY KEY,
    name TEXT,
    qty REAL,
    unit TEXT,
    warehouse TEXT,
    price_netto REAL,
    vat REAL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS packages(
    id SERIAL PRIMARY KEY,
    product_id INTEGER,
    package_number TEXT,
    qty REAL
    );
    """)

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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS issue_items(
    id SERIAL PRIMARY KEY,
    doc_id INTEGER,
    product_id INTEGER,
    qty REAL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE,
    password TEXT
    );
    """)

    # 🔥 pierwszy użytkownik
    cur.execute("""
    INSERT INTO users (username, password)
    VALUES ('admin','1234')
    ON CONFLICT (username) DO NOTHING
    """)

    conn.commit()
    conn.close()


# 🔥 INIT NA START (Render)
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
        username = request.form['username']
        password = request.form['password']

        conn = db()
        cur = conn.cursor()

        cur.execute(
            "SELECT * FROM users WHERE username=%s AND password=%s",
            (username, password)
        )
        user = cur.fetchone()
        conn.close()

        if user:
            session['user'] = username
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


# 🟢 PRZYJĘCIE
@app.route('/przyjecie')
@login_required
def przyjecie():
    return render_template("przyjecie.html")


# 🟢 WYDANIE
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


# ❌ USUŃ PRODUKT
@app.route('/delete_product/<int:id>', methods=['POST'])
@login_required
def delete_product(id):
    conn = db()
    cur = conn.cursor()

    cur.execute("DELETE FROM packages WHERE product_id=%s", (id,))
    cur.execute("DELETE FROM products WHERE id=%s", (id,))

    conn.commit()
    conn.close()

    return "OK"


# 📥 PRZYJĘCIE
@app.route('/receive_full', methods=['POST'])
@login_required
def receive_full():
    conn = db()
    cur = conn.cursor()

    warehouse = request.form['warehouse']

    names = request.form.getlist('name')
    qtys = request.form.getlist('qty')
    units = request.form.getlist('unit')

    for i in range(len(names)):
        name = names[i].strip()

        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0

        unit = units[i]

        if not name or qty <= 0:
            continue

        cur.execute(
            "SELECT id FROM products WHERE name=%s AND warehouse=%s",
            (name, warehouse)
        )
        product = cur.fetchone()

        if product:
            cur.execute(
                "UPDATE products SET qty = qty + %s WHERE id=%s",
                (qty, product[0])
            )
        else:
            cur.execute(
                "INSERT INTO products(name, qty, unit, warehouse, price_netto, vat) VALUES (%s,%s,%s,%s,0,0)",
                (name, qty, unit, warehouse)
            )

    conn.commit()
    conn.close()

    return redirect('/magazyn/' + warehouse)


# 📤 WYDANIE
@app.route('/issue_doc', methods=['POST'])
@login_required
def issue_doc():
    conn = db()
    cur = conn.cursor()

    doc_number = request.form['doc_number']
    kontrahent = request.form['kontrahent']
    warehouse = request.form['warehouse']

    cur.execute(
        "INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number) VALUES (%s,%s,%s,%s,%s)",
        (datetime.now().strftime("%Y-%m-%d"), kontrahent, warehouse, "", doc_number)
    )

    conn.commit()
    conn.close()

    return redirect('/magazyn/' + warehouse)


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


# 🔥 EXCEL PODGLĄD
@app.route('/preview_excel', methods=['POST'])
@login_required
def preview_excel():
    file = request.files.get('file')
    warehouse = request.form.get('warehouse')

    df = pd.read_excel(file)
    df.columns = [str(c).lower() for c in df.columns]

    data = []

    for _, row in df.iterrows():
        name = str(row.get('nazwa') or "").strip()
        qty = str(row.get('ilosc') or "0")

        if name:
            data.append({"name": name, "qty": qty, "unit": ""})

    return render_template("preview_import.html", data=data, warehouse=warehouse)


# 💾 IMPORT
@app.route('/import_excel', methods=['POST'])
@login_required
def import_excel():
    conn = db()
    cur = conn.cursor()

    warehouse = request.form.get('warehouse')
    names = request.form.getlist('name')
    qtys = request.form.getlist('qty')

    for i in range(len(names)):
        try:
            qty = float(qtys[i])
        except:
            qty = 0

        if qty <= 0:
            continue

        cur.execute(
            "INSERT INTO products(name, qty, unit, warehouse, price_netto, vat) VALUES (%s,%s,%s,%s,0,0)",
            (names[i], qty, "", warehouse)
        )

    conn.commit()
    conn.close()

    return redirect('/magazyn/' + warehouse)


# 🚀 LOCAL
if __name__ == '__main__':
    app.run()