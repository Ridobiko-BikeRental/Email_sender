import os
import csv
import smtplib
import sqlite3
import time
import logging
import json
import html
import random
import string
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email import encoders
from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from threading import Thread
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from openpyxl import load_workbook
from contextlib import contextmanager
from functools import wraps

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-change-this-in-production')

# Configuration
UPLOAD_FOLDER = 'uploads'
ATTACHMENTS_FOLDER = 'attachments'
ALLOWED_EXTENSIONS = {'csv', 'xlsx', 'xls'}
ALLOWED_ATTACHMENT_EXTENSIONS = {'pdf', 'doc', 'docx', 'txt', 'jpg', 'jpeg', 'png', 'gif'}
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB max file size
LOGS_FOLDER = 'logs'
DATABASE_PATH = 'email_system.db'
EMAIL_LIMIT_PER_ACCOUNT = 15  # Limit per account

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['ATTACHMENTS_FOLDER'] = ATTACHMENTS_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# Create folders if they don't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ATTACHMENTS_FOLDER, exist_ok=True)
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

# Authentication decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Initialize database
def init_database():
    """Initialize SQLite database with required tables"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Create users table (removed username, email is now primary identifier)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name TEXT NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            )
        ''')
        
        # Create email accounts table with user_id foreign key
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                password TEXT NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                sent_count INTEGER DEFAULT 0,
                last_reset DATE DEFAULT CURRENT_DATE,
                default_cc TEXT DEFAULT '',
                default_bcc TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id),
                UNIQUE(user_id, email)
            )
        ''')
        
        # Add user_id column if it doesn't exist (for existing databases)
        try:
            cursor.execute('ALTER TABLE email_accounts ADD COLUMN user_id INTEGER')
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        # Add default_cc and default_bcc columns if they don't exist
        try:
            cursor.execute('ALTER TABLE email_accounts ADD COLUMN default_cc TEXT DEFAULT ""')
        except sqlite3.OperationalError:
            pass
        
        try:
            cursor.execute('ALTER TABLE email_accounts ADD COLUMN default_bcc TEXT DEFAULT ""')
        except sqlite3.OperationalError:
            pass
        
        # Create email logs table with user_id
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                log_filename TEXT NOT NULL,
                sender_email TEXT,
                sender_mode TEXT,
                total_emails INTEGER,
                sent_count INTEGER,
                failed_count INTEGER,
                duration_seconds REAL,
                subject TEXT,
                cc_emails TEXT,
                bcc_emails TEXT,
                attachment_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                log_data TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        
        # Add user_id column to email_logs if it doesn't exist
        try:
            cursor.execute('ALTER TABLE email_logs ADD COLUMN user_id INTEGER')
        except sqlite3.OperationalError:
            pass
        
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

# Global variable to store current email sending status (per user)
email_status = {}

def get_user_email_status(user_id):
    """Get or create email status for a specific user"""
    if user_id not in email_status:
        email_status[user_id] = {
            'is_sending': False,
            'total_emails': 0,
            'sent_count': 0,
            'failed_count': 0,
            'current_email': '',
            'current_sender': '',
            'sender_rotation': {},
            'start_time': None,
            'failed_emails': [],
            'success_emails': [],
            'attachment_name': None,
            'cc_emails': [],
            'bcc_emails': []
        }
    return email_status[user_id]

# User management functions
def create_user(email, password, full_name):
    """Create a new user (removed username parameter)"""
    try:
        password_hash = generate_password_hash(password)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO users (email, password_hash, full_name)
                VALUES (?, ?, ?)
            ''', (email, password_hash, full_name))
            conn.commit()
            user_id = cursor.lastrowid
            logger.info(f"Created user: {email}")
            return True, user_id
    except sqlite3.IntegrityError:
        return False, "Email address already exists"
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        return False, f"Error creating user: {str(e)}"

def authenticate_user(email, password):
    """Authenticate user login (changed from username to email)"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, email, password_hash, full_name, is_active
                FROM users 
                WHERE email = ? AND is_active = 1
            ''', (email,))
            user = cursor.fetchone()
            
            if user and check_password_hash(user['password_hash'], password):
                # Update last login
                cursor.execute('''
                    UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?
                ''', (user['id'],))
                conn.commit()
                return True, dict(user)
            return False, "Invalid email or password"
    except Exception as e:
        logger.error(f"Error authenticating user: {e}")
        return False, "Authentication error"

def get_user_by_id(user_id):
    """Get user by ID (removed username from query)"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, email, full_name, created_at, last_login
            FROM users 
            WHERE id = ? AND is_active = 1
        ''', (user_id,))
        return cursor.fetchone()

# Database functions (updated with user_id)
def get_email_accounts(user_id):
    """Get all email accounts for a specific user"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, email, password, is_active, sent_count, last_reset, 
                   default_cc, default_bcc, created_at 
            FROM email_accounts 
            WHERE user_id = ? AND is_active = 1 
            ORDER BY created_at
        ''', (user_id,))
        return cursor.fetchall()

def get_all_email_accounts(user_id):
    """Get all email accounts for a user including inactive ones"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, email, password, is_active, sent_count, last_reset, 
                   default_cc, default_bcc, created_at 
            FROM email_accounts 
            WHERE user_id = ?
            ORDER BY created_at
        ''', (user_id,))
        return cursor.fetchall()

def add_email_account(user_id, email, password, default_cc='', default_bcc=''):
    """Add new email account to database for a specific user"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO email_accounts (user_id, email, password, default_cc, default_bcc) 
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, email, password, default_cc, default_bcc))
            conn.commit()
            logger.info(f"Added email account: {email} for user {user_id}")
            return True, "Email account added successfully"
    except sqlite3.IntegrityError:
        return False, "Email account already exists for this user"
    except Exception as e:
        logger.error(f"Error adding email account: {e}")
        return False, f"Error adding email account: {str(e)}"

def update_email_account(user_id, account_id, email, password, is_active, default_cc='', default_bcc=''):
    """Update email account for a specific user"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE email_accounts 
                SET email = ?, password = ?, is_active = ?, default_cc = ?, default_bcc = ?, 
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
            ''', (email, password, is_active, default_cc, default_bcc, account_id, user_id))
            conn.commit()
            if cursor.rowcount > 0:
                logger.info(f"Updated email account ID: {account_id} for user {user_id}")
                return True, "Email account updated successfully"
            else:
                return False, "Email account not found or access denied"
    except sqlite3.IntegrityError:
        return False, "Email address already exists"
    except Exception as e:
        logger.error(f"Error updating email account: {e}")
        return False, f"Error updating email account: {str(e)}"

def delete_email_account(user_id, account_id):
    """Delete email account for a specific user"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM email_accounts WHERE id = ? AND user_id = ?', (account_id, user_id))
            conn.commit()
            if cursor.rowcount > 0:
                logger.info(f"Deleted email account ID: {account_id} for user {user_id}")
                return True, "Email account deleted successfully"
            else:
                return False, "Email account not found or access denied"
    except Exception as e:
        logger.error(f"Error deleting email account: {e}")
        return False, f"Error deleting email account: {str(e)}"

def get_account_default_cc_bcc(user_id, sender_email):
    """Get default CC and BCC for a specific account and user"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT default_cc, default_bcc 
            FROM email_accounts 
            WHERE user_id = ? AND email = ? AND is_active = 1
        ''', (user_id, sender_email))
        result = cursor.fetchone()
        if result:
            return result['default_cc'] or '', result['default_bcc'] or ''
        return '', ''

def reset_daily_counts(user_id):
    """Reset email counts daily for a specific user"""
    today = date.today()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE email_accounts 
            SET sent_count = 0, last_reset = ? 
            WHERE user_id = ? AND last_reset < ?
        ''', (today, user_id, today))
        
        if cursor.rowcount > 0:
            conn.commit()
            logger.info(f"Reset email count for {cursor.rowcount} accounts for user {user_id}")

def get_available_sender(user_id):
    """Get next available sender account for a specific user"""
    reset_daily_counts(user_id)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Get the most up-to-date count for all active accounts
        cursor.execute('''
            SELECT email, password, default_cc, default_bcc, sent_count 
            FROM email_accounts 
            WHERE user_id = ? AND is_active = 1 AND sent_count < ? 
            ORDER BY sent_count ASC, created_at ASC 
            LIMIT 1
        ''', (user_id, EMAIL_LIMIT_PER_ACCOUNT))
        
        result = cursor.fetchone()
        if result:
            return result['email'], result['password'], result['default_cc'] or '', result['default_bcc'] or ''
        
        # If no accounts have quota remaining, log the issue
        cursor.execute('''
            SELECT COUNT(*) as total_accounts, 
                   SUM(CASE WHEN sent_count >= ? THEN 1 ELSE 0 END) as maxed_accounts
            FROM email_accounts 
            WHERE user_id = ? AND is_active = 1
        ''', (EMAIL_LIMIT_PER_ACCOUNT, user_id))
        
        stats = cursor.fetchone()
        if stats['total_accounts'] > 0:
            logger.warning(f"All {stats['total_accounts']} accounts for user {user_id} have reached daily limit of {EMAIL_LIMIT_PER_ACCOUNT} emails")
        else:
            logger.error(f"No active email accounts found for user {user_id}")
    
    return None, None, None, None

def check_account_quota(user_id, sender_email):
    """Check if an account has remaining quota"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT sent_count FROM email_accounts 
            WHERE user_id = ? AND email = ? AND is_active = 1
        ''', (user_id, sender_email))
        
        result = cursor.fetchone()
        if result:
            return result['sent_count'] < EMAIL_LIMIT_PER_ACCOUNT, result['sent_count']
        return False, 0

def get_account_stats(user_id):
    """Get statistics for all email accounts for a specific user"""
    reset_daily_counts(user_id)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, email, sent_count, is_active, default_cc, default_bcc, created_at
            FROM email_accounts 
            WHERE user_id = ?
            ORDER BY created_at
        ''', (user_id,))
        
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
                'default_cc': account['default_cc'] or '',
                'default_bcc': account['default_bcc'] or '',
                'created_at': account['created_at']
            })
        
        return stats

def save_email_log(user_id, log_data, log_filename):
    """Save email log to database for a specific user"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Insert main log entry
            cursor.execute('''
                INSERT INTO email_logs 
                (user_id, log_filename, sender_email, sender_mode, total_emails, sent_count, 
                 failed_count, duration_seconds, subject, cc_emails, bcc_emails, 
                 attachment_name, log_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                log_filename,
                log_data.get('sender_email'),
                log_data.get('sender_mode', 'auto'),
                log_data.get('total_emails', 0),
                log_data.get('sent_count', 0),
                log_data.get('failed_count', 0),
                log_data.get('duration_seconds', 0),
                log_data.get('subject', ''),
                ', '.join(log_data.get('cc_emails', [])),
                ', '.join(log_data.get('bcc_emails', [])),
                log_data.get('attachment_name', ''),
                json.dumps(log_data)
            ))
            
            log_id = cursor.lastrowid
            
            # Insert individual email statuses
            for email in log_data.get('success_emails', []):
                cursor.execute('''
                    INSERT INTO email_status 
                    (log_id, recipient_email, sender_email, status, sent_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (log_id, email, log_data.get('sender_email'), 'success', datetime.now()))
            
            for failed_entry in log_data.get('failed_emails', []):
                if ':' in failed_entry:
                    recipient, error = failed_entry.split(':', 1)
                    cursor.execute('''
                        INSERT INTO email_status 
                        (log_id, recipient_email, sender_email, status, error_message, sent_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (log_id, recipient.strip(), log_data.get('sender_email'), 'failed', error.strip(), datetime.now()))
            
            conn.commit()
            logger.info(f"Saved email log to database: {log_filename} for user {user_id}")
            
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

def allowed_attachment_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_ATTACHMENT_EXTENSIONS

def read_csv_file(filepath):
    """Read CSV file"""
    try:
        data = []
        with open(filepath, 'r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            columns = list(reader.fieldnames)
            for row in reader:
                clean_row = {key: str(value).strip() if value is not None else '' for key, value in row.items()}
                data.append(clean_row)
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
                    row_dict[columns[i]] = str(value).strip() if value is not None else ''
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

def parse_email_list(email_string):
    """Parse comma-separated email addresses"""
    if not email_string:
        return []
    
    emails = [email.strip() for email in email_string.split(',') if email.strip()]
    return emails

def merge_cc_bcc_lists(form_cc, form_bcc, default_cc, default_bcc):
    """Merge form CC/BCC with default CC/BCC, removing duplicates"""
    # Parse all email lists
    form_cc_list = parse_email_list(form_cc) if form_cc else []
    form_bcc_list = parse_email_list(form_bcc) if form_bcc else []
    default_cc_list = parse_email_list(default_cc) if default_cc else []
    default_bcc_list = parse_email_list(default_bcc) if default_bcc else []
    
    # Merge and remove duplicates while preserving order
    merged_cc = list(dict.fromkeys(form_cc_list + default_cc_list))
    merged_bcc = list(dict.fromkeys(form_bcc_list + default_bcc_list))
    
    return merged_cc, merged_bcc

def send_email_smtp(smtp_server, smtp_port, sender_email, sender_password, recipient_email, subject, body, user_id, cc_emails=None, bcc_emails=None, attachment_path=None):
    """Send individual email via SMTP with CC, BCC, and attachment support"""
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = sender_email
        msg['To'] = recipient_email
        msg['Subject'] = subject
        
        # Add CC recipients
        if cc_emails:
            msg['CC'] = ', '.join(cc_emails)
        
        # BCC is handled in sendmail, not in headers
        
        # Create plain text version (keep original formatting)
        text_part = MIMEText(body, 'plain', 'utf-8')
        
        # Create HTML version with preserved formatting
        formatted_body = format_email_content(body)
        html_part = MIMEText(formatted_body, 'html', 'utf-8')
        
        # Attach parts to message (order matters - plain text first, then HTML)
        msg.attach(text_part)
        msg.attach(html_part)
        
        # Add attachment if provided
        if attachment_path and os.path.exists(attachment_path):
            try:
                with open(attachment_path, 'rb') as f:
                    attachment_data = f.read()
                
                filename = os.path.basename(attachment_path)
                
                # Determine MIME type based on file extension
                if filename.lower().endswith('.pdf'):
                    attachment = MIMEApplication(attachment_data, _subtype='pdf')
                else:
                    attachment = MIMEBase('application', 'octet-stream')
                    attachment.set_payload(attachment_data)
                    encoders.encode_base64(attachment)
                
                attachment.add_header('Content-Disposition', f'attachment; filename={filename}')
                msg.attach(attachment)
                logger.info(f"Attached file: {filename}")
                
            except Exception as e:
                logger.warning(f"Failed to attach file {attachment_path}: {e}")
        
        # Prepare recipient list (To + CC + BCC)
        all_recipients = [recipient_email]
        if cc_emails:
            all_recipients.extend(cc_emails)
        if bcc_emails:
            all_recipients.extend(bcc_emails)
        
        # Send email
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        text = msg.as_string()
        server.sendmail(sender_email, all_recipients, text)
        server.quit()
        
        # Update the account's sent count in database - FIXED: Now includes user_id for proper isolation
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE email_accounts 
                SET sent_count = sent_count + 1 
                WHERE email = ? AND user_id = ?
            ''', (sender_email, user_id))
            conn.commit()
        
        logger.info(f"Email sent successfully to {recipient_email} from {sender_email} for user {user_id}")
        return True, "Email sent successfully"
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed to send email to {recipient_email} from {sender_email}: {error_msg}")
        return False, error_msg