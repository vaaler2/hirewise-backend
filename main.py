from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import uuid
import json
import os
from datetime import datetime, timedelta

app = FastAPI()

DATA_FILE = "applications.json"

# Ha nincs adatfájl, létrehozzuk
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({"links": {}, "applications": []}, f)


# ----------- SEGÉDFÜGGVÉNYEK -----------

def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)


# ----------- MODELL CÉG LINK GENERÁLÁSHOZ -----------

class LinkRequest(BaseModel):
    client_id: str
    profession: str
    email: str


# ----------- 1. CÉGES LINK GENERÁLÁS -----------

@app.post("/generate-link")
def generate_link(req: LinkRequest):
    data = load_data()
    link_id = str(uuid.uuid4())
    expiry_date = (datetime.utcnow() + timedelta(days=30)).isoformat()

    data["links"][link_id] = {
        "client_id": req.client_id,
        "profession": req.profession,
        "email": req.email,
        "expiry": expiry_date
    }

    save_data(data)

    return {
        "message": "Link successfully generated",
        "link": f"https://YOUR_DOMAIN.com/form/{link_id}",
        "expires_at": expiry_date
    }


# ----------- 2. JELENTKEZŐ ŰRLAP BEKÜLDÉSE -----------

@app.post("/submit-form/{link_id}")
async def submit_form(
    link_id: str,
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    about: str = Form(...),
    cv_image: UploadFile = File(...)
):
    data = load_data()

    # Ellenőrzés: létezik a link és nem járt le
    if link_id not in data["links"]:
        raise HTTPException(status_code=404, detail="Invalid form link")

    link_info = data["links"][link_id]
    if datetime.fromisoformat(link_info["expiry"]) < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Form link has expired")

    # Kép mentése
    uploads_dir = "uploads"
    os.makedirs(uploads_dir, exist_ok=True)
    file_path = os.path.join(uploads_dir, f"{uuid.uuid4()}_{cv_image.filename}")

    with open(file_path, "wb") as f:
        f.write(await cv_image.read())

    # Jelentkező adatainak mentése
    application = {
        "link_id": link_id,
        "client_id": link_info["client_id"],
        "profession": link_info["profession"],
        "company_email": link_info["email"],
        "name": name,
        "phone": phone,
        "email": email,
        "about": about,
        "cv_image_path": file_path,
        "submitted_at": datetime.utcnow().isoformat()
    }

    data["applications"].append(application)
    save_data(data)

    return {"message": "Application submitted successfully"}


# ----------- 3. CÉG LEKÉRDEZHETI A JELENTKEZÉSEKET -----------

@app.get("/applications/{link_id}")
def get_applications(link_id: str):
    data = load_data()
    apps = [app for app in data["applications"] if app["link_id"] == link_id]
    return JSONResponse(content=apps)
