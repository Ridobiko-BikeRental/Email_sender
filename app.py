import os
import csv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from werkzeug.utils import secure_filename
import time
from threading import Thread
import logging
from datetime import datetime
import json
from dotenv import load_dotenv
from openpyxl import load_workbook
import html

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-change-this-in-production')

# Configuration
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'csv', 'xlsx', 'xls'}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
LOGS_FOLDER = 'logs'
EMAIL_LIMIT_PER_ACCOUNT = 15  # Limit per account

# Load multiple email configurations from environment
def load_email_accounts():
    """Load all email accounts from environment variables"""
    email_accounts = {}
    for i in range(1, 11):  # Support up to 10 accounts
        email = os.getenv(f'SENDER_EMAIL_{i}')
        password = os.getenv(f'SENDER_APP_PASSWORD_{i}')
        if email and password:
            email_accounts[email] = {
                'password': password,
                'sent_count': 0,  # Track emails sent from this account
                'last_reset': datetime.now().date()  # Track when count was last reset
            }
    return email_accounts

# Global email accounts configuration
EMAIL_ACCOUNTS = load_email_accounts()

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

# Global variable to store current email sending status
email_status = {
    'is_sending': False,
    'total_emails': 0,
    'sent_count': 0,
    'failed_count': 0,
    'current_email': '',
    'current_sender': '',
    'sender_rotation': {},  # Track which sender is being used
    'start_time': None,
    'failed_emails': [],
    'success_emails': []
}

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

def reset_daily_counts():
    """Reset email counts daily"""
    global EMAIL_ACCOUNTS
    today = datetime.now().date()
    
    for email, account_info in EMAIL_ACCOUNTS.items():
        if account_info['last_reset'] < today:
            account_info['sent_count'] = 0
            account_info['last_reset'] = today
            logger.info(f"Reset email count for {email}")

def get_available_sender():
    """Get next available sender account that hasn't reached the limit"""
    reset_daily_counts()  # Reset counts if it's a new day
    
    for email, account_info in EMAIL_ACCOUNTS.items():
        if account_info['sent_count'] < EMAIL_LIMIT_PER_ACCOUNT:
            return email, account_info['password']
    
    # If all accounts have reached the limit, return the first one with a warning
    if EMAIL_ACCOUNTS:
        first_email = list(EMAIL_ACCOUNTS.keys())[0]
        logger.warning(f"All accounts have reached daily limit. Using {first_email}")
        return first_email, EMAIL_ACCOUNTS[first_email]['password']
    
    return None, None

def get_account_stats():
    """Get statistics for all email accounts"""
    reset_daily_counts()
    stats = []
    for email, account_info in EMAIL_ACCOUNTS.items():
        remaining = EMAIL_LIMIT_PER_ACCOUNT - account_info['sent_count']
        stats.append({
            'email': email,
            'sent_today': account_info['sent_count'],
            'remaining': remaining,
            'limit': EMAIL_LIMIT_PER_ACCOUNT,
            'percentage_used': (account_info['sent_count'] / EMAIL_LIMIT_PER_ACCOUNT) * 100
        })
    return stats

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
        
        # Update the account's sent count
        if sender_email in EMAIL_ACCOUNTS:
            EMAIL_ACCOUNTS[sender_email]['sent_count'] += 1
        
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
    if not EMAIL_ACCOUNTS:
        return False, "No email accounts configured in environment variables"
    
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
        if (current_sender is None or 
            emails_sent_with_current >= EMAIL_LIMIT_PER_ACCOUNT or
            (current_sender in EMAIL_ACCOUNTS and 
             EMAIL_ACCOUNTS[current_sender]['sent_count'] >= EMAIL_LIMIT_PER_ACCOUNT)):
            
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
    
    # Save detailed log to file
    log_data = {
        'timestamp': end_time.isoformat(),
        'duration_seconds': duration.total_seconds(),
        'total_emails': email_status['total_emails'],
        'sent_count': email_status['sent_count'],
        'failed_count': email_status['failed_count'],
        'success_emails': email_status['success_emails'],
        'failed_emails': email_status['failed_emails'],
        'subject': subject,
        'sender_rotation': email_status['sender_rotation'],
        'account_stats': get_account_stats()
    }
    
    log_filename = f"bulk_email_log_{end_time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(os.path.join(LOGS_FOLDER, log_filename), 'w') as f:
        json.dump(log_data, f, indent=2)
    
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
    if sender_email not in EMAIL_ACCOUNTS:
        return False, "Selected sender account not found"
    
    reset_daily_counts()
    if EMAIL_ACCOUNTS[sender_email]['sent_count'] >= EMAIL_LIMIT_PER_ACCOUNT:
        return False, f"Selected account has reached daily limit of {EMAIL_LIMIT_PER_ACCOUNT} emails"
    
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
    remaining_quota = EMAIL_LIMIT_PER_ACCOUNT - EMAIL_ACCOUNTS[sender_email]['sent_count']
    if len(valid_emails) > remaining_quota:
        email_status['is_sending'] = False
        return False, f"Cannot send {len(valid_emails)} emails. Account {sender_email} has only {remaining_quota} emails remaining today."
    
    logger.info(f"Starting bulk email sending to {email_status['total_emails']} recipients from {sender_email}")
    
    smtp_server = "smtp.gmail.com"
    smtp_port = 587
    sender_password = EMAIL_ACCOUNTS[sender_email]['password']
    
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
    
    # Save detailed log to file
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
    with open(os.path.join(LOGS_FOLDER, log_filename), 'w') as f:
        json.dump(log_data, f, indent=2)
    
    logger.info(f"Bulk email sending completed from {sender_email}. {email_status['sent_count']} sent, {email_status['failed_count']} failed. Duration: {duration}")
    
    return True, {
        'success_count': email_status['sent_count'],
        'failed_count': email_status['failed_count'],
        'failed_emails': email_status['failed_emails'],
        'duration': str(duration),
        'log_file': log_filename,
        'sender_rotation': email_status['sender_rotation']
    }

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
                                 email_accounts=EMAIL_ACCOUNTS,
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
    
    # Validate all required fields
    if not all([filename, sender_mode, subject, template, email_column]):
        flash('All fields are required.')
        return redirect(url_for('index'))
    
    # Validate sender selection for manual mode
    if sender_mode == 'manual' and not selected_sender:
        flash('Please select an email account for manual mode.')
        return redirect(url_for('index'))
    
    if not EMAIL_ACCOUNTS:
        flash('No email accounts configured. Please contact administrator.')
        return redirect(url_for('index'))
    
    # Check if selected sender has remaining quota
    if sender_mode == 'manual':
        reset_daily_counts()  # Ensure counts are up to date
        if (selected_sender in EMAIL_ACCOUNTS and 
            EMAIL_ACCOUNTS[selected_sender]['sent_count'] >= EMAIL_LIMIT_PER_ACCOUNT):
            flash(f'Selected account {selected_sender} has reached its daily limit of {EMAIL_LIMIT_PER_ACCOUNT} emails.')
            return redirect(url_for('index'))
    
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    # Start sending emails in background thread
    def send_emails_task():
        if sender_mode == 'auto':
            success, result = send_bulk_emails(
                filepath, subject, template, email_column, delay
            )
        else:  # manual mode
            success, result = send_bulk_emails_single_sender(
                filepath, selected_sender, subject, template, email_column, delay
            )
        
        if success:
            flash(f"Bulk email sending completed! {result['success_count']} sent, {result['failed_count']} failed in {result['duration']}")
            if result.get('sender_rotation'):
                rotation_info = ", ".join([f"{email}: {count}" for email, count in result['sender_rotation'].items()])
                flash(f"Sender distribution: {rotation_info}")
            if result['failed_emails']:
                flash(f"Failed emails: {', '.join(result['failed_emails'][:5])}")  # Show first 5 failures
        else:
            flash(f"Error: {result}")
    
    thread = Thread(target=send_emails_task)
    thread.daemon = True
    thread.start()
    
    flash('Email sending started in background. Check the status page for real-time updates.')
    return redirect(url_for('status'))

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

@app.route('/accounts')
def accounts():
    """Display account statistics"""
    account_stats = get_account_stats()
    return render_template('accounts.html', account_stats=account_stats)

@app.route('/logs')
def logs():
    """Display email sending logs"""
    log_files = []
    if os.path.exists(LOGS_FOLDER):
        for filename in os.listdir(LOGS_FOLDER):
            if filename.endswith('.json'):
                filepath = os.path.join(LOGS_FOLDER, filename)
                try:
                    with open(filepath, 'r') as f:
                        log_data = json.load(f)
                        log_data['filename'] = filename
                        log_files.append(log_data)
                except:
                    continue
    
    # Sort by timestamp (newest first)
    log_files.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    
    return render_template('logs.html', logs=log_files)

# Health check endpoint for Render
@app.route('/health')
def health_check():
    """Health check endpoint for deployment platforms"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)