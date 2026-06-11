import re
from transformers import pipeline

# Load once at import time — takes ~10 s on first run
sentiment_model = pipeline(
    "sentiment-analysis",
    model="nlptown/bert-base-multilingual-uncased-sentiment",
    device=-1      # CPU; set to 0 for GPU
)

def clean_text(text: str) -> str:
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'http\S+', '', text)
    text = re.sub(r'[^a-zA-Z0-9\s]', '', text)
    return text.strip()

def get_sentiment(text: str) -> str:
    """
    Returns 'Positive', 'Neutral', or 'Negative'.
    Guards against empty text after cleaning to prevent BERT index errors.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return "Neutral"
    result = sentiment_model(cleaned[:512])[0]
    label  = result["label"]
    if "1" in label or "2" in label:
        return "Negative"
    elif "3" in label:
        return "Neutral"
    else:
        return "Positive"