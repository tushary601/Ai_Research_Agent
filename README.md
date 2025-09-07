## Setup

1. Create a `.env` file (same folder as `app.py`):

```
GOOGLE_API_KEY=your_google_api_key
GOOGLE_CSE_ID=your_custom_search_engine_id
GEMINI_API_KEY=your_gemini_api_key
```

2. Install dependencies:

```
pip install -r requirements.txt
```

3. Run:

```
python app.py
```

Open http://127.0.0.1:8000

## Notes

- If you see “Found sources, but none could be summarized”, the pages may be paywalled/blocked. Try another query.
- Check the terminal logs to see which extractor succeeded and whether Gemini failed.
