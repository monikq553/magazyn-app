# Uruchomienie i testowanie aplikacji lokalnie

## Zależności

Aplikacja używa bezpośrednio `psycopg2` i nie używa SQLAlchemy/Flask-SQLAlchemy. Dlatego poprawnym sterownikiem PostgreSQL w `requirements.txt` jest `psycopg2-binary`.

Wymagane grupy bibliotek są ujęte w `requirements.txt`:

- Flask/Werkzeug — aplikacja webowa.
- gunicorn — uruchomienie produkcyjne, w tym Render.
- python-dotenv — lokalne wczytywanie `.env`.
- psycopg2-binary — PostgreSQL.
- firebase-admin — Firebase Authentication i sesje.
- flask-limiter, flask-caching — limity i cache.
- pandas, openpyxl — import Excel.
- reportlab — generowanie PDF.
- cryptography — szyfrowanie backupów.
- DOCX w module sklepu jest obecnie generowany przez standardową bibliotekę Pythona (`zipfile` + XML OpenXML), więc nie wymaga dodatkowej paczki `python-docx`.

## Przygotowanie środowiska

```bash
pyenv install 3.13.13 # jeśli nie jest zainstalowany
pyenv local 3.13.13
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Jeżeli środowisko nie używa `pyenv`, użyj dowolnego dostępnego Pythona 3.13 zgodnego z Renderem:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Konfiguracja lokalna

```bash
cp .env.example .env
```

Uzupełnij co najmniej:

- `DATABASE_URL` — lokalna lub testowa baza PostgreSQL,
- `SECRET_KEY`,
- zmienne Firebase (`FIREBASE_*`, `FIREBASE_SERVICE_ACCOUNT_JSON`), jeżeli testowane jest logowanie Firebase,
- `ADMIN_EMAILS` dla pierwszego administratora,
- `BACKUP_ENCRYPTION_KEY`, jeśli testowane są backupy.

## Migracje i uruchomienie aplikacji

```bash
python migrate.py
python app.py
```

Alternatywnie tak jak na Renderze:

```bash
python migrate.py
gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 4 --timeout 60 --access-logfile - app:app
```

## Testy automatyczne

```bash
python -m unittest tests/test_app.py
```

## Scenariusze testów ręcznych po odblokowaniu instalacji zależności

Nie oznaczaj aplikacji jako w pełni przetestowanej, dopóki poniższe kroki nie przejdą w środowisku z zainstalowanymi zależnościami:

1. Logowanie i role:
   - administrator,
   - magazynier,
   - obsługa sklepu,
   - księgowość,
   - brak dostępu do modułów bez uprawnień.
2. Magazyn:
   - lista magazynów,
   - wyszukiwanie produktu,
   - filtr „z numerem paczki / bez numeru paczki”.
3. Przyjęcie towaru:
   - przyjęcie z zaznaczonym `Towar posiada numer paczki` i wpisanym numerem,
   - przyjęcie bez zaznaczonego checkboxa,
   - próba zapisu z zaznaczonym checkboxem i pustym numerem paczki — powinna zostać odrzucona.
4. Wydanie towaru:
   - wydanie z numerem paczki,
   - wydanie bez numeru paczki,
   - blokada ujemnych stanów,
   - zdjęcia przy WZ/RW: upload, podgląd, usunięcie przez administratora.
5. Sklep internetowy:
   - utworzenie zamówienia,
   - rezerwacja towaru,
   - statusy realizacji,
   - blokada wysyłki bez zgody księgowości.
6. Księgowość:
   - proforma,
   - płatność częściowa,
   - zapłacono,
   - sposób płatności,
   - faktura/paragon,
   - handlowiec prowadzący,
   - historia zmian,
   - filtrowanie i podsumowania.
7. Dokument sprzedaży:
   - wygenerowanie dokumentu,
   - pobranie PDF,
   - pobranie DOCX,
   - zatwierdzenie przez księgowość.
8. Backup:
   - ręczny backup administratora,
   - pobranie backupu,
   - cron `backup.py` w środowisku z Firebase Storage.
9. Responsywność:
   - telefon,
   - tablet,
   - desktop.
10. Render:
   - `pip install -r requirements.txt`,
   - `python migrate.py`,
   - start przez `gunicorn`,
   - `/health` zwraca status `ok`.

## Uwaga o środowisku tego zadania

W bieżącym kontenerze instalacja zależności z PyPI jest blokowana przez proxy (`403 Forbidden`), więc nie da się tu wiarygodnie uruchomić Flask, testów aplikacyjnych ani lokalnego serwera. W normalnym środowisku bez blokady proxy należy wykonać dokładnie komendy z sekcji powyżej.
