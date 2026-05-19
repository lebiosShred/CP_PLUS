import os
import time
import glob
import json
from google import genai
from google.genai import types

# --- CONFIGURATION ---
API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyDcOMSWHVkTZhy4mEfcHt5rB6YlcRAZcdQ")
PDF_FOLDER = "rfp_pdfs"
INDEX_FILE = "rfp_file_map.json"

client = genai.Client(api_key=API_KEY)

def index_pdfs():
    # 1. Check if folder exists
    if not os.path.exists(PDF_FOLDER):
        print(f"Error: Folder '{PDF_FOLDER}' not found.")
        os.makedirs(PDF_FOLDER)
        print(f"Created '{PDF_FOLDER}'. Please put your PDFs inside and run again.")
        return

    # 2. Get list of PDF files
    pdf_paths = glob.glob(os.path.join(PDF_FOLDER, "*.pdf"))
    if not pdf_paths:
        print("No PDF files found in the folder.")
        return

    print(f"Found {len(pdf_paths)} PDFs. Starting upload process...")
    
    # Load existing index if it exists (to avoid re-uploading if you restart)
    file_map = {}
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, "r") as f:
            file_map = json.load(f)

    active_files = []

    # 3. Upload Loop
    for path in pdf_paths:
        filename = os.path.basename(path)

        print(f"Uploading {filename}...", end="")
        try:
            file_ref = client.files.upload(
                file=path,
                config=types.UploadFileConfig(display_name=filename)
            )
            print(f" Done. URI: {file_ref.uri}")
            
            # Store details for waiting
            active_files.append({
                "filename": filename,
                "ref": file_ref
            })
        except Exception as e:
            print(f"\nFailed to upload {filename}: {e}")

    # 4. Wait for Processing (Batch Check)
    if active_files:
        print(f"\nWaiting for {len(active_files)} files to process...")
        for item in active_files:
            file_ref = item["ref"]
            while file_ref.state.name == "PROCESSING":
                print(".", end="", flush=True)
                time.sleep(2)
                file_ref = client.files.get(name=file_ref.name)
            
            if file_ref.state.name == "ACTIVE":
                # Update our local map
                file_map[item["filename"]] = {
                    "uri": file_ref.uri,
                    "name": file_ref.name, # Internal Google Name (files/xxxx)
                    "mime_type": file_ref.mime_type
                }
            else:
                print(f"\nFile {item['filename']} failed processing.")

    # 5. Save the "Index" to disk
    with open(INDEX_FILE, "w") as f:
        json.dump(file_map, f, indent=4)

    print(f"\nIndexing Complete! {len(file_map)} files available.")
    print(f"Map saved to '{INDEX_FILE}'")

if __name__ == "__main__":
    index_pdfs()