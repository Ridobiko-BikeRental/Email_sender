import os
import csv
import smtplib
import sqlite3
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from werkzeug.utils import secure_filename
import time
from threading import Thread
import logging
from datetime import datetime, date
import json
from dotenv import load_dotenv
from openpyxl import load_workbook
import html
from contextlib import contextmanager

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-change-this-in-production')

# Configuration
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'csv', 'xlsx', 'xls'}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
LOGS_FOLDER = 'logs'
DATABASE_PATH = 'email_system.db'
EMAIL_LIMIT_PER_ACCOUNT = 15  # Limit per account

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# Create folders if they don't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOGS_FOLDER, exist_ok=True)

# Set up logging
log_level = logging.DEBUG if app.debug else logging.INFO
logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOGS_FOLDER, 'email_logs.log')),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# Database context manager
@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# Initialize database
def init_database():
    """Initialize SQLite database with required tables"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Create email accounts table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                sent_count INTEGER DEFAULT 0,
                last_reset DATE DEFAULT CURRENT_DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create email logs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_filename TEXT NOT NULL,
                sender_email TEXT,
                sender_mode TEXT,
                total_emails INTEGER,
                sent_count INTEGER,
                failed_count INTEGER,
                duration_seconds REAL,
                subject TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                log_data TEXT
            )
        ''')
        
        # Create individual email status table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id INTEGER,
                recipient_email TEXT,
                sender_email TEXT,
                status TEXT,
                error_message TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (log_id) REFERENCES email_logs (id)
            )
        ''')
        
        conn.commit()
        logger.info("Database initialized successfully")

# Initialize database on startup
init_database()

# Global variable to store current email sending status
email_status = {
    'is_sending': False,
    'total_emails': 0,
    'sent_count': 0,
    'failed_count': 0,
    'current_email': '',
    'current_sender': '',
    'sender_rotation': {},
    'start_time': None,
    'failed_emails': [],
    'success_emails': []
}

# Database functions
def get_email_accounts():
    """Get all email accounts from database"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, email, password, is_active, sent_count, last_reset, created_at 
            FROM email_accounts 
            WHERE is_active = 1 
            ORDER BY created_at
        ''')
        return cursor.fetchall()

def add_email_account(email, password):
    """Add new email account to database"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO email_accounts (email, password) 
                VALUES (?, ?)
            ''', (email, password))
            conn.commit()
            logger.info(f"Added email account: {email}")
            return True, "Email account added successfully"
    except sqlite3.IntegrityError:
        return False, "Email account already exists"
    except Exception as e:
        logger.error(f"Error adding email account: {e}")
        return False, f"Error adding email account: {str(e)}"

def update_email_account(account_id, email, password, is_active):
    """Update email account in database"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE email_accounts 
                SET email = ?, password = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (email, password, is_active, account_id))
            conn.commit()
            logger.info(f"Updated email account ID: {account_id}")
            return True, "Email account updated successfully"
    except sqlite3.IntegrityError:
        return False, "Email address already exists"
    except Exception as e:
        logger.error(f"Error updating email account: {e}")
        return False, f"Error updating email account: {str(e)}"

def delete_email_account(account_id):
    """Delete email account from database"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM email_accounts WHERE id = ?', (account_id,))
            conn.commit()
            logger.info(f"Deleted email account ID: {account_id}")
            return True, "Email account deleted successfully"
    except Exception as e:
        logger.error(f"Error deleting email account: {e}")
        return False, f"Error deleting email account: {str(e)}"

def reset_daily_counts():
    """Reset email counts daily"""
    today = date.today()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE email_accounts 
            SET sent_count = 0, last_reset = ? 
            WHERE last_reset < ?
        ''', (today, today))
        
        if cursor.rowcount > 0:
            conn.commit()
            logger.info(f"Reset email count for {cursor.rowcount} accounts")

def get_available_sender():
    """Get next available sender account that hasn't reached the limit"""
    reset_daily_counts()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT email, password FROM email_accounts 
            WHERE is_active = 1 AND sent_count < ? 
            ORDER BY sent_count ASC, created_at ASC 
            LIMIT 1
        ''', (EMAIL_LIMIT_PER_ACCOUNT,))
        
        result = cursor.fetchone()
        if result:
            return result['email'], result['password']
        
        # If all accounts have reached the limit, return the first active one
        cursor.execute('''
            SELECT email, password FROM email_accounts 
            WHERE is_active = 1 
            ORDER BY created_at ASC 
            LIMIT 1
        ''')
        
        result = cursor.fetchone()
        if result:
            logger.warning(f"All accounts have reached daily limit. Using {result['email']}")
            return result['email'], result['password']
    
    return None, None

def get_account_stats():
    """Get statistics for all email accounts"""
    reset_daily_counts()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, email, sent_count, is_active, created_at
            FROM email_accounts 
            ORDER BY created_at
        ''')
        
        accounts = cursor.fetchall()
        stats = []
        
        for account in accounts:
            remaining = EMAIL_LIMIT_PER_ACCOUNT - account['sent_count'] if account['is_active'] else 0
            stats.append({
                'id': account['id'],
                'email': account['email'],
                'sent_today': account['sent_count'],
                'remaining': remaining,
                'limit': EMAIL_LIMIT_PER_ACCOUNT,
                'percentage_used': (account['sent_count'] / EMAIL_LIMIT_PER_ACCOUNT) * 100,
                'is_active': account['is_active'],
                'created_at': account['created_at']
            })
        
        return stats

def save_email_log(log_data, log_filename):
    """Save email log to database"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Insert main log entry
            cursor.execute('''
                INSERT INTO email_logs 
                (log_filename, sender_email, sender_mode, total_emails, sent_count, 
                 failed_count, duration_seconds, subject, log_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                log_filename,
                log_data.get('sender_email'),
                log_data.get('sender_mode', 'auto'),
                log_data.get('total_emails', 0),
                log_data.get('sent_count', 0),
                log_data.get('failed_count', 0),
                log_data.get('duration_seconds', 0),
                log_data.get('subject', ''),
                json.dumps(log_data)
            ))
            
            log_id = cursor.lastrowid
            
            # Insert individual email statuses
            for email in log_data.get('success_emails', []):
                cursor.execute('''
                    INSERT INTO email_status (log_id, recipient_email, sender_email, status)
                    VALUES (?, ?, ?, ?)
                ''', (log_id, email, log_data.get('sender_email'), 'success'))
            
            for failed_entry in log_data.get('failed_emails', []):
                if ':' in failed_entry:
                    email, error = failed_entry.split(':', 1)
                    cursor.execute('''
                        INSERT INTO email_status (log_id, recipient_email, sender_email, status, error_message)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (log_id, email.strip(), log_data.get('sender_email'), 'failed', error.strip()))
            
            conn.commit()
            logger.info(f"Saved email log to database: {log_filename}")
            
    except Exception as e:
        logger.error(f"Error saving email log to database: {e}")

class SimpleDataFrame:
    """Simple DataFrame-like class to replace pandas"""
    def __init__(self, data, columns):
        self.data = data
        self.columns = columns
    
    def head(self, n=5):
        return SimpleDataFrame(self.data[:n], self.columns)
    
    def to_dict(self, orient='records'):
        return self.data
    
    def iterrows(self):
        for i, row in enumerate(self.data):
            yield i, row
    
    def __len__(self):
        return len(self.data)
    
    def __getitem__(self, column):
        return [row.get(column, '') for row in self.data]

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def read_csv_file(filepath):
    """Read CSV file"""
    try:
        data = []
        with open(filepath, 'r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            columns = list(reader.fieldnames)
            for row in reader:
                data.append(dict(row))
        return SimpleDataFrame(data, columns)
    except Exception as e:
        logger.error(f"Error reading CSV file: {e}")
        return None

def read_excel_file(filepath):
    """Read Excel file using openpyxl"""
    try:
        workbook = load_workbook(filepath)
        sheet = workbook.active
        
        # Get column names from first row
        columns = [str(cell.value) for cell in sheet[1] if cell.value is not None]
        
        # Get data from remaining rows
        data = []
        for row in sheet.iter_rows(min_row=2, values_only=True):
            row_dict = {}
            for i, value in enumerate(row):
                if i < len(columns):
                    row_dict[columns[i]] = str(value) if value is not None else ""
            # Only add row if it has some data
            if any(value.strip() for value in row_dict.values() if value):
                data.append(row_dict)
        
        return SimpleDataFrame(data, columns)
    except Exception as e:
        logger.error(f"Error reading Excel file: {e}")
        return None

def read_file(filepath):
    """Read CSV or Excel file and return SimpleDataFrame"""
    try:
        if filepath.endswith('.csv'):
            return read_csv_file(filepath)
        elif filepath.endswith(('.xlsx', '.xls')):
            return read_excel_file(filepath)
        else:
            return None
    except Exception as e:
        logger.error(f"Error reading file: {e}")
        return None

def format_email_content(content):
    """Convert plain text to HTML while preserving formatting exactly as typed"""
    # Escape HTML characters first
    content = html.escape(content)
    
    # Normalize line endings
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    
    # Convert single line breaks to <br> and double line breaks to <br><br>
    # First, replace double line breaks with a unique placeholder
    content = content.replace('\n\n', '|||DOUBLE_BREAK|||')
    
    # Then replace single line breaks with <br>
    content = content.replace('\n', '<br>')
    
    # Finally, replace the placeholder with double <br>
    content = content.replace('|||DOUBLE_BREAK|||', '<br><br>')
    
    # Handle spacing
    content = content.replace('  ', '&nbsp;&nbsp;')
    content = content.replace('\t', '&nbsp;&nbsp;&nbsp;&nbsp;')
    
    # Wrap in a div with proper styling
    html_content = f'''
    <div style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                font-size: 14px; 
                line-height: 1.2; 
                color: #333333;
                margin: 0;
                padding: 0;">
        {content}
    </div>
    '''
    
    return html_content

def send_email_smtp(smtp_server, smtp_port, sender_email, sender_password, recipient_email, subject, body):
    """Send individual email via SMTP"""
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = sender_email
        msg['To'] = recipient_email
        msg['Subject'] = subject
        
        # Create plain text version (keep original formatting)
        text_part = MIMEText(body, 'plain', 'utf-8')
        
        # Create HTML version with preserved formatting
        formatted_body = format_email_content(body)
        html_part = MIMEText(formatted_body, 'html', 'utf-8')
        
        # Attach parts to message (order matters - plain text first, then HTML)
        msg.attach(text_part)
        msg.attach(html_part)
        
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        text = msg.as_string()
        server.sendmail(sender_email, recipient_email, text)
        server.quit()
        
        # Update the account's sent count in database
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE email_accounts 
                SET sent_count = sent_count + 1 
                WHERE email = ?
            ''', (sender_email,))
            conn.commit()
        
        logger.info(f"Email sent successfully to {recipient_email} from {sender_email}")
        return True, "Email sent successfully"
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed to send email to {recipient_email} from {sender_email}: {error_msg}")
        return False, error_msg

def send_bulk_emails(file_path, subject, template, email_column, delay=1):
    """Send bulk emails in background with automatic sender rotation"""
    global email_status
    
    # Check if we have any email accounts configured
    accounts = get_email_accounts()
    if not accounts:
        return False, "No email accounts configured. Please add email accounts first."
    
    # Reset status
    email_status.update({
        'is_sending': True,
        'total_emails': 0,
        'sent_count': 0,
        'failed_count': 0,
        'current_email': '',
        'current_sender': '',
        'sender_rotation': {},
        'start_time': datetime.now(),
        'failed_emails': [],
        'success_emails': []
    })
    
    df = read_file(file_path)
    if df is None:
        email_status['is_sending'] = False
        return False, "Failed to read file"
    
    if email_column not in df.columns:
        email_status['is_sending'] = False
        return False, f"Column '{email_column}' not found in file"
    
    # Filter out empty emails and count total
    valid_emails = [row for row in df.data if row.get(email_column, '').strip()]
    email_status['total_emails'] = len(valid_emails)
    
    logger.info(f"Starting bulk email sending to {email_status['total_emails']} recipients")
    
    smtp_server = "smtp.gmail.com"
    smtp_port = 587
    
    current_sender = None
    current_password = None
    emails_sent_with_current = 0
    
    for index, row in enumerate(valid_emails):
        recipient_email = row.get(email_column, '').strip()
        email_status['current_email'] = recipient_email
        
        # Get next available sender if current one reached limit or is None
        if (current_sender is None or emails_sent_with_current >= EMAIL_LIMIT_PER_ACCOUNT):
            current_sender, current_password = get_available_sender()
            emails_sent_with_current = 0
            
            if current_sender is None:
                logger.error("No available sender accounts")
                break
        
        email_status['current_sender'] = current_sender
        
        # Replace placeholders in template with row data
        personalized_message = template
        for col in df.columns:
            placeholder = f"{{{col}}}"
            if placeholder in personalized_message:
                value = row.get(col, "")
                personalized_message = personalized_message.replace(placeholder, str(value))
        
        success, error_msg = send_email_smtp(
            smtp_server, smtp_port, current_sender, current_password,
            recipient_email, subject, personalized_message
        )
        
        if success:
            email_status['sent_count'] += 1
            email_status['success_emails'].append(recipient_email)
            emails_sent_with_current += 1
            
            # Track which sender sent to which recipients
            if current_sender not in email_status['sender_rotation']:
                email_status['sender_rotation'][current_sender] = 0
            email_status['sender_rotation'][current_sender] += 1
        else:
            email_status['failed_count'] += 1
            email_status['failed_emails'].append(f"{recipient_email}: {error_msg}")
        
        # Add delay between emails to avoid being flagged as spam
        time.sleep(delay)
    
    # Email sending completed
    email_status['is_sending'] = False
    end_time = datetime.now()
    duration = end_time - email_status['start_time']
    
    # Save detailed log to file and database
    log_data = {
        'timestamp': end_time.isoformat(),
        'duration_seconds': duration.total_seconds(),
        'total_emails': email_status['total_emails'],
        'sent_count': email_status['sent_count'],
        'failed_count': email_status['failed_count'],
        'success_emails': email_status['success_emails'],
        'failed_emails': email_status['failed_emails'],
        'subject': subject,
        'sender_mode': 'auto',
        'sender_rotation': email_status['sender_rotation'],
        'account_stats': get_account_stats()
    }
    
    log_filename = f"bulk_email_log_{end_time.strftime('%Y%m%d_%H%M%S')}.json"
    
    # Save to file
    with open(os.path.join(LOGS_FOLDER, log_filename), 'w') as f:
        json.dump(log_data, f, indent=2)
    
    # Save to database
    save_email_log(log_data, log_filename)
    
    logger.info(f"Bulk email sending completed. {email_status['sent_count']} sent, {email_status['failed_count']} failed. Duration: {duration}")
    
    return True, {
        'success_count': email_status['sent_count'],
        'failed_count': email_status['failed_count'],
        'failed_emails': email_status['failed_emails'],
        'duration': str(duration),
        'log_file': log_filename,
        'sender_rotation': email_status['sender_rotation']
    }

def send_bulk_emails_single_sender(file_path, sender_email, subject, template, email_column, delay=1):
    """Send bulk emails using a single specific sender"""
    global email_status
    
    # Check if sender exists and has quota
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT email, password, sent_count, is_active 
            FROM email_accounts 
            WHERE email = ? AND is_active = 1
        ''', (sender_email,))
        
        account = cursor.fetchone()
        if not account:
            return False, "Selected sender account not found or inactive"
        
        reset_daily_counts()
        if account['sent_count'] >= EMAIL_LIMIT_PER_ACCOUNT:
            return False, f"Selected account has reached daily limit of {EMAIL_LIMIT_PER_ACCOUNT} emails"
        
        sender_password = account['password']
    
    # Reset status
    email_status.update({
        'is_sending': True,
        'total_emails': 0,
        'sent_count': 0,
        'failed_count': 0,
        'current_email': '',
        'current_sender': sender_email,
        'sender_rotation': {sender_email: 0},
        'start_time': datetime.now(),
        'failed_emails': [],
        'success_emails': []
    })
    
    df = read_file(file_path)
    if df is None:
        email_status['is_sending'] = False
        return False, "Failed to read file"
    
    if email_column not in df.columns:
        email_status['is_sending'] = False
        return False, f"Column '{email_column}' not found in file"
    
    # Filter out empty emails and count total
    valid_emails = [row for row in df.data if row.get(email_column, '').strip()]
    email_status['total_emails'] = len(valid_emails)
    
    # Check if we can send all emails with this account
    remaining_quota = EMAIL_LIMIT_PER_ACCOUNT - account['sent_count']
    if len(valid_emails) > remaining_quota:
        email_status['is_sending'] = False
        return False, f"Cannot send {len(valid_emails)} emails. Account {sender_email} has only {remaining_quota} emails remaining today."
    
    logger.info(f"Starting bulk email sending to {email_status['total_emails']} recipients from {sender_email}")
    
    smtp_server = "smtp.gmail.com"
    smtp_port = 587
    
    for index, row in enumerate(valid_emails):
        recipient_email = row.get(email_column, '').strip()
        email_status['current_email'] = recipient_email
        
        # Replace placeholders in template with row data
        personalized_message = template
        for col in df.columns:
            placeholder = f"{{{col}}}"
            if placeholder in personalized_message:
                value = row.get(col, "")
                personalized_message = personalized_message.replace(placeholder, str(value))
        
        success, error_msg = send_email_smtp(
            smtp_server, smtp_port, sender_email, sender_password,
            recipient_email, subject, personalized_message
        )
        
        if success:
            email_status['sent_count'] += 1
            email_status['success_emails'].append(recipient_email)
            email_status['sender_rotation'][sender_email] += 1
        else:
            email_status['failed_count'] += 1
            email_status['failed_emails'].append(f"{recipient_email}: {error_msg}")
        
        # Add delay between emails to avoid being flagged as spam
        time.sleep(delay)
    
    # Email sending completed
    email_status['is_sending'] = False
    end_time = datetime.now()
    duration = end_time - email_status['start_time']
    
    # Save detailed log to file and database
    log_data = {
        'timestamp': end_time.isoformat(),
        'duration_seconds': duration.total_seconds(),
        'total_emails': email_status['total_emails'],
        'sent_count': email_status['sent_count'],
        'failed_count': email_status['failed_count'],
        'success_emails': email_status['success_emails'],
        'failed_emails': email_status['failed_emails'],
        'subject': subject,
        'sender_email': sender_email,
        'sender_mode': 'manual',
        'sender_rotation': email_status['sender_rotation'],
        'account_stats': get_account_stats()
    }
    
    log_filename = f"bulk_email_log_{end_time.strftime('%Y%m%d_%H%M%S')}.json"
    
    # Save to file
    with open(os.path.join(LOGS_FOLDER, log_filename), 'w') as f:
        json.dump(log_data, f, indent=2)
    
    # Save to database
    save_email_log(log_data, log_filename)
    
    logger.info(f"Bulk email sending completed from {sender_email}. {email_status['sent_count']} sent, {email_status['failed_count']} failed. Duration: {duration}")
    
    return True, {
        'success_count': email_status['sent_count'],
        'failed_count': email_status['failed_count'],
        'failed_emails': email_status['failed_emails'],
        'duration': str(duration),
        'log_file': log_filename,
        'sender_rotation': email_status['sender_rotation']
    }

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        flash('No file selected')
        return redirect(url_for('index'))
    
    file = request.files['file']
    if file.filename == '':
        flash('No file selected')
        return redirect(url_for('index'))
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Read file to get columns
        df = read_file(filepath)
        if df is not None:
            columns = list(df.columns)
            sample_data = df.head().to_dict('records')
            account_stats = get_account_stats()
            
            return render_template('compose.html', 
                                 filename=filename, 
                                 columns=columns,
                                 sample_data=sample_data,
                                 account_stats=account_stats)
        else:
            flash('Failed to read the uploaded file')
            return redirect(url_for('index'))
    else:
        flash('Invalid file type. Please upload CSV or Excel files only.')
        return redirect(url_for('index'))

@app.route('/send_emails', methods=['POST'])
def send_emails():
    filename = request.form.get('filename')
    sender_mode = request.form.get('sender_mode')
    selected_sender = request.form.get('selected_sender')
    subject = request.form.get('subject')
    template = request.form.get('template')
    email_column = request.form.get('email_column')
    delay = int(request.form.get('delay', 1))
    
    # Debug logging
    logger.info(f"Form data received - filename: {filename}, sender_mode: {sender_mode}, selected_sender: '{selected_sender}', subject: {subject}, template length: {len(template) if template else 0}, email_column: {email_column}")
    
    # Validate all required fields
    if not all([filename, sender_mode, subject, template, email_column]):
        flash('All fields are required.')
        logger.error("Missing required fields")
        return redirect(url_for('index'))
    
    # Clean up selected_sender - convert empty string to None
    if selected_sender == '' or selected_sender is None:
        selected_sender = None
    
    # Validate sender selection for manual mode
    if sender_mode == 'manual':
        if not selected_sender:
            flash('Please select an email account for manual mode.')
            logger.error("Manual mode selected but no sender specified")
            return redirect(url_for('index'))
        
        # Validate that the selected sender exists in database
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM email_accounts WHERE email = ? AND is_active = 1', (selected_sender,))
            if not cursor.fetchone():
                flash(f'Selected email account "{selected_sender}" is not configured or inactive.')
                logger.error(f"Selected sender {selected_sender} not found in database")
                return redirect(url_for('index'))
    
    # Check if we have any email accounts
    accounts = get_email_accounts()
    if not accounts:
        flash('No email accounts configured. Please add email accounts first.')
        logger.error("No email accounts configured")
        return redirect(url_for('manage_accounts'))
    
    # Check if selected sender has remaining quota
    if sender_mode == 'manual' and selected_sender:
        reset_daily_counts()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT sent_count FROM email_accounts WHERE email = ?', (selected_sender,))
            account = cursor.fetchone()
            if account and account['sent_count'] >= EMAIL_LIMIT_PER_ACCOUNT:
                flash(f'Selected account {selected_sender} has reached its daily limit of {EMAIL_LIMIT_PER_ACCOUNT} emails.')
                logger.warning(f"Account {selected_sender} has reached daily limit")
                return redirect(url_for('index'))
    
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    # Validate that the file exists
    if not os.path.exists(filepath):
        flash('Uploaded file not found. Please upload the file again.')
        logger.error(f"File not found: {filepath}")
        return redirect(url_for('index'))
    
    # Start sending emails in background thread
    def send_emails_task():
        try:
            if sender_mode == 'auto':
                logger.info("Starting auto-rotate email sending")
                success, result = send_bulk_emails(
                    filepath, subject, template, email_column, delay
                )
            else:  # manual mode
                logger.info(f"Starting manual email sending with sender: {selected_sender}")
                success, result = send_bulk_emails_single_sender(
                    filepath, selected_sender, subject, template, email_column, delay
                )
            
            if success:
                flash(f"Bulk email sending completed! {result['success_count']} sent, {result['failed_count']} failed in {result['duration']}")
                if result.get('sender_rotation'):
                    rotation_info = ", ".join([f"{email}: {count}" for email, count in result['sender_rotation'].items()])
                    flash(f"Sender distribution: {rotation_info}")
                if result['failed_emails']:
                    flash(f"Failed emails: {', '.join(result['failed_emails'][:5])}")
                logger.info(f"Bulk email sending completed successfully: {result}")
            else:
                flash(f"Error: {result}")
                logger.error(f"Bulk email sending failed: {result}")
        except Exception as e:
            error_msg = f"Error in email sending task: {str(e)}"
            flash(error_msg)
            logger.error(error_msg, exc_info=True)
    
    thread = Thread(target=send_emails_task)
    thread.daemon = True
    thread.start()
    
    flash('Email sending started in background. Check the status page for real-time updates.')
    logger.info("Email sending thread started successfully")
    return redirect(url_for('status'))

@app.route('/manage_accounts')
def manage_accounts():
    """Display email account management page"""
    account_stats = get_account_stats()
    return render_template('manage_accounts.html', account_stats=account_stats)

@app.route('/add_account', methods=['POST'])
def add_account():
    """Add new email account"""
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '').strip()
    
    if not email or not password:
        flash('Email and password are required.')
        return redirect(url_for('manage_accounts'))
    
    success, message = add_email_account(email, password)
    flash(message)
    return redirect(url_for('manage_accounts'))

@app.route('/update_account/<int:account_id>', methods=['POST'])
def update_account(account_id):
    """Update email account"""
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '').strip()
    is_active = request.form.get('is_active') == 'on'
    
    if not email or not password:
        flash('Email and password are required.')
        return redirect(url_for('manage_accounts'))
    
    success, message = update_email_account(account_id, email, password, is_active)
    flash(message)
    return redirect(url_for('manage_accounts'))

@app.route('/delete_account/<int:account_id>', methods=['POST'])
def delete_account(account_id):
    """Delete email account"""
    success, message = delete_email_account(account_id)
    flash(message)
    return redirect(url_for('manage_accounts'))

@app.route('/status')
def status():
    """Display real-time email sending status"""
    account_stats = get_account_stats()
    return render_template('status.html', status=email_status, account_stats=account_stats)

@app.route('/api/status')
def api_status():
    """API endpoint for real-time status updates"""
    status_data = email_status.copy()
    status_data['account_stats'] = get_account_stats()
    return jsonify(status_data)

@app.route('/logs')
def logs():
    """Display email sending logs from database"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT el.*, 
                   COUNT(es.id) as total_recipients,
                   SUM(CASE WHEN es.status = 'success' THEN 1 ELSE 0 END) as successful_sends,
                   SUM(CASE WHEN es.status = 'failed' THEN 1 ELSE 0 END) as failed_sends
            FROM email_logs el
            LEFT JOIN email_status es ON el.id = es.log_id
            GROUP BY el.id
            ORDER BY el.created_at DESC
            LIMIT 50
        ''')
        
        logs = []
        for row in cursor.fetchall():
            log_data = {
                'id': row['id'],
                'log_filename': row['log_filename'],
                'sender_email': row['sender_email'],
                'sender_mode': row['sender_mode'],
                'total_emails': row['total_emails'],
                'sent_count': row['sent_count'],
                'failed_count': row['failed_count'],
                'duration_seconds': row['duration_seconds'],
                'subject': row['subject'],
                'created_at': row['created_at'],
                'total_recipients': row['total_recipients'] or 0,
                'successful_sends': row['successful_sends'] or 0,
                'failed_sends': row['failed_sends'] or 0
            }
            logs.append(log_data)
    
    return render_template('logs.html', logs=logs)

@app.route('/log_details/<int:log_id>')
def log_details(log_id):
    """Display detailed log information"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Get log info
        cursor.execute('SELECT * FROM email_logs WHERE id = ?', (log_id,))
        log = cursor.fetchone()
        
        if not log:
            flash('Log not found.')
            return redirect(url_for('logs'))
        
        # Get email statuses
        cursor.execute('''
            SELECT recipient_email, sender_email, status, error_message, sent_at
            FROM email_status 
            WHERE log_id = ?
            ORDER BY sent_at
        ''', (log_id,))
        
        email_statuses = cursor.fetchall()
    
    return render_template('log_details.html', log=log, email_statuses=email_statuses)

# Health check endpoint for Render
@app.route('/health')
def health_check():
    """Health check endpoint for deployment platforms"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)