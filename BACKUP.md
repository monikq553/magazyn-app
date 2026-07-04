# Kopie zapasowe bazy

## Gdzie znajdują się kopie

Kopie są zapisywane w prywatnym Firebase Storage skonfigurowanym przez
`FIREBASE_STORAGE_BUCKET`, w katalogu `database-backups/ROK/MIESIĄC/`.
Pliki mają rozszerzenie `.json.gz.enc`. Nie są publicznie udostępniane.

Każdy plik jest najpierw kompresowany, a następnie szyfrowany kluczem Fernet
z `BACKUP_ENCRYPTION_KEY`. Klucz należy przechowywać wyłącznie w zmiennych
środowiskowych Render. Utrata klucza uniemożliwi odtworzenie kopii.

## Automatyczne i ręczne kopie

`render.yaml` definiuje zadanie `magazyn-db-backup`, uruchamiane codziennie
o 02:00 UTC. Polecenie zadania to `python backup.py`.

Jeżeli zadanie Cron nie zostało jeszcze utworzone, aplikacja ma mechanizm
awaryjny: pierwsza kontrola `/health` danego dnia uruchamia kopię w tle.
Blokada PostgreSQL zapobiega równoczesnemu wykonaniu dwóch kopii.

Administrator może również wykonać kopię ręcznie w panelu
**Kopie zapasowe**. Ta sama strona pokazuje historię wykonań i umożliwia
pobranie zaszyfrowanego pliku przez uwierzytelnioną sesję.

## Walidacja i przywracanie

Bezpieczna walidacja bez zmian w bazie:

```text
python restore_backup.py database-backups/...json.gz.enc
```

Przywrócenie z wiersza poleceń:

```text
python restore_backup.py database-backups/...json.gz.enc --execute --confirmation PRZYWRÓĆ
```

Przywracanie z panelu jest domyślnie wyłączone. W sytuacji awaryjnej ustaw
tymczasowo `ALLOW_BACKUP_RESTORE=true` w Renderze, wykonaj przywrócenie,
sprawdź aplikację i natychmiast ustaw tę zmienną ponownie na `false`.

Przywrócenie zastępuje bieżącą zawartość tabel. Przed przywróceniem zawsze
wykonaj dodatkową kopię aktualnego stanu.

## Render PostgreSQL

Bezpłatny Render PostgreSQL nie zapewnia PITR ani eksportów logicznych.
Po zmianie bazy na płatny plan należy dodatkowo korzystać z wbudowanego
Point-in-Time Recovery. Kopie w Firebase Storage warto zachować jako drugą,
niezależną warstwę ochrony.
