# doc-intake

Короткое описание: что делает проект и зачем.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # заполнить своими значениями
doc-intake           # или: python -m doc_intake
```

## Тесты и линтер

```bash
pytest
ruff check .
```

## Структура

```
src/doc_intake/   код
tests/              тесты
```
