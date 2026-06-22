# Action Point 6 (P2) Report

## Анализ
Пользователь запросил расширение существующего lint-правила на проверку doc-drift в:
1. **docstrings** — необходимо проверять, что параметры, описанные в блоке `Args:` докстрингов, действительно существуют в сигнатуре функции. Это требует использования `ast` для парсинга Python-кода.
2. **ADR (Architecture Decision Records)** — необходимо проверять, что файлы кода, упоминаемые в ADR (например, `` `packages/core/alembic/versions/0007_entity_emitter.py` ``), действительно существуют в репозитории.
3. Интеграцию в CI, так как текущая проверка (`scripts/lint_stale_architecture.py`) запускалась только локально через pre-commit.

## Реализация
1. Переписан скрипт `scripts/lint_stale_architecture.py`:
   - Добавлена функция `check_docstrings_ast`, использующая `ast.walk` для поиска `FunctionDef` и `AsyncFunctionDef`. Функция извлекает docstring, парсит секцию `Args:` с помощью регулярных выражений и сравнивает перечисленные там параметры с фактическими параметрами из `node.args`. В случае расхождения (описан аргумент, которого нет в сигнатуре) выдаётся ошибка `doc-drift`.
   - Добавлена функция `check_adr_file`, которая парсит Markdown-файлы из директории `docs/decisions/`. Ищет вхождения вида `` `путь/до/файла.py` `` и проверяет существование этого пути относительно корня проекта.
   - Скрипт теперь поддерживает передачу директорий в качестве аргументов командной строки: он рекурсивно находит в них файлы `*.py` и `*.md` и выполняет соответствующие проверки.
2. Обновлён CI workflow в `.github/workflows/ci.yml`:
   - В джобе `lint` добавлен новый шаг `Check for stale architecture and doc-drift`, который запускает `uv run python scripts/lint_stale_architecture.py packages/ apps/ tests/ docs/decisions/`.

## Тесты
1. Добавлены unit-тесты в файл `tests/test_lint_stale_architecture.py`:
   - `test_check_docstrings_ast_doc_drift` проверяет, что скрипт находит несуществующий параметр в докстринге.
   - `test_check_docstrings_ast_no_drift` проверяет успешное прохождение, когда все параметры соответствуют сигнатуре.
   - `test_check_adr_file_doc_drift` создаёт временный ADR с корректной ссылкой и ссылкой на отсутствующий файл, удостоверяясь, что скрипт отлавливает "битую" ссылку и бросает ошибку `doc-drift: ADR references missing file`.
