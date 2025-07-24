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

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-change-this-in-production')

# Configuration
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'csv', 'xlsx', 'xls'}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
LOGS_FOLDER = 'logs'

# Email configuration from environment variables
DEFAULT_SENDER_EMAIL = os.getenv('SENDER_EMAIL')
DEFAULT_SENDER_PASSWORD = os.getenv('SENDER_APP_PASSWORD')

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

def send_email_smtp(smtp_server, smtp_port, sender_email, sender_password, recipient_email, subject, body):
    """Send individual email via SMTP"""
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = recipient_email
        msg['Subject'] = subject
        
        msg.attach(MIMEText(body, 'html'))
        
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        text = msg.as_string()
        server.sendmail(sender_email, recipient_email, text)
        server.quit()
        
        logger.info(f"Email sent successfully to {recipient_email}")
        return True, "Email sent successfully"
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed to send email to {recipient_email}: {error_msg}")
        return False, error_msg

def send_bulk_emails(file_path, sender_email, sender_password, subject, template, email_column, delay=1):
    """Send bulk emails in background"""
    global email_status
    
    # Reset status
    email_status.update({
        'is_sending': True,
        'total_emails': 0,
        'sent_count': 0,
        'failed_count': 0,
        'current_email': '',
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
    
    for index, row in enumerate(valid_emails):
        email = row.get(email_column, '').strip()
        email_status['current_email'] = email
        
        # Replace placeholders in template with row data
        personalized_message = template
        for col in df.columns:
            placeholder = f"{{{col}}}"
            if placeholder in personalized_message:
                value = row.get(col, "")
                personalized_message = personalized_message.replace(placeholder, str(value))
        
        success, error_msg = send_email_smtp(
            smtp_server, smtp_port, sender_email, sender_password,
            email, subject, personalized_message
        )
        
        if success:
            email_status['sent_count'] += 1
            email_status['success_emails'].append(email)
        else:
            email_status['failed_count'] += 1
            email_status['failed_emails'].append(f"{email}: {error_msg}")
        
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
        'sender_email': sender_email
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
        'log_file': log_filename
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
            return render_template('compose.html', 
                                 filename=filename, 
                                 columns=columns,
                                 sample_data=sample_data,
                                 default_sender_email=DEFAULT_SENDER_EMAIL,
                                 has_default_credentials=bool(DEFAULT_SENDER_EMAIL and DEFAULT_SENDER_PASSWORD))
        else:
            flash('Failed to read the uploaded file')
            return redirect(url_for('index'))
    else:
        flash('Invalid file type. Please upload CSV or Excel files only.')
        return redirect(url_for('index'))

@app.route('/send_emails', methods=['POST'])
def send_emails():
    filename = request.form.get('filename')
    # Only use environment variables, no user input for credentials
    sender_email = DEFAULT_SENDER_EMAIL
    sender_password = DEFAULT_SENDER_PASSWORD
    subject = request.form.get('subject')
    template = request.form.get('template')
    email_column = request.form.get('email_column')
    delay = int(request.form.get('delay', 1))
    
    # Check if environment variables are configured
    if not sender_email or not sender_password:
        flash('Email credentials not configured. Please contact administrator.')
        return redirect(url_for('index'))
    
    if not all([filename, subject, template, email_column]):
        flash('All fields are required.')
        return redirect(url_for('index'))
    
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    # Start sending emails in background thread
    def send_emails_task():
        success, result = send_bulk_emails(
            filepath, sender_email, sender_password, 
            subject, template, email_column, delay
        )
        
        if success:
            flash(f"Bulk email sending completed! {result['success_count']} sent, {result['failed_count']} failed in {result['duration']}")
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
    return render_template('status.html', status=email_status)

@app.route('/api/status')
def api_status():
    """API endpoint for real-time status updates"""
    return jsonify(email_status)

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