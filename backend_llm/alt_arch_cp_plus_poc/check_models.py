import os
import sys
from dotenv import load_dotenv
from google import genai

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    print("ERROR: GEMINI_API_KEY not set. Add it to your .env file.")
    sys.exit(1)

client = genai.Client(api_key=API_KEY)

print(f"Checking available models for your API Key...")

try:
    for m in client.models.list():
        # Just print the name property directly
        print(f"Found: {m.name}")
            
except Exception as e:
    print(f"Error listing models: {e}")