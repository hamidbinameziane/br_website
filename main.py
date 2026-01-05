import os
import json
import io
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import aiofiles
import fitz  # PyMuPDF

app = FastAPI()

COMMENTS_FILE = "comments.json"
DELETE_PASSWORD = os.getenv("DELETE_PASSWORD", "1234567890") # Use environment variable for password

# Cache for PDF page counts to avoid re-opening files
pdf_info_cache = {}

async def read_comments():
    """Reads the comments from the JSON file."""
    try:
        async with aiofiles.open(COMMENTS_FILE, mode='r', encoding='utf-8') as f:
            return json.loads(await f.read())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

async def write_comments(data: dict):
    """Writes the comments to the JSON file."""
    async with aiofiles.open(COMMENTS_FILE, mode='w', encoding='utf-8') as f:
        await f.write(json.dumps(data, indent=2))

@app.get("/", response_class=HTMLResponse)
async def get_index():
    """Serves the main index.html file."""
    return FileResponse("index.html")

@app.get("/pdf-info/{pdf_name}")
async def get_pdf_info(pdf_name: str):
    """Gets the number of pages in a PDF."""
    if pdf_name in pdf_info_cache:
        return JSONResponse(content={"num_pages": pdf_info_cache[pdf_name]})
    
    try:
        doc = fitz.open(f"{pdf_name}.pdf")
        num_pages = doc.page_count
        doc.close()
        pdf_info_cache[pdf_name] = num_pages
        return JSONResponse(content={"num_pages": num_pages})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/pdf-page/{pdf_name}/{page_num}")
async def get_pdf_page_as_image(pdf_name: str, page_num: int):
    """Renders a specific PDF page as a PNG image."""
    try:
        doc = fitz.open(f"{pdf_name}.pdf")
        if 0 < page_num <= doc.page_count:
            page = doc.load_page(page_num - 1)  # 0-indexed
            pix = page.get_pixmap(dpi=150)  # Increase DPI for better quality
            img_bytes = pix.tobytes("png")
            doc.close()
            return Response(content=img_bytes, media_type="image/png")
        else:
            doc.close()
            return JSONResponse(content={"error": "Page not found"}, status_code=404)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/comments/{pdf_name}/{page_num}")
async def get_comments(pdf_name: str, page_num: int):
    """Gets comments for a specific page of a specific PDF."""
    all_comments = await read_comments()
    pdf_comments = all_comments.get(pdf_name, {})
    return JSONResponse(content=pdf_comments.get(str(page_num), []))

@app.post("/comments/{pdf_name}/{page_num}")
async def post_comment(pdf_name: str, page_num: int, request: Request):
    """Posts a new comment with a line number for a specific page."""
    data = await request.json()
    comment_text = data.get("comment")
    line_number = data.get("line_number", "") # default to empty string if not provided

    if not comment_text:
        return JSONResponse(content={"error": "Comment text is required"}, status_code=400)

    all_comments = await read_comments()

    if pdf_name not in all_comments:
        all_comments[pdf_name] = {}
        
    page_key = str(page_num)
    if page_key not in all_comments[pdf_name]:
        all_comments[pdf_name][page_key] = []
    
    # Store comment as an object with text and line number
    new_comment = {"line": line_number, "text": comment_text}
    all_comments[pdf_name][page_key].append(new_comment)
    
    await write_comments(all_comments)
    
    return JSONResponse(content={"message": "Comment added successfully"})

@app.delete("/comments/{pdf_name}/{page_num}/{comment_index}")
async def delete_comment(pdf_name: str, page_num: int, comment_index: int, request: Request):
    """Deletes a specific comment from a page, protected by a password."""
    password_data = await request.json()
    provided_password = password_data.get("password")
    
    if provided_password != DELETE_PASSWORD: # Use environment variable for password
        return JSONResponse(content={"error": "Incorrect password"}, status_code=403)

    all_comments = await read_comments()
    
    pdf_comments = all_comments.get(pdf_name)
    if not pdf_comments:
        return JSONResponse(content={"error": "PDF not found in comments"}, status_code=404)
        
    page_key = str(page_num)
    page_comments = pdf_comments.get(page_key)
    if not page_comments:
        return JSONResponse(content={"error": "Page not found in comments"}, status_code=404)
        
    if 0 <= comment_index < len(page_comments):
        deleted_comment = page_comments.pop(comment_index)
        await write_comments(all_comments)
        return JSONResponse(content={"message": "Comment deleted successfully", "deleted_comment": deleted_comment})
    else:
        return JSONResponse(content={"error": "Comment index out of range"}, status_code=404)
