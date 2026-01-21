# Setup Guide

Follow these steps to get your Crowned Trader Dashboard running:

## Step 1: Install Python Dependencies

```bash
pip install -r requirements.txt
```

## Step 2: Create Environment File

Create a file named `.env` in the project root with your Discord + market data configuration:

```env
DISCORD_BOT_TOKEN=YOUR_BOT_TOKEN
DISCORD_CHANNEL_ID=YOUR_CHANNEL_ID
POLYGON_API_KEY=YOUR_POLYGON_API_KEY
```

### How to Get a Discord Bot Token:

1. Go to https://discord.com/developers/applications
2. Create a new application or select an existing one
3. Go to **Bot** section in the left sidebar
4. Click **Reset Token** or **Create Bot** if it's a new application
5. Copy the bot token
6. Under **Privileged Gateway Intents**, enable **Message Content Intent**
7. Scroll down and save changes

### How to Invite Bot to Your Server:

1. Go to **OAuth2** → **URL Generator** in the left sidebar
2. Under **Scopes**, select `bot`
3. Under **Bot Permissions**, select:
   - Send Messages
   - Embed Links
   - Read Message History
4. Copy the generated URL and open it in your browser
5. Select your server and authorize the bot

### How to Get Channel ID:

1. In Discord, go to **User Settings** → **Advanced**
2. Enable **Developer Mode**
3. Right-click on the channel where you want to receive signals
4. Click **Copy ID** (this is your channel ID)
5. Paste it into your `.env` file

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

### Discord bot not working
- Verify your bot token and channel ID are correct in the `.env` file
- Make sure the bot has been invited to your server and has proper permissions
- Ensure Message Content Intent is enabled in the bot settings
- Check that the bot has permission to send messages in the channel
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

