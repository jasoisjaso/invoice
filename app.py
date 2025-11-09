

import os
import logging
import re
from datetime import datetime
from io import StringIO
import pandas as pd
import pdfplumber
from PIL import Image
import pytesseract
from flask import Flask, request, render_template, redirect, url_for, session, make_response

# =====================================================================================
# CONFIGURATION
# =====================================================================================
UPLOAD_FOLDER = 'uploads'
KNOWLEDGE_BASE_PATH = 'knowledge'
LOGS_FOLDER = 'logs'
VENDOR_LOOKUP_FILE = os.path.join(KNOWLEDGE_BASE_PATH, 'VendorLookup.csv')
FINANCIAL_CALENDAR_FILE = os.path.join(KNOWLEDGE_BASE_PATH, 'FinancialCalendar.csv')
LOG_FILE = os.path.join(LOGS_FOLDER, 'processor.log')
ALLOWED_EXTENSIONS = {'pdf'}

# =====================================================================================
# LOGGING SETUP
# =====================================================================================
if not os.path.exists(LOGS_FOLDER):
    os.makedirs(LOGS_FOLDER)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# =====================================================================================
# FLASK APP INITIALIZATION
# =====================================================================================
app = Flask(__name__, static_folder='static')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['SECRET_KEY'] = 'supersecretkeyyoushouldchange'

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_pdf(file_path):
    try:
        with pdfplumber.open(file_path) as pdf:
            return "".join(page.extract_text() for page in pdf.pages if page.extract_text())
    except Exception as e:
        logging.error(f"pdfplumber failed for {file_path}: {e}")
        return None

def _process_shunt_docket(docket_filename):
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], docket_filename)
    text = extract_text_from_pdf(filepath)
    if not text: return None
    docket_no_match = re.search(r"Delivery Docket No\.\s*:\s*(\d+)", text, re.IGNORECASE)
    shunt_qty_match = re.search(r"ATTSHUNT.*?(\d+)", text, re.IGNORECASE)
    if docket_no_match and shunt_qty_match:
        return {"docket_number": docket_no_match.group(1), "shunt_qty": int(shunt_qty_match.group(1))}
    return None

def _process_single_invoice(invoice_filename, vendor_df, financial_calendar_df, shunt_data):
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], invoice_filename)
    text = extract_text_from_pdf(filepath)
    if not text: return None, None, None

    # --- Extract Header Info ---
    vendor_name, vendor_no, invoice_no, invoice_date_obj = "BP AUSTRALIA", "96029099", "Not Found", None
    invoice_no_match = re.search(r"Invoice Number\s*(\d+)", text, re.IGNORECASE)
    if invoice_no_match: invoice_no = invoice_no_match.group(1)
    invoice_date_match = re.search(r"Invoice Date\s*(\d{2} \w{3} \d{4})", text, re.IGNORECASE)
    if invoice_date_match: invoice_date_obj = pd.to_datetime(invoice_date_match.group(1))
    invoice_date = invoice_date_obj.strftime('%d/%m/%Y') if invoice_date_obj else 'Not Found'

    # --- Process Line Items ---
    fuel_tracking_rows = []
    total_trailer_cost_excl_gst = 0
    total_shunt_cost_excl_gst = 0
    last_delivery_date = None

    # Split the text by delivery docket sections
    sections = text.split("Delivery Docket Number / Date:")[1:]

    for section in sections:
        # Extract docket number and date from the start of the section
        header_match = re.search(r"^\s*(\d+)\s*/\s*(\d{2} \w{3} \d{4})", section, re.IGNORECASE)
        if not header_match:
            continue

        docket_no = header_match.group(1)
        delivery_date_str = header_match.group(2)
        delivery_date = pd.to_datetime(delivery_date_str)
        last_delivery_date = delivery_date

        # Find the financial data within this section
        data_line_match = re.search(r"(ULSD 10PPM|Diesel)\s+([\d,]+)\s+L\s+([\d.]+)\s+([\d,]+\.\d{2})", section, re.IGNORECASE)
        if data_line_match:
            total_litres = int(data_line_match.group(2).replace(',',''))
            unit_price = float(data_line_match.group(3))
            excl_gst = float(data_line_match.group(4).replace(',',''))

            shunt_qty = shunt_data.get(docket_no, {}).get('shunt_qty', 0)
            trailer_litres = total_litres - shunt_qty
            shunt_cost = shunt_qty * unit_price
            trailer_cost = trailer_litres * unit_price
            total_trailer_cost_excl_gst += trailer_cost
            total_shunt_cost_excl_gst += shunt_cost

            # --- Build Fuel Tracking Row (with numeric types for costs) ---
            fy_wk = ''
            week_lookup_date = delivery_date.strftime('%#d/%#m/%Y')
            week_code_row = financial_calendar_df[financial_calendar_df['Date'] == week_lookup_date]
            if not week_code_row.empty:
                fy_wk = week_code_row['Week'].iloc[0]

            fuel_tracking_rows.append({
                'Invoice': invoice_no, 'Invoice Date': invoice_date, 'excl GST': excl_gst, 
                'GST': excl_gst * 0.1, 'incl GST': excl_gst * 1.1, 
                'Delivery Date': delivery_date.strftime('%d-%b'), 'FY WK': fy_wk, 'Docket': docket_no,
                'Total Litres QTY': total_litres, 'SHUNT QTY': shunt_qty, 'Trailer litres': trailer_litres,
                'UNIT PRICE': unit_price, 'SHUNT COST': shunt_cost, 'INVOICE DIFF': '',
                'Trailer total': trailer_cost, 'SHUNT Total': shunt_cost, 'Uploading WK': 'WK 01'
            })

    # --- Aggregate and Build Final DataFrames ---
    data_sheet_rows = []
    week_ending_date = last_delivery_date.strftime('%d/%m/%Y') if last_delivery_date else ''
    week_lookup_date = last_delivery_date.strftime('%#d/%#m/%Y') if last_delivery_date else ''
    week_code_row = financial_calendar_df[financial_calendar_df['Date'] == week_lookup_date]
    fy_wk = week_code_row['Week'].iloc[0] if not week_code_row.empty else ''

    # Trailer Row (Aggregated)
    data_sheet_rows.append({
        'Vendor Name (Check)': vendor_name, 'Vendor No.': vendor_no, 'Invoice No.': invoice_no, 'Invoice Date': invoice_date,
        'GL No.': 640520, 'Trade Dept': '', 'Merch Dept': '', 'Store': 8409,
        'Amount (Less GST)': f'{total_trailer_cost_excl_gst:.2f}', 'GST Amount': f'{(total_trailer_cost_excl_gst * 0.1):.2f}',
        'Invoice Total (Incl of GST)': f'{(total_trailer_cost_excl_gst * 1.1):.2f}',
        'Vendor Line Text (Optional) Not required for Zone Office Uploads': '',
        'Store Line Text (Optional)': f'{vendor_name} 8409 {fy_wk} BRDC Fuel',
        'Comments': 'BRDC Fuel', 'Week Ending': week_ending_date, 'WeekCount Line Text': fy_wk
    })
    # Shunt Row (Aggregated, if applicable)
    if total_shunt_cost_excl_gst > 0:
        data_sheet_rows.append({
            'Vendor Name (Check)': vendor_name, 'Vendor No.': vendor_no, 'Invoice No.': invoice_no, 'Invoice Date': invoice_date,
            'GL No.': 432211, 'Trade Dept': '', 'Merch Dept': '', 'Store': 8682,
            'Amount (Less GST)': f'{total_shunt_cost_excl_gst:.2f}', 'GST Amount': f'{(total_shunt_cost_excl_gst * 0.1):.2f}',
            'Invoice Total (Incl of GST)': f'{(total_shunt_cost_excl_gst * 1.1):.2f}',
            'Vendor Line Text (Optional) Not required for Zone Office Uploads': '',
            'Store Line Text (Optional)': f'{vendor_name} 8682 {fy_wk} BRDC Shunt Fuel',
            'Comments': 'BRDC Shunt Fuel', 'Week Ending': week_ending_date, 'WeekCount Line Text': fy_wk
        })

    data_sheet = pd.DataFrame(data_sheet_rows)
    
    # --- Final Fuel Tracking Aggregation and Formatting ---
    fuel_tracking_df = pd.DataFrame(fuel_tracking_rows)
    if not fuel_tracking_df.empty:
        invoice_total_trailer = fuel_tracking_df['Trailer total'].sum()
        invoice_total_shunt_qty = fuel_tracking_df['SHUNT QTY'].sum()
        invoice_total_excl_gst = fuel_tracking_df['excl GST'].sum()

        # Now, format the columns for display
        for col in ['excl GST', 'GST', 'incl GST', 'UNIT PRICE', 'SHUNT COST', 'Trailer total', 'SHUNT Total']:
            fuel_tracking_df[col] = fuel_tracking_df[col].apply(lambda x: f'{x:.2f}' if col not in ['UNIT PRICE'] else f'{x:.5f}')

        fuel_tracking_df['INVOICE DIFF'] = ''
        last_row_index = fuel_tracking_df.index[-1]
        fuel_tracking_df.loc[last_row_index, 'INVOICE DIFF'] = f'${invoice_total_excl_gst:.2f}'
        fuel_tracking_df.loc[last_row_index, 'Trailer total'] = f'${invoice_total_trailer:.2f}'
        fuel_tracking_df.loc[last_row_index, 'SHUNT Total'] = invoice_total_shunt_qty

    total_excl_gst = total_trailer_cost_excl_gst + total_shunt_cost_excl_gst
    checklist_sheet = pd.DataFrame([{
        'Vendor': vendor_name, 'Vendor #': vendor_no, 'Invoice No.': invoice_no,
        'Exc GST': f'{total_excl_gst:.2f}',
        'GST Amount': f'{(total_excl_gst * 0.1):.2f}',
        'Invoice Total (Incl of GST)': f'{(total_excl_gst * 1.1):.2f}'
    }])

    return data_sheet, checklist_sheet, fuel_tracking_df

# --- Routes (largely the same as before, just ensuring they call the new logic) ---
@app.route('/', methods=['GET', 'POST'])
def upload_files():
    if request.method == 'POST':
        invoice_files = request.files.getlist("invoices")
        docket_files = request.files.getlist("dockets")
        invoice_filenames = [f.filename for f in invoice_files if f and allowed_file(f.filename)]
        docket_filenames = [f.filename for f in docket_files if f and allowed_file(f.filename)]
        for file in invoice_files + docket_files:
            if file.filename in invoice_filenames + docket_filenames:
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], file.filename))
        session['invoice_filenames'] = invoice_filenames
        session['docket_filenames'] = docket_filenames
        return redirect(url_for('process_files'))
    return render_template('index.html')

@app.route('/process_files')
def process_files():
    invoice_filenames = session.get('invoice_filenames', [])
    docket_filenames = session.get('docket_filenames', [])
    if not invoice_filenames: return "No invoice files to process.", 400

    shunt_data = {info['docket_number']: info for docket_filename in docket_filenames if (info := _process_shunt_docket(docket_filename))}
    
    vendor_df = pd.read_csv(VENDOR_LOOKUP_FILE)
    financial_calendar_df = pd.read_csv(FINANCIAL_CALENDAR_FILE)
    financial_calendar_df['Date'] = pd.to_datetime(financial_calendar_df['Date'], format='%d/%m/%Y').dt.strftime('%#d/%#m/%Y')

    all_data, all_checklists, all_fuel_trackers = [], [], []
    for filename in invoice_filenames:
        data, check, fuel = _process_single_invoice(filename, vendor_df, financial_calendar_df, shunt_data)
        if data is not None: all_data.append(data)
        if check is not None: all_checklists.append(check)
        if fuel is not None: all_fuel_trackers.append(fuel)

    if not all_data: return "Could not process any invoices.", 500

    session['data_sheet'] = pd.concat(all_data, ignore_index=True).to_json(orient='split')
    session['checklist_sheet'] = pd.concat(all_checklists, ignore_index=True).to_json(orient='split')
    session['fuel_tracking_sheet'] = pd.concat(all_fuel_trackers, ignore_index=True).to_json(orient='split')

    return render_template('results.html', 
                           data_sheet=pd.read_json(StringIO(session['data_sheet']), orient='split').to_html(classes='table table-striped', index=False),
                           checklist_sheet=pd.read_json(StringIO(session['checklist_sheet']), orient='split').to_html(classes='table table-striped', index=False),
                           fuel_tracking_sheet=pd.read_json(StringIO(session['fuel_tracking_sheet']), orient='split').to_html(classes='table table-striped', index=False))

@app.route('/download_combined_csv')
def download_combined_csv():
    if 'data_sheet' not in session: return "Error: No data in session.", 404
    data_df = pd.read_json(StringIO(session['data_sheet']), orient='split')
    checklist_df = pd.read_json(StringIO(session['checklist_sheet']), orient='split')
    fuel_df = pd.read_json(StringIO(session['fuel_tracking_sheet']), orient='split')
    output = checklist_df.to_csv(index=False)
    output += "\n\n--- Data Sheet ---\n"
    output += data_df.to_csv(index=False)
    output += "\n\n--- Fuel Tracking Sheet ---\n"
    output += fuel_df.to_csv(index=False)
    response = make_response(output)
    response.headers["Content-Disposition"] = "attachment; filename=combined_invoice_data.csv"
    response.headers["Content-Type"] = "text/csv"
    return response

@app.route('/deployment')
def deployment():
    return render_template('deployment.html')

if __name__ == '__main__':
    if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)
    if not os.path.exists(KNOWLEDGE_BASE_PATH): os.makedirs(KNOWLEDGE_BASE_PATH)
    app.run(debug=True)
