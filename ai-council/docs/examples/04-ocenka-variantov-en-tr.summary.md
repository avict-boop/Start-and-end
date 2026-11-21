# Which database should we use for product analytics: PostgreSQL, ClickHouse or BigQuery?

*Итог заседания · 08.11.2026 18:30 · Совет нейросетей*

| Решение | Согласие совета | Первых мест | Совет |
|---|---|---|---|
| **PostgreSQL + TimescaleDB** (вариант A, пользователь) | высокое (τ = 0.83) | 3 из 4 | 4 моделей, Оценка вариантов |

## Решение совета

**Choose PostgreSQL with the TimescaleDB extension.** It covers 90% of the analytics workload without a second
database and keeps operations simple for a team of three.

### Neden PostgreSQL? (Türkçe özet)

Ekip küçük; tek bir veritabanı işletmek, iki ayrı sistem işletmekten çok daha ucuzdur. Günlük 40 milyon satıra kadar
TimescaleDB yeterli performans gösterir.

### Capacity estimate

Expected load is $N = 4 \times 10^7$ rows per day; with compression ratio $k \approx 8$ storage for a year is

$$S = \frac{N \cdot 365 \cdot 120\ \text{bytes}}{k} \approx 220\ \text{GB}$$

### Benchmark summary

| Engine | Ingest, rows/s | p95 query, ms | Storage/yr | Ops effort | License | Managed in RU | SQL dialect | Joins |
|---|---|---|---|---|---|---|---|---|
| PostgreSQL + TimescaleDB | 180 000 | 420 | 220 GB | low | Apache/TSL | yes | full SQL | full |
| ClickHouse | 1 200 000 | 85 | 90 GB | medium | Apache 2.0 | yes | ClickHouse SQL | limited |
| BigQuery | n/a (batch) | 1 900 | pay per TB | very low | proprietary | no | GoogleSQL | full |

### Migration snippet

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
SELECT create_hypertable('events', 'created_at', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE events SET (timescaledb.compress, timescaledb.compress_segmentby = 'tenant_id');
```

Documentation: https://docs.timescale.com/use-timescale/latest/compression/about-compression/?utm_source=council-report&utm_medium=pdf&utm_campaign=very-long-link-check

## Обоснование

### Почему совет так решил

Three of four ballots ranked PostgreSQL first; reviewers valued the lower operational load.

### Риски

- Compression settings must be tested on real data before go-live.

### Особые мнения

- gemini: ClickHouse will be needed once ingest exceeds 100k rows/s sustained.

## Запрос и условия

> Which database should we use for product analytics: PostgreSQL, ClickHouse or BigQuery?  

### Условия заседания

| | |
|---|---|
| Режим | Оценка вариантов |
| Участники | claude, chatgpt, gemini, kimi |
| Критерии | Performance, Operations, Cost |
| Дата | 08.11.2026 18:30 |
| Длительность | 2 мин 8 с |
| Стоимость | $0.1032 |

---
*Это краткая версия: решение и его обоснование. Полный отчёт с вариантами всех моделей, критикой и голосованием скачивается отдельно.*
