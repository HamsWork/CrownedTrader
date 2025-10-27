# Crowned Trader Dashboard

A modern Django web dashboard for managing and submitting trading signals with automatic Discord notifications.

## Features

- 📊 **Modern Dashboard Interface** - Clean, professional UI with gradient design
- 🚨 **Signal Management** - Submit trading signals with ticker, contract info, and signal type
- 🔔 **Discord Integration** - Automatic webhook notifications to Discord channels
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

2. **Set Up Discord Webhook**
   - Create a webhook in your Discord server (Server Settings > Integrations > Webhooks)
   - Copy the webhook URL
   - Create a `.env` file in the project root:
     ```env
     DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_URL
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

### Discord Webhook Format

The dashboard sends embeds to Discord with the following structure:
- **Title**: New Trading Signal
- **Color coding**:
  - 🟢 Green for Entry signals
  - 🔴 Red for Stop Loss signals
  - 🔵 Cyan for Take Profit signals
- **Fields**: Ticker, Signal Type, Contract Information, Extra Information

### Environment Variables

```env
# Required - Discord webhook URL
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_URL

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

