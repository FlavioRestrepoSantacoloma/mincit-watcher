# 📡 RegWatcher – MINCIT Decrees Monitor

RegWatcher is an automated pipeline that monitors **Colombia’s Ministry of Commerce, Industry and Tourism (MINCIT)** for new decrees, downloads the official PDFs, summarizes them with AI, and publishes a simple web dashboard.

This project is designed as a **RegTech / data engineering portfolio piece** showing end-to-end automation: scraping, state tracking, AI summarization, reporting and scheduled execution.

---

## 🔍 What it does

- Checks periodically:
  - `https://www.mincit.gov.co/normatividad/decretos/2025`
- Detects **new decree files** (PDFs served via `.aspx` links)
- Downloads each new decree into `downloads/`
- Extracts text from the PDF
- Calls the OpenAI API to generate:
  - A concise legal/compliance-oriented summary (in Spanish)
  - A list of key regulatory themes
  - The source institution (MINCIT)
- Stores results in:
  - `known_files.json` – which URLs have already been processed
  - `summaries.json` – all summaries + metadata

---

## 📊 Outputs

Each run updates:

- `report_decretos_2025.md` – Markdown summary of all decrees
- `report_decretos_2025.html` – Static HTML report
- `docs/index.html` – Interactive dashboard (for GitHub Pages):
  - Search bar
  - Source badge (MINCIT)
  - Tags by topic
  - One card per decree with summary and links

Optionally, the pipeline can send an **email notification** when new decrees are detected (configured via SMTP secrets).

---

## ⚙️ How it runs

The entire system runs on **GitHub Actions**, no server needed.

Workflow (see `.github/workflows/watcher.yml`):

1. Triggered every 30 minutes (cron) or manually.
2. Installs Python dependencies from `requirements.txt`.
3. Executes `watcher.py`.
4. Commits and pushes updated:
   - `summaries.json`
   - `known_files.json`
   - `report_decretos_2025.*`
   - `docs/index.html`

Secrets used:

- `OPENAI_API_KEY`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
- `EMAIL_FROM`, `EMAIL_TO`

---

## 🧠 Tech stack

- **Python** (requests, BeautifulSoup, PyPDF, dotenv)
- **OpenAI API** (GPT-4.1-mini for legal summaries)
- **GitHub Actions** (scheduled + CI-style automation)
- **GitHub Pages** (static dashboard)

---

## 🎯 Why this project matters

This project demonstrates:

- Real-world **regulatory monitoring** workflow
- Clean **stateful scraping** (no duplicate processing)
- Practical **AI integration** for legal/compliance use cases
- Automated **reporting & web publishing** on GitHub Pages
- CI/CD-style thinking applied to data & regulatory intelligence

It’s a compact but production-style example of how to turn messy public websites into structured, actionable legal insights.
