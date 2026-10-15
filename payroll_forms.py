"""Official tax form generation for household-employer payroll.

Three outputs:
  1. W-2 PDF — fills the official IRS fillable PDF (all copies in one file).
     Plain-paper printouts of Copies B, C, 2 are legitimate substitute forms
     under IRS Pub 1141 when produced from the official IRS PDF template.
  2. Schedule H PDF — fills the official IRS fillable PDF.
  3. EFW2 text file — the SSA electronic filing format (SSA Pub 42-007).
     Upload to SSA Business Services Online for official W-2 Copy A filing.

Usage pattern:
    from payroll_forms import (
        fill_w2_pdf, fill_schedule_h_pdf, build_efw2, inspect_pdf_fields,
        get_w2_fieldmap, get_schedule_h_fieldmap, forms_dir,
    )
"""
import json
import os
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter

FORMS_DIR = Path(__file__).parent / 'forms'


def forms_dir():
    return FORMS_DIR


def _load_json(path):
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def get_w2_fieldmap(year):
    per_year = FORMS_DIR / f'w2_fieldmap_{year}.json'
    default = FORMS_DIR / 'w2_fieldmap.json'
    data = _load_json(per_year) or _load_json(default) or {}
    return {k: v for k, v in data.items() if not k.startswith('_')}, data


def get_schedule_h_fieldmap(year):
    per_year = FORMS_DIR / f'schedule_h_fieldmap_{year}.json'
    default = FORMS_DIR / 'schedule_h_fieldmap.json'
    data = _load_json(per_year) or _load_json(default) or {}
    return {k: v for k, v in data.items() if not k.startswith('_')}, data


def _w2_template_path(year):
    return FORMS_DIR / f'fw2_{year}.pdf'


def _schedule_h_template_path(year):
    return FORMS_DIR / f'f1040sh_{year}.pdf'


# Optional DB-backed template resolver — app.py injects this at import time
# so templates uploaded via the UI (stored in MongoDB) persist across container restarts.
_template_resolver = None


def set_template_resolver(fn):
    """Register a function that returns bytes for a given template key (e.g. 'fw2_2025.pdf'), or None."""
    global _template_resolver
    _template_resolver = fn


def _load_template(kind, year):
    """Return (bytes, source_str) for the requested template, preferring DB over disk. (None, None) if missing."""
    key = f"{'fw2' if kind == 'w2' else 'f1040sh'}_{year}.pdf"
    if _template_resolver is not None:
        data = _template_resolver(key)
        if data:
            return data, f'db:{key}'
    path = FORMS_DIR / key
    if path.exists():
        return path.read_bytes(), f'disk:{path}'
    return None, None


def w2_template_exists(year):
    return _load_template('w2', year)[0] is not None


def schedule_h_template_exists(year):
    return _load_template('sh', year)[0] is not None


def inspect_pdf_fields(pdf_source):
    """Return a list of form field names found in the PDF. Accepts path or bytes."""
    if isinstance(pdf_source, (bytes, bytearray)):
        reader = PdfReader(BytesIO(pdf_source))
    else:
        reader = PdfReader(str(pdf_source))
    fields = reader.get_fields() or {}
    return sorted(fields.keys())


def _money(v):
    try:
        return f'{float(v):.2f}'
    except (ValueError, TypeError):
        return '0.00'


def _build_w2_copy_values(employee, totals, settings):
    """Build a dict of logical-field-name -> string-value for one W-2 copy."""
    emp_name_addr = '\n'.join(filter(None, [
        settings.get('employer_name', ''),
        settings.get('employer_address', ''),
    ]))
    state_code = settings.get('state', '')
    return {
        'ssn': employee.get('ssn', '') or '',
        'ein': settings.get('employer_ein', '') or '',
        'employer_name_addr': emp_name_addr,
        'control_number': '',
        'employee_name': employee.get('name', '') or '',
        'employee_addr': employee.get('address', '') or '',
        'box_1_wages': _money(totals.get('gross', 0)),
        'box_2_fit': _money(totals.get('fit', 0)),
        'box_3_ss_wages': _money(totals.get('ss_wages', 0)),
        'box_4_ss_tax': _money(totals.get('ss_tax', 0)),
        'box_5_medicare_wages': _money(totals.get('medicare_wages', 0)),
        'box_6_medicare_tax': _money(totals.get('medicare_tax', 0)),
        'box_7_ss_tips': '0.00',
        'box_8_allocated_tips': '0.00',
        'box_10_dep_care': '0.00',
        'box_11_nonqual': '0.00',
        'box_15_state': state_code,
        'box_15_state_id': settings.get('state_id', '') or '',
        'box_16_state_wages': _money(totals.get('gross', 0)),
        'box_17_state_tax': _money(totals.get('sit', 0)),
        'box_18_local_wages': '',
        'box_19_local_tax': '',
        'box_20_locality': '',
    }


def fill_w2_pdf(employee, totals, settings, year):
    """Return a BytesIO of the filled W-2 PDF, or None if template is missing."""
    template_bytes, _ = _load_template('w2', year)
    if template_bytes is None:
        return None
    fieldmap, full_map = get_w2_fieldmap(year)
    values = _build_w2_copy_values(employee, totals, settings)

    # IRS W-2 PDFs have all six copies on one form. The same values get stamped
    # on each copy. If the map declares copy prefixes we expand; otherwise we
    # pass the raw field names and let pypdf match anything it can (it will
    # silently ignore fields it doesn't find).
    copies = full_map.get('_copies') or []
    repeats = bool(full_map.get('_repeats_per_copy'))
    reader = PdfReader(BytesIO(template_bytes))
    writer = PdfWriter(clone_from=reader)

    # Build the dict of actual-pdf-field-name -> value
    filled = {}
    if repeats and copies:
        for copy in copies:
            for logical, pdf_name in fieldmap.items():
                filled[f'topmostSubform[0].{copy}[0].{pdf_name}[0]'] = values.get(logical, '')
                # Also try without the topmostSubform prefix (some PDFs use shorter paths)
                filled[f'{copy}[0].{pdf_name}[0]'] = values.get(logical, '')
                filled[f'{copy}.{pdf_name}'] = values.get(logical, '')
    # Always also try the raw field name (for PDFs that don't use the copy prefix convention)
    for logical, pdf_name in fieldmap.items():
        filled[pdf_name] = values.get(logical, '')
        filled[f'{pdf_name}[0]'] = values.get(logical, '')

    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, filled)
        except Exception:
            # update_page_form_field_values raises if no fields on page; skip
            pass

    buf = BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf


def _build_schedule_h_values(totals, per_emp, settings, year, filer_name=None, filer_ssn=None):
    total_8 = (totals.get('ss_tax', 0) + totals.get('medicare_tax', 0)
               + totals.get('addl_medicare', 0) + totals.get('fit_withheld', 0))
    total_tax = total_8 + totals.get('futa_tax', 0)
    return {
        'name': filer_name or settings.get('employer_name', ''),
        'ssn': filer_ssn or '',
        'ein': settings.get('employer_ein', ''),
        'calendar_year': str(year),
        'line_1a': _money(totals.get('cash_wages_fica', 0)),
        'line_1b': _money(totals.get('ss_tax', 0)),
        'line_3': _money(totals.get('cash_wages_fica', 0)),
        'line_4': _money(totals.get('medicare_tax', 0)),
        'line_5': _money(totals.get('addl_medicare_wages', 0)),
        'line_6': _money(totals.get('addl_medicare', 0)),
        'line_7': _money(totals.get('fit_withheld', 0)),
        'line_8': _money(total_8),
        'part2_futa_wages': _money(totals.get('futa_wages', 0)),
        'part2_futa_tax': _money(totals.get('futa_tax', 0)),
        'part2_total': _money(totals.get('futa_tax', 0)),
        'total_tax': _money(total_tax),
    }


def fill_schedule_h_pdf(totals, per_emp, settings, year, filer_name=None, filer_ssn=None):
    """Return a BytesIO of the filled Schedule H PDF, or None if template is missing."""
    template_bytes, _ = _load_template('sh', year)
    if template_bytes is None:
        return None
    fieldmap, full_map = get_schedule_h_fieldmap(year)
    values = _build_schedule_h_values(totals, per_emp, settings, year, filer_name, filer_ssn)

    reader = PdfReader(BytesIO(template_bytes))
    writer = PdfWriter(clone_from=reader)

    filled = {}
    for logical, pdf_name in fieldmap.items():
        filled[pdf_name] = values.get(logical, '')
        filled[f'{pdf_name}[0]'] = values.get(logical, '')
        filled[f'topmostSubform[0].Page1[0].{pdf_name}[0]'] = values.get(logical, '')

    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, filled)
        except Exception:
            pass

    buf = BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf


# ============ EFW2 (SSA Electronic W-2 Filing Format) ============
# Spec: SSA Publication 42-007 "Specifications for Filing Forms W-2 Electronically"
# Each record is a fixed 512-character line. Required records: RA, RE, RW, RT, RF.
# Optional: RO (state wage), RS (state record), RU (state totals), RV (local totals).

def _ef_field(value, length, numeric=False, decimals=None):
    """Format a value for an EFW2 record field.
    numeric=True → right-align, zero-pad, strip decimals; if decimals given,
    interpret as money and pack as zero-padded cents (SSA wants dollars-only
    fields for some lines, cents for others — see caller).
    """
    if value is None:
        value = ''
    s = str(value)
    if numeric:
        # For money fields in EFW2: dollars-and-cents with no decimal point,
        # right-justified zero-padded. e.g. $12,345.67 → "00000001234567"
        if decimals is not None:
            try:
                cents = int(round(float(value) * 100))
            except (ValueError, TypeError):
                cents = 0
            s = str(cents)
        else:
            # Extract digits only
            s = ''.join(c for c in s if c.isdigit())
        s = s[-length:].rjust(length, '0')
    else:
        # Text: uppercase, left-justified, space-padded, truncate
        s = s.upper()[:length].ljust(length)
    return s


def _split_address(addr):
    """Split a free-form address into (street, city, state, zip)."""
    if not addr:
        return '', '', '', ''
    parts = [p.strip() for p in addr.split(',')]
    street = parts[0] if parts else ''
    city = parts[1] if len(parts) > 1 else ''
    state_zip = parts[2] if len(parts) > 2 else ''
    bits = state_zip.split()
    state = bits[0] if bits else ''
    zip_code = bits[1] if len(bits) > 1 else ''
    return street, city, state, zip_code


def _split_name(full_name):
    """Return (first, middle, last, suffix) best-effort."""
    if not full_name:
        return '', '', '', ''
    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0], '', '', ''
    if len(parts) == 2:
        return parts[0], '', parts[1], ''
    if len(parts) == 3:
        return parts[0], parts[1], parts[2], ''
    return parts[0], parts[1], ' '.join(parts[2:-1]), parts[-1]


def _ra_record(settings, submitter_name=None):
    """RA Submitter Record — one per file, first line."""
    ein = ''.join(c for c in (settings.get('employer_ein') or '') if c.isdigit())
    submitter_street, submitter_city, submitter_state, submitter_zip = _split_address(settings.get('employer_address'))
    name = submitter_name or settings.get('employer_name', '')
    line = (
        'RA'
        + _ef_field(ein, 9)                              # 3-11: Submitter EIN
        + _ef_field('', 17)                              # 12-28: User Identification (BSO user ID - leave blank for form-generated)
        + _ef_field('', 4)                               # 29-32: Software Vendor Code
        + _ef_field('', 5)                               # 33-37: Blank
        + _ef_field('', 1)                               # 38: Resub Indicator
        + _ef_field('', 6)                               # 39-44: Resub WFID
        + _ef_field('L', 1)                              # 45: Software Code (L=In-house)
        + _ef_field(name, 57)                            # 46-102: Company Name
        + _ef_field(submitter_street, 22)                # 103-124: Location Address
        + _ef_field('', 22)                              # 125-146: Delivery Address
        + _ef_field(submitter_city, 22)                  # 147-168: City
        + _ef_field(submitter_state, 2)                  # 169-170: State abbrev
        + _ef_field(submitter_zip, 5)                    # 171-175: ZIP
        + _ef_field('', 4)                               # 176-179: ZIP ext
        + _ef_field('', 5)                               # 180-184: blank
        + _ef_field('', 23)                              # 185-207: Foreign State/Province
        + _ef_field('', 15)                              # 208-222: Foreign Postal Code
        + _ef_field('', 2)                               # 223-224: Country code
        + _ef_field(name, 57)                            # 225-281: Submitter Name
        + _ef_field(submitter_street, 22)                # 282-303: Submitter Location Address
        + _ef_field('', 22)                              # 304-325
        + _ef_field(submitter_city, 22)                  # 326-347
        + _ef_field(submitter_state, 2)                  # 348-349
        + _ef_field(submitter_zip, 5)                    # 350-354
        + _ef_field('', 4)                               # 355-358
        + _ef_field('', 5)                               # 359-363
        + _ef_field('', 23)                              # 364-386
        + _ef_field('', 15)                              # 387-401
        + _ef_field('', 2)                               # 402-403
        + _ef_field(name, 27)                            # 404-430: Contact Name
        + _ef_field('', 15)                              # 431-445: Contact Phone
        + _ef_field('', 5)                               # 446-450: Contact Phone Ext
        + _ef_field('', 3)                               # 451-453: blank
        + _ef_field('', 40)                              # 454-493: Contact Email
        + _ef_field('', 3)                               # 494-496: blank
        + _ef_field('', 10)                              # 497-506: Contact Fax
        + _ef_field('', 1)                               # 507: Preferred Notification Method
        + _ef_field('', 1)                               # 508: Preparer Code
        + _ef_field('', 4)                               # 509-512: blank
    )
    return line[:512].ljust(512)


def _re_record(settings, year):
    ein = ''.join(c for c in (settings.get('employer_ein') or '') if c.isdigit())
    street, city, state, zip_code = _split_address(settings.get('employer_address'))
    line = (
        'RE'
        + _ef_field(str(year), 4)                        # 3-6: Tax Year
        + _ef_field('', 1)                               # 7: Agent Indicator
        + _ef_field(ein, 9)                              # 8-16: Employer EIN
        + _ef_field('', 9)                               # 17-25: Agent EIN
        + _ef_field('', 1)                               # 26: Terminating Business Indicator
        + _ef_field('', 4)                               # 27-30: Establishment Number
        + _ef_field('', 9)                               # 31-39: Other EIN
        + _ef_field(settings.get('employer_name', ''), 57)  # 40-96: Employer Name
        + _ef_field(street, 22)                          # 97-118: Location Address
        + _ef_field('', 22)                              # 119-140: Delivery Address
        + _ef_field(city, 22)                            # 141-162: City
        + _ef_field(state, 2)                            # 163-164: State abbrev
        + _ef_field(zip_code, 5)                         # 165-169: ZIP
        + _ef_field('', 4)                               # 170-173: ZIP ext
        + _ef_field('', 5)                               # 174-178: blank
        + _ef_field('', 23)                              # 179-201: Foreign State/Province
        + _ef_field('', 15)                              # 202-216: Foreign Postal Code
        + _ef_field('', 2)                               # 217-218: Country Code
        + _ef_field('H', 1)                              # 219: Employment Code (H=Household)
        + _ef_field('N', 1)                              # 220: Tax Jurisdiction Code (N=None, W=P.R., etc.)
        + _ef_field('', 1)                               # 221: Third-Party Sick Pay Indicator
        + _ef_field('', 291)                             # 222-512: blank
    )
    return line[:512].ljust(512)


def _rw_record(employee, totals, settings):
    ssn = ''.join(c for c in (employee.get('ssn') or '') if c.isdigit())
    first, middle, last, suffix = _split_name(employee.get('name'))
    street, city, state, zip_code = _split_address(employee.get('address'))
    line = (
        'RW'
        + _ef_field(ssn, 9)                              # 3-11: SSN
        + _ef_field(first, 15)                           # 12-26: First Name
        + _ef_field(middle, 15)                          # 27-41: Middle Name
        + _ef_field(last, 20)                            # 42-61: Last Name
        + _ef_field(suffix, 4)                           # 62-65: Suffix
        + _ef_field(street, 22)                          # 66-87: Location Address
        + _ef_field('', 22)                              # 88-109: Delivery Address
        + _ef_field(city, 22)                            # 110-131: City
        + _ef_field(state, 2)                            # 132-133: State
        + _ef_field(zip_code, 5)                         # 134-138: ZIP
        + _ef_field('', 4)                               # 139-142: ZIP ext
        + _ef_field('', 5)                               # 143-147: blank
        + _ef_field('', 23)                              # 148-170: Foreign State
        + _ef_field('', 15)                              # 171-185: Foreign Postal
        + _ef_field('', 2)                               # 186-187: Country Code
        + _ef_field(totals.get('gross', 0), 11, numeric=True, decimals=2)   # 188-198: Wages (Box 1)
        + _ef_field(totals.get('fit', 0), 11, numeric=True, decimals=2)     # 199-209: Federal Income Tax Withheld (Box 2)
        + _ef_field(totals.get('ss_wages', 0), 11, numeric=True, decimals=2)# 210-220: SS Wages (Box 3)
        + _ef_field(totals.get('ss_tax', 0), 11, numeric=True, decimals=2)  # 221-231: SS Tax (Box 4)
        + _ef_field(totals.get('medicare_wages', 0), 11, numeric=True, decimals=2)  # 232-242: Medicare Wages (Box 5)
        + _ef_field(totals.get('medicare_tax', 0), 11, numeric=True, decimals=2)    # 243-253: Medicare Tax (Box 6)
        + _ef_field(0, 11, numeric=True, decimals=2)     # 254-264: SS Tips (Box 7)
        + _ef_field(0, 11, numeric=True, decimals=2)     # 265-275: Allocated Tips (Box 8)
        + _ef_field(0, 11, numeric=True, decimals=2)     # 276-286: (Reserved)
        + _ef_field(0, 11, numeric=True, decimals=2)     # 287-297: Dependent Care (Box 10)
        + _ef_field(0, 11, numeric=True, decimals=2)     # 298-308: Nonqualified 457 (Box 11)
        + _ef_field(0, 11, numeric=True, decimals=2)     # 309-319: Nonqualified Not 457
        + _ef_field('', 24)                              # 320-343: blank
        + _ef_field(0, 11, numeric=True, decimals=2)     # 344-354: Employer Cost of Premiums 457 (Box 12 DD)
        + _ef_field(0, 11, numeric=True, decimals=2)     # 355-365: Non-Taxable Combat Pay
        + _ef_field('', 11)                              # 366-376: blank
        + _ef_field(0, 11, numeric=True, decimals=2)     # 377-387: Employer HSA
        + _ef_field(0, 11, numeric=True, decimals=2)     # 388-398: Employee HSA
        + _ef_field(0, 11, numeric=True, decimals=2)     # 399-409: (Reserved)
        + _ef_field(0, 11, numeric=True, decimals=2)     # 410-420: Roth 401k
        + _ef_field(0, 11, numeric=True, decimals=2)     # 421-431: Roth 403b
        + _ef_field(0, 11, numeric=True, decimals=2)     # 432-442: Employer Cost HSA
        + _ef_field(0, 11, numeric=True, decimals=2)     # 443-453: Employer Cost Non-Qual Def
        + _ef_field('', 23)                              # 454-476
        + _ef_field('', 1)                               # 477: Statutory Employee Indicator
        + _ef_field('', 1)                               # 478: blank
        + _ef_field('', 1)                               # 479: Retirement Plan
        + _ef_field('', 1)                               # 480: Third-Party Sick Pay
        + _ef_field('', 32)                              # 481-512
    )
    return line[:512].ljust(512)


def _rt_record(employee_count, grand_totals):
    line = (
        'RT'
        + _ef_field(employee_count, 7, numeric=True)
        + _ef_field(grand_totals.get('gross', 0), 15, numeric=True, decimals=2)
        + _ef_field(grand_totals.get('fit', 0), 15, numeric=True, decimals=2)
        + _ef_field(grand_totals.get('ss_wages', 0), 15, numeric=True, decimals=2)
        + _ef_field(grand_totals.get('ss_tax', 0), 15, numeric=True, decimals=2)
        + _ef_field(grand_totals.get('medicare_wages', 0), 15, numeric=True, decimals=2)
        + _ef_field(grand_totals.get('medicare_tax', 0), 15, numeric=True, decimals=2)
        + _ef_field(0, 15, numeric=True, decimals=2)  # SS Tips
        + _ef_field(0, 15, numeric=True, decimals=2)  # Allocated Tips
        + _ef_field(0, 15, numeric=True, decimals=2)  # Reserved
        + _ef_field(0, 15, numeric=True, decimals=2)  # Dep Care
        + _ef_field(0, 15, numeric=True, decimals=2)  # Nonqual 457
        + _ef_field(0, 15, numeric=True, decimals=2)  # Nonqual non-457
        + _ef_field('', 30)
        + _ef_field(0, 15, numeric=True, decimals=2)  # DD
        + _ef_field(0, 15, numeric=True, decimals=2)  # Combat pay
        + _ef_field('', 15)
        + _ef_field(0, 15, numeric=True, decimals=2)  # Employer HSA
        + _ef_field(0, 15, numeric=True, decimals=2)  # Employee HSA
        + _ef_field(0, 15, numeric=True, decimals=2)  # Reserved
        + _ef_field(0, 15, numeric=True, decimals=2)  # Roth 401k
        + _ef_field(0, 15, numeric=True, decimals=2)  # Roth 403b
        + _ef_field(0, 15, numeric=True, decimals=2)  # Cost HSA
        + _ef_field(0, 15, numeric=True, decimals=2)  # Cost non-qual def
        + _ef_field('', 120)
    )
    return line[:512].ljust(512)


def _rf_record(total_employee_count):
    line = (
        'RF'
        + _ef_field('', 5)
        + _ef_field(total_employee_count, 9, numeric=True)
        + _ef_field('', 496)
    )
    return line[:512].ljust(512)


def build_efw2(year, employer_settings, employee_totals_list):
    """Build an EFW2 file as a string.

    employee_totals_list: list of dicts, each {'employee': emp_dict, 'totals': totals_dict}

    Returns the full EFW2 text with CRLF line endings (SSA requires CRLF).
    Upload the resulting file to SSA Business Services Online.
    """
    lines = [_ra_record(employer_settings), _re_record(employer_settings, year)]
    grand = {'gross': 0, 'fit': 0, 'ss_wages': 0, 'ss_tax': 0, 'medicare_wages': 0, 'medicare_tax': 0}
    for row in employee_totals_list:
        emp = row['employee']
        totals = row['totals']
        lines.append(_rw_record(emp, totals, employer_settings))
        for k in grand:
            grand[k] += float(totals.get(k, 0) or 0)
    lines.append(_rt_record(len(employee_totals_list), grand))
    lines.append(_rf_record(len(employee_totals_list)))
    # SSA requires CRLF, fixed 512 chars per record.
    return '\r\n'.join(lines) + '\r\n'
