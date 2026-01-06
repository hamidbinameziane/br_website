import os
import io
from typing import List, Optional

from fastapi import FastAPI, Request, Response, Depends, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import fitz  # PyMuPDF

from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# --- FastAPI App Setup ---
app = FastAPI()

# --- Security and Configuration ---
DELETE_PASSWORD = os.getenv("DELETE_PASSWORD", "1234567890") # Use environment variable for password

# Cache for PDF page counts to avoid re-opening files
pdf_info_cache = {}

# --- Database Setup (PostgreSQL with SQLAlchemy) ---
DATABASE_URL = os.getenv("DATABASE_URL") # Read from environment variable

engine = create_engine(
    DATABASE_URL
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# Comment Model
class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    pdf_name = Column(String, index=True)
    page_num = Column(Integer, index=True)
    line_number = Column(String, default="") # Store as string to allow non-numeric input
    text = Column(String)

    def to_dict(self):
        return {
            "id": self.id,
            "pdf_name": self.pdf_name,
            "page_num": self.page_num,
            "line": self.line_number,
            "text": self.text,
        }

# Dependency to get a database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Create database tables on startup
@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)


# --- API Endpoints ---

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

@app.get("/comments/{pdf_name}/{page_num}", response_model=List[dict])
async def get_comments(pdf_name: str, page_num: int, db: Session = Depends(get_db)):
    """Gets comments for a specific page of a specific PDF."""
    comments = db.query(Comment).filter(
        Comment.pdf_name == pdf_name,
        Comment.page_num == page_num
    ).order_by(Comment.line_number.asc()).all()
    return [comment.to_dict() for comment in comments]

@app.post("/comments/{pdf_name}/{page_num}", status_code=status.HTTP_201_CREATED)
async def post_comment(pdf_name: str, page_num: int, request: Request, db: Session = Depends(get_db)):
    """Posts a new comment with a line number for a specific page."""
    data = await request.json()
    comment_text = data.get("comment")
    line_number = data.get("line_number", "")

    if not comment_text:
        raise HTTPException(status_code=400, detail="Comment text is required")

    new_comment = Comment(
        pdf_name=pdf_name,
        page_num=page_num,
        line_number=line_number,
        text=comment_text
    )
    db.add(new_comment)
    db.commit()
    db.refresh(new_comment) # Get the ID back
    
    return JSONResponse(content={"message": "Comment added successfully", "comment_id": new_comment.id})

@app.delete("/comments/{pdf_name}/{page_num}/{comment_id}")
async def delete_comment(
    pdf_name: str,
    page_num: int,
    comment_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    """Deletes a specific comment from a page, protected by a password."""
    password_data = await request.json()
    provided_password = password_data.get("password")
    
    if provided_password != DELETE_PASSWORD:
        raise HTTPException(status_code=403, detail="Incorrect password")

    comment_to_delete = db.query(Comment).filter(
        Comment.id == comment_id,
        Comment.pdf_name == pdf_name,
        Comment.page_num == page_num
    ).first()

    if not comment_to_delete:
        raise HTTPException(status_code=404, detail="Comment not found")
        
    db.delete(comment_to_delete)
    db.commit()
    
    return JSONResponse(content={"message": "Comment deleted successfully"})
