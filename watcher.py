import os
import json
import smtplib
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pypdf import PdfReader
from openai import OpenAI

# ================== CONFIG ==================

# Years you want to monitor
YEARS = [2025]  # you can extend later: [2025, 2024, 2023, 2022]

BASE_URL = "https://www.mincit.gov.co/normatividad/decretos/{year}"

STATE_FILE = "known_files.json"
DOWNLOAD_DIR = Path("downloads")
SUMMARIES_FILE = "summaries.json"
ERROR_LOG_FILE = "error_log.log"

# For now all these URLs are from MINCIT (you can introduce more sources later)
DEFAULT_SOURCE = "Ministerio de Comercio, Industria y Turismo"

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


# ================== LOGGING ==================

def log_error(message: str):
    """Append a timestamped error message to ERROR_LOG_FILE."""
    ts = datetime.utcnow().isoformat() + "Z"
    line = f"[{ts}] {message}\n"
    try:
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # As a last resort, at least print it
        print("❌ Could not write to error log:", line)


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

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log_error(f"Error fetching page {url}: {e}")
        raise

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
    path = Path(STATE_FILE)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        log_error(f"{STATE_FILE} is empty or invalid JSON: {e}. Resetting state.")
        return {}


def save_known_files(files_dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(files_dict, f, indent=2, ensure_ascii=False)


def load_summaries():
    path = Path(SUMMARIES_FILE)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        log_error(f"{SUMMARIES_FILE} is empty or invalid JSON: {e}. Resetting summaries.")
        return {}


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

    try:
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        log_error(f"Error downloading {url}: {e}")
        raise

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
    try:
        reader = PdfReader(str(filepath))
    except Exception as e:
        log_error(f"Error opening PDF {filepath}: {e}")
        raise

    texts = []

    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception as e:
            log_error(f"Could not read a page from {filepath}: {e}")
            print(f"   ⚠️  Warning: could not read a page from {filepath}: {e}")

    full_text = "\n\n".join(texts)
    return full_text.strip()


# ================== CHATGPT ANALYSIS ==================

def analyze_file(filepath: Path, title: str, year: int | None, source_hint: str | None) -> dict:
    """
    Extracts text from the PDF and asks ChatGPT for:
      - summary (Spanish, tech/compliance oriented)
      - thematic tags
      - source (institution)
    Returns: {"summary": str, "themes": [str, ...], "source": str}
    """
    if client is None:
        return {
            "summary": "Resumen omitido (no hay OPENAI_API_KEY configurada).",
            "themes": [],
            "source": source_hint or "Desconocida",
        }

    try:
        text = extract_text_from_pdf(filepath)
    except Exception as e:
        log_error(f"Text extraction failed for {filepath}: {e}")
        return {
            "summary": "No se pudo extraer el texto del PDF para resumirlo.",
            "themes": [],
            "source": source_hint or "Desconocida",
        }

    if not text:
        return {
            "summary": "El PDF no contiene texto legible o está escaneado como imagen.",
            "themes": [],
            "source": source_hint or "Desconocida",
        }

    max_chars = 12000  # to keep the prompt manageable
    if len(text) > max_chars:
        text_to_summarize = text[:max_chars]
        truncated_note = " (Texto truncado para el resumen por límite de longitud.)"
    else:
        text_to_summarize = text
        truncated_note = ""

    year_info = f"{year}" if year is not None else "desconocido"
    source_info = source_hint or "Entidad emisora desconocida"

    prompt = f"""
Eres un analista especializado en derecho regulatorio, políticas públicas y cumplimiento
para empresas del sector tecnológico en América Latina.

Tu función es apoyar a una plataforma de monitoreo normativo que identifica, resume y analiza
nuevas regulaciones provenientes de entidades gubernamentales, ministerios, agencias de
supervisión y organismos multilaterales.

Analiza el siguiente documento normativo y devuelve **EXCLUSIVAMENTE** un JSON válido con la
siguiente estructura:

{{
  "summary": "resumen claro y conciso (máx. 200 palabras), orientado a equipos legales y de compliance",
  "themes": ["lista de temas o áreas regulatorias clave"],
  "source": "nombre de la entidad emisora, ej: {source_info}"
}}

Instrucciones de contenido:

- El resumen debe explicar de forma comprensible para empresas tecnológicas:
  - el propósito de la norma
  - obligaciones o requisitos relevantes
  - implicaciones para negocios digitales, plataformas o servicios tecnológicos
  - cambios regulatorios claves

- "themes" debe contener etiquetas temáticas breves como:
  "Comercio exterior", "Protección al consumidor", "Datos personales",
  "Competencia", "Servicios digitales", "Aduanas", "Zonas francas",
  "Propiedad intelectual", "Telecomunicaciones", "Fintech", "Pymes", etc.

- "source" debe identificar explícitamente la institución emisora.
  Para este documento, si procede: "{source_info}"

Metadatos relevantes:
- Título del archivo: {title}{truncated_note}
- Año estimado: {year_info}
- Fuente esperada (si se conoce): {source_info}

Texto del documento completo:
{text_to_summarize}
"""

    print(f"🤖 Solicitando análisis a ChatGPT para {title} ...")

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Respondes SIEMPRE con JSON válido. No incluyas nada de texto fuera del JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        )
        content = response.choices[0].message.content.strip()
    except Exception as e:
        log_error(f"OpenAI API error for {filepath}: {e}")
        return {
            "summary": "No se pudo generar el resumen (error de la API).",
            "themes": [],
            "source": source_info,
        }

    # Try to parse JSON; fallback if something goes wrong
    try:
        # In case the model adds some text, try to extract the first {...} block
        start = content.index("{")
        end = content.rindex("}") + 1
        json_str = content[start:end]
        data = json.loads(json_str)
        summary = str(data.get("summary", "")).strip()
        themes_raw = data.get("themes") or []
        source = str(data.get("source", source_info)).strip()
        themes = [str(t).strip() for t in themes_raw if str(t).strip()]
        return {
            "summary": summary or "Resumen no disponible.",
            "themes": themes,
            "source": source or source_info,
        }
    except Exception as e:
        log_error(
            f"Could not parse JSON from OpenAI for {filepath}: {e}. "
            f"Content (first 300 chars): {content[:300]}"
        )
        return {
            "summary": content,
            "themes": [],
            "source": source_info,
        }


# ================== REPORTS ==================

def generate_markdown_report(summaries: dict, output_path: Path):
    """
    Generates a simple Markdown report listing each decree and its summary.
    """
    lines = []
    lines.append("# Decretos – Resumen automático\n")
    lines.append(f"_Total de decretos resumidos: {len(summaries)}_\n")
    lines.append("---\n")

    # Sort by year then filename
    def sort_key(item):
        url, info = item
        return (info.get("year") or 9999, info["name"])

    for url, info in sorted(summaries.items(), key=sort_key):
        name = info.get("name", "Sin nombre")
        summary = info.get("summary", "Sin resumen disponible.")
        local_path = info.get("local_path", "")
        year = info.get("year")
        themes = info.get("themes") or []
        source = info.get("source") or "Desconocida"

        lines.append(f"## {name}\n")
        if year:
            lines.append(f"- Año: **{year}**")
        lines.append(f"- Fuente: **{source}**")
        lines.append(f"- URL original: {url}")
        if local_path:
            lines.append(f"- Archivo local: `{local_path}`")
        if themes:
            lines.append(f"- Temas: {', '.join(themes)}")
        lines.append("\n**Resumen:**\n")
        lines.append(summary.strip())
        lines.append("\n---\n")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"📄 Reporte Markdown generado en: {output_path}")


def generate_html_report(summaries: dict, output_path: Path):
    """
    Generates a HTML report with search box, source filters, tags and cards per decree.
    """
    html_parts = []
    html_parts.append("""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Decretos – Resumen automático</title>
  <style>
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      max-width: 1024px;
      margin: 2rem auto;
      padding: 0 1.5rem;
      line-height: 1.6;
      background-color: #f7f7f9;
    }
    h1 {
      border-bottom: 2px solid #333;
      padding-bottom: 0.5rem;
      margin-bottom: 0.5rem;
    }
    .subtitle {
      color: #555;
      margin-bottom: 1.5rem;
    }
    .search-container {
      margin-bottom: 1rem;
    }
    .search-input {
      width: 100%;
      padding: 0.6rem 0.8rem;
      font-size: 1rem;
      border-radius: 0.5rem;
      border: 1px solid #ccc;
      box-sizing: border-box;
    }
    .source-filters {
      margin-bottom: 1rem;
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
    }
    .source-btn {
      border: 1px solid #d1d5db;
      background-color: #f3f4f6;
      border-radius: 999px;
      padding: 0.25rem 0.8rem;
      font-size: 0.8rem;
      cursor: pointer;
    }
    .source-btn.active {
      background-color: #312e81;
      color: #ffffff;
      border-color: #312e81;
    }
    .card {
      margin-bottom: 1.5rem;
      padding: 1rem 1.2rem;
      border-radius: 0.7rem;
      background-color: #ffffff;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    }
    .card h2 {
      margin: 0 0 0.3rem 0;
      font-size: 1.05rem;
    }
    .meta {
      font-size: 0.85rem;
      color: #666;
      margin-bottom: 0.4rem;
    }
    .summary {
      margin-top: 0.5rem;
      white-space: pre-wrap;
      font-size: 0.95rem;
    }
    .tags {
      margin-top: 0.3rem;
    }
    .tag {
      display: inline-block;
      margin-right: 0.35rem;
      margin-bottom: 0.25rem;
      padding: 0.15rem 0.45rem;
      font-size: 0.78rem;
      border-radius: 999px;
      background-color: #eef2ff;
      color: #3730a3;
    }
    .source-pill {
      display: inline-block;
      margin-right: 0.5rem;
      padding: 0.15rem 0.6rem;
      font-size: 0.78rem;
      border-radius: 999px;
      background-color: #e0f2fe;
      color: #0369a1;
    }
    a {
      color: #0645ad;
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
    .no-results {
      margin-top: 1rem;
      color: #777;
      font-style: italic;
    }
  </style>
</head>
<body>
""")

    html_parts.append("<h1>Decretos – Resumen automático</h1>\n")
    html_parts.append(
        f"<p class='subtitle'>Total de decretos resumidos: "
        f"<strong>{len(summaries)}</strong>. "
        f"Use el buscador y los filtros de fuente para explorar la normativa relevante.</p>\n"
    )

    # Search box
    html_parts.append("""
<div class="search-container">
  <input id="searchInput" class="search-input" type="text" placeholder="Buscar por texto en el título, temas o resumen...">
</div>
""")

    # Source filters
    sources = sorted(
        {info.get("source", "Desconocida") for info in summaries.values() if info.get("source")}
    )
    html_parts.append('<div id="sourceFilters" class="source-filters">')
    html_parts.append('<button class="source-btn active" data-source="">Todas las fuentes</button>')
    for src in sources:
        safe_attr = src.replace('"', '&quot;')
        html_parts.append(
            f'<button class="source-btn" data-source="{safe_attr}">{src}</button>'
        )
    html_parts.append("</div>\n")

    html_parts.append(
        '<div id="noResults" class="no-results" style="display:none;">'
        "No se encontraron decretos con ese criterio."
        "</div>\n"
    )

    html_parts.append('<div id="cardsContainer">\n')

    def sort_key(item):
        url, info = item
        return (info.get("year") or 9999, info["name"])

    for url, info in sorted(summaries.items(), key=sort_key):
        name = info.get("name", "Sin nombre")
        summary = info.get("summary", "Sin resumen disponible.")
        local_path = info.get("local_path", "")
        year = info.get("year")
        themes = info.get("themes") or []
        source = info.get("source") or "Desconocida"

        search_blob = f"{name} {summary} {' '.join(themes)} {year or ''} {source}"
        search_blob = search_blob.lower().replace('"', '\\"')
        card_source_attr = source.replace('"', '&quot;')

        html_parts.append(
            f'<div class="card" data-search="{search_blob}" '
            f'data-source="{card_source_attr}">'
        )
        html_parts.append(f"<h2>{name}</h2>")
        html_parts.append('<div class="meta">')
        html_parts.append(f'<span class="source-pill">{source}</span><br>')
        if year:
            html_parts.append(f"Año: <strong>{year}</strong><br>")
        html_parts.append(f'URL original: <a href="{url}" target="_blank">{url}</a><br>')
        if local_path:
            html_parts.append(
                f"Archivo local (en entorno de ejecución): <code>{local_path}</code><br>"
            )
        html_parts.append("</div>")

        if themes:
            html_parts.append('<div class="tags">')
            for t in themes:
                html_parts.append(f'<span class="tag">{t}</span>')
            html_parts.append("</div>")

        html_parts.append('<div class="summary">')
        html_parts.append(summary.replace("\n", "<br>\n"))
        html_parts.append("</div>")
        html_parts.append("</div>\n")

    html_parts.append("</div>\n")

    # Client-side search + source filter logic
    html_parts.append("""
<script>
  const input = document.getElementById('searchInput');
  const cardsContainer = document.getElementById('cardsContainer');
  const noResults = document.getElementById('noResults');
  const sourceButtons = document.querySelectorAll('.source-btn');

  let activeSource = '';

  sourceButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      activeSource = btn.getAttribute('data-source') || '';
      sourceButtons.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      applyFilters();
    });
  });

  input.addEventListener('input', function() {
    applyFilters();
  });

  function applyFilters() {
    const query = input.value.toLowerCase().trim();
    const cards = cardsContainer.getElementsByClassName('card');
    let visibleCount = 0;

    for (const card of cards) {
      const haystack = card.getAttribute('data-search') || '';
      const cardSource = card.getAttribute('data-source') || '';
      const matchesText = !query || haystack.indexOf(query) !== -1;
      const matchesSource = !activeSource || cardSource === activeSource;

      if (matchesText && matchesSource) {
        card.style.display = '';
        visibleCount++;
      } else {
        card.style.display = 'none';
      }
    }

    if (visibleCount === 0 && (query || activeSource)) {
      noResults.style.display = 'block';
    } else {
      noResults.style.display = 'none';
    }
  }
</script>
""")

    html_parts.append("</body>\n</html>\n")

    output_path.parent.mkdir(parents=True, exist_ok=True)
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

    subject = f"[MINCIT] Nuevos decretos: {len(new_items)} nuevo(s)"

    lines = []
    lines.append("Se han detectado nuevos decretos en la página de MINCIT.\n")
    for item in new_items:
        lines.append(f"- {item['name']}")
        if item.get("year"):
            lines.append(f"  Año: {item['year']}")
        source = item.get("source")
        if source:
            lines.append(f"  Fuente: {source}")
        lines.append(f"  URL original: {item['url']}")
        lines.append("")
        summary = item.get("summary", "")
        if summary:
            snippet = summary[:400] + ("..." if len(summary) > 400 else "")
            lines.append("  Resumen:")
            lines.append("  " + snippet.replace("\n", "\n  "))
            lines.append("")
        themes = item.get("themes") or []
        if themes:
            lines.append(f"  Temas: {', '.join(themes)}")
            lines.append("")

    if html_report_path and html_report_path.exists():
        lines.append(f"Reporte HTML (en el entorno de ejecución): {html_report_path.name}")

    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    print("📧 Enviando email de notificación...")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        print("   ✓ Email enviado")
    except Exception as e:
        log_error(f"Error sending email notification: {e}")
        print("   ❌ Error enviando el email (registrado en el log).")


# ================== MAIN FLOW ==================

def main():
    try:
        all_decree_files = []

        # --- Multi-year scraping ---
        for year in YEARS:
            url = BASE_URL.format(year=year)
            print(f"Fetching index page for {year}: {url} ...")
            index_html = fetch_page(url)
            decree_files = extract_decree_files(index_html, url)
            for f in decree_files:
                f["year"] = year
            print(f"✓ Found {len(decree_files)} decree files for {year}.\n")
            all_decree_files.extend(decree_files)

        print(f"✓ Total decree files across years {YEARS}: {len(all_decree_files)}\n")

        # Load previous state and summaries
        known = load_known_files()
        summaries = load_summaries()

        # Process anything that does NOT have a summary yet
        new_files = [f for f in all_decree_files if f["url"] not in summaries]

        print(f"🆕 Files to summarize: {len(new_files)}")
        for f in new_files:
            year_display = f.get("year") or "?"
            print(f"   - ({year_display}) {f['name']}")

        processed_items_for_email = []

        # Process each file that still needs a summary
        for f in new_files:
            year = f.get("year")
            source_hint = DEFAULT_SOURCE  # later you can make this dynamic per source

            # 1) Download
            pdf_path = download_file(f)

            # 2) Analyze (summary + themes + source)
            analysis = analyze_file(pdf_path, f["name"], year, source_hint)
            summary = analysis.get("summary", "")
            themes = analysis.get("themes") or []
            source = analysis.get("source") or source_hint

            # 3) Save info in summaries dict (keyed by URL)
            summaries[f["url"]] = {
                "name": f["name"],
                "local_path": str(pdf_path),
                "summary": summary,
                "themes": themes,
                "source": source,
                "year": year,
            }

            # 4) Mark as known
            known[f["url"]] = f

            # 5) Collect for email
            processed_items_for_email.append({
                "url": f["url"],
                "name": f["name"],
                "summary": summary,
                "themes": themes,
                "source": source,
                "year": year,
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
        html_path_root = None
        if summaries:
            # Keep file names for backwards compatibility
            md_path = Path("report_decretos_2025.md")
            html_path_root = Path("report_decretos_2025.html")
            docs_html_path = Path("docs") / "index.html"

            generate_markdown_report(summaries, md_path)
            generate_html_report(summaries, html_path_root)
            generate_html_report(summaries, docs_html_path)

        # Send email for new items
        if processed_items_for_email:
            send_email_notification(processed_items_for_email, html_report_path=html_path_root)

    except Exception as e:
        log_error(f"Unhandled error in main(): {e}")
        raise


if __name__ == "__main__":
    main()
