# Digital Inventory & Sales Management System

A Flask + SQLite shop management system for products, customers, billing, invoices, sales reports and business settings.

## Main features
- Login and proprietor account
- Shop/business name, location and GST registration number
- Proprietor phone/email and password management
- Weekly sales report by email through SMTP
- Products with purchase/selling price, stock, minimum stock and units (pcs, kg, liters, dozens, boxes, etc.)
- Customer management and walk-in customer auto-save
- Billing with actual price, applied price and discounts
- Calculator in billing
- Printable invoices with shop details, proprietor details, location, GST, date and time
- Sales history and reports
- English, Kannada and Hindi UI
- SQLite database with safe migrations
- Database backup download from Business Settings

## Run
```powershell
python -m venv venv
.\\venv\\Scripts\\activate
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000

Default login:
- Email: admin@inventory.com
- Password: admin123

## Database backup
Use **Business Settings -> Database Backup** before replacing or updating the project. The live database is stored at `database/inventory.db`.


## Free deployment (Render + Neon + Resend)
The app uses SQLite automatically when `DATABASE_URL` is not set, so local development stays unchanged. For online deployment, set `DATABASE_URL` to a Neon PostgreSQL connection string. Set `RESEND_API_KEY` and `RESEND_FROM` for online email. The weekly report is triggered by the included GitHub Actions workflow so it does not depend on a free Render service staying awake.

### Render settings
Build: `pip install -r requirements.txt`
Start: `gunicorn app:app`

### Required Render environment variables
- `DATABASE_URL` = Neon PostgreSQL connection string
- `SECRET_KEY` = long random secret
- `RESEND_API_KEY` = Resend API key
- `RESEND_FROM` = verified sender, e.g. `Inventory System <onboarding@resend.dev>` for eligible test sending
- `CRON_SECRET` = random secret used by GitHub Actions

### GitHub Actions secrets
Add `APP_URL` = your Render URL and `CRON_SECRET` = the same value used on Render. The workflow runs every Monday and can also be started manually.

### Moving existing local data
Create the Neon database, set `DATABASE_URL` locally, install requirements, and run `python migrate_sqlite_to_postgres.py`. Never commit the `.db` file or API keys to GitHub.
