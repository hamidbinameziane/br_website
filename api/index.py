import os
import json
import logging
from typing import List, Optional

from fastapi import FastAPI, Request, Response, HTTPException, status, Depends
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import fitz  # PyMuPDF
import io
from PIL import Image
import firebase_admin
from firebase_admin import credentials, firestore, storage, auth as firebase_auth
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


# --- Auth Helpers ---
def verify_id_token(request: Request):
    """FastAPI dependency to verify Firebase ID token from Authorization header.

    Raises HTTPException(401) on missing/invalid token. Returns decoded token on success.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    id_token = auth_header.split(" ", 1)[1]
    try:
        decoded = firebase_auth.verify_id_token(id_token)
        return decoded
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


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
async def get_pdf_page_as_image(pdf_name: str, page_num: int, w: Optional[int] = None, fmt: Optional[str] = "webp"):
    """Return a rasterized page image.

    Query params:
      - w: desired target width in pixels (clamped)
      - fmt: image format ('webp' or 'png')

    The handler will check Cloud Storage cache under `cache/{pdf_name}/...` and
    upload the rendered image after the first render. Responses include
    aggressive cache-control headers for CDN caching.
    """
    try:
        blob = bucket.blob(pdf_name)
        if not blob.exists():
            raise HTTPException(status_code=404, detail="PDF not found in storage")

        # sanitize format
        fmt = (fmt or "webp").lower()
        if fmt not in ("webp", "png"):
            fmt = "webp"

        # clamp requested width
        DEFAULT_WIDTH = 1200
        MIN_WIDTH = 300
        MAX_WIDTH = 2500
        target_w = DEFAULT_WIDTH if not w else max(MIN_WIDTH, min(MAX_WIDTH, int(w)))

        # cache blob path
        ext = "webp" if fmt == "webp" else "png"
        cache_path = f"cache/{pdf_name}/page_{page_num}_w{target_w}_fmt.{ext}"
        cache_blob = bucket.blob(cache_path)

        # If cached image exists, return it with cache headers
        if cache_blob.exists():
            content = cache_blob.download_as_bytes()
            media_type = "image/webp" if ext == "webp" else "image/png"
            headers = {"Cache-Control": "public, max-age=31536000, immutable"}
            return Response(content=content, media_type=media_type, headers=headers)

        # Not cached: render from PDF
        pdf_bytes = blob.download_as_bytes()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        if not (0 < page_num <= doc.page_count):
            doc.close()
            return JSONResponse(content={"error": "Page not found"}, status_code=404)

        page = doc.load_page(page_num - 1)
        rect = page.rect
        # rect.width is in PDF points; compute scale to reach target pixel width
        scale = target_w / rect.width
        # avoid extremely small or huge scale factors
        scale = max(0.2, min(4.0, scale))
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)

        # convert to PIL image for encoding to desired format
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        buf = io.BytesIO()
        if ext == "webp":
            img.save(buf, format="WEBP", quality=80)
            media_type = "image/webp"
        else:
            img.save(buf, format="PNG", optimize=True)
            media_type = "image/png"

        buf.seek(0)
        content = buf.read()
        doc.close()

        # upload to cache path for future requests
        try:
            cache_blob.upload_from_string(content, content_type=media_type)
            cache_blob.cache_control = "public, max-age=31536000, immutable"
            cache_blob.patch()
        except Exception as e:
            # cache failures should not break serving
            logging.warning("Failed to upload cache image: %s", e)

        headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        return Response(content=content, media_type=media_type, headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("Error rendering PDF page")
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
async def post_comment(pdf_name: str, page_num: int, comment: CommentCreate, user=Depends(verify_id_token)):
    """Posts a new comment to Firestore. Requires authenticated user.

    `user` is the decoded Firebase token (contains `uid`, `email`, etc.).
    """
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
        "author_uid": user.get("uid"),
        "author_email": user.get("email"),
    })

    return JSONResponse(content={"message": "Comment added successfully", "comment_id": doc_ref.id})

@app.delete("/api/comments/{pdf_name}/{page_num}/{comment_id}")
async def delete_comment(
    pdf_name: str,
    page_num: int,
    comment_id: str,
    request: Request,
    user=Depends(verify_id_token),
):
    """Deletes a specific comment from Firestore.

    Requires authenticated user. Authorization: allow deletion if user is author or provides DELETE_PASSWORD.
    """
    payload = await request.json()
    provided_password = payload.get("password")

    collection_path = f"{pdf_name}_{page_num}"
    doc_ref = db.collection(collection_path).document(comment_id)
    doc_snapshot = doc_ref.get()
    if not doc_snapshot.exists:
        raise HTTPException(status_code=404, detail="Comment not found")

    comment = doc_snapshot.to_dict()
    author_uid = comment.get("author_uid")

    # Allow deletion if requester is the author
    if user.get("uid") == author_uid:
        doc_ref.delete()
        return JSONResponse(content={"message": "Comment deleted successfully"})

    # Or allow deletion with the DELETE_PASSWORD
    if provided_password and provided_password == DELETE_PASSWORD:
        doc_ref.delete()
        return JSONResponse(content={"message": "Comment deleted successfully (by admin)"})

    raise HTTPException(status_code=403, detail="Not authorized to delete this comment")

# --- User Preferences Endpoints ---

@app.get("/api/preferences")
async def get_preferences(user=Depends(verify_id_token)):
    """Retrieve the authenticated user's preferences from Firestore.

    Returns a default structure if no preferences exist yet.
    """
    try:
        uid = user.get("uid")
        doc_ref = db.collection("preferences").document(uid)
        doc = doc_ref.get()
        if doc.exists:
            return JSONResponse(content=doc.to_dict())
        else:
            return JSONResponse(content={"last_opened_pdf": None, "pdf_positions": {}})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/preferences", status_code=status.HTTP_200_OK)
async def set_preferences(preferences: PreferencesUpdate, user=Depends(verify_id_token)):
    """Save the authenticated user's preferences to Firestore.

    Expects a JSON body matching `PreferencesUpdate` (object with `data` field).
    """
    try:
        data = preferences.data
        uid = user.get("uid")
        doc_ref = db.collection("preferences").document(uid)
        doc_ref.set(data, merge=True)
        return JSONResponse(content={"message": "Preferences saved successfully"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/firebase-config")
async def firebase_config():
    """Return non-sensitive Firebase client configuration (apiKey) to the frontend.

    The API key is safe to expose to clients; it's used by the Firebase Web SDK.
    """
    api_key = os.getenv("FIREBASE_API_KEY", "")
    return JSONResponse(content={"apiKey": api_key})
