import os
from flask import Flask, render_template, request, redirect, session
from functools import wraps
import psycopg2
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

app = Flask(__name__)
app.secret_key = "supersecretkey"
INVESTMENT_WAREHOUSE = "Inwestycja Suwaj"


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
    cur.execute("""
    ALTER TABLE issue_items
    ADD COLUMN IF NOT EXISTS warehouse TEXT;
    """)
    cur.execute("""
    ALTER TABLE packages
    ADD COLUMN IF NOT EXISTS warehouse TEXT;
    """)
    cur.execute("""
    ALTER TABLE packages
    ADD COLUMN IF NOT EXISTS number TEXT;
    """)

    # migracja kompatybilności: stare bazy mogły mieć kolumnę package_number zamiast number
    cur.execute("""
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'packages' AND column_name = 'package_number'
    )
    """)
    has_package_number = cur.fetchone()[0]
    if has_package_number:
        cur.execute("""
        UPDATE packages
        SET number = COALESCE(number, package_number::TEXT)
        WHERE number IS NULL
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


@app.route('/dashboard', endpoint='dashboard_page_view')
@login_required
def dashboard_page():
    return render_template("home.html")


@app.route('/magazyny')
@login_required
def magazyny():
    return render_template("magazyny.html")


@app.route('/users')
@login_required
def users():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, role FROM users ORDER BY id")
    users_list = cur.fetchall()
    conn.close()
    return render_template("users.html", users=users_list)


@app.route('/add_user', methods=['POST'])
@login_required
def add_user():
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users(username, password, role) VALUES (%s,%s,%s) ON CONFLICT (username) DO NOTHING",
        (
            request.form.get('username'),
            generate_password_hash(request.form.get('password')),
            request.form.get('role', 'user')
        )
    )
    conn.commit()
    conn.close()
    return redirect('/users')


@app.route('/delete_user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if user_id == 1:
        return redirect('/users')

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
    conn.commit()
    conn.close()
    return redirect('/users')


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


@app.route('/inwestycja-suwaj', endpoint='inwestycja_suwaj_page_view')
@login_required
def inwestycja_suwaj():
    return render_template("inwestycja_suwaj.html", warehouse=INVESTMENT_WAREHOUSE)


@app.route('/inwestycja-suwaj/magazyn')
@login_required
def inwestycja_suwaj_magazyn():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE warehouse=%s", (INVESTMENT_WAREHOUSE,))
    products = cur.fetchall()
    conn.close()
    return render_template("index.html", products=products, warehouse=INVESTMENT_WAREHOUSE)


@app.route('/inwestycja-suwaj/przyjecie')
@login_required
def inwestycja_suwaj_przyjecie():
    conn = db()
    cur = conn.cursor()

    # pokazujemy wszystkie produkty, żeby można było dodać nowy asortyment do magazynu inwestycji
    cur.execute("SELECT * FROM products")
    products = cur.fetchall()

    cur.execute("SELECT * FROM packages WHERE warehouse=%s OR warehouse IS NULL", (INVESTMENT_WAREHOUSE,))
    packages = cur.fetchall()

    conn.close()

    return render_template(
        "przyjecie.html",
        products=products,
        packages=packages,
        forced_warehouse=INVESTMENT_WAREHOUSE,
        form_action="/inwestycja-suwaj/receive_doc",
        page_title="📥 Przyjęcie (PZ) – Inwestycja Suwaj"
    )


@app.route('/inwestycja-suwaj/receive_doc', methods=['POST'])
@login_required
def inwestycja_suwaj_receive_doc():
    conn = db()
    cur = conn.cursor()

    date = datetime.now().strftime("%Y-%m-%d")
    kontrahent = request.form.get('kontrahent')

    cur.execute("SELECT COUNT(*) FROM issue_docs WHERE warehouse=%s", (INVESTMENT_WAREHOUSE,))
    num = cur.fetchone()[0] + 1
    doc_number = f"PZ-IS/{num}/{datetime.now().year}"

    cur.execute("""
        INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (date, kontrahent, INVESTMENT_WAREHOUSE, "", doc_number))
    doc_id = cur.fetchone()[0]

    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')
    package_numbers = request.form.getlist('package_number')

    for i in range(len(product_ids)):
        if not product_ids[i]:
            continue
        pid = int(product_ids[i])
        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0
        if qty <= 0:
            continue

        cur.execute("""
            UPDATE products
            SET qty = qty + %s
            WHERE id=%s AND warehouse=%s
        """, (qty, pid, INVESTMENT_WAREHOUSE))
        updated = cur.rowcount

        # jeśli produktu nie ma jeszcze w magazynie inwestycji, sklonuj kartotekę i dodaj stan
        if updated == 0:
            cur.execute("""
                SELECT name, unit, price_netto, vat
                FROM products
                WHERE id=%s
            """, (pid,))
            source = cur.fetchone()

            if source:
                cur.execute("""
                    INSERT INTO products(name, qty, unit, warehouse, price_netto, vat)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (source[0], qty, source[1], INVESTMENT_WAREHOUSE, source[2], source[3]))
                pid = cur.fetchone()[0]
            else:
                continue

        cur.execute("""
            INSERT INTO issue_items(doc_id, product_id, qty, warehouse)
            VALUES (%s,%s,%s,%s)
        """, (doc_id, pid, qty, INVESTMENT_WAREHOUSE))

        if i < len(package_numbers) and package_numbers[i]:
            cur.execute("""
                INSERT INTO packages(product_id, number, qty, warehouse)
                VALUES (%s,%s,%s,%s)
            """, (pid, package_numbers[i], qty, INVESTMENT_WAREHOUSE))

    conn.commit()
    conn.close()

    return redirect('/inwestycja-suwaj/magazyn')


@app.route('/inwestycja-suwaj/wydanie')
@login_required
def inwestycja_suwaj_wydanie():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM products WHERE warehouse=%s", (INVESTMENT_WAREHOUSE,))
    products = cur.fetchall()

    cur.execute("SELECT * FROM packages WHERE warehouse=%s OR warehouse IS NULL", (INVESTMENT_WAREHOUSE,))
    packages = cur.fetchall()
    conn.close()

    return render_template(
        "wydanie.html",
        products=products,
        packages=packages,
        forced_warehouse=INVESTMENT_WAREHOUSE,
        form_action="/inwestycja-suwaj/issue_doc",
        page_title="📄 Wydanie (WZ) – Inwestycja Suwaj"
    )


@app.route('/inwestycja-suwaj/issue_doc', methods=['POST'])
@login_required
def inwestycja_suwaj_issue_doc():
    conn = db()
    cur = conn.cursor()

    date = datetime.now().strftime("%Y-%m-%d")
    kontrahent = request.form.get('kontrahent')

    cur.execute("SELECT COUNT(*) FROM issue_docs WHERE warehouse=%s", (INVESTMENT_WAREHOUSE,))
    num = cur.fetchone()[0] + 1
    doc_number = f"WZ-IS/{num}/{datetime.now().year}"

    cur.execute("""
        INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (date, kontrahent, INVESTMENT_WAREHOUSE, "", doc_number))
    doc_id = cur.fetchone()[0]

    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')
    package_ids = request.form.getlist('package_id')

    for i in range(len(product_ids)):
        if not product_ids[i]:
            continue

        pid = int(product_ids[i])
        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0
        if qty <= 0:
            continue

        pkg = package_ids[i] if package_ids[i] else None
        if pkg:
            pkg = int(pkg)
            cur.execute("""
                UPDATE packages
                SET qty = qty - %s
                WHERE id=%s AND (warehouse=%s OR warehouse IS NULL) AND qty >= %s
            """, (qty, pkg, INVESTMENT_WAREHOUSE, qty))
            if cur.rowcount == 0:
                conn.rollback()
                conn.close()
                return "Brak w paczce"

        cur.execute("""
            UPDATE products
            SET qty = qty - %s
            WHERE id=%s AND warehouse=%s AND qty >= %s
        """, (qty, pid, INVESTMENT_WAREHOUSE, qty))

        if cur.rowcount == 0:
            conn.rollback()
            conn.close()
            return f"Brak stanu w magazynie {INVESTMENT_WAREHOUSE}"

        cur.execute("""
            INSERT INTO issue_items(doc_id, product_id, qty, warehouse, package_id)
            VALUES (%s,%s,%s,%s,%s)
        """, (doc_id, pid, qty, INVESTMENT_WAREHOUSE, pkg))

    cur.execute("DELETE FROM packages WHERE qty <= 0")
    conn.commit()
    conn.close()

    return redirect(f"/doc/{doc_id}")


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

        pkg = package_ids[i] if package_ids[i] else None

        # pakiet
        if pkg:
            pkg = int(pkg)

            cur.execute("""
                UPDATE packages
                SET qty = qty - %s
                WHERE id=%s AND warehouse=%s AND qty >= %s
            """, (qty, pkg, wh, qty))
            if cur.rowcount == 0:
                conn.rollback()
                conn.close()
                return "Brak w paczce"

        # ✅ stan -
        cur.execute("""
            UPDATE products 
            SET qty = qty - %s 
            WHERE id=%s AND warehouse=%s AND qty >= %s
        """, (qty, pid, wh, qty))
        if cur.rowcount == 0:
            conn.rollback()
            conn.close()
            return f"Brak stanu w magazynie {wh}"

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
        SELECT p.name, i.qty, COALESCE(i.warehouse, p.warehouse), pk.number
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
@app.route('/fix_db')
def fix_db():
    conn = db()
    cur = conn.cursor()

    try:
        cur.execute("""
        ALTER TABLE issue_items
        ADD COLUMN package_id INTEGER;
        """)
    except Exception as e:
        pass

    conn.commit()
    conn.close()

    return "DB FIXED"
