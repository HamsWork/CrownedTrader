"""
Quick start script for Crowned Trader Dashboard
"""
import os
import sys

def check_env_file():
    """Check if .env file exists"""
    if not os.path.exists('.env'):
        print("⚠️  No .env file found!")
        print("\nCreating .env file template...")
        
        bot_token = input("Enter your Discord bot token (or press Enter to skip): ").strip()
        channel_id = input("Enter your Discord channel ID (or press Enter to skip): ").strip()
        
        env_content = "# Discord Bot Configuration\n"
        env_content += f"DISCORD_BOT_TOKEN={bot_token}\n"
        env_content += f"DISCORD_CHANNEL_ID={channel_id}\n"
        env_content += "\n# Django Settings (optional)\n"
        env_content += "DEBUG=True\n"
        env_content += "# SECRET_KEY=your-secret-key-here\n"
        
        with open('.env', 'w') as f:
            f.write(env_content)
        
        if bot_token and channel_id:
            print("✅ .env file created with Discord bot configuration!")
        else:
            print("✅ .env file created. Don't forget to add your Discord bot token and channel ID!")
    else:
        print("✅ .env file found")

def main():
    """Run Django development server"""
    print("\n" + "="*50)
    print("  👑 Crowned Trader Dashboard")
    print("="*50 + "\n")
    
    check_env_file()
    
    print("\n🚀 Starting Django development server...\n")
    print("📍 Access the dashboard at: http://localhost:8000/")
    print("📍 Admin panel at: http://localhost:8000/admin/")
    print("📍 Network access at: http://0.0.0.0:8000/")
    print("\n" + "="*50 + "\n")
    
    # Run Django on all interfaces (0.0.0.0) to allow network access
    os.system('python manage.py runserver 0.0.0.0:8000')

if __name__ == '__main__':
    main()

