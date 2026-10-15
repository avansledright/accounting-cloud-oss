# Tax Form Templates

Drop the official IRS fillable PDF forms here so the app can fill them with your payroll data.

## Required files

For each tax year you run payroll, you need two PDFs named with the year:

- `fw2_<year>.pdf` — Form W-2 (Wage and Tax Statement), current year
- `f1040sh_<year>.pdf` — Schedule H (Form 1040), current year

Example for 2026: `fw2_2026.pdf`, `f1040sh_2026.pdf`.

## Where to get them

- **Form W-2**: https://www.irs.gov/forms-pubs/about-form-w-2 → "Current Revision" → download the fillable PDF
- **Schedule H**: https://www.irs.gov/forms-pubs/about-schedule-h-form-1040 → download the fillable PDF

The IRS publishes each year's fillable PDFs in late Q4 of the prior year (often December).

## Verifying field names

After placing the PDFs, visit `/payroll/forms/inspect` in the app. It will list every form field it found. If any of the app's default field mappings don't match, edit:

- `forms/w2_fieldmap_<year>.json`
- `forms/schedule_h_fieldmap_<year>.json`

If no per-year map exists, the app falls back to `forms/w2_fieldmap.json` / `forms/schedule_h_fieldmap.json` (defaults based on recent IRS conventions).

## Filing workflow

1. **W-2 Copy A → SSA**: use the generated `W2_EFW2_<year>.txt` file. Upload it to [SSA Business Services Online](https://www.ssa.gov/bso/) (free account). This is the official electronic filing channel. Optionally validate with SSA's AccuWage tool before uploading.
2. **W-2 Copies B, C, 2 → Employee**: hand the filled PDF to your employee by January 31.
3. **Schedule H → IRS**: file the filled PDF with your Form 1040. If you're using tax software (TurboTax, H&R Block, etc.), most will generate Schedule H for you directly — the PDF here is a working copy / paper backup.
