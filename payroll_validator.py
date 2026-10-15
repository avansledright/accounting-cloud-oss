"""Local EFW2 validator — catches most issues AccuWage would flag.

Implements validation rules from SSA Publication 42-007 "Specifications for
Filing Forms W-2 Electronically" (2024 edition). Focuses on the subset of
rules most commonly triggered by a household employer filing (employment
code H, no local tax, small number of employees).

Returns a list of Issues, each classified as 'error' (blocks filing in
AccuWage) or 'warning' (will pass but worth reviewing).

NOT a replacement for AccuWage Online — always run AccuWage as the final
check before uploading to BSO. This validator exists to give you a fast
local loop so common errors get caught in the app rather than round-
tripping through BSO's web UI.
"""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Issue:
    severity: str          # 'error' or 'warning'
    code: str              # short identifier (e.g. 'RA.EIN')
    message: str
    record: str = None     # 'RA', 'RE', 'RW[0]', etc.
    line_no: int = None    # 1-indexed


# Valid SSN area numbers (first 3 digits): 001-665 and 667-899 (666 not issued,
# 900+ reserved for ITINs — not valid for W-2).
# https://www.ssa.gov/employer/randomization.html
INVALID_SSN_AREAS = {'000', '666'}
INVALID_SSN_GROUPS = {'00'}
INVALID_SSN_SERIALS = {'0000'}

# Valid state codes (50 states + DC + territories that SSA accepts)
VALID_STATE_CODES = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA',
    'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
    'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT',
    'VA','WA','WV','WI','WY','DC','AS','GU','MP','PR','VI',
}

# Valid employment codes per Pub 42-007 (RE record position 219)
VALID_EMPLOYMENT_CODES = {'A','H','M','Q','R','X','F'}  # H = Household


def _is_numeric(s):
    return s.strip().isdigit() or (s.strip() == '' or s == '0' * len(s))


def _is_money_field(s):
    """EFW2 money fields are zero-padded digits representing cents."""
    return s.isdigit()


def validate_ssn(ssn):
    """Return (is_valid, reason)."""
    ssn = ''.join(c for c in (ssn or '') if c.isdigit())
    if len(ssn) != 9:
        return False, 'must be 9 digits'
    area, group, serial = ssn[:3], ssn[3:5], ssn[5:]
    if area in INVALID_SSN_AREAS:
        return False, f'invalid area number ({area})'
    if area.startswith('9'):
        return False, 'area 900+ reserved for ITIN, not valid on W-2'
    if group in INVALID_SSN_GROUPS:
        return False, f'invalid group number ({group})'
    if serial in INVALID_SSN_SERIALS:
        return False, f'invalid serial number ({serial})'
    return True, ''


def validate_ein(ein):
    ein = ''.join(c for c in (ein or '') if c.isdigit())
    if len(ein) != 9:
        return False, 'EIN must be 9 digits'
    if ein == '0' * 9:
        return False, 'EIN cannot be all zeros'
    # First two digits (prefix) are assigned by IRS; 07, 08, 09, 17, 18, 19, 28,
    # 29, 49, 69, 70, 78, 79, 89 were historically invalid but IRS has added
    # prefixes over the years; skip range check to avoid false-positive errors.
    return True, ''


def _parse_lines(efw2_text):
    """Split EFW2 text into records, preserving line numbers."""
    # EFW2 requires CRLF line endings.
    lines = efw2_text.split('\r\n')
    if lines and lines[-1] == '':
        lines = lines[:-1]  # drop trailing blank from final CRLF
    return lines


def validate_efw2(efw2_text, expected_year=None):
    """Validate an EFW2 file. Returns list[Issue]."""
    issues = []
    lines = _parse_lines(efw2_text)

    if not lines:
        issues.append(Issue('error', 'FILE.EMPTY', 'EFW2 file has no records'))
        return issues

    # Structural: every record must be exactly 512 characters
    for i, line in enumerate(lines, 1):
        if len(line) != 512:
            issues.append(Issue('error', 'LINE.LENGTH',
                                f'Record must be exactly 512 chars, got {len(line)}',
                                record=line[:2], line_no=i))

    # Must start with RA and end with RF
    if lines[0][:2] != 'RA':
        issues.append(Issue('error', 'RA.MISSING', 'First record must be RA (Submitter)', line_no=1))
    if lines[-1][:2] != 'RF':
        issues.append(Issue('error', 'RF.MISSING', 'Last record must be RF (Final)', line_no=len(lines)))

    # Exactly one RA record
    ra_count = sum(1 for l in lines if l[:2] == 'RA')
    if ra_count != 1:
        issues.append(Issue('error', 'RA.COUNT', f'Expected exactly 1 RA record, got {ra_count}'))

    # At least one RE + RT pair
    re_count = sum(1 for l in lines if l[:2] == 'RE')
    rt_count = sum(1 for l in lines if l[:2] == 'RT')
    if re_count == 0:
        issues.append(Issue('error', 'RE.MISSING', 'No RE (Employer) records found'))
    if re_count != rt_count:
        issues.append(Issue('error', 'RE_RT.MISMATCH',
                            f'RE count ({re_count}) must equal RT count ({rt_count})'))

    # Per-record validation
    current_re = None
    rw_records = []
    rw_totals = {
        'gross': 0, 'fit': 0, 'ss_wages': 0, 'ss_tax': 0,
        'medicare_wages': 0, 'medicare_tax': 0,
    }
    total_rw = 0

    for i, line in enumerate(lines, 1):
        if len(line) < 2:
            continue
        rec_type = line[:2]

        if rec_type == 'RA':
            issues.extend(_validate_ra(line, i))
        elif rec_type == 'RE':
            current_re = line
            issues.extend(_validate_re(line, i, expected_year))
        elif rec_type == 'RW':
            if current_re is None:
                issues.append(Issue('error', 'RW.ORPHAN',
                                    'RW record appears before any RE record', line_no=i))
                continue
            emp_totals = _validate_rw(line, i, issues)
            rw_records.append((i, emp_totals))
            for k, v in emp_totals.items():
                rw_totals[k] += v
            total_rw += 1
        elif rec_type == 'RT':
            issues.extend(_validate_rt(line, i, rw_totals))
            # Reset totals for the next RE block
            rw_totals = {k: 0 for k in rw_totals}
        elif rec_type == 'RF':
            issues.extend(_validate_rf(line, i, total_rw))
        elif rec_type in ('RS', 'RU', 'RV', 'RO'):
            # Optional state/local records; length is what we mostly care about
            pass
        else:
            issues.append(Issue('warning', 'RECORD.UNKNOWN',
                                f'Unknown record type "{rec_type}"', record=rec_type, line_no=i))

    return issues


def _validate_ra(line, line_no):
    issues = []
    # Position 3-11: Submitter EIN
    ein = line[2:11]
    ok, reason = validate_ein(ein)
    if not ok:
        issues.append(Issue('error', 'RA.EIN', f'Submitter EIN invalid: {reason}',
                            record='RA', line_no=line_no))
    # Position 46-102 (0-indexed 45:102): Company Name
    name = line[45:102].strip()
    if not name:
        issues.append(Issue('error', 'RA.NAME', 'Submitter name is blank',
                            record='RA', line_no=line_no))
    # Software Code position 45 (0-indexed 44): must be non-blank
    if line[44] == ' ':
        issues.append(Issue('warning', 'RA.SOFTWARE_CODE',
                            'Software code (pos 45) is blank; set to L for in-house',
                            record='RA', line_no=line_no))
    return issues


def _validate_re(line, line_no, expected_year):
    issues = []
    year = line[2:6]
    if not year.isdigit():
        issues.append(Issue('error', 'RE.YEAR', f'Tax year must be 4 digits, got "{year}"',
                            record='RE', line_no=line_no))
    elif expected_year is not None and int(year) != expected_year:
        issues.append(Issue('error', 'RE.YEAR_MISMATCH',
                            f'Tax year {year} does not match expected {expected_year}',
                            record='RE', line_no=line_no))
    else:
        year_int = int(year)
        this_year = datetime.now().year
        if year_int > this_year:
            issues.append(Issue('warning', 'RE.YEAR_FUTURE',
                                f'Tax year {year_int} is in the future',
                                record='RE', line_no=line_no))
        elif year_int < this_year - 4:
            issues.append(Issue('warning', 'RE.YEAR_STALE',
                                f'Tax year {year_int} is more than 4 years old; SSA may reject late filings',
                                record='RE', line_no=line_no))

    # Position 8-16 (0-indexed 7:16): Employer EIN
    ein = line[7:16]
    ok, reason = validate_ein(ein)
    if not ok:
        issues.append(Issue('error', 'RE.EIN', f'Employer EIN invalid: {reason}',
                            record='RE', line_no=line_no))
    # Position 40-96 (0-indexed 39:96): Employer name
    name = line[39:96].strip()
    if not name:
        issues.append(Issue('error', 'RE.NAME', 'Employer name is blank',
                            record='RE', line_no=line_no))
    # Position 163-164 (0-indexed 162:164): State
    state = line[162:164].strip()
    if state and state not in VALID_STATE_CODES:
        issues.append(Issue('error', 'RE.STATE', f'Invalid state code "{state}"',
                            record='RE', line_no=line_no))
    # Position 165-169 (0-indexed 164:169): ZIP
    zip_code = line[164:169].strip()
    if zip_code and not (zip_code.isdigit() and len(zip_code) == 5):
        issues.append(Issue('error', 'RE.ZIP', f'ZIP must be 5 digits, got "{zip_code}"',
                            record='RE', line_no=line_no))
    # Position 219 (0-indexed 218): Employment Code
    emp_code = line[218]
    if emp_code not in VALID_EMPLOYMENT_CODES:
        issues.append(Issue('error', 'RE.EMPLOYMENT_CODE',
                            f'Invalid employment code "{emp_code}"; must be one of {sorted(VALID_EMPLOYMENT_CODES)}',
                            record='RE', line_no=line_no))
    return issues


def _validate_rw(line, line_no, issues):
    """Validate an RW record and return the money-field totals for math-check aggregation."""
    # Position 3-11 (0-indexed 2:11): SSN
    ssn = line[2:11]
    ok, reason = validate_ssn(ssn)
    if not ok:
        issues.append(Issue('error', 'RW.SSN', f'SSN invalid: {reason}',
                            record='RW', line_no=line_no))

    # Position 42-61 (0-indexed 41:61): Last name
    last_name = line[41:61].strip()
    if not last_name:
        issues.append(Issue('error', 'RW.LAST_NAME', 'Employee last name is blank',
                            record='RW', line_no=line_no))

    # Position 132-133 (0-indexed 131:133): State
    state = line[131:133].strip()
    if state and state not in VALID_STATE_CODES:
        issues.append(Issue('warning', 'RW.STATE', f'Invalid state code "{state}"',
                            record='RW', line_no=line_no))

    # Money fields — extract as cents for math
    def money(start, length):
        raw = line[start:start + length]
        if not raw.isdigit():
            issues.append(Issue('error', 'RW.NUMERIC',
                                f'Money field at pos {start+1}-{start+length} must be all digits, got "{raw}"',
                                record='RW', line_no=line_no))
            return 0
        return int(raw)

    # Position 188-198 (0-indexed 187): Wages Box 1 (11 chars)
    gross_cents = money(187, 11)
    fit_cents = money(198, 11)
    ss_wages_cents = money(209, 11)
    ss_tax_cents = money(220, 11)
    mc_wages_cents = money(231, 11)
    mc_tax_cents = money(242, 11)

    # Math sanity
    if ss_wages_cents > 0:
        expected_ss = round(ss_wages_cents * 0.062)
        delta = abs(ss_tax_cents - expected_ss)
        if delta > 2:  # tolerate rounding to the nearest cent
            issues.append(Issue('warning', 'RW.SS_MATH',
                                f'SS tax ({ss_tax_cents/100:.2f}) ≠ 6.2% of SS wages ({ss_wages_cents/100:.2f}) — expected ~{expected_ss/100:.2f}',
                                record='RW', line_no=line_no))

    if mc_wages_cents > 0:
        expected_mc = round(mc_wages_cents * 0.0145)
        # Medicare tax may be higher than 1.45% if additional Medicare applied;
        # flag only if lower than expected.
        if mc_tax_cents + 2 < expected_mc:
            issues.append(Issue('warning', 'RW.MC_MATH',
                                f'Medicare tax ({mc_tax_cents/100:.2f}) < 1.45% of Medicare wages ({mc_wages_cents/100:.2f})',
                                record='RW', line_no=line_no))

    if gross_cents > 0 and ss_wages_cents == 0 and mc_wages_cents == 0:
        issues.append(Issue('warning', 'RW.FICA_MISSING',
                            'Box 1 wages present but SS/Medicare wages are zero — verify FICA exemption is intended',
                            record='RW', line_no=line_no))

    return {
        'gross': gross_cents,
        'fit': fit_cents,
        'ss_wages': ss_wages_cents,
        'ss_tax': ss_tax_cents,
        'medicare_wages': mc_wages_cents,
        'medicare_tax': mc_tax_cents,
    }


def _validate_rt(line, line_no, expected_totals):
    issues = []

    # RT money fields are 15 chars each starting at position 8 (0-indexed 7)
    def money(start, length):
        raw = line[start:start + length]
        if not raw.isdigit():
            issues.append(Issue('error', 'RT.NUMERIC',
                                f'RT money field at pos {start+1} must be all digits, got "{raw}"',
                                record='RT', line_no=line_no))
            return 0
        return int(raw)

    rt_count = money(2, 7)
    rt_gross = money(9, 15)
    rt_fit = money(24, 15)
    rt_ss_wages = money(39, 15)
    rt_ss_tax = money(54, 15)
    rt_mc_wages = money(69, 15)
    rt_mc_tax = money(84, 15)

    def check(label, expected_key, actual):
        expected = expected_totals.get(expected_key, 0)
        if abs(expected - actual) > 2:  # allow tiny rounding drift
            issues.append(Issue('error', f'RT.{label}',
                                f'RT {label} total ({actual/100:.2f}) does not match sum of RW records ({expected/100:.2f})',
                                record='RT', line_no=line_no))

    check('GROSS', 'gross', rt_gross)
    check('FIT', 'fit', rt_fit)
    check('SS_WAGES', 'ss_wages', rt_ss_wages)
    check('SS_TAX', 'ss_tax', rt_ss_tax)
    check('MC_WAGES', 'medicare_wages', rt_mc_wages)
    check('MC_TAX', 'medicare_tax', rt_mc_tax)

    return issues


def _validate_rf(line, line_no, total_rw):
    issues = []
    raw = line[7:16]
    if not raw.isdigit():
        issues.append(Issue('error', 'RF.COUNT_FORMAT',
                            f'RF employee count must be digits, got "{raw}"',
                            record='RF', line_no=line_no))
        return issues
    rf_count = int(raw)
    if rf_count != total_rw:
        issues.append(Issue('error', 'RF.COUNT_MISMATCH',
                            f'RF count ({rf_count}) does not match total RW records ({total_rw})',
                            record='RF', line_no=line_no))
    return issues


def summarize(issues):
    """Return (error_count, warning_count, summary_text)."""
    errors = [i for i in issues if i.severity == 'error']
    warnings = [i for i in issues if i.severity == 'warning']
    lines = []
    if errors:
        lines.append(f'{len(errors)} ERROR(S) — AccuWage will reject the file')
        for i in errors:
            loc = f'line {i.line_no}' if i.line_no else ''
            lines.append(f'  [{i.code}] {i.message} {loc}')
    if warnings:
        lines.append(f'{len(warnings)} WARNING(S) — review before uploading')
        for i in warnings:
            loc = f'line {i.line_no}' if i.line_no else ''
            lines.append(f'  [{i.code}] {i.message} {loc}')
    if not errors and not warnings:
        lines.append('No issues found. File passes local validation.')
        lines.append('Run AccuWage Online (https://www.ssa.gov/bso/) as a final check.')
    return len(errors), len(warnings), '\n'.join(lines)
