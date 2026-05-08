---
name: telegram-miniapps
description: Разработка Telegram Mini Apps (TWA): инициализация, платежи через Stars, подводные камни iOS/Android. Используй когда задача — сделать мини-апп.
---

# Telegram Mini Apps (TWA)

## Архитектура
• Frontend: React/Vue/Vanilla JS (Vite предпочтительнее CRA)
• Backend: Node.js (Express/Fastify) или Python (FastAPI)
• Деплой: Vercel (фронт) + Railway/Fly.io (бэк)
• БД: Supabase (PostgreSQL + Auth) или Neon
• Авторизация: проверка initData на бэке (HMAC SHA256)

## Инициализация (фронт)
```javascript
const tg = window.Telegram.WebApp;
tg.ready();              // обязательно ПЕРЕД любыми другими вызовами
tg.expand();             // раскрыть на полный экран
tg.setHeaderColor('bg_color');
tg.MainButton.setText('Купить').show().onClick(() => buy());

const user = tg.initDataUnsafe?.user;
const userId = user?.id;        // НЕ доверяй — только для UI; подтверждай initData на бэке
```

## Верификация initData (бэк, обязательно)
```python
import hmac, hashlib, urllib.parse

def verify(init_data: str, bot_token: str) -> bool:
    parsed = dict(urllib.parse.parse_qsl(init_data))
    received_hash = parsed.pop("hash", "")
    data_check = "\n".join(f"{k}={v}" for k,v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received_hash)
```

Без этой проверки любой может прислать чужой user_id.

## Платежи через Telegram Stars (XTR)
```javascript
// На фронте: открыть ссылку оплаты
tg.openInvoice(invoiceUrl, (status) => {
  if (status === 'paid') onPaid();
});
```
```python
# На бэке: создание инвойса
url = await bot.create_invoice_link(
    title="Подписка",
    description="Месячная",
    payload="user_42_subscription",
    currency="XTR",
    prices=[LabeledPrice(label="1 месяц", amount=100)],  # 100 stars
)
```
Обработать `pre_checkout_query` (одобрить за 10 секунд!) и `successful_payment` через webhook бота.

## Подводные камни (выученные кровью)
1. `tg.ready()` вызвать ДО любого `MainButton`/`BackButton`.
2. iOS не учитывает `viewport-height` при открытой клавиатуре — используй `tg.viewportHeight`.
3. На Android `BackButton` физическая = твой `tg.BackButton.onClick`. Не блокируй.
4. `setHeaderColor` принимает только `bg_color` или `secondary_bg_color` — другие игнорятся.
5. Темы: всегда поддерживай светлую И тёмную (`tg.colorScheme`).
6. iOS обрезает overflow контента — отступ снизу 16+ px от MainButton.
7. CSP: Telegram прокидывает свой — внешние шрифты от Google могут не загружаться.

## Быстрый старт (15 минут до деплоя)
1. @BotFather → /newbot → токен
2. /newapp → имя + URL вашей deploy-ссылки → возвращается `t.me/yourbot/yourapp`
3. Vercel → подключаешь репо → получаешь HTTPS URL
4. Вставляешь URL в /editapp у BotFather
5. Открываешь `t.me/yourbot/yourapp` в Telegram — работает

## Бесплатный стек для MVP
• Vercel — фронт (бесплатно)
• Railway — бэк (5$ free credit/мес)
• Supabase — БД + auth (бесплатно до 500MB)
• Cloudflare — DNS + cache (бесплатно)
