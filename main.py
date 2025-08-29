import os
import uuid
import shutil
from datetime import datetime, timedelta
from typing import List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# --- OpenAI kliens (env-ben legyen: OPENAI_API_KEY) ---
try:
    from openai import OpenAI
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception:
    openai_client = None  # ha nincs telepítve / nincs kulcs, fallbackot használunk

app = FastAPI(title="Hirewise backend")

# "Adatbázis" memóriában (demó)
links_db = {}          # link_id -> { client_id, profession, company_email, expires_at }
applications_db = {}   # link_id -> [ {name, phone, email, about, cv_image_path, submitted_at, score?, rank?} ]

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ---------- MODELS ----------
class LinkRequest(BaseModel):
    client_id: str
    profession: str
    email: str


# ---------- SEGÉDFÜGGVÉNYEK ----------
def _local_score(app: dict, profession: str) -> float:
    """
    Egyszerű, offline pontozó, ha az AI nem érhető el.
    0–100-as skála.
    """
    text = (app.get("about") or "").lower()
    name_ok = 1 if app.get("name") else 0
    phone_ok = 1 if app.get("phone") else 0
    email_ok = 1 if app.get("email") else 0
    len_bonus = min(len(text) / 400, 1)  # max +1, hosszabb bemutatkozás = kis bónusz
    prof_hit = 1 if profession.lower() in text else 0

    # nagyon egyszerű kulcsszavas példák
    kw = {
        "asztalos": ["fa", "bútor", "marás", "csiszolás", "gépek"],
        "software developer": ["python", "java", "react", "docker", "api"],
    }
    kws = kw.get(profession.lower(), [])
    kw_hits = sum(1 for k in kws if k in text)
    kw_score = min(kw_hits / 3, 1)  # max +1

    raw = 2 * prof_hit + 2 * kw_score + 1.5 * len_bonus + name_ok + phone_ok + email_ok
    return round(100 * raw / 7.5, 1)  # skálázás 0–100-ig


def _ai_evaluate(applications: List[dict], profession: str) -> str:
    """
    Szöveges AI-értékelés GPT-vel. Ha nincs kliens/kulcs, hibát dobunk, amit kívül elkapunk.
    """
    if openai_client is None:
        raise RuntimeError("OpenAI kliens nem elérhető")

    prompt = (
        "Értékeld az alábbi jelentkezőket a megadott szakma alapján. "
        "Adj mindenkinek 1–10 pontszámot és rövid indoklást. "
        "A válasz végén adj egy javasolt sorrendet is.\n\n"
        f"Szakma: {profession}\n\n"
    )
    for i, app in enumerate(applications, 1):
        prompt += (
            f"Jelentkező {i}:\n"
            f"- Név: {app.get('name','')}\n"
            f"- Telefon: {app.get('phone','')}\n"
            f"- E-mail: {app.get('email','')}\n"
            f"- Bemutatkozás: {app.get('about','')}\n\n"
        )

    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Te egy HR szakértő vagy, aki jelentkezőket értékel."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()


# ---------- ENDPOINTOK ----------
@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.post("/generate-link")
def generate_link(data: LinkRequest):
    link_id = str(uuid.uuid4())
    links_db[link_id] = {
        "client_id": data.client_id,
        "profession": data.profession,
        "company_email": data.email,
        "expires_at": datetime.utcnow() + timedelta(days=30),
    }
    applications_db[link_id] = []

    return {
        "message": "Link successfully generated",
        "link": f"https://YOUR-DOMAIN.com/form/{link_id}",  # ide majd a frontend URL megy
        "link_id": link_id,
        "expires_at": links_db[link_id]["expires_at"].isoformat(),
    }


@app.post("/submit-form/{link_id}")
def submit_form(
    link_id: str,
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    about: str = Form(...),
    cv_image: UploadFile = File(...),
):
    if link_id not in links_db:
        raise HTTPException(status_code=404, detail="Invalid link")

    # fájl mentés
    ext = (cv_image.filename or "bin").split(".")[-1]
    file_name = f"{uuid.uuid4()}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, file_name)
    with open(file_path, "wb") as f:
        shutil.copyfileobj(cv_image.file, f)

    applications_db[link_id].append(
        {
            "name": name,
            "phone": phone,
            "email": email,
            "about": about,
            "cv_image_path": file_path,
            "submitted_at": datetime.utcnow().isoformat(),
        }
    )
    return {"message": "Application submitted successfully"}


@app.get("/applications/{link_id}")
def get_applications(link_id: str):
    if link_id not in applications_db:
        raise HTTPException(status_code=404, detail="Invalid link")

    profession = links_db[link_id]["profession"]
    apps = applications_db[link_id]

    if not apps:
        return {
            "link_id": link_id,
            "client_id": links_db[link_id]["client_id"],
            "profession": profession,
            "company_email": links_db[link_id]["company_email"],
            "applications": [],
            "evaluation": "No applications yet",
        }

    # 1) AI értékelés megkísérlése
    evaluation: str
    try:
        evaluation = _ai_evaluate(apps, profession)
        # AI esetén NEM módosítjuk a listát, csak szöveges értékelést adunk vissza
        return {
            "link_id": link_id,
            "client_id": links_db[link_id]["client_id"],
            "profession": profession,
            "company_email": links_db[link_id]["company_email"],
            "applications": apps,
            "evaluation": evaluation,
        }
    except Exception as e:
        # 2) Fallback: helyi pontozás + rangsor
        for a in apps:
            a["score"] = _local_score(a, profession)
        apps.sort(key=lambda x: x.get("score", 0), reverse=True)
        for i, a in enumerate(apps, 1):
            a["rank"] = i
        evaluation = f"AI értékelés nem elérhető ({e}); helyi pontozás és rangsor látható."

        return {
            "link_id": link_id,
            "client_id": links_db[link_id]["client_id"],
            "profession": profession,
            "company_email": links_db[link_id]["company_email"],
            "applications": apps,
            "evaluation": evaluation,
        }
from fastapi import Request, HTTPException
import os

CRON_BEARER = os.getenv("CRON_BEARER", "")

def require_cron_bearer(req: Request):
    auth = req.headers.get("Authorization", "").strip()
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()  # levágja a "Bearer " részt és a felesleges szóközöket
    if not CRON_BEARER or token != CRON_BEARER.strip():
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.post("/tasks/send_weekly_reports")
async def send_weekly_reports(request: Request):
    require_cron_bearer(request)
    # --- ide jön majd az igazi heti email riport logika ---
    print(">>> Weekly report task triggered!")
    return {"ok": True, "sent": 0}
