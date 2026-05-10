---
name: code-review
description: Чек-лист ревью кода: безопасность, edge cases, производительность. Специфика Telegram-ботов, мини-аппов, Python и TypeScript. Используй когда проверяешь любой код перед деплоем или коммитом.
---

# Code Review Checklist

Используй когда проверяешь Python или TypeScript код — Telegram-боты,
мини-аппы, бэкенд для инфопродуктов, интеграции.

## 1. Безопасность

### Секреты
✗ Хардкод токенов, ключей, паролей в коде
✗ Секреты в URL (`https://x?api_key=...`)
✗ Логирование запросов с токенами в открытом виде
✓ Все секреты через env vars (`os.environ` / `process.env`)
✓ `.env` в `.gitignore`
✓ В логах ключи маскируются: `sk-ant-…` → первые 10 символов

### Telegram Mini App initData
✗ Доверие `initDataUnsafe.user.id` на бэке без проверки
✓ Бэк ВСЕГДА верифицирует HMAC SHA256 от bot_token + initData
Без верификации — любой подменит чужой user_id.

### SQL и инъекции
✗ Конкатенация строк в SQL (`f"SELECT * WHERE id={x}"`)
✓ Параметризованные запросы (`?` или `%s`)
✓ ORM: проверь что фильтры берут пользовательский ввод как параметры

### Webhook безопасность
✓ Endpoint подписан секретом (Telegram: `secret_token` параметр)
✓ Проверка подписи провайдера платежей (ЮKassa, Stripe)
✓ Idempotency-key на повторных webhook'ах

## 2. Edge cases

✗ `obj.something` без проверки на `None` / `null` / `undefined`
✗ Пустые строки/массивы не обрабатываются
✗ Деление без проверки на ноль
✗ `int(user_input)` без try/except
✓ Defaults через `.get(key, default)`
✓ Boundary checks: empty list, single item, max length

### Telegram-specific edge cases
• Сообщение длиннее 4096 символов — режется или падает?
• `callback_query` без `answer_callback_query` за 10 сек — у юзера крутится индикатор
• `from_user is None` (бывает у каналов и анонимных админов)
• Бот добавлен/удалён из группы посреди сценария
• Privacy Mode ON: бот в группе видит только команды и упоминания
• Сообщение от другого бота — `is_bot=True`, нужно фильтровать

## 3. Производительность

### Async корректность (Python)
✗ `await` забыт перед coroutine — функция возвращает coroutine object
✗ Блокирующие вызовы в async (`requests.get`, `time.sleep`, sync `open`)
✓ Async-эквиваленты: `aiohttp`, `asyncio.sleep`, `aiofiles`
✓ Тяжёлые задачи через `asyncio.create_task` или executor

### Async корректность (TypeScript)
✗ Promise без await/then — silent failure
✗ Unhandled promise rejection
✓ `Promise.all` для параллели
✓ Top-level `try/catch` с логом

### Запросы в БД
✗ N+1: цикл с запросом внутри (`for u in users: db.fetch(u.id)`)
✓ Один запрос с JOIN или batch fetch
✓ Индексы на полях из WHERE / ORDER BY
✓ Limit на запросах списков

### Telegram API rate limits
• 30 сообщений в секунду в одном чате — иначе 429
• 30 в секунду в общем потоке отправки
• При массовой рассылке — очередь с throttle

## 4. Обработка ошибок

✗ `except: pass` — глушит всё, включая SystemExit
✗ Голый `try/except Exception` без логирования
✓ Конкретные исключения там где они важны
✓ Логирование с контекстом: `log.exception("X failed for user=%s", uid)`
✓ Graceful fallback пользователю: «попробуй позже» вместо стектрейса

## 5. Платежи

✗ Активация продукта на success-странице (без webhook)
✗ Сумма в дробях (deal-breaker для Stripe/ЮKassa — нужны минимальные единицы)
✗ Без идемпотентности — повтор webhook = дублирующая активация
✓ Активация ТОЛЬКО после webhook от провайдера
✓ Idempotency-key уникальный per-попытка
✓ Логи всех refund-операций отдельно

## 6. Telegram Mini Apps

✗ `MainButton.show()` до `tg.ready()` — silent fail
✗ Игнорирование `tg.colorScheme` — выглядит уродливо в тёмной теме
✗ Игнорирование `tg.viewportHeight` — плохо при открытой клавиатуре
✗ Внешние Google Fonts — Telegram CSP может их рубить
✓ Поддержка светлой и тёмной темы
✓ Тестировать iOS и Android отдельно — рендер разный

## 7. Python — типичное

✗ Mutable default argument (`def f(x=[])`)
✗ `dict[key]` без проверки → KeyError; используй `.get()`
✗ Открытие файлов без `with` (утечки FD)
✗ `==` для сравнения с `None` (используй `is None`)
✓ Type hints для публичного API
✓ `pathlib.Path` вместо ручной склейки путей

## 8. TypeScript — типичное

✗ `any` без причины — теряется тайпчек
✗ `as Foo` каст без рантайм-проверки
✗ Non-null assertion `!` без обоснования
✓ `unknown` + type guard для внешних данных
✓ Exhaustive switch с `never`-проверкой
✓ Discriminated unions для состояний

## Финальный формат вывода

✅ СИЛЬНО: что хорошо в этом коде (1–2 пункта)
❌ СЛАБО: критичная проблема + как исправить (макс 5 пунктов, в порядке важности)
⚠️ ПРЕДЛОЖЕНИЯ: некритичные улучшения (опционально)
ВЕРДИКТ: ПРИНЯТО / ДОРАБОТАТЬ

Критичные триггеры ДОРАБОТАТЬ:
• Захардкоженный секрет
• Отсутствие верификации initData
• Не обработанный None/undefined в критичной точке
• SQL-инъекция возможна
• Race condition или deadlock
• Без обработки error на платежах
• Блокирующий вызов в async
