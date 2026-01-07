import os
import io
import json
from typing import List

from fastapi import FastAPI, Request, Response, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import fitz  # PyMuPDF
import firebase_admin
from firebase_admin import credentials, firestore, storage

# --- FastAPI App Setup ---
app = FastAPI()

# --- Security and Configuration ---
DELETE_PASSWORD = os.getenv("DELETE_PASSWORD", "1234567890")

# Cache for PDF page counts
pdf_info_cache = {}

# --- Firebase Setup ---
SERVICE_ACCOUNT_JSON_STRING = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET") # e.g., your-project-id.appspot.com

if SERVICE_ACCOUNT_JSON_STRING:
    service_account_info = json.loads(SERVICE_ACCOUNT_JSON_STRING)
    cred = credentials.Certificate(service_account_info)
else:
    cred = credentials.Certificate("firebase-service-account.json")

# Initialize Firebase with Storage
firebase_admin.initialize_app(cred, {'storageBucket': STORAGE_BUCKET})
db = firestore.client()
bucket = storage.bucket()

# --- API Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def get_index():
    return FileResponse("index.html")

@app.get("/list-pdfs", response_model=List[str])
async def list_pdfs():
    """Lists all .pdf files from Firebase Storage."""
    blobs = bucket.list_blobs()
    pdf_files = [blob.name for blob in blobs if blob.name.lower().endswith('.pdf')]
    return pdf_files

@app.get("/pdf-info/{pdf_name}")
async def get_pdf_info(pdf_name: str):
    if pdf_name in pdf_info_cache:
        return JSONResponse(content={"num_pages": pdf_info_cache[pdf_name]})
    try:
        blob = bucket.blob(pdf_name)
        if not blob.exists():
            raise HTTPException(status_code=404, detail="PDF not found in storage")
        
        pdf_bytes = blob.download_as_bytes()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        num_pages = doc.page_count
        doc.close()
        pdf_info_cache[pdf_name] = num_pages
        return JSONResponse(content={"num_pages": num_pages})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/pdf-page/{pdf_name}/{page_num}")
async def get_pdf_page_as_image(pdf_name: str, page_num: int):
    try:
        blob = bucket.blob(pdf_name)
        if not blob.exists():
            raise HTTPException(status_code=404, detail="PDF not found in storage")

        pdf_bytes = blob.download_as_bytes()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        
        if 0 < page_num <= doc.page_count:
            page = doc.load_page(page_num - 1)
            pix = page.get_pixmap(dpi=150)
            img_bytes = pix.tobytes("png")
            doc.close()
            return Response(content=img_bytes, media_type="image/png")
        else:
            doc.close()
            return JSONResponse(content={"error": "Page not found"}, status_code=404)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/comments/{pdf_name}/{page_num}", response_model=List[dict])
async def get_comments(pdf_name: str, page_num: int):
    """Gets comments from Firestore."""
    collection_path = f"{pdf_name}_{page_num}"
    docs = db.collection(collection_path).order_by("line_number_int").get()
    comments = []
    for doc in docs:
        comment_data = doc.to_dict()
        comment_data["id"] = doc.id
        comments.append(comment_data)
    return comments

@app.post("/comments/{pdf_name}/{page_num}", status_code=status.HTTP_201_CREATED)
async def post_comment(pdf_name: str, page_num: int, request: Request):
    """Posts a new comment to Firestore."""
    data = await request.json()
    comment_text = data.get("comment")
    line_number = data.get("line_number", "")

    if not comment_text:
        raise HTTPException(status_code=400, detail="Comment text is required")

    line_number_int = -1
    try:
        line_number_int = int(line_number)
    except (ValueError, TypeError):
        pass

    collection_path = f"{pdf_name}_{page_num}"
    doc_ref = db.collection(collection_path).document()
    doc_ref.set({
        "line": line_number,
        "text": comment_text,
        "line_number_int": line_number_int
    })
    
    return JSONResponse(content={"message": "Comment added successfully", "comment_id": doc_ref.id})

@app.delete("/comments/{pdf_name}/{page_num}/{comment_id}")
async def delete_comment(
    pdf_name: str,
    page_num: int,
    comment_id: str,
    request: Request,
):
    """Deletes a specific comment from Firestore."""
    password_data = await request.json()
    provided_password = password_data.get("password")
    
    if provided_password != DELETE_PASSWORD:
        raise HTTPException(status_code=403, detail="Incorrect password")

    collection_path = f"{pdf_name}_{page_num}"
    doc_ref = db.collection(collection_path).document(comment_id)
    
    if not doc_ref.get().exists:
        raise HTTPException(status_code=404, detail="Comment not found")
        
    doc_ref.delete()
    
    return JSONResponse(content={"message": "Comment deleted successfully"})
