import os
import uuid
from datetime import datetime, timedelta
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List
import shutil
from openai import OpenAI

# OpenAI kliens inicializálása (API kulcsot az env-ben kell tárolni)
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = FastAPI()

# Egyszerű memória alapú "adatbázis"
links_db = {}
applications_db = {}

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# --- MODELS ---

class LinkRequest(BaseModel):
    client_id: str
    profession: str
    email: str


# --- ENDPOINTOK ---

@app.post("/generate-link")
def generate_link(data: LinkRequest):
    link_id = str(uuid.uuid4())
    links_db[link_id] = {
        "client_id": data.client_id,
        "profession": data.profession,
        "company_email": data.email,
        "expires_at": datetime.utcnow() + timedelta(days=30)
    }
    applications_db[link_id] = []
    return {
        "message": "Link successfully generated",
        "link": f"https://YOUR-DOMAIN.com/form/{link_id}",
        "expires_at": links_db[link_id]["expires_at"]
    }


@app.post("/submit-form/{link_id}")
def submit_form(
    link_id: str,
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    about: str = Form(...),
    cv_image: UploadFile = File(...)
):
    if link_id not in links_db:
        raise HTTPException(status_code=404, detail="Invalid link")

    # Mentés
    file_ext = cv_image.filename.split(".")[-1]
    file_name = f"{uuid.uuid4()}.{file_ext}"
    file_path = os.path.join(UPLOAD_DIR, file_name)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(cv_image.file, buffer)

    # Jelentkező mentése
    applications_db[link_id].append({
        "name": name,
        "phone": phone,
        "email": email,
        "about": about,
        "cv_image_path": file_path,
        "submitted_at": datetime.utcnow()
    })

    return {"message": "Application submitted successfully"}


@app.get("/applications/{link_id}")
def get_applications(link_id: str):
    if link_id not in applications_db:
        raise HTTPException(status_code=404, detail="Invalid link")

    applications = applications_db[link_id]

    if not applications:
        return {"applications": [], "evaluation": "No applications yet"}

    # AI értékelés
    prompt = "Értékeld az alábbi jelentkezőket a megadott szakma alapján, adj 1-10 pontszámot mindenkinek:\n\n"
    profession = links_db[link_id]["profession"]
    for idx, app in enumerate(applications, start=1):
        prompt += (
            f"Jelentkező {idx}:\n"
            f"Név: {app['name']}\n"
            f"Telefon: {app['phone']}\n"
            f"E-mail: {app['email']}\n"
            f"Leírás: {app['about']}\n"
            f"Szakma: {profession}\n\n"
        )

    try:
        ai_response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Te egy HR szakértő vagy, aki értékeli a jelentkezőket."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        evaluation = ai_response.choices[0].message.content.strip()
    except Exception as e:
        evaluation = f"AI értékelési hiba: {str(e)}"

    return {
        "link_id": link_id,
        "client_id": links_db[link_id]["client_id"],
        "profession": profession,
        "company_email": links_db[link_id]["company_email"],
        "applications": applications,
        "evaluation": evaluation
    }
