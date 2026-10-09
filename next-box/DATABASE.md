# Learning databases with Next Box

Next Box keeps everything in a **database**: one file on your computer, using SQLite. This guide
walks you through it with real commands, from your first query to backups. Each lesson ends with
exercises, and the answers are hidden under "Show answer" so you can try first.

You'll practice on a **separate practice database** full of realistic fake data, so nothing you
do can break your real one.

## 0. Setup (2 minutes)

```bash
cd ~/Desktop/"custom projects"/next-box
.venv/bin/nextbox db demo        # builds the practice database
.venv/bin/nextbox db where       # shows where both databases are
```

Three ways to look inside. Use whichever you like:

| Tool | How | Good for |
|---|---|---|
| `nextbox db` | `nextbox db sql --practice "SELECT ..."` | quick queries, safe by default |
| `sqlite3` | `sudo apt install sqlite3`, then `sqlite3 practice/nextbox.db` | the classic command line |
| DB Browser for SQLite | free app from sqlitebrowser.org; open the `.db` file | clicking around, seeing tables as a spreadsheet |

**Safety rules**

- `nextbox db sql` is **read-only** on your real database. Changes are only allowed with
  `--practice --write`.
- Password hashes, salts and session hashes always show as `•••• (hidden)`. To-do app secrets are
  encrypted, and their key isn't in the database at all.
- If you ever open the real database in another tool, run `nextbox db backup` first, and don't
  change anything while the server is running.

## 1. Tables, rows and columns

A database is a set of **tables**. A table is like a spreadsheet: each **row** is one thing (one
person, one ticket) and each **column** is one fact about it.

```bash
nextbox db tables --practice     # every table, how many rows, what it's for
nextbox db diagram               # how they connect
nextbox db schema users --practice
```

The **primary key** is the column that makes each row unique, like `users.username`. Columns like
`users.household_id` point at another table's primary key (`households.id`). That's how tables
**relate**.

## 2. Your first query: SELECT

```sql
SELECT username, role, email FROM users;
```

Run it:

```bash
nextbox db sql --practice "SELECT username, role, email FROM users"
```

- `SELECT` picks the columns.
- `FROM` picks the table.
- `*` means every column: `SELECT * FROM households`.

**Exercises**
1. List every household's name.
2. Show each printer's `id` and `household_id`.

<details><summary>Show answer</summary>

```sql
SELECT name FROM households;
```
```sql
SELECT id, household_id FROM printers;
```
</details>

## 3. Filtering and sorting: WHERE, ORDER BY, LIMIT

```sql
SELECT username, role FROM users WHERE role = 'admin' ORDER BY username;
```

- `WHERE` keeps only matching rows. You can use `=`, `!=`, `<`, `>`, `LIKE 'a%'` (starts with
  "a") and `IN ('a', 'b')`, and combine with `AND` / `OR`.
- `ORDER BY` sorts the results. Add `DESC` for biggest first.
- `LIMIT 5` returns only the first 5 rows.

**Exercises**
1. Everyone who's a beta tester.
2. The 5 most recent tickets: id and when.
3. Everyone whose name starts with "s".

<details><summary>Show answer</summary>

```sql
SELECT username FROM users WHERE beta = 1;
```
```sql
SELECT id, created_at FROM tickets ORDER BY created_at DESC LIMIT 5;
```
```sql
SELECT username FROM users WHERE username LIKE 's%';
```
</details>

## 4. Counting and grouping: COUNT, GROUP BY

```sql
SELECT by, COUNT(*) AS tickets FROM tickets GROUP BY by ORDER BY tickets DESC;
```

`GROUP BY` puts rows into piles, one pile per person here, and `COUNT(*)` counts each pile. Other
ways to summarize a pile: `SUM`, `AVG`, `MIN`, `MAX`. `AS` gives the result column a name.

To filter the piles themselves, use `HAVING`, which works like `WHERE` but runs after grouping:

```sql
SELECT by, COUNT(*) AS tickets FROM tickets GROUP BY by HAVING COUNT(*) >= 15;
```

**Exercises**
1. How many people are in each household? (Group `users` by `household_id`.)
2. How many events of each `kind` are there?
3. Which actions failed most often? (`events` where `ok` is false.)

<details><summary>Show answer</summary>

```sql
SELECT household_id, COUNT(*) AS people FROM users GROUP BY household_id;
```
```sql
SELECT kind, COUNT(*) FROM events GROUP BY kind;
```
```sql
SELECT action, COUNT(*) AS failures FROM events WHERE ok = 0 GROUP BY action ORDER BY failures DESC;
```
</details>

## 5. Joining tables: JOIN

The `users` table only has a `household_id` like `3f9a07c2b1e4`. To see the household's *name*,
**join** it to `households`:

```sql
SELECT u.username, h.name AS household
FROM users u
JOIN households h ON h.id = u.household_id
ORDER BY h.name, u.username;
```

- `u` and `h` are short nicknames for the tables.
- `ON` says which columns match up.
- `JOIN` keeps only rows that match. `LEFT JOIN` keeps every row from the left table, even with
  no match: for example, people who never printed anything.

```sql
SELECT u.username, COUNT(t.id) AS tickets
FROM users u
LEFT JOIN tickets t ON t.by = u.username
GROUP BY u.username
ORDER BY tickets;
```

**Exercises**
1. Each household's name and how many tickets it printed.
2. Each household's name and how many printers it has.

<details><summary>Show answer</summary>

```sql
SELECT h.name, COUNT(t.id) AS tickets FROM households h JOIN tickets t ON t.household_id = h.id GROUP BY h.name;
```
```sql
SELECT h.name, COUNT(p.id) AS printers FROM households h LEFT JOIN printers p ON p.household_id = h.id GROUP BY h.name;
```
</details>

## 6. Dates and time

Ticket times are text like `2026-10-07T09:15:00`, so they sort correctly and you can cut them up:

```sql
SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS tickets
FROM tickets GROUP BY day ORDER BY day DESC LIMIT 7;
```

Events use a number instead: seconds since 1970 (a "Unix timestamp"). SQLite can turn it into a
date:

```sql
SELECT datetime(ts, 'unixepoch', 'localtime') AS at, user, action
FROM events ORDER BY ts DESC LIMIT 5;
```

**Exercise:** how many tickets were printed in the last 7 days?

<details><summary>Show answer</summary>

```sql
SELECT COUNT(*) FROM tickets WHERE created_at >= date('now', '-7 days');
```
</details>

## 7. JSON inside a column

Some columns hold a whole bundle of details as **JSON**: `tickets.record`, `users.prefs`,
`printers.config`. Reach inside with `json_extract`:

```sql
SELECT id, json_extract(record, '$.reason') AS reason, json_extract(record, '$.source') AS source
FROM tickets LIMIT 5;
```

```sql
SELECT json_extract(record, '$.reason') AS reason, COUNT(*) AS tickets
FROM tickets GROUP BY reason ORDER BY tickets DESC;
```

`json_array_length` counts a list. Here's how many tasks were on each ticket:

```sql
SELECT id, json_array_length(json_extract(record, '$.manifest')) AS tasks FROM tickets LIMIT 5;
```

(PostgreSQL writes this as `record->>'reason'`. Same idea, different spelling.)

**Exercise:** which label color is loaded in each printer? (`printers.config`, key `stock_color`)

<details><summary>Show answer</summary>

```sql
SELECT id, json_extract(config, '$.name') AS name, json_extract(config, '$.stock_color') AS color FROM printers;
```
</details>

## 8. Changing data: INSERT, UPDATE, DELETE (practice database only)

These need `--practice --write`. Try one of each:

```bash
nextbox db sql --practice --write "INSERT INTO households (id, name, created, plan) VALUES ('lab1', 'My test house', 0, 'free')"
nextbox db sql --practice --write "UPDATE households SET name = 'Renamed house' WHERE id = 'lab1'"
nextbox db sql --practice --write "DELETE FROM households WHERE id = 'lab1'"
```

- **Always write the `WHERE` first.** `UPDATE households SET name = 'x'` with no `WHERE` renames
  *every* household. Check what you'll hit before changing it:
  `SELECT * FROM households WHERE id = 'lab1'`.
- Made a mess? Run `nextbox db demo` and you have a fresh practice database.

**Exercise:** make everyone in the Chen family a beta tester.

<details><summary>Show answer</summary>

```sql
UPDATE users SET beta = 1 WHERE household_id = (SELECT id FROM households WHERE name = 'Chen family');
```
That `(SELECT ...)` inside is a **subquery**: a query whose answer is used by another query.
</details>

## 9. Transactions: all or nothing

A **transaction** groups changes so they all happen, or none do. Next Box does this whenever it
changes more than one thing. For example, when a household is deleted, its people, sign-ins and
invites go together or not at all. In `sqlite3` you can try it yourself:

```sql
BEGIN;
DELETE FROM invites;
SELECT COUNT(*) FROM invites;   -- 0 ... for now
ROLLBACK;                       -- undo everything since BEGIN
SELECT COUNT(*) FROM invites;   -- they're back
```

## 10. Indexes: why some lookups are fast

An **index** is like the index at the back of a book. Next Box has one on `tickets (by,
created_at)`, so "Sam's latest tickets" doesn't read every ticket. Ask the database how it plans
to run a query:

```sql
EXPLAIN QUERY PLAN SELECT * FROM tickets WHERE by = 'sam' ORDER BY created_at DESC;
```

Look for `USING INDEX ix_tickets_by_time`. Now try one that can't use an index:
`EXPLAIN QUERY PLAN SELECT * FROM tickets WHERE json_extract(record, '$.kind') = 'list'`. That
shows `SCAN tickets`, meaning every row is read.

## 11. Backups

```bash
nextbox db backup      # copies the real database to <data folder>/backups/, safe while running
```

To restore: stop the service, copy the backup over `nextbox.db`, then start the service again.
Also keep `secret.key`, which is next to the database. Without it, the to-do app connections
saved in the backup can't be unlocked.

## 12. Later: PostgreSQL

SQLite is perfect for one computer. A shared cloud server would use **PostgreSQL**, a database
that runs as its own server. Next Box already supports it: set
`DATABASE_URL=postgresql://user:password@host/dbname` and the same code uses it. Every query in
this guide works there too, except the JSON functions (`record->>'reason'` instead of
`json_extract`) and the date helpers.

## 13. Your own PostgreSQL server

SQLite is a file. **PostgreSQL** is a database *server*: a program that runs all the time, has its
own logins, and lets many apps connect at once. Most real products use it, and the cloud services
you'd use later (Supabase, Neon, AWS RDS, Google Cloud SQL) all run PostgreSQL, so everything here
carries over.

**Switch Next Box to it** (one time, about 5 minutes):

```bash
cd ~/next-box
scripts/setup-postgres.sh
```

It installs PostgreSQL, creates a login called `nextbox` and two databases (`nextbox` for real,
`nextbox_practice` for practice), copies everything from your SQLite file, and puts
`DATABASE_URL` in `.env`. Your SQLite file stays where it was, as a backup.

**Three ways in:**

| Tool | How |
|---|---|
| `nextbox db` | same commands as before: `nextbox db demo`, `nextbox db sql --practice "..."` |
| `psql` | `psql -h 127.0.0.1 -U nextbox nextbox_practice` (password: the one in `DATABASE_URL` in `.env`) |
| pgAdmin or DBeaver | free apps; connect to host `127.0.0.1`, port `5432`, user `nextbox` |

**psql survival kit** (these backslash commands are psql's own, not SQL):

```text
\dt              list tables              \d users        columns and indexes of a table
\x               tall/wide output toggle  \timing         show how long each query takes
\l               list databases           \c nextbox      switch database
\q               quit                     ;               every SQL statement ends with one
```

**Same ideas, different spelling.** The lessons above use SQLite. On PostgreSQL:

| SQLite | PostgreSQL |
|---|---|
| `json_extract(record, '$.reason')` | `record->>'reason'` |
| `json_array_length(json_extract(record, '$.manifest'))` | `json_array_length(record->'manifest')` |
| `datetime(ts, 'unixepoch', 'localtime')` | `to_timestamp(ts)` |
| `date('now', '-7 days')` | `(CURRENT_DATE - 7)::text` |
| `substr(created_at, 1, 10)` | `left(created_at, 10)` (substr works too) |
| `EXPLAIN QUERY PLAN SELECT ...` | `EXPLAIN SELECT ...` (or `EXPLAIN ANALYZE` to run it and time it) |
| `beta = 1` | `beta = true` |

For example, lesson 7's "reasons" query on PostgreSQL:

```sql
SELECT record->>'reason' AS reason, COUNT(*) AS tickets
FROM tickets GROUP BY reason ORDER BY tickets DESC;
```

And something new, your built-in to-do lists (the `tasks` table):

```sql
SELECT owner, COUNT(*) FILTER (WHERE done_at IS NULL) AS open, COUNT(done_at) AS done
FROM tasks GROUP BY owner ORDER BY open DESC;
```

**Things PostgreSQL does that SQLite doesn't (try them on the practice database):**
- **Real users and permissions.** `\du` lists logins. A real product gives the app a login that
  can only touch its own database.
- **Strict types.** `SELECT '2026-13-45'::date;` is an error, not a strange string.
- **Many connections at once**, with row locking: Next Box locks a read-back while it's confirmed,
  so two taps can't both delete the same task.
- **`pg_dump`**: `nextbox db backup` now writes a `.sql` file you can open and read. Restoring is
  `psql -h 127.0.0.1 -U nextbox nextbox_practice < backups/nextbox-....sql` (try it on the
  practice database, never on the real one while Next Box is running).

**Going live later:** a cloud provider gives you a `postgresql://...` address. You'd run
`nextbox db copy-to <that address>` to move your data, put the address in `DATABASE_URL`, and
that's it: same tables, same queries.

## Cheat sheet

```text
SELECT cols FROM t WHERE ... GROUP BY ... HAVING ... ORDER BY ... LIMIT n
JOIN other o ON o.id = t.other_id          LEFT JOIN keeps unmatched rows
COUNT(*)  SUM(x)  AVG(x)  MIN(x)  MAX(x)    AS gives a column a name
INSERT INTO t (a, b) VALUES (1, 2)          UPDATE t SET a = 1 WHERE ...    DELETE FROM t WHERE ...
BEGIN; ... COMMIT;  (or ROLLBACK;)          EXPLAIN QUERY PLAN <query>
json_extract(col, '$.key')                  datetime(ts, 'unixepoch', 'localtime')
```
