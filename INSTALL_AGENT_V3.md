# Установка B&BS AI Agent v3

## Рекомендуемый путь: чистая ветка от актуального `main`

Не накладывайте v3 поверх папки, где уже остались файлы старого экспериментального v2. Сначала сохраните её как резервную копию, затем начните с чистого `main`.

```bash
cd <папка-где-хранятся-проекты>

# Текущую экспериментальную папку не удаляем
mv "bot_lead 2" bot_lead_v2_backup

# Получаем актуальный репозиторий
git clone https://github.com/hilfikiri1/bot_lead.git bot_lead_agent_v3
cd bot_lead_agent_v3

git switch main
git pull origin main
git switch -c feature/bbs-ai-agent-v3
```

Распакуйте архив **changed files** во временную папку и скопируйте его поверх чистой ветки:

```bash
rm -rf /tmp/bbs_agent_v3
mkdir -p /tmp/bbs_agent_v3
unzip ~/Downloads/bot_lead_ai_agent_v3_changed_files.zip -d /tmp/bbs_agent_v3
rsync -av /tmp/bbs_agent_v3/ ./
```

Удалите скомпилированные Python-файлы, которые исторически попали в репозиторий:

```bash
find . -type d -name "__pycache__" -prune -exec rm -rf {} +
find . -type f -name "*.py[co]" -delete
rm -rf .pytest_cache
```

Проверьте изменения:

```bash
git status
git diff --stat
git diff -- .env.example app/config.py app/api/telegram.py app/tasks/voice_note_tasks.py
```

## Установка зависимостей

Лучше использовать отдельное виртуальное окружение:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Локальные проверки

```bash
python -m compileall -q app migrations

pytest -q \
  tests/test_agent_planner.py \
  tests/test_agent_digest.py \
  tests/test_agent_safety.py \
  tests/test_agent_notion_schema.py \
  tests/test_agent_voice_safety.py
```

После установки всех зависимостей желательно выполнить и полный набор:

```bash
pytest -q
```

## Миграция базы

Проверьте, к какой базе указывает `DATABASE_URL`. Не запускайте миграцию против production случайно.

```bash
alembic current
alembic heads
alembic upgrade head
```

Новая цепочка:

```text
006_calendar_events -> 007_operational_agent_v2 -> 007_unified_agent_v3
```

## Коммит и Pull Request

```bash
git status
git add -A
git commit -m "feat: add unified BBS AI agent v3"
git push -u origin feature/bbs-ai-agent-v3
```

На GitHub создайте Pull Request:

```text
base: main
compare: feature/bbs-ai-agent-v3
```

Перед merge проверьте Railway Variables и тестовый deployment.

## Проверка в Telegram после deployment

Порядок безопасной проверки:

1. `/agent`
2. `/kommo_test`
3. `/notion_test`
4. `/digest`
5. `Покажи сделку <часть названия>`
6. `Сделай КП по этой сделке`
7. Нажмите `Сохранить в Notion` и затем подтверждение.
8. Голосом попросите показать сделку или подготовить follow-up.
9. Отправьте запись клиентского разговора через сценарий нового лида.
10. Убедитесь, что Notion не изменяется до нажатия отдельной кнопки.
