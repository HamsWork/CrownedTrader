# Setup Guide

Follow these steps to get your Crowned Trader Dashboard running:

## Step 1: Install Python Dependencies

```bash
pip install -r requirements.txt
```

## Step 2: Create Environment File

Create a file named `.env` in the project root with your Discord webhook URL:

```env
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_URL
```

### How to Get a Discord Webhook URL:

1. Open your Discord server
2. Go to **Server Settings** (right-click on server → Server Settings)
3. Click on **Integrations** in the left sidebar
4. Click on **Webhooks** → **New Webhook**
5. Give it a name (e.g., "Trading Signals")
6. Choose a channel where signals will be posted
7. Click **Copy Webhook URL**
8. Paste it into your `.env` file

## Step 3: Run Database Migrations

```bash
python manage.py makemigrations
python manage.py migrate
```

## Step 4: Create Admin User (Optional)

```bash
python manage.py createsuperuser
```

Follow the prompts to create an admin account.

## Step 5: Run the Server

```bash
python manage.py runserver
```

## Step 6: Access the Dashboard

Open your browser and go to:
- **Dashboard**: http://localhost:8000/
- **Admin Panel**: http://localhost:8000/admin/

## Testing

1. Fill out the signal form with:
   - Ticker: `AAPL`
   - Contract Information: `100 shares at $150`
   - Signal Type: `Entry`
   - Extra Information: `Test signal`
2. Click "Submit Signal"
3. Check your Discord channel - you should see the notification!

## Troubleshooting

### "No module named 'dotenv'"
Run: `pip install python-dotenv`

### Discord webhook not working
- Verify your webhook URL is correct in the `.env` file
- Make sure the webhook hasn't been deleted from Discord
- Check the server console for error messages

### Database errors
Run: `python manage.py migrate` again

## Production Deployment

For production, make sure to:
1. Set `DEBUG=False` in `.env`
2. Change `SECRET_KEY` to a secure random value
3. Set `ALLOWED_HOSTS` in settings.py
4. Use a proper database (PostgreSQL recommended)
5. Set up static file serving
6. Use HTTPS

