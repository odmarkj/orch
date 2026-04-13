# PostgreSQL Best Practices

PostgreSQL's rich type system, indexing options, and extensibility make it the most versatile open-source database, but its power demands careful schema design decisions. This reference covers the data modeling, indexing, partitioning, and operational patterns that distinguish production-quality PostgreSQL schemas from prototypes.

Covers: data modeling, normalization decisions, PostgreSQL data types (JSONB, arrays, ranges, vectors), indexing strategies (B-tree, GIN, GiST, BRIN, partial, covering), partitioning, row-level security, constraints, query optimization, generated columns, and extensions.

---

## Data Modeling Fundamentals

- **Normalize first to 3NF** -- eliminate redundancy and update anomalies; denormalize only for measured, high-ROI reads where join cost is proven problematic
- **Define a primary key for reference tables** -- prefer `BIGINT GENERATED ALWAYS AS IDENTITY`; use UUID only when global uniqueness or opacity is required
- **Add NOT NULL everywhere semantically required** -- nullable columns that should never be null create silent data quality bugs
- **Use DEFAULT for common values** -- `DEFAULT now()` for timestamps, `DEFAULT '{}'` for JSONB; reduces boilerplate and prevents omission errors
- **snake_case for all identifiers** -- unquoted identifiers are lowercased by PostgreSQL; mixed-case names require quoting everywhere

## Data Types

- **TEXT over VARCHAR(n)** -- use `CHECK (LENGTH(col) <= n)` for length limits; VARCHAR(n) provides no performance benefit and complicates migrations
- **TIMESTAMPTZ, never TIMESTAMP** -- timestamp without timezone loses context; always store with timezone awareness
- **NUMERIC for money** -- never use FLOAT or the MONEY type; `NUMERIC(10,2)` preserves exact decimal arithmetic
- **BIGINT for integers** -- prefer over INTEGER unless storage is critical; avoids costly type migrations when values grow
- **BOOLEAN with NOT NULL** -- unless you genuinely need tri-state logic (true/false/unknown), constrain to NOT NULL
- **ENUM for small stable sets** -- `CREATE TYPE day AS ENUM ('mon','tue',...)` for values that rarely change; use TEXT + CHECK for evolving business values
- **Arrays for ordered lists** -- `TEXT[]`, `INTEGER[]` with GIN indexes for containment queries; avoid for relational data, use junction tables instead
- **Range types for intervals** -- `daterange`, `numrange`, `tstzrange` with GiST indexes; prefer `[)` (inclusive/exclusive) bounds consistently
- **JSONB for semi-structured data** -- preferred over JSON; use only for optional/variable attributes; keep core relations in proper tables
- **TSVECTOR for full-text search** -- always specify language: `to_tsvector('english', col)`; index with GIN; never use single-argument form
- **pgvector for embeddings** -- `vector` type enables similarity search for AI/ML workloads within PostgreSQL
- **Domain types for reusable validation** -- `CREATE DOMAIN email AS TEXT CHECK (VALUE ~ '^[^@]+@[^@]+$')` enforces rules across tables

## Types to Avoid

- **Never use SERIAL** -- replaced by `GENERATED ALWAYS AS IDENTITY` which prevents accidental value override
- **Never use CHAR(n)** -- pads with spaces; use TEXT instead
- **Never use TIMESTAMP without TZ** -- use TIMESTAMPTZ for all temporal data
- **Never use MONEY type** -- locale-dependent formatting; use NUMERIC instead
- **Never use TIMETZ** -- meaningless without a date; use TIMESTAMPTZ

## Constraints

- **Primary keys create B-tree indexes automatically** -- implicit UNIQUE + NOT NULL; no separate index needed
- **Foreign keys need explicit indexes** -- PostgreSQL does NOT auto-index FK columns; add them manually to speed joins and prevent locking issues
- **UNIQUE with NULLS NOT DISTINCT (PG15+)** -- standard UNIQUE allows multiple NULLs; use NULLS NOT DISTINCT to allow only one
- **CHECK constraints for business rules** -- `CHECK (price > 0)`; remember NULL passes CHECK (three-valued logic), combine with NOT NULL
- **EXCLUDE constraints for overlaps** -- `EXCLUDE USING gist (room_id WITH =, period WITH &&)` prevents double-booking at the database level
- **Deferrable FKs for circular dependencies** -- `DEFERRABLE INITIALLY DEFERRED` checks constraints at transaction end, not per-statement

## Indexing Strategies

- **B-tree for equality and range** -- default index type; supports `=`, `<`, `>`, `BETWEEN`, `ORDER BY`
- **Composite index column order matters** -- put equality-filtered columns first, range columns last; index on `(a, b)` supports `WHERE a = ? AND b > ?` but not `WHERE b = ?` alone
- **Covering indexes for index-only scans** -- `CREATE INDEX ON tbl (id) INCLUDE (name, email)` avoids table heap access for covered queries
- **Partial indexes for hot subsets** -- `CREATE INDEX ON orders (user_id) WHERE status = 'active'` indexes only rows that match the filter
- **Expression indexes for computed lookups** -- `CREATE INDEX ON users (LOWER(email))` supports case-insensitive search; expression must match the WHERE clause exactly
- **GIN for JSONB, arrays, and full-text** -- supports containment (`@>`), key existence (`?`), and text search (`@@`) operators
- **GiST for ranges, geometry, and exclusion** -- enables range overlap queries and spatial operations
- **BRIN for time-series data** -- minimal storage for large tables where physical row order correlates with the indexed column
- **jsonb_path_ops for containment-only** -- smaller and faster GIN index; trades away key existence query support

## Partitioning

- **Use for very large tables (100M+ rows)** -- or when data maintenance (pruning, bulk replacement) benefits from partition-level operations
- **RANGE for time-series** -- `PARTITION BY RANGE (created_at)` with monthly or weekly partitions; enables fast partition pruning on date filters
- **LIST for discrete categories** -- `PARTITION BY LIST (region)` for geographic or categorical data segmentation
- **HASH for even distribution** -- `PARTITION BY HASH (user_id)` distributes rows across N partitions when no natural range key exists
- **Include partition key in PK/UNIQUE** -- global unique constraints require the partition key; this is a PostgreSQL limitation
- **Prefer declarative partitioning** -- never use table inheritance for partitioning; declarative (PG10+) handles constraint exclusion automatically
- **TimescaleDB for automated time-series partitioning** -- hypertables, retention policies, compression, and continuous aggregates

## Row-Level Security

- **Enable per table** -- `ALTER TABLE tbl ENABLE ROW LEVEL SECURITY` activates RLS; table owner bypasses by default
- **Create policies per operation** -- `CREATE POLICY user_access ON orders FOR SELECT USING (user_id = current_user_id())` filters rows transparently
- **Combine with application roles** -- set `current_setting('app.user_id')` at connection start; reference in RLS policies

## Query Optimization

- **Explain Analyze for real execution plans** -- `EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)` shows actual row counts and I/O; never optimize from estimates alone
- **Index foreign key columns** -- the most common missed optimization; speeds joins and prevents locks on parent table deletes
- **Avoid SELECT star** -- fetch only needed columns; reduces I/O and enables index-only scans
- **Use CTEs for readability, not performance** -- CTEs are optimization fences in older PG; PG12+ can inline them, but verify with EXPLAIN
- **Batch inserts with COPY** -- `COPY` or multi-row INSERT is orders of magnitude faster than single-row inserts for bulk loads

## Update-Heavy Tables

- **Separate hot and cold columns** -- frequently updated columns in a separate table minimize bloat in the main table
- **Use fillfactor=90** -- leaves space for HOT (Heap Only Tuple) updates that avoid index maintenance
- **Avoid updating indexed columns** -- changes to indexed columns prevent HOT updates and increase index bloat

## Safe Schema Evolution

- **Transactional DDL** -- most DDL runs in transactions and can be rolled back; test with `BEGIN; ALTER TABLE...; ROLLBACK;`
- **CREATE INDEX CONCURRENTLY** -- avoids blocking writes during index creation; cannot run inside a transaction
- **Non-volatile defaults are fast** -- adding a NOT NULL column with a constant default is instant; volatile defaults (`now()`, `gen_random_uuid()`) rewrite the entire table

## Generated Columns

- **STORED generated columns** -- `GENERATED ALWAYS AS (expr) STORED` computes and indexes derived values; PG18+ adds VIRTUAL (computed on read)
- **JSONB field extraction** -- `theme TEXT GENERATED ALWAYS AS (attrs->>'theme') STORED` enables B-tree indexing on JSONB subfields

## Essential Extensions

- **pg_trgm** -- fuzzy text search with `%` operator and `similarity()`; GIN-indexed `LIKE '%pattern%'` acceleration
- **pgcrypto** -- `crypt()` for password hashing; `gen_random_uuid()` for UUID generation
- **PostGIS** -- comprehensive geospatial support for location-based applications
- **TimescaleDB** -- automated time-series partitioning, retention, compression, and continuous aggregates
- **pgvector** -- vector similarity search for embedding-based AI workloads
- **pgaudit** -- audit logging for compliance; tracks all database activity
- **btree_gin/btree_gist** -- mixed-type composite indexes combining GIN/GiST with standard types

## PostgreSQL Gotchas

- **FK indexes are not automatic** -- unlike primary keys, foreign key columns get no index; always create one manually
- **Sequences have gaps** -- rollbacks, crashes, and concurrency create gaps in identity columns; this is normal, do not try to fix it
- **No clustered indexes** -- PostgreSQL is heap-based; CLUSTER is a one-time physical reorder, not maintained on inserts
- **MVCC creates dead tuples** -- updates and deletes leave dead rows; autovacuum cleans them; tune autovacuum for write-heavy tables
- **Length overflows error, not truncate** -- inserting 999 into NUMERIC(2,0) fails; PostgreSQL does not silently truncate
- **now() returns transaction start time** -- use `clock_timestamp()` for wall-clock time within a transaction if multiple timestamps are needed
- **UNIQUE allows multiple NULLs by default** -- `(1, NULL)` and `(1, NULL)` are both allowed unless `NULLS NOT DISTINCT` is specified
