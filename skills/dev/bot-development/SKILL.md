---
name: bot-development
description: Разработка Telegram-ботов: grammy/telegraf, FSM, webhook vs polling, deploy. Используй для любой задачи где нужно сделать бота.
---

# Telegram Bot Development

## Выбор библиотеки
• grammY (TS/JS) — современная, типизированная, лучшая для TS-проектов.
• Telegraf (TS/JS) — старее, больше плагинов, но местами сырая.
• aiogram (Python) — стандарт де-факто для Python.
• python-telegram-bot — старая, добротная, идёт с большой экосистемой.

## Webhook vs polling
• Polling: проще на dev, но требует постоянно живой процесс. Подходит для MVP.
• Webhook: продакшн-стандарт, нужен HTTPS-эндпоинт. Не теряет апдейты при перезапусках.

Для разработки: polling. Для прода: webhook на Vercel/Railway.

## Минимальный grammY бот
```typescript
import { Bot, GrammyError } from "grammy";

const bot = new Bot(process.env.BOT_TOKEN!);

bot.command("start", (ctx) => ctx.reply("Привет!"));
bot.on("message:text", (ctx) => ctx.reply(`Эхо: ${ctx.message.text}`));

bot.catch((err) => {
  if (err instanceof GrammyError) console.error("Telegram error:", err);
});

bot.start();
```

## Состояния (FSM) — пошаговые диалоги
grammY: используй `@grammyjs/conversations` плагин.
aiogram: встроенный FSM с MemoryStorage/RedisStorage.

Не пиши свой FSM — глюки на параллельных юзерах гарантированы.

## Inline-кнопки и callback_query
```typescript
import { InlineKeyboard } from "grammy";

const kb = new InlineKeyboard()
  .text("Купить", "buy")
  .text("Отмена", "cancel");

bot.command("offer", (ctx) => ctx.reply("Хочешь?", { reply_markup: kb }));

bot.callbackQuery("buy", async (ctx) => {
  await ctx.answerCallbackQuery();   // обязательно ответить за 10 сек
  await ctx.editMessageText("Куплено!");
});
```

## Подводные камни
1. `answerCallbackQuery` нужно вызвать на КАЖДЫЙ callback_query — иначе у юзера крутится индикатор.
2. Лимиты: 30 сообщений в секунду в одном чате, 30 в группах. Используй очередь при массовой рассылке.
3. Большие тексты режутся на 4096 символов — режь сам.
4. Удаление сообщений работает только до 48 часов.
5. Если бот не админ в группе — не видит обычные сообщения (только команды и упоминания) при включённой Privacy Mode. Отключай через @BotFather.
6. Один бот = одна polling-сессия. Не запускай две копии.

## Обработка ошибок и троттлинг
```typescript
bot.use(async (ctx, next) => {
  try { await next(); }
  catch (e) {
    console.error(e);
    await ctx.reply("Что-то сломалось, попробуй позже");
  }
});
```

`@grammyjs/auto-retry` — автоматический ретрай при `429 Too Many Requests`.

## Деплой webhook (Railway пример)
1. `bot.api.setWebhook("https://yourapp.railway.app/webhook")`
2. Express:
```typescript
app.post("/webhook", webhookCallback(bot, "express"));
```
3. Health-check `/` чтобы Railway не убивал контейнер.
4. Переменные окружения: `BOT_TOKEN`, `WEBHOOK_SECRET` (если используешь).

## Хранилище сессий
Memory: только для тестов. Перезапуск = потеря.
Redis: продакшн-стандарт (Upstash бесплатно до 10k запросов/день).
PostgreSQL: если уже есть БД — допустимо.
