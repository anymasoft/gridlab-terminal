# Исследовательские прогоны

Скрипты, которыми проверялись пункты аудита. Результаты — в
[../docs/AUDIT-RESULTS.md](../docs/AUDIT-RESULTS.md).

Кэш скачанной истории кладётся в `research/cache/` и в git не попадает.
Первый запуск качает данные с Bybit и занимает несколько минут.

| скрипт | что делает | пункт аудита |
|---|---|---|
| `trendscan.py` | сканирует два года часовой истории и находит самые трендовые окна | 1.2 |
| `trendrun.py` | гоняет сетку на найденных трендовых окнах против buy-and-hold | 1.2, 1.3, 1.4 |
| `walkforward.py` | подбор параметров на обучении, проверка на следующем отрезке | 1.1, 1.3 |

```bash
python research/trendscan.py
python research/trendrun.py
python research/walkforward.py
```

Инварианты учёта проверяются обычным тестом: `pytest -q test_equity_invariants.py`.
