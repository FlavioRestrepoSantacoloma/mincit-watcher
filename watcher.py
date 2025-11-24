import os
import json
import smtplib
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pypdf import PdfReader
from openai import OpenAI

# ================== CONFIG ==================

TARGET_URL = "https://www.mincit.gov.co/normatividad/decretos/2025"

STATE_FILE = "known_files.json"
DOWNLOAD_DIR = Path("downloads")
SUMMARIES_FILE = "summaries.json"

# Load environment variables (.env)
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Email config
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")  # can be comma-separated

if not OPENAI_API_KEY:
    print("⚠️  WARNING: OPENAI_API_KEY not set in .env. Summaries will be skipped.")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# ================== BASIC SCRAPING ==================

def fetch_page(url: str) -> str:
    """
    Download the HTML for a given URL, pretending to be a real browser,
    and save it to debug.html so we can see what the server is returning.
    """
    print(f"→ Fetching page: {url}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    html = resp.text

    # Save for inspection if needed
    with open("debug.html", "w", encoding="utf-8") as f:
        f.write(html)

    return html


def extract_decree_files(html: str, base_url: str):
    """
    Extract document links from the page.
    Mincit uses /getattachment/.../Decreto-XXXX.aspx which serves a PDF.
    """
    soup = BeautifulSoup(html, "html.parser")
    files = []

    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()

        # In the page the docs look like /getattachment/.../Decreto-XXX.aspx
        if "/getattachment/" in href and href.lower().endswith(".aspx"):
            full_url = urljoin(base_url, href)
            file_name = full_url.split("/")[-1]

            files.append({
                "url": full_url,
                "name": file_name
            })

    # Remove duplicates by URL
    return list({f["url"]: f for f in files}.values())


# ================== STATE HANDLING ==================

def load_known_files():
    if not Path(STATE_FILE).exists():
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_known_files(files_dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(files_dict, f, indent=2, ensure_ascii=False)


def load_summaries():
    if not Path(SUMMARIES_FILE).exists():
        return {}
    with open(SUMMARIES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_summaries(summaries_dict):
    with open(SUMMARIES_FILE, "w", encoding="utf-8") as f:
        json.dump(summaries_dict, f, indent=2, ensure_ascii=False)


# ================== DOWNLOAD ==================

def download_file(file_info):
    """
    Downloads the file. The .aspx file served is actually a PDF.
    """
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    url = file_info["url"]
    name = file_info["name"].replace(".aspx", ".pdf")
    dest = DOWNLOAD_DIR / name

    print(f"⬇️  Downloading {url} → {dest}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()

    with open(dest, "wb") as f:
        f.write(resp.content)

    print("   ✓ Downloaded")
    return dest


# ================== PDF → TEXT ==================

def extract_text_from_pdf(filepath: Path) -> str:
    """
    Extracts text from a PDF using pypdf.
    Returns a single string with all pages concatenated.
    """
    print(f"📝 Extracting text from {filepath.name} ...")
    reader = PdfReader(str(filepath))
    texts = []

    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception as e:
            print(f"   ⚠️  Warning: could not read a page from {filepath}: {e}")

    full_text = "\n\n".join(texts)
    return full_text.strip()


# ================== CHATGPT SUMMARY ==================

def summarize_file(filepath: Path, title: str) -> str:
    """
    Extracts text from the PDF and asks ChatGPT for a summary in Spanish.
    If the file is huge, we truncate the text for safety.
    """
    if client is None:
        return "Resumen omitido (no hay OPENAI_API_KEY configurada)."

    try:
        text = extract_text_from_pdf(filepath)
    except Exception as e:
        print(f"   ❌ Error extracting text from {filepath}: {e}")
        return "No se pudo extraer el texto del PDF para resumirlo."

    if not text:
        return "El PDF no contiene texto legible o está escaneado como imagen."

    max_chars = 12000  # to keep the prompt manageable
    if len(text) > max_chars:
        text_to_summarize = text[:max_chars]
        truncated_note = " (Texto truncado para el resumen por límite de longitud.)"
    else:
        text_to_summarize = text
        truncated_note = ""

    prompt = f"""
Resume en español claro y conciso el siguiente decreto/regulación del Ministerio de Comercio, Industria y Turismo de Colombia.
Indica:
- De qué trata
- A quién aplica
- Los puntos clave principales
Máximo 200 palabras.

Título del archivo: {title}{truncated_note}

Texto del documento:
{text_to_summarize}
"""

    print(f"🤖 Solicitando resumen a ChatGPT para {title} ...")

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "system",
                "content": "Eres un asistente experto en derecho administrativo colombiano. Resumes decretos en lenguaje claro y no técnico."
            },
            {
                "role": "user",
                "content": prompt
            },
        ],
    )

    summary = response.choices[0].message.content.strip()
    print("   ✓ Resumen generado")
    return summary


# ================== REPORTS ==================

def generate_markdown_report(summaries: dict, output_path: Path):
    """
    Generates a simple Markdown report listing each decree and its summary.
    """
    lines = []
    lines.append("# Decretos 2025 – Resumen automático\n")
    lines.append(f"_Total de decretos resumidos: {len(summaries)}_\n")
    lines.append("---\n")

    # Sort by filename for stable order
    for url, info in sorted(summaries.items(), key=lambda x: x[1]["name"]):
        name = info.get("name", "Sin nombre")
        summary = info.get("summary", "Sin resumen disponible.")
        local_path = info.get("local_path", "")
        lines.append(f"## {name}\n")
        lines.append(f"- URL original: {url}")
        if local_path:
            lines.append(f"- Archivo local: `{local_path}`")
        lines.append("\n**Resumen:**\n")
        lines.append(summary.strip())
        lines.append("\n---\n")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"📄 Reporte Markdown generado en: {output_path}")


def generate_html_report(summaries: dict, output_path: Path):
    """
    Generates a very simple HTML report listing each decree and its summary.
    """
    html_parts = []
    html_parts.append("""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Decretos 2025 – Resumen automático</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
           max-width: 900px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
    h1 { border-bottom: 2px solid #333; padding-bottom: 0.5rem; }
    h2 { margin-top: 2rem; }
    .meta { font-size: 0.9rem; color: #555; margin-bottom: 0.5rem; }
    .card { margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 1px solid #ddd; }
    .summary { margin-top: 0.5rem; white-space: pre-wrap; }
    a { color: #0645ad; }
  </style>
</head>
<body>
""")

    html_parts.append(f"<h1>Decretos 2025 – Resumen automático</h1>\n")
    html_parts.append(f"<p>Total de decretos resumidos: <strong>{len(summaries)}</strong></p>\n")
    html_parts.append("<hr>\n")

    for url, info in sorted(summaries.items(), key=lambda x: x[1]["name"]):
        name = info.get("name", "Sin nombre")
        summary = info.get("summary", "Sin resumen disponible.")
        local_path = info.get("local_path", "")

        html_parts.append('<div class="card">')
        html_parts.append(f"<h2>{name}</h2>")
        html_parts.append('<div class="meta">')
        html_parts.append(f'URL original: <a href="{url}" target="_blank">{url}</a><br>')
        if local_path:
            html_parts.append(f"Archivo local: <code>{local_path}</code><br>")
        html_parts.append("</div>")
        html_parts.append('<div class="summary">')
        html_parts.append(summary.replace("\n", "<br>\n"))
        html_parts.append("</div>")
        html_parts.append("</div>\n")

    html_parts.append("</body>\n</html>\n")

    output_path.write_text("".join(html_parts), encoding="utf-8")
    print(f"🌐 Reporte HTML generado en: {output_path}")


# ================== EMAIL NOTIFICATIONS ==================

def send_email_notification(new_items: list, html_report_path: Path | None = None):
    """
    Sends a simple email listing new decrees and their summaries (shortened).
    """
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and EMAIL_FROM and EMAIL_TO):
        print("⚠️  Email not configured, skipping notification.")
        return

    if not new_items:
        print("No new items to notify by email.")
        return

    recipients = [e.strip() for e in EMAIL_TO.split(",") if e.strip()]

    subject = f"[MINCIT] Nuevos decretos 2025: {len(new_items)} nuevo(s)"

    lines = []
    lines.append("Se han detectado nuevos decretos en la página de MINCIT 2025.\n")
    for item in new_items:
        lines.append(f"- {item['name']}")
        lines.append(f"  URL original: {item['url']}")
        lines.append("")
        summary = item.get("summary", "")
        if summary:
            snippet = summary[:400] + ("..." if len(summary) > 400 else "")
            lines.append("  Resumen:")
            lines.append("  " + snippet.replace("\n", "\n  "))
            lines.append("")

    if html_report_path and html_report_path.exists():
        lines.append(f"Reporte HTML generado en el servidor: {html_report_path.name}")

    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    print("📧 Enviando email de notificación...")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print("   ✓ Email enviado")


# ================== MAIN FLOW ==================

def main():
    print(f"Fetching index page: {TARGET_URL} ...")
    index_html = fetch_page(TARGET_URL)

    decree_files = extract_decree_files(index_html, TARGET_URL)
    print(f"\n✓ Found {len(decree_files)} decree files on the website.\n")

    # Load previous state and summaries
    known = load_known_files()
    summaries = load_summaries()

    # Process anything that does NOT have a summary yet
    new_files = [f for f in decree_files if f["url"] not in summaries]

    print(f"🆕 Files to summarize: {len(new_files)}")
    for f in new_files:
        print(f"   - {f['name']}")

    processed_items_for_email = []

    # Process each file that still needs a summary
    for f in new_files:
        # 1) Download
        pdf_path = download_file(f)

        # 2) Summarize
        summary = summarize_file(pdf_path, f["name"])

        # 3) Save info in summaries dict (keyed by URL)
        summaries[f["url"]] = {
            "name": f["name"],
            "local_path": str(pdf_path),
            "summary": summary,
        }

        # 4) Mark as known
        known[f["url"]] = f

        # 5) Collect for email
        processed_items_for_email.append({
            "url": f["url"],
            "name": f["name"],
            "summary": summary,
        })

    # Save updated state and summaries
    save_known_files(known)
    save_summaries(summaries)

    print("\n✓ Done.")
    if new_files:
        print("📝 Summaries stored/updated in summaries.json")
    else:
        print("No new summaries needed.")

    # Generate reports if there is at least one summary
    html_path = None
    if summaries:
        md_path = Path("report_decretos_2025.md")
        html_path = Path("report_decretos_2025.html")
        generate_markdown_report(summaries, md_path)
        generate_html_report(summaries, html_path)

    # Send email for new items
    if processed_items_for_email:
        send_email_notification(processed_items_for_email, html_report_path=html_path)


if __name__ == "__main__":
    main()
