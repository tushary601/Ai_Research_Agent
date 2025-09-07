import os
import re
import time
import json
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request, jsonify, send_file, Response
from googleapiclient.discovery import build
from newspaper import Article, ArticleException
from bs4 import BeautifulSoup
import trafilatura
from readability import Document
from dotenv import load_dotenv
import google.generativeai as genai
from io import BytesIO
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch


load_dotenv()

app = Flask(__name__)

# -------- Config & API Keys --------
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("CRITICAL: GEMINI_API_KEY not found. Summarization will fail.")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# -------- Utilities --------
def google_search(search_term: str, api_key: str, cse_id: str, **kwargs):
    try:
        service = build("customsearch", "v1", developerKey=api_key)
        res = service.cse().list(q=search_term, cx=cse_id, **kwargs).execute()
        items = res.get("items", []) or []
        return [{"title": it.get("title"), "link": it.get("link")} for it in items]
    except Exception as e:
        print(f"[google_search] Error: {e}")
        return []

def fetch_html(url: str):
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception as e:
        print(f"[fetch_html] {url} error: {e}")
    return None

def extract_text(url: str):
    """Try multiple extractors in order: newspaper3k -> trafilatura -> readability -> BeautifulSoup."""
    title, text, publish_date = None, None, None

    # newspaper3k
    try:
        art = Article(url)
        art.download()
        art.parse()
        title = art.title or title
        text = art.text or text
        if art.publish_date:
            publish_date = art.publish_date
        if text and len(text) >= 300:
            return title, text, publish_date
    except Exception as e:
        print(f"[extract] newspaper3k failed: {e}")

    # trafilatura
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            extracted = trafilatura.extract(downloaded)
            if extracted and len(extracted) >= 300:
                return title, extracted, publish_date
    except Exception as e:
        print(f"[extract] trafilatura failed: {e}")

    # readability
    try:
        html = fetch_html(url)
        if html:
            doc = Document(html)
            title = title or doc.short_title()
            soup = BeautifulSoup(doc.summary(), "lxml")
            text = soup.get_text("\n", strip=True)
            if len(text) >= 300:
                return title, text, publish_date
    except Exception as e:
        print(f"[extract] readability failed: {e}")

    # BeautifulSoup fallback
    try:
        html = fetch_html(url)
        if html:
            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text("\n", strip=True)
            if len(text) >= 300:
                if not title and soup.title and soup.title.string:
                    title = soup.title.string.strip()
                return title, text, publish_date
    except Exception as e:
        print(f"[extract] bs4 failed: {e}")

    return None, None, None

def summarize_with_gemini(text: str, context: dict):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing.")
    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = f"""
You are a research assistant. Summarize the article into clear bullet points.

Requirements:
- 5-8 concise bullets.
- One-sentence tl;dr at top.
- Include any concrete numbers, dates, names.
- If the article is older, note its age.
- Output valid GitHub-flavored Markdown only.

Article title: {context.get('title') or 'Unknown'}
Article URL: {context.get('url')}
Article date (if any): {context.get('date_iso')}

CONTENT (first 8000 chars):
---
{text[:8000]}
---
"""
    resp = model.generate_content(prompt)
    return getattr(resp, "text", None)

# -------- Routes --------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/search", methods=["POST"])
def search():
    if not all([GOOGLE_API_KEY, GOOGLE_CSE_ID, GEMINI_API_KEY]):
        return jsonify({"error": "One or more API keys are missing. Check your .env."}), 500

    data = request.get_json(force=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "Search query cannot be empty."}), 400

    results = google_search(query, GOOGLE_API_KEY, GOOGLE_CSE_ID, num=10)
    summaries = []

    for item in results:
        url = item.get("link")
        if not url:
            continue
        title, text, publish_date = extract_text(url)
        if not text:
            continue

        try:
            md = summarize_with_gemini(text, {
                "title": title,
                "url": url,
                "date_iso": publish_date.isoformat() if publish_date else None
            })
        except Exception as e:
            print(f"Gemini failed for {url}: {e}")
            continue

        if not md:
            continue
        summaries.append({
            "title": title or item.get("title") or "Untitled",
            "url": url,
            "summary": md
        })
        if len(summaries) >= 6:
            break

    if results and not summaries:
        return jsonify({
            "error": "Found sources, but none could be summarized. "
                     "Likely paywalls, video links, or blocked scrapers."
        }), 500

    return jsonify(summaries)
@app.route("/export", methods=["POST"])
def export_pdf():
    """Accept summaries JSON and return a compiled PDF report."""
    data = request.get_json(force=True) or {}
    items = data.get("items", [])
    query = data.get("query", "Research Summary")
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if not items:
        return jsonify({"error": "No items to export."}), 400

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []

    # Report header
    story.append(Paragraph(f"<b>{query}</b>", styles["Title"]))
    story.append(Paragraph(f"Generated: {ts}", styles["Normal"]))
    story.append(Spacer(1, 0.3 * inch))

    # Add each article
    for i, it in enumerate(items, 1):
        story.append(Paragraph(f"<b>{i}. {it.get('title', 'Untitled')}</b>", styles["Heading2"]))
        if it.get("url"):
            story.append(Paragraph(f"<a href='{it['url']}'>{it['url']}</a>", styles["Normal"]))
        story.append(Spacer(1, 0.1 * inch))

        summary_text = it.get("summary", "No summary available.")
        for line in summary_text.split("\n"):
            if line.strip():
                story.append(Paragraph(line.strip(), styles["Normal"]))
        story.append(Spacer(1, 0.3 * inch))

    doc.build(story)
    buffer.seek(0)

    return Response(
        buffer,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=summary.pdf"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=8000)
