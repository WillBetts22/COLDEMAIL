# Seng Seng Plastic — Cold Outreach Researcher

This tool finds medical supply companies that might want to buy from Seng Seng Plastic, researches each one, and writes a personalized cold email draft for each. Everything lands in a spreadsheet you can review before sending anything. **No emails are ever sent automatically.**

---

## What you need before running

- Python 3.11 or later installed on your computer
- API keys for three services (see below)

---

## Setup (one time only)

### Step 1 — Install the required libraries

Open your Terminal, navigate to this folder, and run:

```
pip install -r requirements.txt
```

If you see "pip not found," try `pip3 install -r requirements.txt`.

### Step 2 — Add your API keys

Open the file called `.env` in this folder. It looks like this:

```
APOLLO_API_KEY=paste_your_apollo_key_here
ANTHROPIC_API_KEY=paste_your_anthropic_key_here
FIRECRAWL_API_KEY=paste_your_firecrawl_key_here
```

Replace each placeholder with your real key:

| Key | Where to find it |
|-----|-----------------|
| `APOLLO_API_KEY` | apollo.io → Settings → Integrations → API Keys |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `FIRECRAWL_API_KEY` | firecrawl.dev → Dashboard → API Keys |

Save the file. The `.env` file is private — it is never uploaded to GitHub.

**Apollo plan note:** Email reveals (unlocking a contact's email address) require at least Apollo's Basic paid plan. If you are on the free tier, most contacts will be skipped because their emails can't be retrieved.

---

## Running the tool

From this folder in your Terminal:

```
python main.py
```

That's it. The tool will:
1. Search Apollo for medical supply distributors
2. Remove any companies already contacted in previous runs
3. Scrape each company's website for research
4. Write a personalized cold email for each company using Claude
5. Save everything to a spreadsheet in the `output/` folder

### Options

Run with only 2 companies (good for testing):
```
python main.py --count 2
```

Dry run — does everything but does NOT update the history file, and marks the output file as a test:
```
python main.py --dry-run --count 2
```

---

## Reading the spreadsheet

Open the file in the `output/` folder in Excel or Numbers. Each row is one company.

| Column | What it means |
|--------|---------------|
| **Decision** | Type `Approve` or `Skip` — this is for you to fill in |
| **Company** | The company name |
| **Website** | Their website |
| **Contact Name** | The person to email |
| **Title** | Their job title |
| **Email** | Their verified email address |
| **Fit Summary** | 2–3 sentences on why they might want Seng Seng's packaging |
| **Email Subject** | The drafted subject line |
| **Email Body** | The full drafted email — read it carefully before approving |
| **Sources** | Every URL that was used to research this company |
| **Apollo / Data Notes** | Company size, industry, any warnings |

After you approve a row, copy the subject and body and send the email yourself from your own email account.

---

## Changing the targeting

Open `config.yaml` to adjust:

- **`companies_per_run`** — how many companies to research each time you run (default: 12)
- **`icp.keywords`** — keywords Apollo uses to find companies
- **`icp.contact_titles`** — job titles of the people you want to reach
- **`icp.employee_range`** — minimum and maximum company size (number of employees)
- **`icp.blocklist_companies`** — companies that must NEVER be contacted (e.g., Medline competitors)

---

## The blocklist (important)

The `blocklist_companies` list in `config.yaml` is a hard "never contact" list. Any company whose name matches a blocklist entry will be removed before research even starts — they will never appear in your spreadsheet.

Currently blocked: **Interplast** (a competitor of Medline, Seng Seng's current medical customer).

To add more companies to the blocklist, edit `config.yaml`:

```yaml
blocklist_companies:
  - "Interplast"
  - "Another Company Name"
```

---

## Things to verify if something doesn't work

- **"Missing APOLLO_API_KEY"** → Open `.env` and make sure your Apollo key is pasted after the `=` sign with no spaces.
- **Apollo returns 0 results** → Your Apollo plan may not support people search with keyword filters. Check apollo.io/settings/billing.
- **"Firecrawl account has no remaining credits"** → Add more credits at firecrawl.dev.
- **Draft failed rows** → Claude couldn't parse the response. The row will say "DRAFT FAILED" — you can write that email manually.
- **Running `python main.py` from the wrong folder** → Make sure your Terminal is in the folder that contains `main.py` and `config.yaml`.
