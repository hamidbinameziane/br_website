# Project Overview

This is a web-based PDF viewer and annotation application designed for serverless deployment on Vercel.

It consists of a Python backend using the FastAPI framework and a vanilla JavaScript frontend. The backend serves the HTML frontend and provides a REST API for interacting with PDF files and their associated comments.

The application uses Google Firebase for its data storage:
*   **Firestore:** Stores comments and user preferences.
*   **Cloud Storage:** Stores the PDF files.

## Architecture

The application is composed of two main parts:

1.  **Backend (`api/index.py`):** A Python serverless function built with FastAPI. It handles all business logic, including:
    *   Serving the main `index.html` file.
    *   Listing available PDFs from Firebase Storage.
    *   Rendering individual PDF pages as images using the `PyMuPDF` library.
    *   CRUD (Create, Read, Update, Delete) operations for comments stored in Firestore.
    *   Managing user preferences, such as the last opened PDF and page position.

2.  **Frontend (`static/index.html`):** A single HTML file containing all the necessary HTML, CSS, and JavaScript to create the user interface. It communicates with the backend via `fetch` requests to its API endpoints.

## Building and Running

This project is configured for deployment on Vercel.

### Deployment

1.  **Prerequisites:**
    *   A Vercel account.
    *   A Firebase project with Firestore and Cloud Storage enabled.

2.  **Environment Variables:**
    To deploy the project, you must set the following environment variables in your Vercel project settings:
    *   `FIREBASE_SERVICE_ACCOUNT_JSON`: The JSON content of your Firebase service account key.
    *   `FIREBASE_STORAGE_BUCKET`: The name of your Firebase Cloud Storage bucket (e.g., `your-project-id.appspot.com`).
    *   `DELETE_PASSWORD`: A secure password that must be provided when deleting comments.
    *   `FIREBASE_API_KEY`: Your Firebase Web API key (used by the frontend for authentication). This is safe to expose to the client.

3.  **Deploy Command:**
    There is no specific build command. Pushing the code to a Vercel-linked GitHub repository will trigger a deployment. Vercel will automatically use the `@vercel/python` runtime to build and deploy the `api/index.py` function.

### Local Development

To run the project locally, you would typically use a Python environment and an ASGI server like `uvicorn`.

1.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    pip install uvicorn
    ```

2.  **Set Environment Variables:**
    Create a `.env` file in the root directory and add your Firebase credentials:
    ```
    FIREBASE_SERVICE_ACCOUNT_JSON='{...}'
    FIREBASE_STORAGE_BUCKET='your-project-id.appspot.com'
    DELETE_PASSWORD='a-strong-password'
    ```

3.  **Run the Server:**
    ```bash
    uvicorn api.index:app --reload
    ```
    The application will be available at `http://127.0.0.1:8000`.

## Authentication (Firebase)

This project supports user authentication via Firebase Authentication (recommended). Follow these steps to enable and use authentication:

1. Enable Email/Password sign-in in the [Firebase Console] and configure your project.
2. Add the `FIREBASE_API_KEY` environment variable to your deployment (Vercel) or local `.env` file.
3. The frontend should use the Firebase Web SDK to sign up / sign in users and obtain an ID token. The ID token must be sent to protected API endpoints in the `Authorization: Bearer <idToken>` header.

Server-side notes:
- The backend uses the Firebase Admin SDK and verifies ID tokens on protected endpoints. Endpoints that modify data (posting or deleting comments) are protected and require a valid ID token.
- To force logout of a user from the server side, call `firebase_admin.auth.revoke_refresh_tokens(uid)`. ID tokens remain valid until expiry (~1 hour) but refresh tokens will be invalidated.

If you'd like, I can add the frontend login/signup UI and wire it to the backend now.

## Development Conventions

*   **Serverless First:** The code is structured to work within a serverless environment (e.g., relying on environment variables for configuration).
*   **RESTful API:** The backend exposes a RESTful API for the frontend to consume.
*   **Vanilla Frontend:** The frontend is built with standard HTML, CSS, and JavaScript without any additional frameworks, keeping it lightweight.
*   **Security:** Sensitive information like API keys and passwords are managed through environment variables and are not hardcoded.
