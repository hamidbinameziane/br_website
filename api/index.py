import os
import json
import logging
from typing import List, Optional

from fastapi import FastAPI, Request, Response, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import fitz  # PyMuPDF
import firebase_admin
from firebase_admin import credentials, firestore, storage
from pydantic import BaseModel

# --- FastAPI App Setup ---
app = FastAPI()

# --- Security and Configuration ---
logging.basicConfig(level=logging.INFO)
DELETE_PASSWORD = os.getenv("DELETE_PASSWORD")
if not DELETE_PASSWORD:
    raise RuntimeError("DELETE_PASSWORD environment variable must be set for security.")

# Cache for PDF page counts
pdf_info_cache = {}

# --- Firebase Setup ---
SERVICE_ACCOUNT_JSON_STRING = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET")  # e.g., your-project-id.appspot.com

# Initialize Firebase with Storage (safe init, validate envs)
try:
    firebase_admin.get_app()
    logging.info("Firebase already initialized")
except ValueError:
    if not STORAGE_BUCKET:
        raise RuntimeError("FIREBASE_STORAGE_BUCKET environment variable is required.")
    try:
        if SERVICE_ACCOUNT_JSON_STRING:
            try:
                service_account_info = json.loads(SERVICE_ACCOUNT_JSON_STRING)
            except json.JSONDecodeError as e:
                raise RuntimeError("Invalid JSON in FIREBASE_SERVICE_ACCOUNT_JSON") from e
            cred = credentials.Certificate(service_account_info)
        else:
            if not os.path.exists("firebase-service-account.json"):
                raise RuntimeError(
                    "Firebase service account not provided via env and firebase-service-account.json not found."
                )
            cred = credentials.Certificate("firebase-service-account.json")

        firebase_admin.initialize_app(cred, {"storageBucket": STORAGE_BUCKET})
    except Exception as e:
        raise RuntimeError("Failed to initialize Firebase") from e

db = firestore.client()
bucket = storage.bucket()


# --- Request models ---


class CommentCreate(BaseModel):
    comment: str
    line_number: Optional[int] = None


class PreferencesUpdate(BaseModel):
    data: dict

# --- API Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def get_index():
    # Adjust path to be relative to the root where vercel runs the script
    return FileResponse("static/index.html")

@app.get("/api/list-pdfs", response_model=List[str])
async def list_pdfs():
    """Lists all .pdf files from Firebase Storage."""
    blobs = bucket.list_blobs()
    pdf_files = [blob.name for blob in blobs if blob.name.lower().endswith('.pdf')]
    return pdf_files

@app.get("/api/pdf-info/{pdf_name}")
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

@app.get("/api/pdf-page/{pdf_name}/{page_num}")
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

@app.get("/api/comments/{pdf_name}/{page_num}", response_model=List[dict])
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

@app.post("/api/comments/{pdf_name}/{page_num}", status_code=status.HTTP_201_CREATED)
async def post_comment(pdf_name: str, page_num: int, comment: CommentCreate):
    """Posts a new comment to Firestore."""
    comment_text = comment.comment
    line_number = comment.line_number

    if not comment_text:
        raise HTTPException(status_code=400, detail="Comment text is required")

    line_number_int = line_number if line_number is not None else -1

    collection_path = f"{pdf_name}_{page_num}"
    doc_ref = db.collection(collection_path).document()
    doc_ref.set({
        "line": line_number if line_number is not None else "",
        "text": comment_text,
        "line_number_int": line_number_int,
    })

    return JSONResponse(content={"message": "Comment added successfully", "comment_id": doc_ref.id})

@app.delete("/api/comments/{pdf_name}/{page_num}/{comment_id}")
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

# --- User Preferences Endpoints ---

@app.get("/api/preferences")
async def get_preferences():
    """Retrieves the last saved state from Firestore."""
    try:
        doc_ref = db.collection("preferences").document("default-user")
        doc = doc_ref.get()
        if doc.exists:
            return JSONResponse(content=doc.to_dict())
        else:
            # Return a default state if no preferences are saved yet
            return JSONResponse(content={"last_opened_pdf": None, "pdf_positions": {}})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/preferences", status_code=status.HTTP_200_OK)
async def set_preferences(preferences: PreferencesUpdate):
    """Saves the current state to Firestore."""
    try:
        data = preferences.data
        doc_ref = db.collection("preferences").document("default-user")
        # Use set with merge=True to update fields without overwriting the whole document
        doc_ref.set(data, merge=True)
        return JSONResponse(content={"message": "Preferences saved successfully"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
