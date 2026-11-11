# Решение совета

*08.11.2026 18:30 · Совет нейросетей*

**Choose PostgreSQL with the TimescaleDB extension.** It covers 90% of the analytics workload without a second
database and keeps operations simple for a team of three.

## Neden PostgreSQL? (Türkçe özet)

Ekip küçük; tek bir veritabanı işletmek, iki ayrı sistem işletmekten çok daha ucuzdur. Günlük 40 milyon satıra kadar
TimescaleDB yeterli performans gösterir.

## Capacity estimate

Expected load is $N = 4 \times 10^7$ rows per day; with compression ratio $k \approx 8$ storage for a year is

$$S = \frac{N \cdot 365 \cdot 120\ \text{bytes}}{k} \approx 220\ \text{GB}$$

## Benchmark summary

| Engine | Ingest, rows/s | p95 query, ms | Storage/yr | Ops effort | License | Managed in RU | SQL dialect | Joins |
|---|---|---|---|---|---|---|---|---|
| PostgreSQL + TimescaleDB | 180 000 | 420 | 220 GB | low | Apache/TSL | yes | full SQL | full |
| ClickHouse | 1 200 000 | 85 | 90 GB | medium | Apache 2.0 | yes | ClickHouse SQL | limited |
| BigQuery | n/a (batch) | 1 900 | pay per TB | very low | proprietary | no | GoogleSQL | full |

## Migration snippet

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
SELECT create_hypertable('events', 'created_at', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE events SET (timescaledb.compress, timescaledb.compress_segmentby = 'tenant_id');
```

Documentation: https://docs.timescale.com/use-timescale/latest/compression/about-compression/?utm_source=council-report&utm_medium=pdf&utm_campaign=very-long-link-check

---
*08.11.2026 18:30 · участники: claude, chatgpt, gemini, kimi*
