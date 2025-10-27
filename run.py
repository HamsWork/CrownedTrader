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
        
        webhook_url = input("Enter your Discord webhook URL (or press Enter to skip): ").strip()
        
        env_content = "# Discord Webhook Configuration\n"
        env_content += f"DISCORD_WEBHOOK_URL={webhook_url}\n"
        env_content += "\n# Django Settings (optional)\n"
        env_content += "DEBUG=True\n"
        env_content += "# SECRET_KEY=your-secret-key-here\n"
        
        with open('.env', 'w') as f:
            f.write(env_content)
        
        if webhook_url:
            print("✅ .env file created with Discord webhook URL!")
        else:
            print("✅ .env file created. Don't forget to add your Discord webhook URL!")
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
    print("\n" + "="*50 + "\n")
    
    # Run Django
    os.system('python manage.py runserver')

if __name__ == '__main__':
    main()

