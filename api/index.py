from TelegramWebhookBot import TelegramWebhookBot

# Vercel expects a module-level WSGI app named "app"
app = TelegramWebhookBot().app
