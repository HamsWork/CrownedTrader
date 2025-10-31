# Crowned Trader Dashboard

A modern Django web dashboard for managing and submitting trading signals with automatic Discord notifications.

## Features

- 📊 **Modern Dashboard Interface** - Clean, professional UI with gradient design
- 🚨 **Signal Management** - Submit trading signals with ticker, contract info, and signal type
- 🔔 **Discord Integration** - Automatic bot notifications to Discord channels
- 📈 **Signal History** - View and filter all submitted signals
- 🎯 **Signal Types**:
  - Entry signals
  - Stop Loss Hit
  - Take Profit Hit

## Installation

1. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Set Up Discord Integration** (Choose one method)

   **Method 1: Webhook (Recommended - Easier)**
   - Go to Discord channel settings → Integrations → Create Webhook
   - Copy the webhook URL
   - Create a `.env` file in the project root:
     ```env
     DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN
     ```
   - See `DISCORD_WEBHOOK_SETUP.md` for detailed instructions

   **Method 2: Bot Token (Alternative)**
   - Create a Discord application at https://discord.com/developers/applications
   - Create a bot and copy the token
   - Enable Message Content Intent in bot settings
   - Invite the bot to your server with send message permissions
   - Get your channel ID (enable Developer Mode, right-click channel → Copy ID)
   - Create a `.env` file in the project root:
     ```env
     DISCORD_BOT_TOKEN=YOUR_BOT_TOKEN
     DISCORD_CHANNEL_ID=YOUR_CHANNEL_ID
     ```

3. **Run Migrations**
   ```bash
   python manage.py makemigrations
   python manage.py migrate
   ```

4. **Create Superuser (Optional - for admin panel)**
   ```bash
   python manage.py createsuperuser
   ```

5. **Run Development Server**
   ```bash
   python manage.py runserver
   ```

6. **Access the Dashboard**
   - Main Dashboard: http://localhost:8000/
   - Admin Panel: http://localhost:8000/admin/

## Usage

### Submitting a Signal

1. Navigate to the dashboard
2. Fill in the form:
   - **Ticker**: Stock or asset symbol (e.g., AAPL, TSLA)
   - **Contract Information**: Details about the trading contract
   - **Signal Type**: Choose Entry, Stop Loss Hit, or Take Profit Hit
   - **Extra Information**: Additional notes (optional)
3. Click "Submit Signal"
4. The signal will be saved and sent to your Discord channel

### Viewing Signal History

- Click "Signal History" in the navigation
- Filter signals by type using the filter tabs
- View all signal details including timestamps

## Configuration

### Discord Bot Messages

The dashboard sends embeds to Discord with the following structure:
- **Title**: New Trading Signal
- **Color coding**:
  - 🟢 Green for Entry signals
  - 🔴 Red for Stop Loss signals
  - 🔵 Cyan for Take Profit signals
- **Fields**: Ticker, Signal Type, Contract Information, Extra Information

### Environment Variables

```env
# Discord Configuration - Choose ONE method:

# Method 1: Webhook (Recommended)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN

# Method 2: Bot Token (Alternative)
# DISCORD_BOT_TOKEN=YOUR_BOT_TOKEN
# DISCORD_CHANNEL_ID=YOUR_CHANNEL_ID

# Optional - Django secret key for production
SECRET_KEY=your-secret-key-here

# Optional - Debug mode
DEBUG=True
```

## Project Structure

```
CrownedTraderDashboard/
├── crownedtrader/       # Django project settings
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── signals/            # Main app for trading signals
│   ├── models.py      # Signal data model
│   ├── views.py       # Dashboard and history views
│   ├── forms.py       # Signal input form
│   ├── admin.py       # Admin panel configuration
│   └── templates/     # HTML templates
├── manage.py
├── requirements.txt
└── README.md
```

## Technologies Used

- **Django 4.2** - Web framework
- **Python** - Backend language
- **SQLite** - Database (can be changed in settings)
- **HTML/CSS** - Frontend with modern gradient design
- **Requests** - HTTP client for Discord API

## License

This project is part of the StocksWithJosh CrownedTraderDashboard system.

