from flask import Flask, request, render_template_string
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import os
import re
import time
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
import pytz
import anthropic
import google.generativeai as genai
import urllib.parse
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
print("📂 Loading environment variables from .env file...")

app = Flask(__name__)

# ============================================================================
# DATABASE LAYER - DUAL COMPATIBILITY (SQLite locally, PostgreSQL on Railway)
# ============================================================================

USE_POSTGRES = bool(os.environ.get('DATABASE_URL', '').strip())

if USE_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor
else:
    import sqlite3

def get_db_connection():
    """Get database connection - auto-detects SQLite or PostgreSQL"""
    if USE_POSTGRES:
        database_url = os.environ.get('DATABASE_URL')
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql://', 1)
        return psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    else:
        # SQLite with proper timeout and WAL mode for concurrent access
        conn = sqlite3.connect('messages.db', timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for better concurrent access
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')  # 30 second timeout
        return conn

def execute_query(query, params=None, fetch=False, max_retries=3):
    """Execute query with automatic SQLite/PostgreSQL syntax handling and retry logic"""
    
    for attempt in range(max_retries):
        conn = None
        cursor = None
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Convert PostgreSQL syntax to SQLite if needed
            if not USE_POSTGRES:
                query = query.replace('%s', '?')
                query = query.replace('SERIAL', 'INTEGER')
                query = query.replace('TIMESTAMPTZ', 'DATETIME')
                query = query.replace('BOOLEAN', 'INTEGER')
                query = query.replace('DEFAULT now()', 'DEFAULT CURRENT_TIMESTAMP')
                query = query.replace('true', '1')
                query = query.replace('false', '0')
            
            cursor.execute(query, params or ())
            
            if fetch:
                result = cursor.fetchall()
                return result
            else:
                conn.commit()
                last_id = cursor.lastrowid if hasattr(cursor, 'lastrowid') else None
                return last_id
                
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            if conn:
                conn.rollback()
                
            # If database is locked and we have retries left, wait and try again
            if "locked" in str(e).lower() and attempt < max_retries - 1:
                wait_time = 0.1 * (attempt + 1)  # Exponential backoff
                print(f"Database locked, retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            else:
                print(f"Database error: {e}")
                raise
                
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"Database error: {e}")
            raise
            
        finally:
            # Always close cursor and connection
            if cursor:
                cursor.close()
            if conn:
                conn.close()
    
    # If we get here, all retries failed
    raise Exception("Database operation failed after all retries")

# ============================================================================
# CONFIGURATION
# ============================================================================

YOUR_WEBSITE_URL = "https://scarletblue.com.au/escort/adella-allure"
GOOGLE_CALENDAR_ID = "henry.klemm99@gmail.com"
PAYID_EMAIL = "Adella2.0xxx@gmail.com"
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'adella2024')

# AI CONFIGURATION
USE_AI_POLISHING = True  # Always use AI to polish messages (recommended)

# Tone progression settings
AI_TONE_PROGRESSION = True  # Adjust tone based on how many times we've repeated info
# First message: Warm and friendly
# 2-3 repetitions: More direct but polite  
# 4+ repetitions: Very direct and to the point

# TWILIO CONFIGURATION
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = '+61440133407'

# ADELLA'S PHONE NUMBER (for forwarding)
ADELLA_PHONE_NUMBER = os.getenv('ADELLA_PHONE_NUMBER', '+61412345729')

# AUTHORIZED ADMIN NUMBERS
AUTHORIZED_ADMIN_NUMBERS = [
    '+61412345729',
    '+61407530802',
    '+61427590188',
    '+61480089652'
]

# AI CONFIGURATION
CLAUDE_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# Google Calendar Setup
SCOPES = ['https://www.googleapis.com/auth/calendar']

# Try to load credentials.json from local file first, then environment variable
import os.path
current_dir = os.path.dirname(os.path.abspath(__file__))
credentials_path = os.path.join(current_dir, 'credentials.json')

print(f"🔍 Looking for credentials.json in: {current_dir}")

if os.path.exists(credentials_path):
    SERVICE_ACCOUNT_FILE = credentials_path
    print(f"✅ Found credentials.json at: {credentials_path}")
elif os.path.exists('credentials.json'):
    SERVICE_ACCOUNT_FILE = 'credentials.json'
    print("✅ Found credentials.json in current directory")
elif os.path.exists('/tmp/credentials.json'):
    SERVICE_ACCOUNT_FILE = '/tmp/credentials.json'
    print("✅ Using /tmp/credentials.json file")
elif os.getenv('CREDENTIALS_JSON'):
    # Fallback to environment variable (for Railway)
    CREDENTIALS_JSON = os.getenv('CREDENTIALS_JSON')
    with open('/tmp/credentials.json', 'w') as f:
        f.write(CREDENTIALS_JSON)
    SERVICE_ACCOUNT_FILE = '/tmp/credentials.json'
    print("✅ Using CREDENTIALS_JSON from environment")
else:
    SERVICE_ACCOUNT_FILE = None
    print("⚠️  No Google Calendar credentials found (bot will work without calendar)")

# Calendar color codes
COLOR_PEACOCK = "7"  # Turquoise - Reserved (no deposit)
COLOR_BASIL = "2"    # Green - Confirmed (deposit paid)

# ============================================================================
# CITY TO TIMEZONE MAPPING
# ============================================================================

CITY_TIMEZONES = {
    'Adelaide': 'Australia/Adelaide',
    'Sydney': 'Australia/Sydney',
    'Melbourne': 'Australia/Sydney',
    'Brisbane': 'Australia/Brisbane',
    'Perth': 'Australia/Perth',
    'Darwin': 'Australia/Darwin',
    'Hobart': 'Australia/Hobart',
    'Canberra': 'Australia/Sydney',
    'Gold Coast': 'Australia/Brisbane',
    'Newcastle': 'Australia/Sydney'
}

def get_timezone_for_city(city):
    """Get timezone for a city (case-insensitive)"""
    # Normalize city name: title case for lookup
    city_normalized = city.strip().title()
    return CITY_TIMEZONES.get(city_normalized, 'Australia/Adelaide')

# ============================================================================
# ESCORT INDUSTRY TERMINOLOGY - COMPLETE LIST
# ============================================================================

UNSAFE_TERMS = {
    'bbs': 'bareback sex',
    'bareback': 'bareback sex',
    'bb': 'bareback',
    'no condom': 'bareback sex',
    'raw': 'bareback sex',
}

STANDARD_SERVICES = {
    '69': 'Mutual Oral',
    'a-level': 'Anal sex',
    'greek': 'Anal sex',
    'atm': 'Ass to mouth',
    'ar': 'Anal rimming',
    'bbbj': 'Bare back blow job',
    'bbbjtc': 'Bare back blow job to completion',
    'bbw': 'Big beautiful woman',
    'b & d': 'Bondage and discipline',
    'bdsm': 'BDSM activities',
    'bondage': 'Bondage activities',
    'bj': 'Blow job',
    'bjtc': 'Blow job to completion',
    'bls': 'Ball licking and sucking',
    'bs': 'Body slide',
    'cbj': 'Covered blow job',
    'cbt': 'Cock and ball torture',
    'cd': 'Cross dressing',
    'cim': 'Cum in mouth',
    'cimws': 'Cum in mouth with swallowing',
    'cof': 'Cum on face',
    'cob': 'Cum on breasts',
    'daty': 'Dining at the Y',
    'dato': 'Rimming',
    'dp': 'Double penetration',
    'ddp': 'Double digit penetration',
    'dfk': 'Deep french kissing',
    'dt': 'Deep throat',
    'facial': 'Ejaculating on face',
    'fe': 'Female ejaculation',
    'filming': 'Filming or recording',
    'fire and ice': 'Hot and cold sensations',
    'fisting': 'Fisting',
    'fk': 'French kissing',
    'foot fetish': 'Foot fetish',
    'foot job': 'Foot job',
    'french': 'Oral sex',
    'fs': 'Full service',
    'gagging': 'Gagging',
    'gfe': 'Girlfriend Experience',
    'pse': 'Porn Star Experience',
    'gs': 'Golden shower',
    'happy ending': 'Hand job after massage',
    'hj': 'Hand job',
    'italian': 'Penis between buttocks',
    'lk': 'Light kissing',
    'milf': 'Mother I would like to fuck',
    'mff': 'Male Female Female threesome',
    'mmf': 'Male Male Female threesome',
    'mutual french': 'Mutual oral sex',
    'msog': 'Multiple shots on goal',
    'nsa': 'No strings attached',
    'owo': 'Oral without condom',
    'pegging': 'Strap on anal sex',
    'r&t': 'Rub and tug',
    'rimming': 'Licking anus',
    'russian': 'Penis between breasts',
    'spanish': 'Penis between breasts',
    'snowballing': 'Transferring semen',
    'squirting': 'Female ejaculation',
    'strap on': 'Dildo with harness',
    'strip tease': 'Erotic dance',
    'tea bagging': 'Balls in mouth',
    'toy show': 'Masturbation with toys',
    'tromboning': 'Rimming while masturbating',
    'ttm': 'Testicular tongue massage',
    'water sports': 'Urine play'
}

PROFANITY_WORDS = [
    'fuck', 'shit', 'bitch', 'cunt', 'bastard', 'asshole', 'prick', 
    'dick', 'cock', 'pussy', 'whore', 'slut', 'damn', 'hell'
]

def detect_unsafe_requests(message):
    """Detect unsafe/bareback requests"""
    message_lower = message.lower()
    detected = []
    for term in UNSAFE_TERMS:
        if re.search(r'\b' + re.escape(term) + r'\b', message_lower):
            detected.append(term)
    return detected

def detect_profanity(message):
    """Detect if message contains profanity"""
    message_lower = message.lower()
    profanity_count = 0
    for word in PROFANITY_WORDS:
        if re.search(r'\b' + re.escape(word) + r'\b', message_lower):
            profanity_count += 1
    return profanity_count >= 2

def is_cancellation(message):
    """Detect if message is a cancellation request"""
    cancellation_keywords = [
        'cancel', "can't make", 'cannot make', 'need to cancel',
        'have to cancel', 'not coming', "won't make it",
        'reschedule', 'change booking'
    ]
    message_lower = message.lower()
    return any(keyword in message_lower for keyword in cancellation_keywords)

# ============================================================================
# TWILIO & AI INITIALIZATION
# ============================================================================

try:
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        print("✅ Twilio client initialized")
    else:
        print("⚠️  Twilio credentials not found")
        twilio_client = None
except Exception as e:
    print(f"⚠️  Twilio initialization failed: {e}")
    twilio_client = None

# Initialize Claude
claude_client = None
if CLAUDE_API_KEY:
    try:
        claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        print("✅ Claude AI initialized")
    except Exception as e:
        print(f"⚠️  Claude initialization failed: {e}")

# Initialize Gemini
gemini_model = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel("gemini-1.5-pro")
        print("✅ Gemini AI initialized")
    except Exception as e:
        print(f"⚠️  Gemini initialization failed: {e}")

# ============================================================================
# DATABASE INITIALIZATION
# ============================================================================

def init_db():
    """Initialize database tables"""
    print("🔧 Initializing database...")
    
    # Messages table
    execute_query('''CREATE TABLE IF NOT EXISTS messages
                     (id SERIAL PRIMARY KEY,
                      phone_number TEXT,
                      message_body TEXT,
                      timestamp TIMESTAMPTZ DEFAULT now())''')
    
    # Pending confirmations
    execute_query('''CREATE TABLE IF NOT EXISTS pending_confirmations
                     (id SERIAL PRIMARY KEY,
                      phone_number TEXT,
                      client_name TEXT,
                      city TEXT,
                      date TEXT,
                      time TEXT,
                      duration TEXT,
                      experience_type TEXT,
                      incall_outcall TEXT,
                      outcall_address TEXT,
                      booking_status TEXT,
                      awaiting_deposit_screenshot INTEGER DEFAULT 0,
                      peacock_event_id TEXT,
                      created_at TIMESTAMPTZ DEFAULT now())''')
    
    # Booking progress
    execute_query('''CREATE TABLE IF NOT EXISTS booking_progress
                     (id SERIAL PRIMARY KEY,
                      phone_number TEXT UNIQUE,
                      city TEXT,
                      date TEXT,
                      time TEXT,
                      duration TEXT,
                      experience_type TEXT,
                      incall_outcall TEXT,
                      outcall_address TEXT,
                      updated_at TIMESTAMPTZ DEFAULT now())''')
    
    # Incall location
    execute_query('''CREATE TABLE IF NOT EXISTS incall_location
                     (id INTEGER PRIMARY KEY,
                      city TEXT,
                      address TEXT,
                      intercom_number TEXT,
                      timezone TEXT,
                      updated_at TIMESTAMPTZ DEFAULT now())''')
    
    # Insert default location (only if it doesn't exist)
    if USE_POSTGRES:
        # PostgreSQL: Use INSERT ... ON CONFLICT DO NOTHING
        execute_query('''INSERT INTO incall_location (id, city, address, intercom_number, timezone) 
                         VALUES (1, 'Adelaide', 'Adelaide CBD - Location details on confirmation', 'TBA', 'Australia/Adelaide')
                         ON CONFLICT (id) DO NOTHING''')
    else:
        # SQLite: Use INSERT OR IGNORE
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''INSERT OR IGNORE INTO incall_location (id, city, address, intercom_number, timezone) 
                          VALUES (1, 'Adelaide', 'Adelaide CBD - Location details on confirmation', 'TBA', 'Australia/Adelaide')''')
        conn.commit()
        conn.close()
    
    print("✅ Default location ready")
    
    # Confirmed bookings
    execute_query('''CREATE TABLE IF NOT EXISTS confirmed_bookings
                     (id SERIAL PRIMARY KEY,
                      phone_number TEXT,
                      client_name TEXT,
                      booking_datetime TEXT,
                      city TEXT,
                      address TEXT,
                      intercom_number TEXT,
                      experience_type TEXT,
                      duration TEXT,
                      reminder_sent INTEGER DEFAULT 0,
                      forwarding_active INTEGER DEFAULT 0,
                      created_at TIMESTAMPTZ DEFAULT now())''')
    
    # Room detail reminders (for sending room number 1 hour before)
    execute_query('''CREATE TABLE IF NOT EXISTS room_detail_reminders
                     (id SERIAL PRIMARY KEY,
                      phone_number TEXT,
                      client_name TEXT,
                      booking_datetime TEXT,
                      city TEXT,
                      incall_outcall TEXT,
                      outcall_address TEXT,
                      sent INTEGER DEFAULT 0,
                      created_at TIMESTAMPTZ DEFAULT now())''')
    
    # Blocked numbers (for 5-repeat rule)
    execute_query('''CREATE TABLE IF NOT EXISTS blocked_numbers
                     (id SERIAL PRIMARY KEY,
                      phone_number TEXT UNIQUE,
                      reason TEXT,
                      blocked_at TIMESTAMPTZ DEFAULT now())''')
    
    # Deposit requests
    execute_query('''CREATE TABLE IF NOT EXISTS deposit_requests
                     (id SERIAL PRIMARY KEY,
                      phone_number TEXT,
                      booking_datetime TEXT,
                      deposit_request_sent_at TIMESTAMPTZ DEFAULT now(),
                      client_responded INTEGER DEFAULT 0,
                      followup_sent INTEGER DEFAULT 0,
                      created_at TIMESTAMPTZ DEFAULT now())''')
    
    # Message tracking (for 3+ messages rule)
    execute_query('''CREATE TABLE IF NOT EXISTS message_tracking
                     (id SERIAL PRIMARY KEY,
                      phone_number TEXT,
                      booking_id INTEGER,
                      message_count INTEGER DEFAULT 0,
                      enquiry_sent INTEGER DEFAULT 0,
                      created_at TIMESTAMPTZ DEFAULT now())''')
    
    print("✅ Database tables created/verified")

init_db()

# ============================================================================
# MESSAGE TEMPLATES (HARD-CODED RULES)
# ============================================================================

def get_message_template(situation, data=None):
    """Get hard-coded message for each situation. These are YOUR EXACT messages.
    AI can only polish the TONE, never change the content/meaning."""
    
    # Get current location for first contact message
    if situation == 'first_contact':
        location = get_current_incall_location()
        city = location.get('city', 'Adelaide')
        address = location.get('address', 'Location details on confirmation')
        
        # Extract hotel name from address (before comma or full address if no comma)
        hotel = address.split(',')[0] if ',' in address else address
        
        # Get webform URL - will be domain where bot is hosted
        webform_url = os.environ.get('BOT_URL', 'http://localhost:5000') + '/booking'
        
        first_contact_msg = f"Hi if your wanting to make a booking please confirm the following details Date: Time: Duration: Experience: (GFE/PSE) Incall/Outcall. Either text this information back to me or let me know by clicking this link to my webform: {webform_url}\n\nI'm currently in {city} at {hotel}"
    
    templates = {
        'first_contact': first_contact_msg if situation == 'first_contact' else "",
        
        # Random message response - YOUR EXACT WORDING
        'random_request': (
            "If your wanting to see me then please advise me of the following so I can check availability:\n\n"
            "DATE:\n"
            "TIME:\n"
            "DURATION:\n"
            "EXPERIENCE: (GFE/PSE)\n"
            "INCALL/OUTCALL:"
        ),
        
        # 4th message auto-response - YOUR EXACT WORDING
        'fourth_message_enquiry': (
            f"Due to the number of enquires I receive this is an automated responce. "
            f"If there is anything specific you wish to discuss with Adella text ENQUIRY followed by your question. "
            f"For further information please visit my profile by clicking the link below: {YOUR_WEBSITE_URL}"
        ),
        
        # 5th message final block - YOUR EXACT WORDING
        'fifth_message_block': (
            "Im sorry but unfortunatly as you have not confirmed any of the details "
            "I have requested I am not able to schedule in a booking."
        ),
        
        'missing_city': "Which city are you in? (Adelaide, Sydney, Melbourne, Brisbane, Perth, etc)",
        
        'missing_date': "What date would you like? (e.g., Friday 20/12 or 20/12)",
        
        'missing_time': "What time? (e.g., 7pm or 19:00)",
        
        'missing_duration': "How long? (30min, 1 hour, 2 hours)",
        
        'missing_experience': "GFE or PSE?",
        
        'missing_incall_outcall': "Incall or Outcall?",
        
        'missing_address': "What's the address for the outcall?",
        
        'time_unavailable': "Sorry, that time isn't available. Do you have another time in mind?",
        
        'unsafe_request': "I don't offer that service. Let's continue with your booking.",
        
        'need_name': "I need your name to confirm. Reply with your name + YES\n\nExample: John YES",
        
        'deposit_mandatory': f"A $100 deposit is required to confirm this booking.\n\nText DEPOSIT for payment details.",
        
        # Non-mandatory deposit - YOUR EXACT WORDING
        'deposit_non_mandatory': f"Do you mind paying a small deposit? Its not mandatory, but its appreciated as it helps reassure me my time wont be wasted when I Reserve my time for you. If you could please pay $50-$100 to my pay ID which is {PAYID_EMAIL}",
        
        # 30-minute follow-up - YOUR EXACT WORDING
        'deposit_followup_30min': "I havnt heard back from you just confirming your still coming as i've reserved the time for you.",
        
        'booking_cancelled': "Your booking has been cancelled. Thanks for letting me know.",
    }
    
    # Get the base message
    base_message = templates.get(situation, "Thanks for your message.")
    
    # For some messages, add dynamic data
    if situation == 'time_available' and data:
        base_message = f"{data.get('date', '')} at {data.get('time', '')} is available! 😊\n\n{data.get('duration', '')}, {data.get('experience_type', '')}, {data.get('incall_outcall', '')}\n\nReply with your name + YES to confirm.\n\nExample: John YES"
    
    return base_message


def polish_with_ai(message, use_ai=True, repetition_count=0):
    """STRICTLY polish tone only - AI cannot change content, meaning, or what information is requested.
    
    Args:
        message: The hard-coded message to polish (YOUR EXACT RULES)
        use_ai: Whether to use AI polishing (default True)
        repetition_count: How many times we've asked (0=first time, 1+=repeating)
    
    CRITICAL RULES FOR AI:
    - Keep EXACT same information/requests
    - Keep EXACT same structure (if formatted with newlines, keep them)
    - Only adjust tone warmth (warm/direct/firm)
    - Can change: "I need" → "I'll need" or "your" → "the"
    - CANNOT add/remove: information, questions, requirements, URLs, field names
    - CANNOT remove: emojis, formatting, newlines, colons, punctuation structure
    """
    
    # If AI is disabled, return original
    if not use_ai or not (claude_client or gemini_model):
        return message
    
    # Determine tone based on repetition count
    if repetition_count <= 1:
        # First or second time asking - warm and friendly
        tone_instruction = "Make this warmer and friendlier. Keep all information EXACTLY the same."
    elif repetition_count <= 3:
        # Third or fourth time - more direct but polite
        tone_instruction = "Make this more direct and businesslike. Keep all information EXACTLY the same."
    else:
        # Fifth time or more - very direct and firm
        tone_instruction = "Make this firm and to-the-point. Keep all information EXACTLY the same."
    
    # Try to polish with Claude (just tone, not content)
    try:
        if claude_client:
            response = claude_client.messages.create(
                model="claude-3-5-haiku-20241022",
                max_tokens=300,
                messages=[{
                    "role": "user", 
                    "content": f"""You are a tone adjuster. Your ONLY job is to adjust the warmth/directness of text.

STRICT RULES:
1. Keep EVERY piece of information exactly as written
2. Keep ALL formatting (newlines, colons, structure)
3. Keep ALL field names (DATE:, TIME:, etc)
4. Keep ALL URLs exactly as written
5. Keep ALL punctuation structure
6. ONLY change: warmth of phrasing ("I need" vs "I'll need"), pronoun choices ("your" vs "the")
7. DO NOT add new information
8. DO NOT remove information
9. DO NOT change what's being asked for

{tone_instruction}

Original message:
{message}

Adjusted version (same information, adjusted tone):"""
                }]
            )
            polished = response.content[0].text.strip()
            
            # Safety checks - if AI changed too much or refuses, use original
            if len(polished) < len(message) * 0.7:  # Too short - probably removed content
                return message
            if len(polished) > len(message) * 1.5:  # Too long - probably added content
                return message
            if "can't" in polished.lower() or "sorry" in polished.lower()[:30]:
                return message
            if "cannot" in polished.lower()[:50] or "unable" in polished.lower()[:50]:
                return message
            
            return polished
    except:
        pass
    
    # Try Gemini as fallback
    try:
        if gemini_model:
            response = gemini_model.generate_content(
                f"""Adjust the tone of this message. {tone_instruction}

CRITICAL: Keep EVERY piece of information, ALL formatting, ALL field names, ALL URLs exactly as written.
Only adjust warmth/directness of phrasing.

Original:
{message}

Adjusted (same info, different tone):"""
            )
            polished = response.text.strip()
            
            # Safety checks
            if len(polished) < len(message) * 0.7 or len(polished) > len(message) * 1.5:
                return message
            if "can't" in polished.lower() or "sorry" in polished.lower()[:30]:
                return message
                
            return polished
    except:
        pass
    
    # Fallback: return original message
    return message

# ============================================================================
# GOOGLE CALENDAR FUNCTIONS
# ============================================================================

def get_calendar_service():
    """Create Google Calendar API service"""
    if not SERVICE_ACCOUNT_FILE:
        return None
    
    try:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        service = build('calendar', 'v3', credentials=credentials)
        return service
    except Exception as e:
        print(f"Error creating calendar service: {e}")
        return None

def create_calendar_event(booking_details, phone_number, is_confirmed=False, client_name=None):
    """Create calendar event"""
    service = get_calendar_service()
    if not service:
        return None
    
    try:
        tz = pytz.timezone(get_current_timezone())
        date_str = booking_details.get('date', '')
        time_str = booking_details.get('time', '')
        
        # Parse date: "Friday 15/12/2025"
        date_parts = date_str.split()
        if len(date_parts) >= 2:
            date_only = date_parts[1]  # Get "15/12/2025"
        else:
            date_only = date_str
            
        date_obj = datetime.strptime(date_only, '%d/%m/%Y')
        date_obj = tz.localize(date_obj)
        
        # Parse time
        time_match = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)?', time_str, re.IGNORECASE)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            period = time_match.group(3)
            
            if period and period.upper() == 'PM' and hour < 12:
                hour += 12
            elif period and period.upper() == 'AM' and hour == 12:
                hour = 0
            
            start_datetime = date_obj.replace(hour=hour, minute=minute)
        else:
            return None
        
        # Duration
        duration_str = booking_details.get('duration', '1 hour')
        duration_hours = 1
        if 'hour' in duration_str.lower():
            duration_match = re.search(r'(\d+)', duration_str)
            if duration_match:
                duration_hours = int(duration_match.group(1))
        elif 'min' in duration_str.lower():
            duration_match = re.search(r'(\d+)', duration_str)
            if duration_match:
                duration_hours = int(duration_match.group(1)) / 60
        
        end_datetime = start_datetime + timedelta(hours=duration_hours)
        
        event_title = f"{client_name or 'Booking'} - {booking_details.get('experience_type', 'Client')}"
        
        event = {
            'summary': event_title,
            'description': f"Client: {client_name or 'Not provided'}\nPhone: {phone_number}",
            'start': {'dateTime': start_datetime.isoformat(), 'timeZone': get_current_timezone()},
            'end': {'dateTime': end_datetime.isoformat(), 'timeZone': get_current_timezone()},
            'colorId': COLOR_BASIL if is_confirmed else COLOR_PEACOCK,
        }
        
        created_event = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return created_event.get('id')
        
    except Exception as e:
        print(f"Error creating calendar event: {e}")
        return None

# ============================================================================
# LOCATION MANAGEMENT
# ============================================================================

def get_current_incall_location():
    """Get current incall location"""
    try:
        result = execute_query('SELECT city, address, intercom_number, timezone FROM incall_location WHERE id = 1', fetch=True)
        if result and len(result) > 0:
            row = result[0]
            return {
                'city': row['city'] if USE_POSTGRES else row[0],
                'address': row['address'] if USE_POSTGRES else row[1],
                'intercom_number': row['intercom_number'] if USE_POSTGRES else row[2],
                'timezone': row['timezone'] if USE_POSTGRES else row[3]
            }
    except Exception as e:
        print(f"Error getting location: {e}")
    
    return {
        'city': 'Adelaide',
        'address': 'Adelaide CBD - Location details on confirmation',
        'intercom_number': 'TBA',
        'timezone': 'Australia/Adelaide'
    }

def get_current_timezone():
    """Get current timezone"""
    location = get_current_incall_location()
    return location['timezone']

def update_incall_location(city, new_address, intercom_number=None):
    """Update incall location"""
    timezone = get_timezone_for_city(city)
    
    if intercom_number is not None:
        execute_query('''UPDATE incall_location 
                         SET city = %s, address = %s, intercom_number = %s, timezone = %s 
                         WHERE id = 1''',
                      (city, new_address, intercom_number, timezone))
    else:
        execute_query('''UPDATE incall_location 
                         SET city = %s, address = %s, timezone = %s 
                         WHERE id = 1''',
                      (city, new_address, timezone))
    
    print(f"✅ Location updated: {city} - {new_address}")
    return timezone

def format_location_for_confirmation(location, incall_outcall, outcall_address=None, include_room_details=False):
    """Format location message for booking confirmation
    
    Format: City first, then hotel name, then full address (no repetition)
    
    Args:
        location: Location dict with city, address, intercom
        incall_outcall: 'Incall' or 'Outcall'
        outcall_address: Address for outcall bookings
        include_room_details: If True, includes room/intercom (sent 1hr before booking)
    """
    if incall_outcall == 'Outcall':
        links = create_transport_links(outcall_address)
        return f"\n\nI'll come to: {outcall_address}\n\n🚗 Uber: {links['uber']}\n🗺️ Google Maps: {links['google']}"
    
    city = location['city']
    address = location['address']
    
    # Extract hotel name (everything before first comma)
    hotel = address.split(',')[0] if ',' in address else address
    
    # For initial confirmation: City, Hotel, note about room details
    if not include_room_details:
        return f"\n\nLocation: {city}\nHotel: {hotel}\n\nRoom details will be sent 1 hour before your booking."
    
    # For 1-hour-before message: City, Full Address, Room/Instructions
    intercom = location['intercom_number']
    
    if city == 'Perth':
        return f"\n\nLocation: {city}\n{address}\n\nI'll meet you in the lobby approximately 5 minutes before your booking time."
    else:
        return f"\n\nLocation: {city}\n{address}\n\nIntercom/Room: {intercom}"

def create_transport_links(address):
    """Create transport links for outcall bookings"""
    encoded_address = urllib.parse.quote(address)
    return {
        'uber': f"https://m.uber.com/ul/?action=setPickup&pickup=my_location&dropoff[formatted_address]={encoded_address}",
        'google': f"https://www.google.com/maps/dir/?api=1&destination={encoded_address}"
    }

# ============================================================================
# SMS FUNCTIONS
# ============================================================================

def send_sms(to_number, message):
    """Send SMS via Twilio"""
    if not twilio_client:
        print(f"⚠️  Cannot send SMS - Twilio not initialized")
        return False
    
    try:
        msg = twilio_client.messages.create(
            body=message,
            from_=TWILIO_PHONE_NUMBER,
            to=to_number
        )
        print(f"✅ SMS sent to {to_number}")
        return True
    except Exception as e:
        print(f"❌ Error sending SMS: {e}")
        return False

# ============================================================================
# MESSAGE LOGGING & TRACKING
# ============================================================================

def log_message(phone_number, message_body):
    """Log incoming message"""
    execute_query('INSERT INTO messages (phone_number, message_body) VALUES (%s, %s)',
                  (phone_number, message_body))

def get_message_count(phone_number):
    """Get message count for phone number"""
    result = execute_query('SELECT COUNT(*) as count FROM messages WHERE phone_number = %s', 
                          (phone_number,), fetch=True)
    if result:
        return result[0]['count'] if USE_POSTGRES else result[0][0]
    return 0

def save_booking_progress(phone_number, details):
    """Save partial booking details as client provides them"""
    try:
        # Check if progress exists
        result = execute_query('SELECT * FROM booking_progress WHERE phone_number = %s', (phone_number,), fetch=True)
        
        if result:
            # Update existing progress
            current = result[0]
            merged = {
                'city': details.get('city') or (current['city'] if USE_POSTGRES else current[1]),
                'date': details.get('date') or (current['date'] if USE_POSTGRES else current[2]),
                'time': details.get('time') or (current['time'] if USE_POSTGRES else current[3]),
                'duration': details.get('duration') or (current['duration'] if USE_POSTGRES else current[4]),
                'experience_type': details.get('experience_type') or (current['experience_type'] if USE_POSTGRES else current[5]),
                'incall_outcall': details.get('incall_outcall') or (current['incall_outcall'] if USE_POSTGRES else current[6]),
                'outcall_address': details.get('outcall_address') or (current['outcall_address'] if USE_POSTGRES else current[7]),
            }
            
            execute_query('''UPDATE booking_progress 
                            SET city=%s, date=%s, time=%s, duration=%s, experience_type=%s, 
                                incall_outcall=%s, outcall_address=%s, updated_at=CURRENT_TIMESTAMP
                            WHERE phone_number=%s''',
                         (merged['city'], merged['date'], merged['time'], merged['duration'],
                          merged['experience_type'], merged['incall_outcall'], merged['outcall_address'], phone_number))
            return merged
        else:
            # Create new progress
            execute_query('''INSERT INTO booking_progress 
                            (phone_number, city, date, time, duration, experience_type, incall_outcall, outcall_address)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)''',
                         (phone_number, details.get('city'), details.get('date'), details.get('time'),
                          details.get('duration'), details.get('experience_type'),
                          details.get('incall_outcall'), details.get('outcall_address')))
            return details
    except Exception as e:
        print(f"Error saving booking progress: {e}")
        return details

def get_booking_progress(phone_number):
    """Get accumulated booking details for a phone number"""
    try:
        result = execute_query('SELECT * FROM booking_progress WHERE phone_number = %s', (phone_number,), fetch=True)
        if result:
            row = result[0]
            if USE_POSTGRES:
                return {
                    'city': row['city'],
                    'date': row['date'],
                    'time': row['time'],
                    'duration': row['duration'],
                    'experience_type': row['experience_type'],
                    'incall_outcall': row['incall_outcall'],
                    'outcall_address': row['outcall_address']
                }
            else:
                return {
                    'city': row[2],
                    'date': row[3],
                    'time': row[4],
                    'duration': row[5],
                    'experience_type': row[6],
                    'incall_outcall': row[7],
                    'outcall_address': row[8]
                }
    except:
        pass
    return None

def clear_booking_progress(phone_number):
    """Clear booking progress after successful booking or cancellation"""
    try:
        execute_query('DELETE FROM booking_progress WHERE phone_number = %s', (phone_number,))
    except:
        pass

def increment_booking_attempts(phone_number):
    """Track how many times we've asked for booking details - for 3-strike deposit rule"""
    try:
        result = execute_query('SELECT message_count FROM message_tracking WHERE phone_number = %s', (phone_number,), fetch=True)
        
        if result:
            count = result[0]['message_count'] if USE_POSTGRES else result[0][0]
            new_count = count + 1
            execute_query('UPDATE message_tracking SET message_count = %s WHERE phone_number = %s',
                         (new_count, phone_number))
            return new_count
        else:
            execute_query('INSERT INTO message_tracking (phone_number, message_count) VALUES (%s, 1)',
                         (phone_number,))
            return 1
    except:
        return 1

def increment_post_booking_messages(phone_number):
    """Track messages sent after booking confirmation (for 3+ message ENQUIRY rule)"""
    try:
        # Use a different tracking mechanism than booking_attempts
        # Check in confirmed_bookings or message_tracking with a flag
        result = execute_query(
            'SELECT message_count FROM message_tracking WHERE phone_number = %s AND booking_id IS NOT NULL',
            (phone_number,), fetch=True)
        
        if result:
            count = result[0]['message_count'] if USE_POSTGRES else result[0][0]
            new_count = count + 1
            execute_query(
                'UPDATE message_tracking SET message_count = %s WHERE phone_number = %s',
                (new_count, phone_number))
            return new_count
        else:
            # Create tracking for post-booking messages
            execute_query(
                'INSERT INTO message_tracking (phone_number, message_count, booking_id) VALUES (%s, 1, 1)',
                (phone_number,))
            return 1
    except Exception as e:
        print(f"Error incrementing post-booking messages: {e}")
        return 1

def get_booking_attempts(phone_number):
    """Get number of times we've asked for booking details"""
    try:
        result = execute_query('SELECT message_count FROM message_tracking WHERE phone_number = %s', (phone_number,), fetch=True)
        if result:
            return result[0]['message_count'] if USE_POSTGRES else result[0][0]
    except:
        pass
    return 0
    """Track messages sent after booking confirmation"""
    result = execute_query('SELECT message_count FROM message_tracking WHERE phone_number = %s',
                          (phone_number,), fetch=True)
    
    if result:
        count = result[0]['message_count'] if USE_POSTGRES else result[0][0]
        execute_query('UPDATE message_tracking SET message_count = %s WHERE phone_number = %s',
                     (count + 1, phone_number))
        return count + 1
    else:
        execute_query('INSERT INTO message_tracking (phone_number, message_count) VALUES (%s, 1)',
                     (phone_number,))
        return 1

def check_post_booking_message_limit(phone_number):
    """Check if client has sent 3+ messages after booking"""
    result = execute_query(
        'SELECT message_count, enquiry_sent FROM message_tracking WHERE phone_number = %s',
        (phone_number,), fetch=True)
    
    if result:
        count = result[0]['message_count'] if USE_POSTGRES else result[0][0]
        enquiry_sent = result[0]['enquiry_sent'] if USE_POSTGRES else result[0][1]
        return count >= 3, bool(enquiry_sent)
    return False, False

# ============================================================================
# BOOKING EXTRACTION & VALIDATION
# ============================================================================

def extract_booking_details(message_body):
    """Extract booking details from message"""
    details = {}
    
    # City
    cities = list(CITY_TIMEZONES.keys())
    for city in cities:
        if city.lower() in message_body.lower():
            details['city'] = city
            break
    
    # Date
    requested_date = parse_date_from_message(message_body)
    if requested_date:
        details['date'] = requested_date.strftime('%A %d/%m/%Y')
    
    # Time
    hour, minute = parse_time_from_message(message_body)
    if hour is not None:
        period = 'AM' if hour < 12 else 'PM'
        display_hour = hour if hour <= 12 else hour - 12
        if display_hour == 0:
            display_hour = 12
        details['time'] = f"{display_hour}:{minute:02d}{period}"
    
    # Duration
    duration_match = re.search(r'(\d+)\s*(hour|hr|min)', message_body.lower())
    if duration_match:
        details['duration'] = duration_match.group(0)
    
    # Experience type
    if 'gfe' in message_body.lower() or 'girlfriend' in message_body.lower():
        details['experience_type'] = 'GFE'
    elif 'pse' in message_body.lower() or 'porn star' in message_body.lower():
        details['experience_type'] = 'PSE'
    
    # Incall/Outcall
    if 'incall' in message_body.lower():
        details['incall_outcall'] = 'Incall'
    elif 'outcall' in message_body.lower():
        details['incall_outcall'] = 'Outcall'
    
    # Outcall address
    address_pattern = r'\d+\s+[A-Za-z\s]+(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Lane|Ln|Court|Ct|Place|Pl),?\s*[A-Za-z\s]+,?\s*\d{4}'
    address_match = re.search(address_pattern, message_body, re.IGNORECASE)
    if address_match:
        details['outcall_address'] = address_match.group(0)
    
    return details

def parse_date_from_message(message_body):
    """Extract date from message"""
    message_lower = message_body.lower()
    tz = pytz.timezone(get_current_timezone())
    
    # DD/MM or DD-MM
    date_match = re.search(r'(\d{1,2})[/-](\d{1,2})', message_body)
    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year = datetime.now(tz).year
        try:
            return datetime(year, month, day, tzinfo=tz)
        except:
            pass
    
    # Day names
    days = {
        'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
        'friday': 4, 'saturday': 5, 'sunday': 6
    }
    for day_name, day_num in days.items():
        if day_name in message_lower:
            today = datetime.now(tz)
            days_ahead = day_num - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            return today + timedelta(days=days_ahead)
    
    # Today/Tomorrow
    if 'today' in message_lower:
        return datetime.now(tz)
    if 'tomorrow' in message_lower:
        return datetime.now(tz) + timedelta(days=1)
    
    return None

def parse_time_from_message(message_body):
    """Extract time from message"""
    time_patterns = [
        r'(\d{1,2}):(\d{2})\s?(am|pm)?',
        r'(\d{1,2})\s?(am|pm)',
    ]
    
    for pattern in time_patterns:
        match = re.search(pattern, message_body.lower())
        if match:
            try:
                hour = int(match.group(1))
                minute = int(match.group(2)) if len(match.groups()) >= 2 and match.group(2) and match.group(2) not in ['am', 'pm'] else 0
                
                ampm = None
                if 'am' in message_body.lower():
                    ampm = 'am'
                elif 'pm' in message_body.lower():
                    ampm = 'pm'
                
                if ampm == 'pm' and hour < 12:
                    hour += 12
                elif ampm == 'am' and hour == 12:
                    hour = 0
                
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return hour, minute
                    
            except (ValueError, IndexError):
                continue
    
    return None, None

def extract_name_from_yes_message(message_body):
    """Extract client name from YES message"""
    message_clean = message_body.strip()
    name = re.sub(r'\byes\b', '', message_clean, flags=re.IGNORECASE).strip()
    name = re.sub(r'\s+', ' ', name)
    name = name.strip('.,!?')
    
    if name and len(name) >= 2 and not name.lower() in ['ok', 'yeah', 'yep', 'sure']:
        return name.title()
    return None

# ============================================================================
# DEPOSIT FUNCTIONS
# ============================================================================

def block_phone_number(phone_number, reason="5+ repeated requests without providing booking details"):
    """Block a phone number from future communications"""
    try:
        execute_query(
            'INSERT INTO blocked_numbers (phone_number, reason) VALUES (%s, %s) ON CONFLICT (phone_number) DO NOTHING',
            (phone_number, reason)
        )
        print(f"🚫 Blocked number: {phone_number} - Reason: {reason}")
        return True
    except Exception as e:
        print(f"Error blocking number: {e}")
        return False

def is_number_blocked(phone_number):
    """Check if a phone number is blocked"""
    try:
        result = execute_query(
            'SELECT phone_number, reason FROM blocked_numbers WHERE phone_number = %s',
            (phone_number,),
            fetch=True
        )
        return len(result) > 0 if result else False
    except:
        return False

def save_deposit_request(phone_number, booking_datetime):
    """Save deposit request for tracking"""
    execute_query('INSERT INTO deposit_requests (phone_number, booking_datetime, client_responded) VALUES (%s, %s, 0)',
                 (phone_number, booking_datetime))

def schedule_room_detail_reminder(phone_number, client_name, booking_datetime, city, incall_outcall, outcall_address=None):
    """Schedule room details to be sent 1 hour before booking"""
    execute_query('''INSERT INTO room_detail_reminders 
                     (phone_number, client_name, booking_datetime, city, incall_outcall, outcall_address, sent)
                     VALUES (%s, %s, %s, %s, %s, %s, 0)''',
                 (phone_number, client_name, booking_datetime, city, incall_outcall, outcall_address))
    print(f"✅ Scheduled room details for {client_name} at {booking_datetime}")

def check_and_send_room_details():
    """Check if any bookings need room details sent (1 hour before booking)"""
    try:
        # Get all unsent room detail reminders
        reminders = execute_query(
            'SELECT * FROM room_detail_reminders WHERE sent = 0',
            fetch=True
        )
        
        if not reminders:
            return
        
        now = datetime.now(pytz.timezone(get_current_timezone()))
        
        for reminder in reminders:
            if USE_POSTGRES:
                phone = reminder['phone_number']
                client_name = reminder['client_name']
                booking_dt_str = reminder['booking_datetime']
                city = reminder['city']
                incall_outcall = reminder['incall_outcall']
                outcall_address = reminder['outcall_address']
                reminder_id = reminder['id']
            else:
                phone = reminder[1]
                client_name = reminder[2]
                booking_dt_str = reminder[3]
                city = reminder[4]
                incall_outcall = reminder[5]
                outcall_address = reminder[6]
                reminder_id = reminder[0]
            
            # Parse booking datetime
            try:
                # Format: "Friday 20/12/2025 7:00PM"
                booking_dt = datetime.strptime(booking_dt_str, '%A %d/%m/%Y %I:%M%p')
                booking_dt = pytz.timezone(get_current_timezone()).localize(booking_dt)
            except:
                continue
            
            # Check if it's 1 hour before booking
            time_until_booking = (booking_dt - now).total_seconds()
            
            # Send if between 55-65 minutes before (5 min window to catch it)
            if 3300 <= time_until_booking <= 3900:  # 55-65 minutes
                # Get location details
                location = get_current_incall_location()
                
                # Format message with full room details
                location_msg = format_location_for_confirmation(
                    location, incall_outcall, outcall_address, include_room_details=True
                )
                
                msg = f"Hi {client_name}! Your booking is in 1 hour.{location_msg}"
                
                if send_sms(phone, msg):
                    # Mark as sent
                    execute_query('UPDATE room_detail_reminders SET sent = 1 WHERE id = %s', (reminder_id,))
                    print(f"✅ Sent room details to {client_name}")
                    
    except Exception as e:
        print(f"Error checking room detail reminders: {e}")

# ============================================================================
# PENDING CONFIRMATION FUNCTIONS
# ============================================================================

def save_pending_confirmation(phone_number, booking_details, booking_status='available', 
                              awaiting_deposit=False, peacock_event_id=None, client_name=None):
    """Save pending confirmation"""
    execute_query('DELETE FROM pending_confirmations WHERE phone_number = %s', (phone_number,))
    
    execute_query('''INSERT INTO pending_confirmations 
                     (phone_number, client_name, city, date, time, duration, experience_type, 
                      incall_outcall, outcall_address, booking_status, awaiting_deposit_screenshot, peacock_event_id)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                  (phone_number, client_name,
                   booking_details.get('city', 'Not specified'),
                   booking_details.get('date', 'Not specified'),
                   booking_details.get('time', 'Not specified'),
                   booking_details.get('duration', 'Not specified'),
                   booking_details.get('experience_type', 'Not specified'),
                   booking_details.get('incall_outcall', 'Not specified'),
                   booking_details.get('outcall_address'),
                   booking_status,
                   1 if awaiting_deposit else 0,
                   peacock_event_id))

def get_pending_confirmation(phone_number):
    """Get pending confirmation"""
    result = execute_query('SELECT * FROM pending_confirmations WHERE phone_number = %s ORDER BY created_at DESC LIMIT 1',
                          (phone_number,), fetch=True)
    
    if result:
        row = result[0]
        if USE_POSTGRES:
            return {
                'client_name': row['client_name'],
                'city': row['city'],
                'date': row['date'],
                'time': row['time'],
                'duration': row['duration'],
                'experience_type': row['experience_type'],
                'incall_outcall': row['incall_outcall'],
                'outcall_address': row['outcall_address'],
                'booking_status': row['booking_status'],
                'awaiting_deposit_screenshot': bool(row['awaiting_deposit_screenshot']),
                'peacock_event_id': row['peacock_event_id']
            }
        else:
            return {
                'client_name': row[2],
                'city': row[3],
                'date': row[4],
                'time': row[5],
                'duration': row[6],
                'experience_type': row[7],
                'incall_outcall': row[8],
                'outcall_address': row[9],
                'booking_status': row[10],
                'awaiting_deposit_screenshot': bool(row[11]),
                'peacock_event_id': row[12]
            }
    
    return None

def delete_pending_confirmation(phone_number):
    """Delete pending confirmation"""
    execute_query('DELETE FROM pending_confirmations WHERE phone_number = %s', (phone_number,))

# ============================================================================
# MAIN SMS HANDLER
# ============================================================================

@app.route('/sms/incoming', methods=['POST'])
def incoming_sms():
    phone_number = request.form.get('From')
    message_body = request.form.get('Body', '').strip()
    
    # Check if number is blocked (5-repeat rule)
    if is_number_blocked(phone_number):
        print(f"🚫 Blocked number attempted contact: {phone_number}")
        # Send no response to blocked numbers
        return str(MessagingResponse())
    
    log_message(phone_number, message_body)
    
    print(f"\n{'='*60}")
    print(f"SMS from {phone_number}: {message_body}")
    
    # ENQUIRY KEYWORD - Forward to Adella
    if message_body.upper().startswith('ENQUIRY'):
        enquiry_message = message_body[7:].strip()  # Remove "ENQUIRY" prefix
        forward_message = f"📩 ENQUIRY from {phone_number}:\n\n{enquiry_message}"
        send_sms(ADELLA_PHONE_NUMBER, forward_message)
        
        response = MessagingResponse()
        response.message("Your message has been forwarded to Adella. She'll respond personally soon.")
        return str(response)
    
    # Check for unsafe requests
    unsafe_detected = detect_unsafe_requests(message_body)
    if unsafe_detected:
        response = MessagingResponse()
        base_message = get_message_template('unsafe_request')
        polished_message = polish_with_ai(base_message, use_ai=USE_AI_POLISHING, repetition_count=0)
        response.message(polished_message)
        return str(response)
    
    # Check for cancellation
    if is_cancellation(message_body):
        # Try to cancel any existing booking
        try:
            execute_query('DELETE FROM pending_confirmations WHERE phone_number = %s', (phone_number,))
            execute_query('DELETE FROM message_tracking WHERE phone_number = %s', (phone_number,))
        except:
            pass
        
        response = MessagingResponse()
        base_message = get_message_template('booking_cancelled')
        polished_message = polish_with_ai(base_message, use_ai=USE_AI_POLISHING, repetition_count=0)
        response.message(polished_message)
        return str(response)
    
    # Admin commands
    if phone_number in AUTHORIZED_ADMIN_NUMBERS:
        if message_body.startswith('LOCATION '):
            location_data = message_body[9:].strip()  # Remove "LOCATION " prefix
            
            # Extract intercom if present
            intercom_match = re.search(r'INTERCOM\s+(\w+)', location_data, re.IGNORECASE)
            intercom_number = intercom_match.group(1) if intercom_match else None
            
            # Remove INTERCOM part from location_data
            if intercom_number:
                location_data = re.sub(r'\s*INTERCOM\s+\w+', '', location_data, flags=re.IGNORECASE).strip()
            
            # Parse: "City: Address" format
            if ':' in location_data:
                parts = location_data.split(':', 1)
                city = parts[0].strip().title()  # Normalize to title case (Perth, Sydney, etc)
                address = parts[1].strip()
            else:
                # If no colon, assume it's just address and keep current city
                city = get_current_incall_location()['city']
                address = location_data
            
            # Update location (timezone will be auto-set based on city)
            new_timezone = update_incall_location(city, address, intercom_number)
            
            response = MessagingResponse()
            response.message(f"✅ Location updated!\n\nCity: {city}\nAddress: {address}\nTimezone: {new_timezone}")
            return str(response)
    
    # Get pending confirmation
    pending = get_pending_confirmation(phone_number)
    
    # Handle YES confirmation
    if 'yes' in message_body.lower() and pending:
        client_name = extract_name_from_yes_message(message_body)
        
        if not client_name:
            response = MessagingResponse()
            base_message = get_message_template('need_name')
            polished_message = polish_with_ai(base_message, use_ai=USE_AI_POLISHING, repetition_count=0)
            response.message(polished_message)
            return str(response)
        
        booking_status = pending.get('booking_status')
        
        # Check 3-strike rule: if we asked for info 3+ times, mandatory deposit
        attempts = get_booking_attempts(phone_number)
        three_strikes_triggered = attempts >= 3
        
        # Determine if deposit is mandatory
        deposit_mandatory = (
            pending.get('incall_outcall') == 'Outcall' or
            detect_profanity(message_body) or
            three_strikes_triggered  # NEW: 3-strike rule
        )
        
        if deposit_mandatory or booking_status == 'peacock_available':
            # Mandatory deposit required
            save_pending_confirmation(phone_number, pending, booking_status='deposit_required',
                                    peacock_event_id=pending.get('peacock_event_id'),
                                    client_name=client_name)
            
            deposit_reason = ""
            if three_strikes_triggered:
                deposit_reason = " (Due to multiple information requests)"
            
            response = MessagingResponse()
            base_message = f"{client_name}, {get_message_template('deposit_mandatory')}{deposit_reason}"
            polished_message = polish_with_ai(base_message, use_ai=USE_AI_POLISHING, repetition_count=0)
            response.message(polished_message)
            return str(response)
        else:
            # Book without mandatory deposit
            create_calendar_event(pending, phone_number, is_confirmed=False, client_name=client_name)
            
            location = get_current_incall_location()
            # Don't include room details in initial confirmation
            location_msg = format_location_for_confirmation(
                location, pending.get('incall_outcall'), pending.get('outcall_address'), 
                include_room_details=False
            )
            
            booking_datetime = f"{pending['date']} {pending['time']}"
            
            base_confirmation = f"Booking confirmed for {client_name}.{location_msg}\n\n{pending['date']} at {pending['time']}\n{pending['duration']}, {pending['experience_type']}"
            
            response = MessagingResponse()
            polished_confirmation = polish_with_ai(base_confirmation, use_ai=USE_AI_POLISHING, repetition_count=0)
            response.message(polished_confirmation)
            
            # Schedule room details to be sent 1 hour before booking
            if pending.get('incall_outcall') == 'Incall':
                schedule_room_detail_reminder(
                    phone_number, client_name, booking_datetime,
                    pending.get('city'), pending.get('incall_outcall'),
                    pending.get('outcall_address')
                )
            
            # Send NON-MANDATORY deposit request (YOUR EXACT WORDING)
            deposit_msg = get_message_template('deposit_non_mandatory')
            polished_deposit = polish_with_ai(deposit_msg, use_ai=USE_AI_POLISHING, repetition_count=0)
            send_sms(phone_number, polished_deposit)
            
            save_deposit_request(phone_number, booking_datetime)
            
            # Clear booking progress - we have a confirmed booking now
            clear_booking_progress(phone_number)
            
            # Initialize message tracking for this booking (for 3+ message rule)
            try:
                execute_query('DELETE FROM message_tracking WHERE phone_number = %s', (phone_number,))
                execute_query('INSERT INTO message_tracking (phone_number, message_count) VALUES (%s, 0)',
                             (phone_number,))
            except:
                pass
            
            delete_pending_confirmation(phone_number)
            
            return str(response)
    
    # Handle DEPOSIT keyword
    if message_body.upper().strip() == 'DEPOSIT' and pending:
        payment_reference = f"{pending['date']} {pending['time']}"
        
        response = MessagingResponse()
        response.message(f"PayID: {PAYID_EMAIL}\nReference: {payment_reference}\n\nSend screenshot when done.")
        
        save_pending_confirmation(phone_number, pending,
                                booking_status=pending.get('booking_status'),
                                awaiting_deposit=True,
                                peacock_event_id=pending.get('peacock_event_id'),
                                client_name=pending.get('client_name'))
        
        return str(response)
    
    # Check for post-booking message limit (3+ messages)
    limit_reached, enquiry_sent = check_post_booking_message_limit(phone_number)
    if limit_reached and not enquiry_sent:
        response = MessagingResponse()
        response.message("For specific questions or to speak directly with Adella, text ENQUIRY followed by your question.\n\nExample: ENQUIRY What services do you offer?")
        
        execute_query('UPDATE message_tracking SET enquiry_sent = 1 WHERE phone_number = %s',
                     (phone_number,))
        
        return str(response)
    
    # Increment post-booking message count if booking exists
    result = execute_query('SELECT 1 FROM message_tracking WHERE phone_number = %s', (phone_number,), fetch=True)
    if result:
        increment_post_booking_messages(phone_number)
    
    # First message from new client
    previous_messages = get_message_count(phone_number)
    if previous_messages == 1:
        response = MessagingResponse()
        message = get_message_template('first_contact')
        # Polish with warm tone (first contact, repetition_count=0)
        polished_message = polish_with_ai(message, use_ai=USE_AI_POLISHING, repetition_count=0)
        response.message(polished_message)
        return str(response)
    
    # Extract booking details from THIS message
    new_details = extract_booking_details(message_body)
    
    # Get any previously saved progress
    existing_progress = get_booking_progress(phone_number)
    
    # Check if message contains ANY booking-related info
    has_booking_info = any(new_details.values())
    
    # If no booking info in message and no existing progress, they're sending random text
    if not has_booking_info and not existing_progress:
        # Get how many times we've asked for booking info
        attempts = get_booking_attempts(phone_number)
        
        # After 5+ attempts, send final message and BLOCK the number
        if attempts >= 5:
            response = MessagingResponse()
            # Use YOUR exact wording from template
            final_message = get_message_template('fifth_message_block')
            # AI only adjusts tone, keeps your exact message
            polished_message = polish_with_ai(final_message, use_ai=USE_AI_POLISHING, repetition_count=6)
            response.message(polished_message)
            
            # Block the number
            block_phone_number(phone_number, "5+ repeated requests without providing booking details")
            
            return str(response)
        
        # After 4 attempts, send automated ENQUIRY message instead
        if attempts >= 4:
            response = MessagingResponse()
            # Use YOUR exact wording from template
            auto_message = get_message_template('fourth_message_enquiry')
            # AI only adjusts tone, keeps your exact message
            polished_message = polish_with_ai(auto_message, use_ai=USE_AI_POLISHING, repetition_count=5)
            response.message(polished_message)
            
            # Increment so next message triggers block
            increment_booking_attempts(phone_number)
            
            return str(response)
        
        response = MessagingResponse()
        # Use YOUR exact wording from template
        base_message = get_message_template('random_request')
        # AI only adjusts tone (warm/direct/firm), keeps your exact fields and structure
        polished_message = polish_with_ai(base_message, use_ai=USE_AI_POLISHING, repetition_count=attempts)
        response.message(polished_message)
        
        # Increment attempts for tone progression
        increment_booking_attempts(phone_number)
        
        return str(response)
    
    # If no new booking info but they have existing progress, they're still being random
    if not has_booking_info and existing_progress:
        # Get current attempt count
        attempts = get_booking_attempts(phone_number)
        
        # After 5+ attempts, send final message and BLOCK the number
        if attempts >= 5:
            response = MessagingResponse()
            # Use YOUR exact wording from template
            final_message = get_message_template('fifth_message_block')
            # AI only adjusts tone, keeps your exact message
            polished_message = polish_with_ai(final_message, use_ai=USE_AI_POLISHING, repetition_count=6)
            response.message(polished_message)
            
            # Block the number
            block_phone_number(phone_number, "5+ repeated requests without completing booking details")
            
            return str(response)
        
        # After 4 attempts, send automated ENQUIRY message instead
        if attempts >= 4:
            response = MessagingResponse()
            # Use YOUR exact wording from template
            auto_message = get_message_template('fourth_message_enquiry')
            # AI only adjusts tone, keeps your exact message
            polished_message = polish_with_ai(auto_message, use_ai=USE_AI_POLISHING, repetition_count=5)
            response.message(polished_message)
            
            # Increment so next message triggers block
            increment_booking_attempts(phone_number)
            
            return str(response)
        
        # They have partial booking, ask for what's still missing
        required = ['city', 'date', 'time', 'duration', 'experience_type', 'incall_outcall']
        missing = [field for field in required if not existing_progress.get(field)]
        
        if existing_progress.get('incall_outcall') == 'Outcall' and not existing_progress.get('outcall_address'):
            missing.append('outcall_address')
        
        if missing:
            # Increment attempt counter
            increment_booking_attempts(phone_number)
            
            response = MessagingResponse()
            
            # Build smart question asking only for what's missing
            missing_questions = []
            if 'city' in missing:
                missing_questions.append("City")
            if 'date' in missing:
                missing_questions.append("Date")
            if 'time' in missing:
                missing_questions.append("Time")
            if 'duration' in missing:
                missing_questions.append("Duration")
            if 'experience_type' in missing:
                missing_questions.append("Experience (GFE/PSE)")
            if 'incall_outcall' in missing:
                missing_questions.append("Incall or Outcall")
            if 'outcall_address' in missing:
                missing_questions.append("Outcall address")
            
            # Create base message
            if len(missing_questions) == 1:
                base_msg = f"I just need your {missing_questions[0]}"
            elif len(missing_questions) == 2:
                base_msg = f"I just need your {missing_questions[0]} and {missing_questions[1]}"
            else:
                base_msg = f"I still need: {', '.join(missing_questions)}"
            
            # Polish with tone progression
            polished_msg = polish_with_ai(base_msg, use_ai=USE_AI_POLISHING, repetition_count=attempts)
            response.message(polished_msg)
            return str(response)
    
    # Merge new details with existing progress
    if existing_progress:
        merged_details = {}
        for key in ['city', 'date', 'time', 'duration', 'experience_type', 'incall_outcall', 'outcall_address']:
            # Use new value if provided, otherwise use existing
            merged_details[key] = new_details.get(key) or existing_progress.get(key)
    else:
        merged_details = new_details
    
    # Save the merged progress
    if any(merged_details.values()):
        save_booking_progress(phone_number, merged_details)
    
    # Check what's still missing
    required = ['city', 'date', 'time', 'duration', 'experience_type', 'incall_outcall']
    missing = [field for field in required if not merged_details.get(field)]
    
    if merged_details.get('incall_outcall') == 'Outcall' and not merged_details.get('outcall_address'):
        missing.append('outcall_address')
    
    if missing:
        # Get current attempt count for tone progression
        attempts = get_booking_attempts(phone_number)
        
        # After 5+ attempts, send final message and BLOCK the number
        if attempts >= 5:
            response = MessagingResponse()
            # Use YOUR exact wording from template
            final_message = get_message_template('fifth_message_block')
            # AI only adjusts tone, keeps your exact message
            polished_message = polish_with_ai(final_message, use_ai=USE_AI_POLISHING, repetition_count=6)
            response.message(polished_message)
            
            # Block the number
            block_phone_number(phone_number, "5+ repeated requests without completing booking details")
            
            return str(response)
        
        # After 4 attempts, send automated ENQUIRY message instead
        if attempts >= 4:
            response = MessagingResponse()
            # Use YOUR exact wording from template
            auto_message = get_message_template('fourth_message_enquiry')
            # AI only adjusts tone, keeps your exact message
            polished_message = polish_with_ai(auto_message, use_ai=USE_AI_POLISHING, repetition_count=5)
            response.message(polished_message)
            
            # Increment so next message triggers block
            increment_booking_attempts(phone_number)
            
            return str(response)
        
        # Increment attempt counter
        increment_booking_attempts(phone_number)
        
        response = MessagingResponse()
        
        # Build smart question asking only for what's missing
        missing_questions = []
        if 'city' in missing:
            missing_questions.append("City")
        if 'date' in missing:
            missing_questions.append("Date")
        if 'time' in missing:
            missing_questions.append("Time")
        if 'duration' in missing:
            missing_questions.append("Duration")
        if 'experience_type' in missing:
            missing_questions.append("Experience (GFE/PSE)")
        if 'incall_outcall' in missing:
            missing_questions.append("Incall or Outcall")
        if 'outcall_address' in missing:
            missing_questions.append("Outcall address")
        
        # Create base message
        if len(missing_questions) == 1:
            base_msg = f"I just need your {missing_questions[0]}"
        elif len(missing_questions) == 2:
            base_msg = f"I just need your {missing_questions[0]} and {missing_questions[1]}"
        else:
            base_msg = f"I still need: {', '.join(missing_questions)}"
        
        # Polish with tone progression based on attempts
        polished_msg = polish_with_ai(base_msg, use_ai=USE_AI_POLISHING, repetition_count=attempts)
        response.message(polished_msg)
        return str(response)
    
    # All required fields present - time is available
    # Save pending confirmation with complete details
    save_pending_confirmation(phone_number, merged_details, 'available')
    
    # Clear booking attempts - we have all info now
    try:
        execute_query('UPDATE message_tracking SET message_count = 0 WHERE phone_number = %s', (phone_number,))
    except:
        pass
    
    response = MessagingResponse()
    base_message = get_message_template('time_available', merged_details)
    # Polish with warm tone (first time telling availability, repetition_count=0)
    polished_message = polish_with_ai(base_message, use_ai=USE_AI_POLISHING, repetition_count=0)
    response.message(polished_message)
    
    return str(response)

# ============================================================================
# WEB DASHBOARD
# ============================================================================

@app.route('/booking', methods=['GET', 'POST'])
def booking_form():
    """Webform for clients to submit booking details"""
    
    # Get phone number from query parameter
    phone = request.args.get('phone', '')
    
    if request.method == 'POST':
        # Get form data
        phone_number = request.form.get('phone')
        date_str = request.form.get('date')
        time_str = request.form.get('time')
        duration = request.form.get('duration')
        experience = request.form.get('experience')
        incall_outcall = request.form.get('incall_outcall')
        outcall_address = request.form.get('outcall_address', '')
        total_price = request.form.get('total_price', '')
        
        # Format the booking details
        booking_details = {
            'date': date_str,
            'time': time_str,
            'duration': duration,
            'experience_type': experience,
            'incall_outcall': incall_outcall,
            'outcall_address': outcall_address if incall_outcall == 'Outcall' else None,
            'city': get_current_incall_location()['city'],  # Use current city
            'price': total_price
        }
        
        # Save to pending confirmations
        save_pending_confirmation(phone_number, booking_details, 'available')
        
        # Send SMS with time available message
        message = get_message_template('time_available', booking_details)
        send_sms(phone_number, message)
        
        # Show success page
        return render_template_string("""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Booking Submitted</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                * { margin: 0; padding: 0; box-sizing: border-box; }
                body {
                    font-family: 'Segoe UI', sans-serif;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 20px;
                }
                .container {
                    background: white;
                    padding: 40px;
                    border-radius: 20px;
                    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                    max-width: 500px;
                    text-align: center;
                }
                h1 { color: #667eea; margin-bottom: 20px; font-size: 32px; }
                p { color: #666; font-size: 16px; line-height: 1.6; margin-bottom: 15px; }
                .success { color: #2e7d32; font-size: 64px; margin-bottom: 20px; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="success">✅</div>
                <h1>Booking Request Sent!</h1>
                <p>I've sent you an SMS with availability details.</p>
                <p>Please check your phone and reply with your name + YES to confirm.</p>
                <p style="margin-top: 30px; font-size: 14px; color: #999;">You can close this window now.</p>
            </div>
        </body>
        </html>
        """)
    
    # Display booking form
    location = get_current_incall_location()
    
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Book with Adella</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            .container {
                max-width: 600px;
                margin: 0 auto;
                background: white;
                padding: 30px;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            }
            h1 {
                color: #333;
                margin-bottom: 10px;
                font-size: 28px;
                text-align: center;
            }
            .subtitle {
                text-align: center;
                color: #666;
                margin-bottom: 30px;
                font-size: 14px;
            }
            .location-info {
                background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
                padding: 20px;
                border-radius: 15px;
                margin-bottom: 25px;
                border-left: 5px solid #667eea;
            }
            .location-info p {
                margin: 8px 0;
                color: #555;
                font-size: 15px;
            }
            .price-display {
                background: linear-gradient(135deg, #ffd89b 0%, #19547b 100%);
                padding: 20px;
                border-radius: 15px;
                margin-bottom: 25px;
                text-align: center;
                border: 3px solid #f39c12;
            }
            .price-display h2 {
                color: white;
                font-size: 24px;
                margin-bottom: 5px;
            }
            .price-display .price {
                color: white;
                font-size: 42px;
                font-weight: bold;
                text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
            }
            .price-display .note {
                color: rgba(255,255,255,0.9);
                font-size: 13px;
                margin-top: 10px;
            }
            .form-group {
                margin-bottom: 20px;
            }
            label {
                display: block;
                margin-bottom: 8px;
                color: #333;
                font-weight: 600;
                font-size: 14px;
            }
            input, select, textarea {
                width: 100%;
                padding: 12px 15px;
                border: 2px solid #e0e0e0;
                border-radius: 10px;
                font-size: 15px;
                font-family: inherit;
                transition: border-color 0.3s;
            }
            input:focus, select:focus, textarea:focus {
                outline: none;
                border-color: #667eea;
            }
            select {
                cursor: pointer;
            }
            button {
                width: 100%;
                padding: 16px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 10px;
                font-size: 18px;
                font-weight: 600;
                cursor: pointer;
                transition: transform 0.2s;
            }
            button:hover {
                transform: translateY(-2px);
            }
            button:disabled {
                opacity: 0.6;
                cursor: not-allowed;
            }
            .example {
                font-size: 12px;
                color: #666;
                margin-top: 5px;
                font-style: italic;
            }
            .required { color: #c62828; }
            #outcallAddress {
                display: none;
            }
            .service-price {
                font-size: 12px;
                color: #2e7d32;
                font-weight: 600;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>💜 Book with Adella</h1>
            <p class="subtitle">Select your preferences below</p>
            
            <div class="location-info">
                <p><strong>📍 Currently in:</strong> {{ location.city }}</p>
                <p><strong>🏨 Hotel:</strong> {{ location.address.split(',')[0] }}</p>
            </div>
            
            <div class="price-display">
                <h2>Total Investment</h2>
                <div class="price" id="totalPrice">$0</div>
                <p class="note" id="priceBreakdown">Select service and duration to see price</p>
            </div>
            
            <form method="POST" id="bookingForm">
                <input type="hidden" name="total_price" id="totalPriceInput" value="">
                
                <div class="form-group">
                    <label>📱 Phone Number <span class="required">*</span></label>
                    <input type="tel" name="phone" value="{{ phone }}" required placeholder="+61412345678">
                    <p class="example">Include country code (e.g., +61)</p>
                </div>
                
                <div class="form-group">
                    <label>💕 Experience <span class="required">*</span></label>
                    <select name="experience" id="experience" required onchange="calculatePrice()">
                        <option value="">Select experience...</option>
                        <option value="GFE">Girlfriend Experience (GFE)</option>
                        <option value="GFE + BBBJ + CIM">GFE + BBBJ + CIM</option>
                        <option value="PSE">Pornstar Experience (PSE)</option>
                        <option value="PSE with filming">PSE with Filming</option>
                        <option value="Couples MFF">Couples - MFF Threesome</option>
                        <option value="MMF threesome">MMF Threesome</option>
                        <option value="Duos with escort">Duos with Another Escort</option>
                        <option value="Dinner Date">Dinner Date (1hr dinner + 1hr PSE dessert)</option>
                        <option value="Overnight">Overnight (12 hours, 4hrs min sleep)</option>
                        <option value="Weekend">Weekend (48 hours dirty weekend)</option>
                        <option value="Fly Me To You">Fly Me To You (flights + overnight)</option>
                        <option value="Holiday Barbie">Holiday Barbie (exotic holidays)</option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label>⏱️ Duration <span class="required">*</span></label>
                    <select name="duration" id="duration" required onchange="calculatePrice()">
                        <option value="">Select duration...</option>
                        <option value="30 minutes">30 minutes</option>
                        <option value="1 hour">1 hour</option>
                        <option value="2 hours">2 hours</option>
                        <option value="12 hours">12 hours (Overnight)</option>
                        <option value="48 hours">48 hours (Weekend)</option>
                    </select>
                    <p class="example" id="durationNote">Duration varies by service</p>
                </div>
                
                <div class="form-group">
                    <label>📍 Location Type <span class="required">*</span></label>
                    <select name="incall_outcall" id="incallOutcall" required onchange="toggleOutcallAddress(); calculatePrice();">
                        <option value="">Select location type...</option>
                        <option value="Incall">Incall (Visit me at my hotel)</option>
                        <option value="Outcall">Outcall (I'll come to you) +$100</option>
                    </select>
                </div>
                
                <div class="form-group" id="outcallAddress">
                    <label>🏠 Your Address <span class="required">*</span></label>
                    <textarea name="outcall_address" rows="3" placeholder="123 Main St, City, Postcode"></textarea>
                    <p class="example">Full address for outcall visits</p>
                </div>
                
                <div class="form-group">
                    <label>📅 Date <span class="required">*</span></label>
                    <input type="date" name="date" required>
                </div>
                
                <div class="form-group">
                    <label>🕐 Time <span class="required">*</span></label>
                    <input type="time" name="time" required>
                </div>
                
                <button type="submit" id="submitBtn">📨 Submit Booking Request</button>
            </form>
        </div>
        
        <script>
            // Comprehensive pricing structure
            const pricing = {
                'GFE': {
                    '30 minutes': 350,
                    '1 hour': 500
                },
                'GFE + BBBJ + CIM': {
                    '30 minutes': 400,
                    '1 hour': 600
                },
                'PSE': {
                    '30 minutes': 500,
                    '1 hour': 800
                },
                'PSE with filming': {
                    '1 hour': 1000
                },
                'Couples MFF': {
                    '1 hour': 900
                },
                'MMF threesome': {
                    '30 minutes': 900,
                    '1 hour': 1600
                },
                'Duos with escort': {
                    '30 minutes': 800,
                    '1 hour': 1200
                },
                'Dinner Date': {
                    '2 hours': 1000
                },
                'Overnight': {
                    '12 hours': 4000
                },
                'Weekend': {
                    '48 hours': 9000
                },
                'Fly Me To You': {
                    '12 hours': 5000
                },
                'Holiday Barbie': {
                    '48 hours': 5000
                }
            };
            
            function calculatePrice() {
                const experience = document.getElementById('experience').value;
                const duration = document.getElementById('duration').value;
                const incallOutcall = document.getElementById('incallOutcall').value;
                
                if (!experience || !duration) {
                    document.getElementById('totalPrice').textContent = '$0';
                    document.getElementById('priceBreakdown').textContent = 'Select service and duration to see price';
                    document.getElementById('submitBtn').disabled = true;
                    return;
                }
                
                // Get base price
                let basePrice = 0;
                if (pricing[experience] && pricing[experience][duration]) {
                    basePrice = pricing[experience][duration];
                } else {
                    document.getElementById('totalPrice').textContent = 'N/A';
                    document.getElementById('priceBreakdown').textContent = 'This duration not available for selected service';
                    document.getElementById('submitBtn').disabled = true;
                    return;
                }
                
                // Add outcall surcharge
                let totalPrice = basePrice;
                let breakdown = `Base: $${basePrice}`;
                
                if (incallOutcall === 'Outcall') {
                    totalPrice += 100;
                    breakdown += ` + Outcall: $100`;
                }
                
                // Update display
                document.getElementById('totalPrice').textContent = `$${totalPrice}`;
                document.getElementById('priceBreakdown').textContent = breakdown;
                document.getElementById('totalPriceInput').value = totalPrice;
                document.getElementById('submitBtn').disabled = false;
            }
            
            function toggleOutcallAddress() {
                const select = document.getElementById('incallOutcall');
                const addressDiv = document.getElementById('outcallAddress');
                const addressField = addressDiv.querySelector('textarea');
                
                if (select.value === 'Outcall') {
                    addressDiv.style.display = 'block';
                    addressField.required = true;
                } else {
                    addressDiv.style.display = 'none';
                    addressField.required = false;
                }
            }
            
            // Initialize
            document.getElementById('submitBtn').disabled = true;
        </script>
    </body>
    </html>
    """
    
    return render_template_string(html, location=location, phone=phone)

@app.route('/admin', methods=['GET', 'POST'])
def admin_dashboard():
    """Admin panel for location management"""
    error = None
    success = None
    
    if request.method == 'POST':
        password = request.form.get('password')
        city = request.form.get('city')
        new_address = request.form.get('address')
        intercom_number = request.form.get('intercom_number')
        
        if password == ADMIN_PASSWORD:
            if city and new_address:
                new_timezone = update_incall_location(city, new_address, intercom_number)
                success = f"Location updated! Timezone: {new_timezone}"
            else:
                error = "Please provide city and address"
        else:
            error = "Invalid password"
    
    location = get_current_incall_location()
    
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Location Manager - Adella SMS Bot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 20px;
            }
            .container {
                background: white;
                padding: 40px;
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                max-width: 600px;
                width: 100%;
            }
            h1 { 
                color: #333; 
                margin-bottom: 10px;
                font-size: 28px;
            }
            .subtitle {
                color: #666;
                margin-bottom: 30px;
                font-size: 14px;
            }
            .current { 
                background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
                padding: 25px; 
                border-radius: 15px; 
                margin-bottom: 30px;
                border-left: 5px solid #667eea;
            }
            .current h3 {
                color: #333;
                margin-bottom: 15px;
                font-size: 18px;
            }
            .current p {
                margin: 8px 0;
                color: #555;
                font-size: 15px;
            }
            .current strong {
                color: #333;
                display: inline-block;
                min-width: 100px;
            }
            .info-box {
                background: #e3f2fd;
                padding: 15px;
                border-radius: 10px;
                margin-bottom: 20px;
                border-left: 4px solid #2196F3;
            }
            .info-box p {
                margin: 5px 0;
                color: #1565c0;
                font-size: 13px;
            }
            .form-group { margin-bottom: 20px; }
            label { 
                display: block; 
                margin-bottom: 8px; 
                color: #333; 
                font-weight: 600;
                font-size: 14px;
            }
            input, select, textarea {
                width: 100%;
                padding: 12px 15px;
                border: 2px solid #e0e0e0;
                border-radius: 10px;
                font-size: 15px;
                transition: border-color 0.3s;
                font-family: inherit;
            }
            input:focus, select:focus, textarea:focus {
                outline: none;
                border-color: #667eea;
            }
            textarea {
                min-height: 80px;
                resize: vertical;
            }
            button {
                width: 100%;
                padding: 16px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 10px;
                font-size: 16px;
                font-weight: 600;
                cursor: pointer;
                transition: transform 0.2s, box-shadow 0.2s;
            }
            button:hover {
                transform: translateY(-2px);
                box-shadow: 0 10px 20px rgba(102, 126, 234, 0.4);
            }
            button:active {
                transform: translateY(0);
            }
            .alert { 
                padding: 15px 20px; 
                border-radius: 10px; 
                margin-bottom: 20px;
                font-size: 14px;
                font-weight: 500;
            }
            .alert-error { 
                background: #ffebee; 
                color: #c62828;
                border-left: 4px solid #c62828;
            }
            .alert-success { 
                background: #e8f5e9; 
                color: #2e7d32;
                border-left: 4px solid #2e7d32;
            }
            .example {
                font-size: 12px;
                color: #666;
                margin-top: 5px;
                font-style: italic;
            }
            .tip {
                background: #fff3cd;
                padding: 12px;
                border-radius: 8px;
                margin-top: 20px;
                border-left: 4px solid #ffc107;
                font-size: 13px;
                color: #856404;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🏨 Location Manager</h1>
            <p class="subtitle">Update your current location for client bookings</p>
            
            {% if error %}
            <div class="alert alert-error">❌ {{ error }}</div>
            {% endif %}
            
            {% if success %}
            <div class="alert alert-success">✅ {{ success }}</div>
            {% endif %}
            
            <div class="current">
                <h3>📍 Current Location</h3>
                <p><strong>City:</strong> {{ location.city }}</p>
                <p><strong>Address:</strong> {{ location.address }}</p>
                <p><strong>Intercom:</strong> {{ location.intercom_number }}</p>
                <p><strong>Timezone:</strong> {{ location.timezone }}</p>
            </div>
            
            <div class="info-box">
                <p><strong>ℹ️ How this appears to clients:</strong></p>
                <p>• <strong>First message:</strong> "I'm currently located in {{ location.city }} at {{ location.address.split(',')[0] }}"</p>
                <p>• <strong>Booking confirmation:</strong> "Room details will be sent 1 hour before"</p>
                <p>• <strong>1 hour before:</strong> Full address + room number sent automatically</p>
            </div>
            
            <form method="POST">
                <div class="form-group">
                    <label>🔐 Password</label>
                    <input type="password" name="password" required placeholder="Enter admin password">
                </div>
                
                <div class="form-group">
                    <label>🌆 City</label>
                    <select name="city" required>
                        <option value="">Select city...</option>
                        {% for city in cities %}
                        <option value="{{ city }}" {% if city == location.city %}selected{% endif %}>{{ city }}</option>
                        {% endfor %}
                    </select>
                    <p class="example">This sets the timezone automatically</p>
                </div>
                
                <div class="form-group">
                    <label>🏨 Hotel Address</label>
                    <textarea name="address" required placeholder="Hotel Name, Street Address">{{ location.address }}</textarea>
                    <p class="example">Example: Hyatt Regency, 99 Adelaide Terrace</p>
                </div>
                
                <div class="form-group">
                    <label>🔢 Room Number / Intercom</label>
                    <input type="text" name="intercom_number" value="{{ location.intercom_number }}" required placeholder="512">
                    <p class="example">This is sent 1 hour before booking only</p>
                </div>
                
                <button type="submit">💾 Update Location</button>
                
                <div class="tip">
                    <strong>💡 Tip:</strong> You can also update via SMS by texting:<br>
                    <code>LOCATION Perth: Hyatt Regency, 99 Adelaide Terrace INTERCOM 512</code>
                </div>
            </form>
        </div>
    </body>
    </html>
    """
    
    return render_template_string(html, location=location, cities=list(CITY_TIMEZONES.keys()), error=error, success=success)

@app.route('/messages', methods=['GET'])
def view_messages():
    """View messages dashboard"""
    messages = execute_query('SELECT * FROM messages ORDER BY timestamp DESC LIMIT 100', fetch=True)
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Messages</title>
    <style>
        body {{ font-family: Arial; margin: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; }}
        table {{ border-collapse: collapse; width: 100%; background: white; }}
        th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
        th {{ background-color: #4CAF50; color: white; }}
    </style>
</head>
<body>
    <h1>📊 Messages</h1>
    <table>
        <tr><th>Phone</th><th>Message</th><th>Time</th></tr>"""
    
    for msg in messages:
        if USE_POSTGRES:
            html += f"<tr><td>{msg['phone_number']}</td><td>{msg['message_body']}</td><td>{msg['timestamp']}</td></tr>"
        else:
            html += f"<tr><td>{msg[1]}</td><td>{msg[2]}</td><td>{msg[3]}</td></tr>"
    
    html += "</table></body></html>"
    return html

@app.route('/test', methods=['GET'])
def test():
    return "✅ SMS Booking Bot - Production Ready!"

@app.route('/check-reminders', methods=['GET'])
def check_reminders():
    """Endpoint to check and send room detail reminders - call this hourly via cron"""
    try:
        check_and_send_room_details()
        return "✅ Reminders checked"
    except Exception as e:
        return f"❌ Error: {e}", 500

# ============================================================================
# STARTUP
# ============================================================================

if __name__ == '__main__':
    print("\n" + "="*60)
    print("🤖 SMS BOOKING BOT - PRODUCTION VERSION")
    print("="*60)
    print(f"Database: {'PostgreSQL (Railway)' if USE_POSTGRES else 'SQLite (Local)'}")
    print(f"Claude AI: {'✅ Enabled' if claude_client else '❌ Disabled'}")
    print(f"Gemini AI: {'✅ Enabled' if gemini_model else '❌ Disabled'}")
    print(f"Twilio: {'✅ Enabled' if twilio_client else '❌ Disabled'}")
    print("="*60 + "\n")
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
