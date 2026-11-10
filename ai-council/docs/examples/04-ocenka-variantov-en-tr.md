# Which database should we use for product analytics: PostgreSQL, ClickHouse or BigQuery?

*Отчёт о заседании · 08.11.2026 18:30 · Совет нейросетей*

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

## Голосование

| Место | Вариант | Автор | Первых мест | Балл |
|---|---|---|---|---|
| 1 | **A. PostgreSQL + TimescaleDB** | пользователь | 3 из 4 | 0.88 |
| 2 | B. ClickHouse | пользователь | 1 из 4 | 0.62 |
| 3 | C. BigQuery | пользователь | 0 из 4 | 0.00 |

### Насколько надёжен результат

- Согласие совета: высокое (τ = 0.83) — модели в основном одинаково упорядочили варианты (это согласие, а не гарантия правоты).
- Каждая пара вариантов сравнивалась в бюллетенях 4 раза.

### Бюллетени

| Участник | Порядок | Почему |
|---|---|---|
| claude | **B › A › C** | Simplest operations for a small team. |
| chatgpt | **A › B › C** | Simplest operations for a small team. |
| gemini | **A › B › C** | Simplest operations for a small team. |
| kimi | **B › A › C** | Simplest operations for a small team. |

### Подробный подсчёт

| # | Вариант | Автор | Шульце | Борда | Попарно | Критика | 1-х мест |
|---|---|---|---|---|---|---|---|
| 1 | A | пользователь | 2 из 2 | 0.88 | +2 | 8.5 | 3/4 |
| 2 | B | пользователь | 1 из 2 | 0.62 | +0 | 7.9 | 1/4 |
| 3 | C | пользователь | 0 из 2 | 0.00 | -2 | 5.6 | 0/4 |

*Порядок: метод Шульце — кто выигрывает в прямых сравнениях (сколько соперников побеждает); при равенстве — балл Борда, затем попарный счёт, затем жребий. Оценка критики показана для сведения и на порядок не влияет: она дана версиям до доработки.*

## Варианты

### A. PostgreSQL + TimescaleDB — решение совета

*Автор: **пользователь** · место: 1*

PostgreSQL with the TimescaleDB extension.

**Оценки других участников:**

- **claude** (Performance: 8, Operations: 8, Cost: 8)
  - ➕ Clear trade-offs
  - ➖ Needs a load test
- **chatgpt** (Performance: 9, Operations: 9, Cost: 9)
  - ➕ Clear trade-offs
  - ➖ Needs a load test
- **gemini** (Performance: 8, Operations: 8, Cost: 8)
  - ➕ Clear trade-offs
  - ➖ Needs a load test
- **kimi** (Performance: 9, Operations: 9, Cost: 9)
  - ➕ Clear trade-offs
  - ➖ Needs a load test

### B. ClickHouse

*Автор: **пользователь** · место: 2*

ClickHouse cluster of three nodes.

**Оценки других участников:**

- **claude** (Performance: 7.4, Operations: 7.4, Cost: 7.4)
  - ➕ Clear trade-offs
  - ➖ Needs a load test
- **chatgpt** (Performance: 8.4, Operations: 8.4, Cost: 8.4)
  - ➕ Clear trade-offs
  - ➖ Needs a load test
- **gemini** (Performance: 7.4, Operations: 7.4, Cost: 7.4)
  - ➕ Clear trade-offs
  - ➖ Needs a load test
- **kimi** (Performance: 8.4, Operations: 8.4, Cost: 8.4)
  - ➕ Clear trade-offs
  - ➖ Needs a load test

### C. BigQuery

*Автор: **пользователь** · место: 3*

Google BigQuery with scheduled loads.

**Оценки других участников:**

- **claude** (Performance: 5.1, Operations: 5.1, Cost: 5.1)
  - ➕ Clear trade-offs
  - ➖ Needs a load test
- **chatgpt** (Performance: 6.1, Operations: 6.1, Cost: 6.1)
  - ➕ Clear trade-offs
  - ➖ Needs a load test
- **gemini** (Performance: 5.1, Operations: 5.1, Cost: 5.1)
  - ➕ Clear trade-offs
  - ➖ Needs a load test
- **kimi** (Performance: 6.1, Operations: 6.1, Cost: 6.1)
  - ➕ Clear trade-offs
  - ➖ Needs a load test

## Критика

*Средняя оценка каждого рецензента, от 1 до 10.*

| Рецензент | A | B | C |
|---|---|---|---|
| claude | 8.0 | 7.4 | 5.1 |
| chatgpt | 9.0 | 8.4 | 6.1 |
| gemini | 8.0 | 7.4 | 5.1 |
| kimi | 9.0 | 8.4 | 6.1 |
| **Среднее** | **8.5** | **7.9** | **5.6** |

## Участники и расходы

| Участник | Обращений | Токены (вход / выход) | Стоимость |
|---|---|---|---|
| claude | 3 | 18000 / 5200 | $0.0140 |
| chatgpt | 3 | 19500 / 5600 | $0.0170 |
| gemini | 3 | 21000 / 6000 | $0.0200 |
| kimi | 3 | 22500 / 6400 | $0.0230 |
| **Итого** | | | **$0.1032** |

*Из кэша провайдеров прочитано 25 800 из 81 000 входных токенов (32%) — повторно прочитанные вопрос и вложения стоят дешевле.*

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
