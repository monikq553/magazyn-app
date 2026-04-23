import os
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect
from datetime import datetime
import pandas as pd
import psycopg2

app = Flask(__name__)

UPLOAD_FOLDER = "static/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# 🔥 DB
def db():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))


# 🔥 INIT DB (WAŻNE – uruchamia się zawsze)
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

    conn.commit()
    conn.close()


# 🔥 WAŻNE – wykona się na Renderze
init_db()


# 🟢 HOME
@app.route('/')
def home():
    return render_template("home.html")


# 🟢 MAGAZYNY
@app.route('/magazyny')
def magazyny():
    return render_template("magazyny.html")


# 🟢 MAGAZYN
@app.route('/magazyn/<name>')
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
def przyjecie():
    return render_template("przyjecie.html")


# 🟢 WYDANIE
@app.route('/wydanie')
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
def receive_full():
    conn = db()
    cur = conn.cursor()

    warehouse = request.form['warehouse']

    names = request.form.getlist('name')
    qtys = request.form.getlist('qty')
    units = request.form.getlist('unit')

    package_numbers = request.form.getlist('package_number')
    package_qtys = request.form.getlist('package_qty')

    package_index = 0

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
            pid = product[0]
            cur.execute(
                "UPDATE products SET qty = qty + %s WHERE id=%s",
                (qty, pid)
            )
        else:
            cur.execute(
                "INSERT INTO products(name, qty, unit, warehouse, price_netto, vat) VALUES (%s,%s,%s,%s,0,0) RETURNING id",
                (name, qty, unit, warehouse)
            )
            pid = cur.fetchone()[0]

        # PACZKI
        if warehouse == "Drewno":
            while package_index < len(package_numbers):
                p_num = package_numbers[package_index]
                p_qty = package_qtys[package_index]
                package_index += 1

                if not p_num:
                    break

                try:
                    p_qty = float((p_qty or "0").replace(",", "."))
                except:
                    p_qty = 0

                cur.execute(
                    "INSERT INTO packages(product_id, package_number, qty) VALUES (%s,%s,%s)",
                    (pid, p_num, p_qty)
                )

    conn.commit()
    conn.close()

    return redirect('/magazyn/' + warehouse)


# 📤 WYDANIE
@app.route('/issue_doc', methods=['POST'])
def issue_doc():
    conn = db()
    cur = conn.cursor()

    doc_number = request.form['doc_number']
    kontrahent = request.form['kontrahent']
    warehouse = request.form['warehouse']
    date = request.form['date'] or datetime.now().strftime("%Y-%m-%d")

    image = request.files.get('image')
    filename = ""

    if image and image.filename:
        filename = secure_filename(image.filename)
        image.save(os.path.join(UPLOAD_FOLDER, filename))

    cur.execute(
        "INSERT INTO issue_docs(date, kontrahent, warehouse, image, doc_number) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (date, kontrahent, warehouse, filename, doc_number)
    )
    doc_id = cur.fetchone()[0]

    product_ids = request.form.getlist('product_id')
    qtys = request.form.getlist('qty')

    for i in range(len(product_ids)):
        if product_ids[i] and qtys[i]:
            pid = int(product_ids[i])
            q = float(qtys[i].replace(",", "."))

            cur.execute(
                "INSERT INTO issue_items(doc_id, product_id, qty) VALUES (%s,%s,%s)",
                (doc_id, pid, q)
            )

            cur.execute(
                "UPDATE products SET qty = qty - %s WHERE id=%s",
                (q, pid)
            )

    conn.commit()
    conn.close()

    return redirect('/magazyn/' + warehouse)


# 🔥 EXCEL PODGLĄD
@app.route('/preview_excel', methods=['POST'])
def preview_excel():
    file = request.files.get('file')
    warehouse = request.form.get('warehouse')

    df = pd.read_excel(file)

    data = []
    for _, row in df.iterrows():
        data.append({
            "name": str(row[0]),
            "qty": str(row[1]),
            "unit": str(row[2]) if len(row) > 2 else ""
        })

    return render_template("preview_import.html", data=data, warehouse=warehouse)


# 💾 IMPORT
@app.route('/import_excel', methods=['POST'])
def import_excel():
    conn = db()
    cur = conn.cursor()

    warehouse = request.form.get('warehouse')

    names = request.form.getlist('name')
    qtys = request.form.getlist('qty')
    units = request.form.getlist('unit')

    for i in range(len(names)):
        name = names[i]

        try:
            qty = float(qtys[i].replace(",", "."))
        except:
            qty = 0

        unit = units[i]

        if qty <= 0:
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


# 🚀 LOCAL ONLY
if __name__ == '__main__':
    app.run()