import os
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect
from datetime import datetime
import pandas as pd
import psycopg2

app = Flask(__name__)

UPLOAD_FOLDER = "static/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# 🔥 DB ONLINE
def db():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))


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


# ❌ USUŃ PRODUKT + PACZKI
@app.route('/delete_product/<int:id>', methods=['POST'])
def delete_product(id):
    conn = db()
    cur = conn.cursor()

    cur.execute("DELETE FROM packages WHERE product_id=%s", (id,))
    cur.execute("DELETE FROM products WHERE id=%s", (id,))

    conn.commit()
    conn.close()

    return "OK"


# 📥 PRZYJĘCIE + PACZKI
@app.route('/receive_full', methods=['POST'])
def receive_full():
    conn = db()
    cur = conn.cursor()

    warehouse = request.form['warehouse']
    date = request.form['date'] or datetime.now().strftime("%Y-%m-%d")

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

        # 🔥 PACZKI
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


# 📤 WYDANIE + ZDJĘCIE
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
    package_ids = request.form.getlist('package_id')

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

            if package_ids and package_ids[i]:
                cur.execute(
                    "UPDATE packages SET qty = qty - %s WHERE id=%s",
                    (q, int(package_ids[i]))
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


# 🚀 START
if __name__ == '__main__':
    app.run()