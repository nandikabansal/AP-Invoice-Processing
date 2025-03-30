from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.errors import HttpError
import base64
import pickle
import os
import pymongo
from datetime import datetime, timedelta
import re
import json
import google.generativeai as genai
import io
import random
from forex_python.converter import CurrencyRates
import time
from functools import wraps

from invoice_validator import InvoiceValidator

# Define scopes - we need read access to Gmail
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

# Gemini API configuration
GEMINI_API_KEY = "YOUR API KEY HERE"

# MongoDB connection string
MONGO_URI = "YOUR MONGO DB PUBLIC URL HERE"

# Sample data
currencies = ["INR", "USD", "EUR", "GBP"]
payment_terms = ["NET30", "NET60", "Due on Receipt", "NET15"]
invoice_types = ["Credit Memo", "Standard", "Debit Memo", "Pro Forma"]
descriptions = ["Consulting Fee", "IT Services", "Software License", "Hardware Purchase", "Maintenance Contract"]
vendors = ["ABC Corp", "XYZ Inc", "Global Supplies", "NextGen Solutions"]

# Fallback exchange rates (update these periodically)
FALLBACK_RATES = {
    "INR": 0.012,  # 1 INR = 0.012 USD
    "EUR": 1.07,   # 1 EUR = 1.07 USD
    "GBP": 1.27    # 1 GBP = 1.27 USD
}

def clean_currency_code(currency_input):
    """Ensures currency codes are clean 3-letter formats"""
    if not currency_input:
        return "INR"
    clean_code = ''.join([c for c in str(currency_input) if c.isalpha()]).upper()
    return clean_code if clean_code in currencies else "INR"

def retry(max_retries=3, delay=1):
    """Decorator for retrying failed API calls with exponential backoff"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    retries += 1
                    if retries == max_retries:
                        print(f"Max retries reached for {func.__name__}. Error: {str(e)}")
                        raise
                    time.sleep(delay * retries)
        return wrapper
    return decorator

@retry(max_retries=3, delay=1)
def convert_currency_with_api(amount, from_currency, to_currency="USD"):
    """Convert currency using forex-python with retry logic"""
    c = CurrencyRates()
    return c.convert(from_currency, to_currency, amount)

def convert_invoice_to_usd_and_status(invoice_doc):
    """Convert invoice amount to USD with fallback mechanism"""
    hdr = invoice_doc.get("invoice_header", {})
    amount = hdr.get("invoice_amount")
    code = hdr.get("currency_code", "USD").upper()
    
    # Initialize default values
    hdr["to_usd"] = None
    hdr["invoice_status"] = "pending"
    
    if not amount:
        return invoice_doc
    
    try:
        if code != "USD":
            try:
                # First try with live API
                usd_amount = convert_currency_with_api(amount, code, "USD")
                hdr["to_usd"] = round(usd_amount, 2)
            except:
                # Fallback to hardcoded rates if API fails
                if code in FALLBACK_RATES:
                    hdr["to_usd"] = round(amount * FALLBACK_RATES[code], 2)
                    print(f"Used fallback rate for {code}->USD conversion")
                else:
                    print(f"No conversion rate available for {code}")
        else:
            hdr["to_usd"] = amount
    except Exception as e:
        print("Currency conversion error:", e)
    
    return invoice_doc

def process_pdf_attachment(attachment_data):
    """Process PDF attachment data and return properly encoded base64 string."""
    try:
        # Decode the URL-safe base64 data
        pdf_content = base64.urlsafe_b64decode(attachment_data)
        
        # Re-encode as standard base64
        pdf_base64 = base64.b64encode(pdf_content).decode('utf-8')
        
        return pdf_base64
    except Exception as e:
        print(f"Error processing PDF attachment: {e}")
        return None

def authenticate_gmail():
    """Authenticates with Gmail API and returns the service object."""
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'client_secret_202402310349-p1kjg7kcs1f5eb19qg9rvklje3168i6k.apps.googleusercontent.com.json', 
                SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    
    return build('gmail', 'v1', credentials=creds)

def connect_to_mongodb():
    """Connects to the specified MongoDB database."""
    try:
        client = pymongo.MongoClient(MONGO_URI)
        client.server_info()
        return client["invoice_automation"]
    except Exception as e:
        print(f"Failed to connect to MongoDB: {e}")
        raise

def get_emails_with_invoice_in_subject(service, start_date):
    """Retrieves emails where 'invoice' appears in the subject (case-insensitive)."""
    try:
        formatted_date = start_date.strftime('%Y/%m/%d')
        query = f"after:{formatted_date} subject:invoice"
        results = service.users().messages().list(
            userId='me', 
            q=query,
            maxResults=500
        ).execute()
        return results.get('messages', [])
    except HttpError as error:
        print(f"An error occurred: {error}")
        return []

def initialize_gemini():
    """Initialize Gemini API."""
    genai.configure(api_key=GEMINI_API_KEY)
    return genai.GenerativeModel('gemini-1.5-flash')

def process_pdf_with_gemini(pdf_content):
    """Process a PDF with Gemini API to extract invoice information."""
    model = initialize_gemini()
    
    prompt = """
    Analyze this invoice PDF and extract the following information in JSON format:
    1. Invoice number
    2. Invoice date
    3. Vendor name
    4. Total invoice amount (numeric only)
    5. Currency (3-letter code only)
    6. Line items (with description, quantity, unit price, and line amount)
    
    Format the response as a valid JSON object with these fields:
    {
        "invoice_num": "",
        "invoice_date": "YYYY-MM-DD",
        "vendor_name": "",
        "invoice_amount": 0.0,
        "currency_code": "",
        "line_items": [
            {
                "description": "",
                "quantity": 0,
                "unit_price": 0.0,
                "line_amount": 0.0
            }
        ]
    }
    """
    
    try:
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_content}])
        response_text = response.text
        json_start = response_text.find('{')
        json_end = response_text.rfind('}') + 1
        
        if json_start >= 0 and json_end > json_start:
            data = json.loads(response_text[json_start:json_end])
            data["currency_code"] = clean_currency_code(data.get("currency_code"))
            return data
        return {}
    except Exception as e:
        print(f"Error processing with Gemini API: {e}")
        return {}

def create_invoice_document(gemini_data, pdf_base64=None):
    """Create invoice document with clean data."""
    invoice_num = gemini_data.get("invoice_num") or f"INV{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    invoice_doc = {
        "invoice_header": {
            "organization_code": 100000,
            "invoice_num": invoice_num,
            "invoice_date": gemini_data.get("invoice_date", datetime.now().strftime("%Y-%m-%d")),
            "vendor_name": gemini_data.get("vendor_name", random.choice(vendors)),
            "vendor_site_code": f"V{str(random.randint(1, 100)).zfill(3)}",
            "invoice_amount": float(gemini_data.get("invoice_amount", 0)) if gemini_data.get("invoice_amount") else None,
            "currency_code": clean_currency_code(gemini_data.get("currency_code")),
            "payment_term": random.choice(payment_terms),
            "invoice_type": random.choice(invoice_types),
            "pdf_base64": pdf_base64
        },
        "invoice_lines": []
    }

    # Process line items
    if "line_items" in gemini_data and isinstance(gemini_data["line_items"], list):
        for line_idx, line_item in enumerate(gemini_data["line_items"], 1):
            new_line = {
                "invoice_num": invoice_num,
                "line_number": line_idx,
                "line_type": random.choice(["Service", "Product"]),
                "description": line_item.get("description", random.choice(descriptions)),
                "quantity": float(line_item.get("quantity", random.randint(1, 5))),
                "unit_price": float(line_item.get("unit_price", round(random.uniform(100.0, 1000.0), 2))),
                "line_amount": float(line_item.get("line_amount", 0))
            }
            
            if new_line["line_amount"] == 0 and new_line["unit_price"] > 0:
                new_line["line_amount"] = round(new_line["unit_price"] * new_line["quantity"], 2)
            
            invoice_doc["invoice_lines"].append(new_line)
    
    # Add default line if none found
    if not invoice_doc["invoice_lines"]:
        unit_price = invoice_doc["invoice_header"]["invoice_amount"] or round(random.uniform(100.0, 1000.0), 2)
        invoice_doc["invoice_lines"].append({
            "invoice_num": invoice_num,
            "line_number": 1,
            "line_type": random.choice(["Service", "Product"]),
            "description": random.choice(descriptions),
            "quantity": random.randint(1, 5),
            "unit_price": unit_price,
            "line_amount": unit_price
        })
    
    return invoice_doc

def extract_basic_invoice_details(message_payload):
    """Extracts basic invoice details from email content."""
    invoice_num = f"INV{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    invoice_doc = {
        "invoice_header": {
            "organization_code": 100000,
            "invoice_num": invoice_num,
            "invoice_date": datetime.now().strftime("%Y-%m-%d"),
            "vendor_name": random.choice(vendors),
            "vendor_site_code": f"V{str(random.randint(1, 100)).zfill(3)}",
            "invoice_amount": None,
            "currency_code": "INR",
            "payment_term": random.choice(payment_terms),
            "invoice_type": random.choice(invoice_types),
            "pdf_base64": None
        },
        "invoice_lines": [{
            "invoice_num": invoice_num,
            "line_number": 1,
            "line_type": random.choice(["Service", "Product"]),
            "description": random.choice(descriptions),
            "quantity": random.randint(1, 5),
            "unit_price": 0,
            "line_amount": 0
        }]
    }

    if 'parts' in message_payload:
        for part in message_payload['parts']:
            if part['mimeType'] == 'text/plain':
                data = part['body']['data']
                text = base64.urlsafe_b64decode(data).decode('utf-8')
                
                # Extract invoice number
                invoice_match = re.search(r'invoice\s*(?:#|number|num|no)?\s*[:\s]?\s*([A-Za-z0-9\-_]+)', text, re.IGNORECASE)
                if invoice_match:
                    invoice_doc["invoice_header"]["invoice_num"] = invoice_match.group(1)
                    invoice_doc["invoice_lines"][0]["invoice_num"] = invoice_match.group(1)
                
                # Extract amount (numeric only)
                amount_match = re.search(r'(?:\b|\s)(\d+(?:,\d+)*(?:\.\d+)?)\b', text)
                if amount_match:
                    amount_str = amount_match.group(1).replace(',', '')
                    invoice_doc["invoice_header"]["invoice_amount"] = float(amount_str)
                    invoice_doc["invoice_lines"][0]["unit_price"] = float(amount_str)
                    invoice_doc["invoice_lines"][0]["line_amount"] = float(amount_str)
                
                # Extract vendor
                vendor_match = re.search(r'from\s*:\s*([A-Za-z0-9\s]+)|(vendor\s*:\s*([A-Za-z0-9\s]+))', text, re.IGNORECASE)
                if vendor_match:
                    vendor_group = vendor_match.group(1) or vendor_match.group(3)
                    if vendor_group:
                        invoice_doc["invoice_header"]["vendor_name"] = vendor_group.strip()
                
                # Extract date
                date_match = re.search(r'date\s*:\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})|(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})', text, re.IGNORECASE)
                if date_match:
                    date_str = date_match.group(1) or date_match.group(2)
                    try:
                        parsed_date = datetime.strptime(date_str, '%m/%d/%Y')
                        invoice_doc["invoice_header"]["invoice_date"] = parsed_date.strftime("%Y-%m-%d")
                    except ValueError:
                        try:
                            parsed_date = datetime.strptime(date_str, '%d/%m/%Y')
                            invoice_doc["invoice_header"]["invoice_date"] = parsed_date.strftime("%Y-%m-%d")
                        except ValueError:
                            pass
                
                # Extract currency code if specified
                currency_match = re.search(r'currency\s*:\s*([A-Z]{3})|([A-Z]{3})\b', text, re.IGNORECASE)
                if currency_match:
                    currency_code = currency_match.group(1) or currency_match.group(2)
                    invoice_doc["invoice_header"]["currency_code"] = clean_currency_code(currency_code)
    
    return invoice_doc

def process_email(service, msg_id, db):
    """Processes a single email with invoice in subject."""
    try:
        message = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        headers = message['payload']['headers']
        subject = next((header['value'] for header in headers if header['name'] == 'Subject'), 'No Subject')
        
        print(f"Processing: {subject}")
        
        # Check for PDF attachment
        pdf_content = None
        pdf_base64 = None
        if 'parts' in message['payload']:
            for part in message['payload']['parts']:
                if 'filename' in part and part['filename'] and part['filename'].lower().endswith('.pdf'):
                    if 'body' in part and 'attachmentId' in part['body']:
                        attachment_id = part['body']['attachmentId']
                        attachment = service.users().messages().attachments().get(
                            userId='me', messageId=msg_id, id=attachment_id).execute()
                        
                        # Process the PDF attachment properly
                        pdf_base64 = process_pdf_attachment(attachment['data'])
                        if pdf_base64:
                            pdf_content = base64.b64decode(pdf_base64)
        
        invoices_collection = db['invoices']
        
        if pdf_content:
            try:
                gemini_data = process_pdf_with_gemini(pdf_content)
                if gemini_data:
                    invoice_doc = create_invoice_document(gemini_data, pdf_base64)
                else:
                    invoice_doc = extract_basic_invoice_details(message['payload'])
                    invoice_doc["invoice_header"]["pdf_base64"] = pdf_base64
            except Exception as e:
                print(f"Error processing with Gemini API: {e}")
                invoice_doc = extract_basic_invoice_details(message['payload'])
                invoice_doc["invoice_header"]["pdf_base64"] = pdf_base64
        else:
            invoice_doc = extract_basic_invoice_details(message['payload'])
        
        invoice_doc = convert_invoice_to_usd_and_status(invoice_doc)
        
        # Check for existing invoice
        existing_invoice = invoices_collection.find_one({"invoice_header.invoice_num": invoice_doc["invoice_header"]["invoice_num"]})
        if existing_invoice:
            print(f"Invoice {invoice_doc['invoice_header']['invoice_num']} already exists. Skipping.")
            return None
        
        # Insert into MongoDB
        result = invoices_collection.insert_one(invoice_doc)
        
        print(f"Successfully processed invoice: {invoice_doc['invoice_header']['invoice_num']}")
        return result.inserted_id
        
    except HttpError as error:
        print(f"An error occurred processing message {msg_id}: {error}")
        return None
    except Exception as e:
        print(f"Error processing email: {e}")
        return None

def main():
    """Main function to authenticate and process invoice emails."""
    print("Authenticating with Gmail API...")
    service = authenticate_gmail()
    print("Gmail API connected successfully!")
    
    print("Connecting to MongoDB...")
    try:
        db = connect_to_mongodb()
        db.client.drop_database("invoice_automation")
        print("Dropped invoice_automation database.")
    except Exception as e:
        print(f"Failed to connect to MongoDB: {e}")
        return
    
    start_date = datetime.now() - timedelta(days=30)
    print(f"Retrieving emails with 'invoice' in subject since {start_date.strftime('%Y-%m-%d')}...")
    
    # Only get emails with "invoice" in the subject
    invoice_emails = get_emails_with_invoice_in_subject(service, start_date)
    
    if not invoice_emails:
        print("No invoice emails found.")
        return
    
    print(f"Found {len(invoice_emails)} potential invoice emails.")
    
    for email in invoice_emails:
        process_email(service, email['id'], db)
    
    print("Email processing complete.")

    print("\nStarting invoice validation...")
    validator = InvoiceValidator(mongo_uri=MONGO_URI)
    validator.process_pending_invoices()
    print("Invoice validation complete.")

if __name__ == '__main__':
    main()
